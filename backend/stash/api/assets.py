"""Asset browsing, editing, tagging, previews and exports."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from ..convert.exporter import available_targets
from ..db import get_db
from ..images import normalize_thumbnail
from ..jobs import enqueue
from ..models import Asset, AssetFile, AssetLink, Job, Project, ProjectAsset, Source, Tag, asset_tags
from ..processing import PRIORITY
from ..search import index_asset, search_ids, unindex_asset
from ..storage import resolve, store_bytes
from .common import ser_asset_brief, ser_file, ser_job

router = APIRouter()

mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("model/gltf+json", ".gltf")
mimetypes.add_type("application/octet-stream", ".fbx")
mimetypes.add_type("model/obj", ".obj")
mimetypes.add_type("model/stl", ".stl")
mimetypes.add_type("image/x-tga", ".tga")

SORTS = {
    "updated": Asset.updated_at,
    "created": Asset.created_at,
    "name": func.lower(Asset.name),
    "size": Asset.size,
    "polys": Asset.poly_count,
    "rating": Asset.rating,
}


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def build_query(
    db: Session,
    q: str | None = None,
    kind: str | None = None,
    tags: str | None = None,
    project: int | None = None,
    source: int | None = None,
    fmt: str | None = None,
    flags: str | None = None,
    parent: str | None = None,
    status: str | None = None,
    ue_class: str | None = None,
):
    query = db.query(Asset)
    ids = search_ids(db, q) if q else None
    if ids is not None:
        query = query.filter(Asset.id.in_(ids))
    if kind:
        query = query.filter(Asset.kind.in_(_split(kind)))
    for t in _split(tags):
        query = query.filter(Asset.tags.any(Tag.name == t.lower()))
    if project:
        query = query.filter(Asset.id.in_(db.query(ProjectAsset.asset_id).filter(ProjectAsset.project_id == project)))
    if source:
        query = query.filter(Asset.source_id == source)
    if fmt:
        query = query.filter(Asset.format.in_(_split(fmt)))
    if ue_class:
        query = query.filter(func.json_extract(Asset.meta, "$.ue_class").in_(_split(ue_class)))
    for f in _split(flags):
        col = {
            "rigged": Asset.is_rigged,
            "animated": Asset.is_animated,
            "procedural": Asset.is_procedural,
            "textured": Asset.has_textures,
            "favorite": Asset.favorite,
        }.get(f)
        if col is not None:
            query = query.filter(col.is_(True))
        elif f == "previewable":
            query = query.filter(Asset.preview_file_id.isnot(None))
        elif f == "untagged":
            query = query.filter(~Asset.tags.any())
    if parent == "root":
        query = query.filter(Asset.parent_id.is_(None))
    elif parent:
        query = query.filter(Asset.parent_id == int(parent))
    if status == "all":
        pass
    elif status:
        query = query.filter(Asset.status.in_(_split(status)))
    else:
        query = query.filter(Asset.status != "missing")
    return query, ids


@router.get("/assets")
def list_assets(
    q: str | None = None,
    kind: str | None = None,
    tags: str | None = None,
    project: int | None = None,
    source: int | None = None,
    format: str | None = None,
    flags: str | None = None,
    parent: str | None = None,
    status: str | None = None,
    ue_class: str | None = None,
    sort: str = "updated",
    order: str = "desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(60, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query, ids = build_query(db, q, kind, tags, project, source, format, flags, parent, status, ue_class)
    total = query.count()
    if q and ids is not None and sort == "relevance":
        rank = {aid: i for i, aid in enumerate(ids)}
        rows = query.options(selectinload(Asset.tags), selectinload(Asset.thumbnail)).all()
        rows.sort(key=lambda a: rank.get(a.id, 1 << 30))
        rows = rows[(page - 1) * per_page : page * per_page]
    else:
        col = SORTS.get(sort, Asset.updated_at)
        query = query.order_by(col.asc() if order == "asc" else col.desc(), Asset.id.desc())
        rows = query.options(selectinload(Asset.tags), selectinload(Asset.thumbnail)).offset((page - 1) * per_page).limit(per_page).all()
    child_counts = dict(
        db.query(Asset.parent_id, func.count(Asset.id)).filter(Asset.parent_id.in_([a.id for a in rows])).group_by(Asset.parent_id).all()
    )
    items = []
    for a in rows:
        d = ser_asset_brief(a)
        d["child_count"] = child_counts.get(a.id, 0)
        items.append(d)
    return {"total": total, "page": page, "per_page": per_page, "items": items}


@router.get("/facets")
def facets(q: str | None = None, source: int | None = None, project: int | None = None, db: Session = Depends(get_db)):
    base, _ = build_query(db, q=q, source=source, project=project)
    sub = base.with_entities(Asset.id).subquery()
    kinds = dict(db.query(Asset.kind, func.count()).filter(Asset.id.in_(sub)).group_by(Asset.kind).all())
    formats = dict(db.query(Asset.format, func.count()).filter(Asset.id.in_(sub)).group_by(Asset.format).all())
    tag_rows = (
        db.query(Tag.name, Tag.color, func.count(asset_tags.c.asset_id))
        .join(asset_tags, asset_tags.c.tag_id == Tag.id)
        .filter(asset_tags.c.asset_id.in_(sub))
        .group_by(Tag.id)
        .order_by(func.count(asset_tags.c.asset_id).desc())
        .limit(200)
        .all()
    )
    ue_classes = dict(
        db.query(func.json_extract(Asset.meta, "$.ue_class"), func.count())
        .filter(Asset.id.in_(sub), func.json_extract(Asset.meta, "$.ue_class").isnot(None))
        .group_by(func.json_extract(Asset.meta, "$.ue_class"))
        .all()
    )
    flags = {
        "rigged": db.query(func.count()).filter(Asset.id.in_(sub), Asset.is_rigged.is_(True)).scalar(),
        "animated": db.query(func.count()).filter(Asset.id.in_(sub), Asset.is_animated.is_(True)).scalar(),
        "procedural": db.query(func.count()).filter(Asset.id.in_(sub), Asset.is_procedural.is_(True)).scalar(),
        "textured": db.query(func.count()).filter(Asset.id.in_(sub), Asset.has_textures.is_(True)).scalar(),
        "favorite": db.query(func.count()).filter(Asset.id.in_(sub), Asset.favorite.is_(True)).scalar(),
        "untagged": db.query(func.count()).filter(Asset.id.in_(sub), ~Asset.tags.any()).scalar(),
    }
    return {
        "kinds": kinds,
        "formats": {k: v for k, v in formats.items() if k},
        "tags": [{"name": n, "color": c, "count": cnt} for n, c, cnt in tag_rows],
        "ue_classes": ue_classes,
        "flags": flags,
    }


def _get(db: Session, asset_id: int) -> Asset:
    a = db.get(Asset, asset_id)
    if a is None:
        raise HTTPException(404, "asset not found")
    return a


@router.get("/assets/{asset_id}")
def get_asset(asset_id: int, db: Session = Depends(get_db)):
    a = _get(db, asset_id)
    d = ser_asset_brief(a)
    links_out = db.query(AssetLink).filter(AssetLink.from_id == a.id).all()
    links_in = db.query(AssetLink).filter(AssetLink.to_id == a.id).all()

    def brief(i):
        x = db.get(Asset, i)
        return ser_asset_brief(x) if x else None

    children = db.query(Asset).filter(Asset.parent_id == a.id).order_by(Asset.kind, Asset.name).limit(1000).all()
    projects = db.query(Project).join(ProjectAsset).filter(ProjectAsset.asset_id == a.id).all()
    jobs = db.query(Job).filter(Job.asset_id == a.id).order_by(Job.id.desc()).limit(10).all()
    d.update(
        description=a.description,
        meta=a.meta or {},
        files=[ser_file(f) for f in a.files],
        preview=ser_file(a.preview) if a.preview else None,
        links=[{"relation": l.relation, "direction": "out", "asset": brief(l.to_id)} for l in links_out]
        + [{"relation": l.relation, "direction": "in", "asset": brief(l.from_id)} for l in links_in],
        children=[ser_asset_brief(c) for c in children],
        parent=ser_asset_brief(a.parent) if a.parent else None,
        source=({"id": a.source.id, "name": a.source.name, "kind": a.source.kind, "path": a.source.path} if a.source else None),
        projects=[{"id": p.id, "name": p.name, "color": p.color} for p in projects],
        targets=available_targets(a),
        exports=[ser_file(f) for f in a.files if f.role == "export"],
        jobs=[ser_job(j) for j in jobs],
        created_at=a.created_at.isoformat() if a.created_at else None,
    )
    return d


class AssetPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    rating: int | None = None
    favorite: bool | None = None
    kind: str | None = None


@router.patch("/assets/{asset_id}")
def patch_asset(asset_id: int, body: AssetPatch, db: Session = Depends(get_db)):
    a = _get(db, asset_id)
    if body.name is not None and body.name != a.name:
        a.name = body.name
        a.meta = {**(a.meta or {}), "renamed": True}
    if body.description is not None:
        a.description = body.description
    if body.rating is not None:
        a.rating = max(0, min(5, body.rating))
    if body.favorite is not None:
        a.favorite = body.favorite
    if body.kind is not None:
        a.kind = body.kind
    index_asset(db, a)
    db.commit()
    return ser_asset_brief(a)


def _tag(db: Session, name: str) -> Tag:
    name = name.strip().lower()
    t = db.query(Tag).filter(Tag.name == name).one_or_none()
    if t is None:
        t = Tag(name=name)
        db.add(t)
        db.flush()
    return t


class TagsBody(BaseModel):
    tags: list[str]


@router.post("/assets/{asset_id}/tags")
def add_tags(asset_id: int, body: TagsBody, db: Session = Depends(get_db)):
    a = _get(db, asset_id)
    for n in body.tags:
        if n.strip():
            t = _tag(db, n)
            if t not in a.tags:
                a.tags.append(t)
    index_asset(db, a)
    db.commit()
    return {"tags": [t.name for t in a.tags]}


@router.delete("/assets/{asset_id}/tags/{name}")
def remove_tag(asset_id: int, name: str, db: Session = Depends(get_db)):
    a = _get(db, asset_id)
    a.tags = [t for t in a.tags if t.name != name.lower()]
    index_asset(db, a)
    db.commit()
    return {"tags": [t.name for t in a.tags]}


class BulkBody(BaseModel):
    ids: list[int]
    add_tags: list[str] = []
    remove_tags: list[str] = []
    add_to_project: int | None = None
    remove_from_project: int | None = None
    favorite: bool | None = None
    kind: str | None = None
    delete: bool = False
    include_children: bool = False


@router.post("/assets/bulk")
def bulk(body: BulkBody, db: Session = Depends(get_db)):
    ids = list(dict.fromkeys(body.ids))
    if body.include_children:
        ids += [i for (i,) in db.query(Asset.id).filter(Asset.parent_id.in_(ids))]
    assets = db.query(Asset).filter(Asset.id.in_(ids)).all()
    tags_add = [_tag(db, n) for n in body.add_tags if n.strip()]
    remove = {n.lower() for n in body.remove_tags}
    for a in assets:
        if body.delete:
            _forget(db, a)
            continue
        for t in tags_add:
            if t not in a.tags:
                a.tags.append(t)
        if remove:
            a.tags = [t for t in a.tags if t.name not in remove]
        if body.favorite is not None:
            a.favorite = body.favorite
        if body.kind:
            a.kind = body.kind
        if body.add_to_project and not db.get(ProjectAsset, (body.add_to_project, a.id)):
            db.add(ProjectAsset(project_id=body.add_to_project, asset_id=a.id))
        if body.remove_from_project:
            pa = db.get(ProjectAsset, (body.remove_from_project, a.id))
            if pa:
                db.delete(pa)
        index_asset(db, a)
    db.commit()
    return {"updated": len(assets)}


def _forget(db: Session, a: Asset) -> None:
    """Remove from the library (never touches the user's files). Rescans skip it."""
    if a.source is not None and a.parent_id is None:
        ignored = list((a.source.options or {}).get("ignored", []))
        if a.source_path not in ignored:
            ignored.append(a.source_path)
        a.source.options = {**(a.source.options or {}), "ignored": ignored}
    for c in db.query(Asset).filter(Asset.parent_id == a.id):
        unindex_asset(db, c.id)
    unindex_asset(db, a.id)
    db.delete(a)


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int, db: Session = Depends(get_db)):
    a = _get(db, asset_id)
    _forget(db, a)
    db.commit()
    return {"ok": True}


