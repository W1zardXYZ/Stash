"""Endpoints used by the Blender add-on and the Unreal plugin, plus the Blender extension repository."""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..jobs import enqueue
from ..models import Asset, AssetFile, Delivery, Source, Tag, utcnow
from ..processing import PRIORITY
from ..search import index_asset
from ..storage import add_stored_file, resolve, sha256_file

router = APIRouter()

INTEGRATIONS_DIR = Path(os.environ.get("STASH_INTEGRATIONS_DIR", Path(__file__).resolve().parents[3] / "integrations"))


def _bundle_source(db: Session, origin: str) -> Source:
    kind = "bundles"
    name = {"unreal": "Unreal exports", "blender": "Blender pushes"}.get(origin, "Pushed assets")
    s = db.query(Source).filter(Source.kind == kind, Source.name == name).one_or_none()
    if s is None:
        d = settings.data_dir / "bundles" / origin
        d.mkdir(parents=True, exist_ok=True)
        s = Source(name=name, kind=kind, path=str(d), mode="import", meta={"origin": origin})
        db.add(s)
        db.flush()
    return s


@router.post("/ingest/bundle")
async def ingest_bundle(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Accept a bundle zip (manifest.json + files) pushed by the Unreal or Blender integration.

    Assets exported from Unreal are matched to packages already indexed from the same
    project (by /Game path) so the scanned .uasset gains a real mesh + preview.
    """
    tmp = Path(tempfile.mkdtemp(prefix="bundle_", dir=settings.work_dir))
    try:
        zpath = tmp / "bundle.zip"
        with open(zpath, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
        with zipfile.ZipFile(zpath) as z:
            for m in z.infolist():
                target = (tmp / "x" / m.filename).resolve()
                if not str(target).startswith(str((tmp / "x").resolve())):
                    continue
                if m.is_dir():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(m))
        root = tmp / "x"
        manifest = json.loads((root / "manifest.json").read_text())
        origin = manifest.get("origin", "unknown")
        project = manifest.get("project", "")
        source = _bundle_source(db, origin)
        results = []
        for entry in manifest.get("assets", []):
            results.append(_ingest_entry(db, source, root, origin, project, manifest, entry))
        db.commit()
        from ..processing import queue_embeddings

        queue_embeddings()
        return {"assets": results}
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def _ingest_entry(db: Session, source: Source, root: Path, origin: str, project: str, manifest: dict, entry: dict) -> dict:
    pkg = entry.get("ue_package")
    asset = None
    matched = False
    if pkg:
        q = db.query(Asset).filter(func.json_extract(Asset.meta, "$.ue_package") == pkg)
        if project:
            q = q.filter(func.json_extract(Asset.meta, "$.ue_project") == project)
        asset = q.order_by(Asset.id).first()
        matched = asset is not None
    key = f"{project}:{pkg or entry.get('name')}"
    if asset is None:
        asset = (
            db.query(Asset).filter(Asset.source_id == source.id, Asset.source_path == key, Asset.sub_key == "").one_or_none()
        )
    if asset is None:
        asset = Asset(source_id=source.id, source_path=key, sub_key="", name=entry.get("name") or Path(pkg or "asset").name)
        db.add(asset)
    asset.kind = entry.get("kind") or asset.kind or "model"
    asset.status = "ok"
    meta = dict(asset.meta or {})
    meta.update(entry.get("meta") or {})
    meta.update({"origin": "bundle" if not matched else meta.get("origin", "scan"), "bundle_origin": origin})
    if pkg:
        meta.update({"ue_package": pkg, "ue_project": project, "ue_class": entry.get("ue_class", meta.get("ue_class")), "ue_engine_version": manifest.get("engine_version", meta.get("ue_engine_version"))})
    asset.meta = meta
    if entry.get("description"):
        asset.description = entry["description"]
    st = entry.get("stats") or {}
    if st.get("triangles"):
        asset.poly_count = int(st["triangles"])
    if st.get("bones"):
        asset.is_rigged = True
    if entry.get("animations") or asset.kind == "animation":
        asset.is_animated = True
    db.flush()
    files = entry.get("files") or {}
    mesh = files.get("mesh")
    if mesh and (root / mesh).exists():
        add_stored_file(asset, root / mesh, "ue_export" if origin == "unreal" else "source", name=Path(mesh).name)
        if not asset.format or asset.format not in ("uasset", "umap"):
            asset.format = Path(mesh).suffix.lstrip(".").lower()
    glb = files.get("glb")
    if glb and (root / glb).exists():
        af = add_stored_file(asset, root / glb, "preview", name=Path(glb).name)
        db.flush()
        asset.preview_file_id = af.id
    blend = files.get("blend")
    if blend and (root / blend).exists():
        add_stored_file(asset, root / blend, "blend", name=Path(blend).name)
    thumb = files.get("thumbnail")
    if thumb and (root / thumb).exists():
        af = add_stored_file(asset, root / thumb, "thumbnail", name=Path(thumb).name, label="render")
        db.flush()
        asset.thumbnail_file_id = af.id
    for ch, rel in (files.get("textures") or {}).items():
        if (root / rel).exists():
            add_stored_file(asset, root / rel, "texture", name=Path(rel).name, label=ch)
            asset.has_textures = True
    for extra in files.get("extra", []) or []:
        if (root / extra).exists():
            add_stored_file(asset, root / extra, "sidecar", name=Path(extra).name)
    for t in entry.get("tags", []) or []:
        tag = db.query(Tag).filter(Tag.name == t.lower()).one_or_none() or Tag(name=t.lower())
        if tag not in asset.tags:
            asset.tags.append(tag)
    db.flush()
    index_asset(db, asset)
    if mesh and not glb:
        enqueue("preview", {}, asset_id=asset.id, priority=PRIORITY["preview"] + 10, db=db)
    elif not thumb and (glb or mesh):
        enqueue("preview", {"render_only": True}, asset_id=asset.id, priority=PRIORITY["preview"], db=db)
    return {"id": asset.id, "name": asset.name, "matched": matched}


# ----------------------------------------------------------------------------- deliveries ("send to Blender/Unreal")

class DeliveryBody(BaseModel):
    app: str
    asset_ids: list[int]
    target: str = ""


@router.post("/deliveries")
def create_deliveries(body: DeliveryBody, db: Session = Depends(get_db)):
    if body.app not in ("blender", "unreal"):
        raise HTTPException(400, "app must be blender or unreal")
    ids = []
    for aid in body.asset_ids:
        if db.get(Asset, aid) is None:
            continue
        d = Delivery(app=body.app, asset_id=aid, target=body.target)
        db.add(d)
        db.flush()
        ids.append(d.id)
    db.commit()
    return {"ids": ids}


@router.get("/deliveries")
def list_deliveries(app: str | None = None, status: str = "pending", limit: int = 50, db: Session = Depends(get_db)):
    q = db.query(Delivery)
    if app:
        q = q.filter(Delivery.app == app)
    if status != "all":
        q = q.filter(Delivery.status == status)
    out = []
    for d in q.order_by(Delivery.id).limit(limit):
        a = db.get(Asset, d.asset_id)
        out.append({
            "id": d.id, "app": d.app, "asset_id": d.asset_id, "target": d.target, "status": d.status,
            "message": d.message, "created_at": d.created_at.isoformat() if d.created_at else None,
            "asset": {"id": a.id, "uid": a.uid, "name": a.name, "kind": a.kind} if a else None,
        })
    return out


class DeliveryAck(BaseModel):
    status: str = "delivered"
    message: str = ""
    client: str = ""


@router.post("/deliveries/{delivery_id}/ack")
def ack_delivery(delivery_id: int, body: DeliveryAck, db: Session = Depends(get_db)):
    d = db.get(Delivery, delivery_id)
    if d is None:
        raise HTTPException(404, "delivery not found")
    d.status = body.status if body.status in ("delivered", "failed", "pending") else "delivered"
    d.message = body.message[:2000]
    d.client = body.client[:200]
    d.delivered_at = utcnow()
    db.commit()
    return {"ok": True}


@router.delete("/deliveries")
def clear_deliveries(db: Session = Depends(get_db)):
    n = db.query(Delivery).filter(Delivery.status != "pending").delete(synchronize_session=False)
    db.commit()
    return {"deleted": n}


# ----------------------------------------------------------------------------- Blender extension repository

def _base_url(request: Request) -> str:
    return settings.public_url or str(request.base_url).rstrip("/")


@router.get("/blender/extensions/index.json")
def extensions_index(request: Request, db: Session = Depends(get_db)):
    """A Blender 4.2+ remote extension repository serving every extension in the library.

    In Blender: Preferences > Get Extensions > Repositories > + > Add Remote Repository,
    URL: http://<nas>:8420/api/blender/extensions/index.json
    """
    base = _base_url(request)
    data = []
    for a in db.query(Asset).filter(Asset.kind == "addon", Asset.status == "ok"):
        info = (a.meta or {}).get("addon") or {}
        if info.get("addon_type") != "extension":
            continue
        f = next((x for x in a.files if x.role == "source" and x.format == "zip"), None)
        if f is None:
            continue
        p = resolve(f)
        if not p.exists():
            continue
        if not f.sha256:
            f.sha256 = sha256_file(p)
        m = info.get("manifest") or {}
        entry = {
            "id": m.get("id"),
            "schema_version": m.get("schema_version", "1.0.0"),
            "name": m.get("name"),
            "version": m.get("version"),
            "tagline": m.get("tagline", ""),
            "maintainer": m.get("maintainer", ""),
            "type": m.get("type", "add-on"),
            "tags": m.get("tags", []),
            "blender_version_min": m.get("blender_version_min", "4.2.0"),
            "license": m.get("license", []),
            "website": m.get("website", ""),
            "archive_url": f"{base}/api/files/{f.id}/{f.name}",
            "archive_size": p.stat().st_size,
            "archive_hash": f"sha256:{f.sha256}",
        }
        for opt in ("blender_version_max", "permissions", "platforms"):
            if m.get(opt):
                entry[opt] = m[opt]
        data.append(entry)
    db.commit()
    return JSONResponse({"version": "v1", "blocklist": [], "data": data})


# ----------------------------------------------------------------------------- integration downloads

def _zip_tree(src: Path, arc_root: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f"{arc_root}/{f.relative_to(src).as_posix()}" if arc_root else f.relative_to(src).as_posix())
    return buf.getvalue()


@router.get("/integrations/blender-addon.zip")
def blender_addon():
    src = INTEGRATIONS_DIR / "blender_addon" / "stash_bridge"
    if not src.is_dir():
        raise HTTPException(404, "add-on not bundled")
    # extension zips need blender_manifest.toml at the root
    return Response(_zip_tree(src, ""), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="stash_bridge.zip"'})


@router.get("/integrations/unreal-plugin.zip")
def unreal_plugin():
    src = INTEGRATIONS_DIR / "unreal" / "StashBridge"
    if not src.is_dir():
        raise HTTPException(404, "plugin not bundled")
    return Response(_zip_tree(src, "StashBridge"), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="StashBridge.zip"'})
