"""Sources, filesystem browsing, tags, projects, jobs, uploads and status."""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import __version__, blender
from ..config import settings
from ..db import get_db
from ..jobs import enqueue
from ..models import Asset, Job, Project, ProjectAsset, Source, Tag, asset_tags
from ..processing import PRIORITY
from ..search import index_asset, reindex_all
from ..ai import enabled as ai_enabled
from .common import auth_enabled, ser_asset_brief, ser_job, ser_source

router = APIRouter()


# ----------------------------------------------------------------------------- status

@router.get("/status")
def status(db: Session = Depends(get_db)):
    jobs = dict(db.query(Job.status, func.count()).group_by(Job.status).all())
    return {
        "version": __version__,
        "blender": blender.available(),
        "blender_path": settings.blender,
        "workers": settings.workers,
        "assets": db.query(func.count(Asset.id)).scalar(),
        "sources": db.query(func.count(Source.id)).scalar(),
        "jobs": jobs,
        "auth": auth_enabled(),
        "ai": ai_enabled(),
        "local_ai": _local_ai(db),
        "browse_roots": _roots(),
        "data_dir": str(settings.data_dir),
    }


def _local_ai(db: Session) -> dict:
    from .. import vision
    from ..models import AssetEmbedding

    if not vision.enabled():
        return {"enabled": False}
    return {
        "enabled": True,
        "model": settings.clip_model,
        "downloaded": vision.model_ready(),
        "embedded": db.query(func.count(AssetEmbedding.asset_id)).scalar(),
    }


@router.get("/status/blender")
def blender_version():
    return {"available": blender.available(), "version": blender.version()}


@router.get("/maintenance/storage")
def storage(db: Session = Depends(get_db)):
    from ..maintenance import storage_stats

    return storage_stats(db)


@router.post("/maintenance/cleanup")
def cleanup(db: Session = Depends(get_db)):
    """Evict over-cap exports, expire old project packages, delete unreferenced store files."""
    from ..maintenance import enforce_export_cache, gc_store

    cache = enforce_export_cache(db)
    db.commit()
    gc = gc_store(db, min_age_seconds=600)
    return {"export_cache": cache, "gc": gc}


@router.post("/maintenance/reindex")
def reindex(db: Session = Depends(get_db)):
    n = reindex_all(db)
    db.commit()
    return {"indexed": n}


# ----------------------------------------------------------------------------- filesystem

def _roots() -> list[str]:
    if settings.browse_roots:
        return settings.browse_roots
    return ["/"]


@router.get("/fs")
def browse(path: str | None = None):
    roots = _roots()
    if not path:
        return {"path": None, "parent": None, "entries": [{"name": r, "path": r, "dir": True} for r in roots]}
    p = Path(path).resolve()
    if not any(str(p) == r.rstrip("/") or str(p).startswith(r.rstrip("/") + "/") or r == "/" for r in roots):
        raise HTTPException(403, "path outside of browse roots")
    if not p.exists():
        raise HTTPException(404, "path not found")
    entries = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.lower())):
            if e.name.startswith("."):
                continue
            is_dir = e.is_dir(follow_symlinks=True)
            if not is_dir and not e.name.lower().endswith((".blend", ".uproject", ".zip", ".fbx", ".glb", ".gltf", ".obj")):
                continue
            entries.append({"name": e.name, "path": str(Path(e.path)), "dir": is_dir})
            if len(entries) > 2000:
                break
    except PermissionError:
        raise HTTPException(403, "permission denied")
    parent = str(p.parent) if str(p) not in [r.rstrip("/") for r in roots] and p.parent != p else None
    hints = {
        "unreal_project": any(x.suffix == ".uproject" for x in p.iterdir()) if p.is_dir() else p.suffix == ".uproject",
        "blend": p.suffix == ".blend",
    }
    return {"path": str(p), "parent": parent, "entries": entries, "hints": hints}


# ----------------------------------------------------------------------------- sources

class SourceBody(BaseModel):
    name: str | None = None
    path: str
    kind: str | None = None
    mode: str = "linked"
    options: dict = {}
    scan: bool = True


def _detect_kind(path: Path) -> str:
    if path.suffix.lower() == ".blend":
        return "blender_file"
    if path.suffix.lower() == ".uproject" or (path.is_dir() and any(path.glob("*.uproject"))):
        return "unreal_project"
    return "folder"


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    counts = dict(db.query(Asset.source_id, func.count(Asset.id)).group_by(Asset.source_id).all())
    return [ser_source(s, counts) for s in db.query(Source).order_by(Source.name)]