class ThumbBody(BaseModel):
    data_url: str


@router.post("/assets/{asset_id}/thumbnail")
def set_thumbnail(asset_id: int, body: ThumbBody, db: Session = Depends(get_db)):
    """Store a thumbnail captured by the browser viewer."""
    a = _get(db, asset_id)
    raw = body.data_url.split(",", 1)[-1]
    data, ext = normalize_thumbnail(base64.b64decode(raw))
    rel, digest, size = store_bytes(data, ext)
    af = next((f for f in a.files if f.role == "thumbnail" and f.label == "viewer"), None)
    if af is None:
        af = AssetFile(role="thumbnail", storage="store", path=rel, name="thumbnail.png", format="png", size=size, sha256=digest, label="viewer")
        a.files.append(af)
    else:
        af.path, af.sha256, af.size = rel, digest, size
    db.flush()
    a.thumbnail_file_id = af.id
    db.commit()
    return {"thumbnail": f"/api/files/{af.id}/thumbnail.png"}


@router.post("/assets/{asset_id}/thumbnail/{file_id}")
def choose_thumbnail(asset_id: int, file_id: int, db: Session = Depends(get_db)):
    a = _get(db, asset_id)
    f = db.get(AssetFile, file_id)
    if f is None or f.asset_id != a.id:
        raise HTTPException(404, "file not found")
    a.thumbnail_file_id = f.id
    db.commit()
    return {"ok": True}


class ExportBody(BaseModel):
    target: str
    force: bool = False


@router.post("/assets/{asset_id}/export")
def export(asset_id: int, body: ExportBody, db: Session = Depends(get_db)):
    a = _get(db, asset_id)
    if body.target not in available_targets(a):
        raise HTTPException(400, f"target '{body.target}' not available for this asset")
    if not body.force:
        cached = next(
            (f for f in a.files if f.role == "export" and f.label == body.target and (f.meta or {}).get("fingerprint") == a.fingerprint),
            None,
        )
        if cached and resolve(cached).exists():
            return {"file": ser_file(cached)}
    jid = enqueue("export", {"target": body.target}, asset_id=a.id, priority=PRIORITY["export"])
    return {"job_id": jid}


@router.post("/assets/{asset_id}/reprocess")
def reprocess(asset_id: int, db: Session = Depends(get_db)):
    a = _get(db, asset_id)
    jobs = []
    if a.format == "blend" and a.parent_id is None:
        jobs.append(enqueue("extract_blend", {}, asset_id=a.id, priority=PRIORITY["extract_blend"] + 5))
    elif a.kind in ("texture", "hdri") or (a.kind == "material" and a.format == "pbr"):
        jobs.append(enqueue("thumbnail", {}, asset_id=a.id, priority=PRIORITY["thumbnail"]))
    elif any(f.role in ("source", "ue_export") for f in a.files) and a.kind in ("model", "skeletal_model", "animation", "scene"):
        jobs.append(enqueue("preview", {}, asset_id=a.id, priority=PRIORITY["preview"] + 5))
    return {"jobs": jobs}