@router.post("/sources")
def create_source(body: SourceBody, db: Session = Depends(get_db)):
    p = Path(body.path).expanduser()
    if not p.exists():
        raise HTTPException(400, f"path does not exist on the server: {p}")
    s = Source(
        name=body.name or p.stem or str(p),
        path=str(p.resolve()),
        kind=body.kind or _detect_kind(p),
        mode=body.mode if body.mode in ("linked", "import") else "linked",
        options=body.options or {},
    )
    db.add(s)
    db.commit()
    if body.scan:
        enqueue("scan_source", {}, source_id=s.id, priority=PRIORITY["scan_source"])
    return ser_source(s)


class SourcePatch(BaseModel):
    name: str | None = None
    mode: str | None = None
    options: dict | None = None


@router.patch("/sources/{source_id}")
def patch_source(source_id: int, body: SourcePatch, db: Session = Depends(get_db)):
    s = db.get(Source, source_id)
    if s is None:
        raise HTTPException(404, "source not found")
    if body.name:
        s.name = body.name
    if body.mode in ("linked", "import"):
        s.mode = body.mode
    if body.options is not None:
        s.options = {**(s.options or {}), **body.options}
    db.commit()
    return ser_source(s)


@router.post("/sources/{source_id}/scan")
def scan(source_id: int, db: Session = Depends(get_db)):
    s = db.get(Source, source_id)
    if s is None:
        raise HTTPException(404, "source not found")
    jid = enqueue("scan_source", {}, source_id=s.id, priority=PRIORITY["scan_source"])
    return {"job_id": jid}


@router.post("/sources/{source_id}/extract-all")
def extract_all(source_id: int, db: Session = Depends(get_db)):
    """Re-run Blender extraction/previews for every container/model in a source."""
    ids = []
    for a in db.query(Asset).filter(Asset.source_id == source_id, Asset.parent_id.is_(None)):
        if a.format == "blend":
            ids.append(enqueue("extract_blend", {}, asset_id=a.id, priority=PRIORITY["extract_blend"]))
        elif a.kind in ("model", "skeletal_model") and any(f.role == "source" for f in a.files):
            ids.append(enqueue("preview", {}, asset_id=a.id, priority=PRIORITY["preview"]))
    return {"jobs": len(ids)}


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db)):
    s = db.get(Source, source_id)
    if s is None:
        raise HTTPException(404, "source not found")
    db.query(Job).filter(Job.source_id == source_id, Job.status == "queued").update({"status": "cancelled"})
    db.delete(s)
    db.commit()
    reindex_all(db)
    db.commit()
    return {"ok": True}


# ----------------------------------------------------------------------------- uploads

@router.post("/upload")
async def upload(files: list[UploadFile] = File(...), paths: list[str] = Form(default=[]), db: Session = Depends(get_db)):
    """Upload files (keeps relative folder structure when `paths` is given) into the Uploads source."""
    batch = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest_root = settings.uploads_dir / batch
    for i, f in enumerate(files):
        rel = paths[i] if i < len(paths) and paths[i] else f.filename or f"file_{i}"
        rel = Path(rel.replace("\\", "/"))
        parts = [x for x in rel.parts if x not in ("..", "/", "")]
        target = dest_root.joinpath(*parts) if parts else dest_root / f"file_{i}"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            shutil.copyfileobj(f.file, out)
    src = db.query(Source).filter(Source.kind == "uploads").one_or_none()
    if src is None:
        src = Source(name="Uploads", path=str(settings.uploads_dir), kind="uploads", mode="linked")
        db.add(src)
        db.commit()
    jid = enqueue("scan_source", {}, source_id=src.id, priority=PRIORITY["scan_source"])
    return {"job_id": jid, "batch": batch, "count": len(files)}


# ----------------------------------------------------------------------------- tags

@router.get("/tags")
def list_tags(db: Session = Depends(get_db)):
    rows = (
        db.query(Tag, func.count(asset_tags.c.asset_id))
        .outerjoin(asset_tags, asset_tags.c.tag_id == Tag.id)
        .group_by(Tag.id)
        .order_by(Tag.name)
        .all()
    )
    return [{"id": t.id, "name": t.name, "color": t.color, "count": c} for t, c in rows]


class TagPatch(BaseModel):
    name: str | None = None
    color: str | None = None
    merge_into: str | None = None


@router.patch("/tags/{tag_id}")
def patch_tag(tag_id: int, body: TagPatch, db: Session = Depends(get_db)):
    t = db.get(Tag, tag_id)
    if t is None:
        raise HTTPException(404, "tag not found")
    affected = list(t.assets)
    if body.merge_into:
        target = db.query(Tag).filter(Tag.name == body.merge_into.lower()).one_or_none()
        if target is None:
            target = Tag(name=body.merge_into.lower())
            db.add(target)
        for a in affected:
            if target not in a.tags:
                a.tags.append(target)
        db.delete(t)
    else:
        if body.name:
            t.name = body.name.lower()
        if body.color is not None:
            t.color = body.color
    db.flush()
    for a in affected:
        index_asset(db, a)
    db.commit()
    return {"ok": True}