# ----------------------------------------------------------------------------- files

def _send(path: Path, name: str, download: bool):
    if not path.exists():
        raise HTTPException(404, "file missing on disk")
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media,
        filename=name if download else None,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/files/{file_id}")
@router.get("/files/{file_id}/{name}")
def get_file(file_id: int, name: str | None = None, download: bool = False, db: Session = Depends(get_db)):
    f = db.get(AssetFile, file_id)
    if f is None:
        raise HTTPException(404, "file not found")
    return _send(resolve(f), f.name or name or "file", download)


@router.get("/assets/{asset_id}/raw/{rel_path:path}")
def raw_relative(asset_id: int, rel_path: str, db: Session = Depends(get_db)):
    """Serve files relative to the asset's source file so .gltf/.obj/.fbx load their textures."""
    a = _get(db, asset_id)
    src = next((f for f in a.files if f.role in ("source", "ue_export")), None)
    if src is None:
        raise HTTPException(404, "no source file")
    base = resolve(src).parent.resolve()
    if rel_path == src.name or rel_path == "":
        return _send(resolve(src), src.name, False)
    allowed_roots = [base, base.parent]
    target = (base / rel_path).resolve()
    if not any(str(target).startswith(str(r) + "/") or target == r for r in allowed_roots):
        raise HTTPException(403, "outside of asset folder")
    if not target.exists():
        # textures referenced with absolute or broken paths: fall back to a known texture file with that name
        name = Path(rel_path).name.lower()
        for f in a.files:
            if f.role in ("texture", "sidecar") and f.name.lower() == name:
                return _send(resolve(f), f.name, False)
        raise HTTPException(404, "not found")
    return _send(target, target.name, False)