@router.delete("/tags/{tag_id}")
def delete_tag(tag_id: int, db: Session = Depends(get_db)):
    t = db.get(Tag, tag_id)
    if t is None:
        raise HTTPException(404, "tag not found")
    affected = list(t.assets)
    db.delete(t)
    db.flush()
    for a in affected:
        index_asset(db, a)
    db.commit()
    return {"ok": True}


# ----------------------------------------------------------------------------- projects

class ProjectBody(BaseModel):
    name: str
    description: str = ""
    color: str = ""


def _ser_project(p: Project, db: Session) -> dict:
    count = db.query(func.count()).select_from(ProjectAsset).filter(ProjectAsset.project_id == p.id).scalar()
    covers = (
        db.query(Asset)
        .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
        .filter(ProjectAsset.project_id == p.id, Asset.thumbnail_file_id.isnot(None))
        .limit(4)
        .all()
    )
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "color": p.color,
        "count": count,
        "covers": [ser_asset_brief(a)["thumbnail"] for a in covers],
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    return [_ser_project(p, db) for p in db.query(Project).order_by(Project.name)]


@router.post("/projects")
def create_project(body: ProjectBody, db: Session = Depends(get_db)):
    if db.query(Project).filter(Project.name == body.name).first():
        raise HTTPException(400, "a project with that name exists")
    p = Project(name=body.name, description=body.description, color=body.color)
    db.add(p)
    db.commit()
    return _ser_project(p, db)


@router.get("/projects/{project_id}")
def get_project(project_id: int, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    return _ser_project(p, db)


@router.patch("/projects/{project_id}")
def patch_project(project_id: int, body: ProjectBody, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    p.name, p.description, p.color = body.name, body.description, body.color
    db.commit()
    return _ser_project(p, db)


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    db.delete(p)
    db.commit()
    return {"ok": True}


class ProjectAssets(BaseModel):
    ids: list[int]


@router.post("/projects/{project_id}/assets")
def add_project_assets(project_id: int, body: ProjectAssets, db: Session = Depends(get_db)):
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "project not found")
    n = 0
    for aid in body.ids:
        if db.get(Asset, aid) and not db.get(ProjectAsset, (project_id, aid)):
            db.add(ProjectAsset(project_id=project_id, asset_id=aid))
            n += 1
    db.commit()
    return {"added": n}


@router.delete("/projects/{project_id}/assets/{asset_id}")
def remove_project_asset(project_id: int, asset_id: int, db: Session = Depends(get_db)):
    pa = db.get(ProjectAsset, (project_id, asset_id))
    if pa:
        db.delete(pa)
        db.commit()
    return {"ok": True}


class PackageBody(BaseModel):
    target: str


@router.post("/projects/{project_id}/package")
def package_project(project_id: int, body: PackageBody, db: Session = Depends(get_db)):
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "project not found")
    jid = enqueue("package_project", {"project_id": project_id, "target": body.target}, priority=PRIORITY["package_project"], dedupe=False)
    return {"job_id": jid}


# ----------------------------------------------------------------------------- jobs

@router.get("/jobs")
def list_jobs(status: str | None = None, limit: int = 100, asset_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(Job)
    if status:
        q = q.filter(Job.status.in_(status.split(",")))
    if asset_id:
        q = q.filter(Job.asset_id == asset_id)
    rows = q.order_by(Job.id.desc()).limit(min(limit, 1000)).all()
    counts = dict(db.query(Job.status, func.count()).group_by(Job.status).all())
    return {"items": [ser_job(j) for j in rows], "counts": counts}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    return ser_job(j, with_log=True)


@router.get("/jobs/{job_id}/download")
def download_job_result(job_id: int, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if j is None or j.status != "done":
        raise HTTPException(404, "job result not available")
    rel = (j.result or {}).get("store_path")
    if rel:
        return FileResponse(settings.store_dir / rel, filename=j.result.get("name", "package.zip"))
    fid = (j.result or {}).get("file_id")
    if fid:
        from ..models import AssetFile
        from ..storage import resolve

        f = db.get(AssetFile, fid)
        if f:
            return FileResponse(resolve(f), filename=f.name)
    raise HTTPException(404, "no downloadable result")


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if j and j.status in ("queued", "running"):
        j.status = "cancelled"
        db.commit()
    return {"ok": True}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    jid = enqueue(j.type, j.payload, asset_id=j.asset_id, source_id=j.source_id, priority=j.priority)
    return {"job_id": jid}


@router.delete("/jobs")
def clear_jobs(db: Session = Depends(get_db)):
    n = db.query(Job).filter(Job.status.in_(("done", "cancelled", "error"))).delete(synchronize_session=False)
    db.commit()
    return {"deleted": n}
