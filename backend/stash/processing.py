"""Job handlers: scanning, .blend extraction, previews, thumbnails, exports."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from . import blender
from .config import settings
from .convert.exporter import available_targets, cached_export, export_asset
from .db import session_scope
from .images import make_thumbnail
from .ingest.formats import kind_for_path
from .ingest.scanner import scan_source, share_thumbnail_with_clips
from .jobs import JobContext, enqueue, handler
from .models import Asset, AssetFile, AssetLink, Job, Project, Source, Tag
from .search import index_asset
from .storage import add_linked_file, add_stored_file, resolve, store_bytes

PRIORITY = {"scan_source": 50, "thumbnail": 20, "extract_blend": 10, "preview": 5, "export": 40, "package_project": 30, "ai_tag": 2, "embed_pending": 1}


def queue_followups(db: Session, followups) -> int:
    n = 0
    for job_type, asset, payload in followups:
        if asset.id is None:
            db.flush()
        enqueue(job_type, payload, asset_id=asset.id, priority=PRIORITY.get(job_type, 0), db=db)
        n += 1
    return n


# ----------------------------------------------------------------------------- scan

@handler("scan_source")
def job_scan(ctx: JobContext):
    with session_scope() as db:
        src = db.get(Source, ctx.source_id)
        if src is None:
            raise RuntimeError("source was deleted")
        src.status = "scanning"
    try:
        with session_scope() as db:
            src = db.get(Source, ctx.source_id)
            result, followups = scan_source(db, src, ctx)
            result["queued"] = queue_followups(db, followups)
            src.status = "idle"
            src.meta = {**(src.meta or {}), "last_result": result}
        ctx.log(f"scan finished: {result}")
        queue_embeddings()
        return result
    except Exception:
        with session_scope() as db:
            src = db.get(Source, ctx.source_id)
            if src:
                src.status = "error"
        raise


# ----------------------------------------------------------------------------- blend extraction

def set_thumbnail(asset: Asset, af: AssetFile) -> None:
    asset.thumbnail_file_id = af.id


def ingest_manifest(db: Session, parent: Asset, manifest: dict, out_dir: Path, ctx: JobContext | None = None) -> dict:
    by_key: dict[str, Asset] = {}
    existing = {a.sub_key: a for a in db.query(Asset).filter(Asset.parent_id == parent.id)}
    seen = set()
    for entry in manifest.get("assets", []):
        key = entry["key"]
        seen.add(key)
        a = existing.get(key)
        if a is None:
            a = Asset(source_id=parent.source_id, source_path=parent.source_path, sub_key=key, parent_id=parent.id, name=entry.get("name", key))
            db.add(a)
        elif not (a.meta or {}).get("renamed"):
            a.name = entry.get("name", a.name)
        a.status = "ok"
        a.kind = entry.get("kind") or "other"
        stats = entry.get("stats") or {}
        meta = {k: v for k, v in entry.items() if k not in ("files", "key", "name", "kind", "tags", "description")}
        meta["origin"] = "blend"
        if (parent.meta or {}).get("pack"):
            meta["pack"] = parent.meta["pack"]
        meta["blend_file"] = parent.source_path
        if (a.meta or {}).get("renamed"):
            meta["renamed"] = True
        a.meta = meta
        a.format = "blend"
        a.poly_count = int(stats.get("triangles") or 0)
        a.is_rigged = bool(entry.get("is_rigged"))
        a.is_animated = bool(entry.get("is_animated")) or a.kind == "animation"
        a.is_procedural = bool(entry.get("is_procedural"))
        a.has_textures = bool(entry.get("textures"))
        if entry.get("description") and not a.description:
            a.description = entry["description"]
        files = entry.get("files", {})
        db.flush()
        for role_key, role in (("blend", "blend"), ("glb", "preview"), ("thumbnail", "thumbnail"), ("image", "source")):
            rel = files.get(role_key)
            if not rel:
                continue
            p = Path(rel)
            if not p.is_absolute():
                p = out_dir / rel
                if not p.exists():
                    continue
                af = add_stored_file(a, p, role, name=p.name, label="render" if role == "thumbnail" else (entry.get("channel") or ""))
            elif settings.store_dir in p.parents:
                # written straight into the store by the extractor (shared, de-duplicated texture)
                if not p.exists():
                    continue
                af = add_stored_file(a, p, role, name=f"{a.name}{p.suffix}" if role == "source" else p.name, label=entry.get("channel") or "")
            else:
                if not p.exists():
                    continue
                af = add_linked_file(a, p, role, label=entry.get("channel") or "")
            db.flush()
            if role == "preview":
                a.preview_file_id = af.id
            elif role == "thumbnail":
                a.thumbnail_file_id = af.id
            elif role == "source" and a.kind in ("texture", "hdri"):
                a.size = af.size
                a.format = p.suffix.lstrip(".").lower()
                a.meta = {**a.meta, "channel": entry.get("channel")}
        for t in entry.get("tags", []) or []:
            tag = db.query(Tag).filter(Tag.name == t.lower()).one_or_none() or Tag(name=t.lower())
            if tag not in a.tags:
                a.tags.append(tag)
        by_key[key] = a
    db.flush()
    # links
    db.query(AssetLink).filter(AssetLink.from_id.in_([a.id for a in by_key.values()])).delete(synchronize_session=False)

    def link(x, y, rel):
        if x and y and x.id != y.id:
            db.add(AssetLink(from_id=x.id, to_id=y.id, relation=rel))

    for key, a in by_key.items():
        entry = next(e for e in manifest["assets"] if e["key"] == key)
        for mk in entry.get("materials", []) or []:
            link(a, by_key.get(mk), "uses_material")
        for tk in entry.get("textures", []) or []:
            link(a, by_key.get(tk), "uses_texture")
        for tk in entry.get("targets", []) or []:
            link(a, by_key.get(tk), "animates")
    # textured models
    for key, a in by_key.items():
        if a.kind in ("model", "skeletal_model"):
            mats = [by_key.get(m) for m in (a.meta.get("materials") or [])]
            if any(m is not None and m.has_textures for m in mats):
                a.has_textures = True
    # sub-assets that vanished from the file
    removed = 0
    for key, a in existing.items():
        if key not in seen:
            a.status = "missing"
            removed += 1
    parent.meta = {
        **(parent.meta or {}),
        "extracted": True,
        "scene_stats": manifest.get("scene_stats"),
        "blender_version": manifest.get("blender_version"),
        "group_mode": manifest.get("group_mode"),
        "extract_errors": manifest.get("errors", [])[:50],
    }
    parent.meta.pop("extract_pending", None)
    parent.poly_count = sum(a.poly_count for a in by_key.values() if a.kind in ("model", "skeletal_model"))
    parent.is_rigged = any(a.is_rigged for a in by_key.values())
    parent.is_animated = any(a.kind == "animation" for a in by_key.values())
    parent.is_procedural = any(a.is_procedural for a in by_key.values())
    parent.has_textures = any(a.kind == "texture" for a in by_key.values())
    # animations borrow the thumbnail of the rig they animate
    for key, a in by_key.items():
        if a.kind == "animation" and a.thumbnail_file_id is None:
            entry = next(e for e in manifest["assets"] if e["key"] == key)
            for tk in entry.get("targets", []) or []:
                t = by_key.get(tk)
                if t is not None and t.thumbnail_file_id:
                    a.thumbnail_file_id = t.thumbnail_file_id
                    a.preview_file_id = t.preview_file_id
                    break
    # a rendered model beats the file's embedded preview (128 px, often a screenshot of Blender's UI)
    embedded = {f.id for f in parent.files if f.role == "thumbnail" and f.label == "embedded"}
    if parent.thumbnail_file_id is None or parent.thumbnail_file_id in embedded:
        for a in by_key.values():
            if a.thumbnail_file_id and a.kind in ("model", "skeletal_model"):
                parent.thumbnail_file_id = a.thumbnail_file_id
                break
    db.flush()
    for a in list(by_key.values()) + [parent]:
        index_asset(db, a)
    # textures without thumbnails get a quick Pillow thumbnail
    for a in by_key.values():
        if a.kind in ("texture", "hdri") and a.thumbnail_file_id is None:
            enqueue("thumbnail", {}, asset_id=a.id, priority=PRIORITY["thumbnail"], db=db)
    return {"assets": len(by_key), "missing": removed, "errors": manifest.get("errors", [])}


@handler("extract_blend")
def job_extract_blend(ctx: JobContext):
    with session_scope() as db:
        asset = db.get(Asset, ctx.asset_id)
        if asset is None:
            return {}
        src = next((f for f in asset.files if f.role == "source"), None)
        if src is None:
            raise RuntimeError("no .blend file")
        path = resolve(src)
        opts = (asset.source.options if asset.source else {}) or {}
    out = Path(tempfile.mkdtemp(prefix=f"extract_{ctx.asset_id}_", dir=settings.work_dir))
    try:
        args = ["--out", str(out), "--group", opts.get("group_mode", "auto"), "--store", str(settings.store_dir), "--preview-max", str(settings.preview_texture_size)]
        if settings.render_thumbnails and opts.get("render_thumbnails", True):
            args += ["--thumbs", "--size", str(settings.thumbnail_size), "--samples", str(opts.get("thumbnail_samples", 24))]
        ctx.log(f"extracting {path}")
        blender.run_script(
            "extract_blend.py",
            args,
            blend_file=path,
            on_progress=lambda v: ctx.progress(v * 0.9, "extracting"),
            should_cancel=ctx.cancelled,
        )
        manifest = json.loads((out / "manifest.json").read_text())
        with session_scope() as db:
            asset = db.get(Asset, ctx.asset_id)
            result = ingest_manifest(db, asset, manifest, out, ctx)
        ctx.log(f"extracted {result['assets']} assets")
        queue_embeddings()
        return result
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ----------------------------------------------------------------------------- previews

def preview_input(asset: Asset) -> AssetFile | None:
    """The file Blender renders an asset's preview from: an FBX/glTF pushed from Unreal, else a source
    Blender can import (never a .uasset), else a GLB pushed from Blender."""
    ue_exp = next((f for f in asset.files if f.role == "ue_export"), None)
    src = next((f for f in asset.files if f.role == "source" and kind_for_path(Path(f.name or f.path)) != "unreal"), None)
    return ue_exp or src or asset.preview


@handler("preview")
def job_preview(ctx: JobContext):
    from .convert.exporter import linked, pbr_maps

    with session_scope() as db:
        asset = db.get(Asset, ctx.asset_id)
        if asset is None:
            return {}
        src = preview_input(asset)
        if src is None:
            return {}
        path = resolve(src)
        # matched texture sets; Blender only uses them for slots without image textures of their own
        pbr = [{**maps, "_name": m.name} for m in linked(db, asset, "uses_material") if (maps := pbr_maps(m))] or None
        texture_dirs = sorted({str(resolve(f).parent) for f in asset.files if f.role == "texture"})
        fingerprint = asset.fingerprint
    render_only = ctx.payload.get("render_only") or path.suffix.lower() in (".glb", ".gltf") and not pbr
    out = Path(tempfile.mkdtemp(prefix=f"preview_{ctx.asset_id}_", dir=settings.work_dir))
    try:
        args = ["--input", str(path), "--preview-max", str(settings.preview_texture_size)]
        if not render_only:
            args += ["--glb", str(out / "preview.glb")]
        if settings.render_thumbnails:
            args += ["--thumb", str(out / "thumb.png"), "--size", str(settings.thumbnail_size)]
        for d in texture_dirs:
            args += ["--search-dir", d]
        if pbr:
            args += ["--pbr", json.dumps(pbr)]
        logtxt = blender.run_script("preview_model.py", args, should_cancel=ctx.cancelled)
        stats = {}
        for line in logtxt.splitlines():
            if line.startswith("[stash-stats] "):
                stats = json.loads(line[14:])
        with session_scope() as db:
            asset = db.get(Asset, ctx.asset_id)
            if (out / "preview.glb").exists():
                af = add_stored_file(asset, out / "preview.glb", "preview", name=f"{asset.name}.glb")
                db.flush()
                asset.preview_file_id = af.id
            if (out / "thumb.png").exists():
                af = add_stored_file(asset, out / "thumb.png", "thumbnail", name="thumbnail.png", label="render")
                db.flush()
                asset.thumbnail_file_id = af.id
            asset.meta = {**(asset.meta or {}), "preview_fingerprint": fingerprint, "blender_stats": stats}
            if stats.get("bones"):
                asset.is_rigged = True
                if asset.kind == "model":
                    asset.kind = "skeletal_model"
            if stats.get("actions"):
                asset.is_animated = True
            db.flush()
            share_thumbnail_with_clips(db, asset)
        queue_embeddings()
        return {"stats": stats}
    finally:
        shutil.rmtree(out, ignore_errors=True)


@handler("thumbnail")
def job_thumbnail(ctx: JobContext):
    with session_scope() as db:
        asset = db.get(Asset, ctx.asset_id)
        if asset is None:
            return {}
        cand = None
        tex = {f.label: f for f in asset.files if f.role == "texture"}
        for ch in ("base_color", "orm", "normal", "height", "roughness"):
            if ch in tex:
                cand = tex[ch]
                break
        if cand is None:
            cand = next((f for f in asset.files if f.role in ("source", "texture")), None)
        if cand is None:
            return {}
        path = resolve(cand)
    data = make_thumbnail(path)
    ext = ".png"
    if data is None and blender.available() and path.suffix.lower() in (".exr", ".hdr", ".tga", ".tif", ".tiff", ".psd", ".dds"):
        out = Path(tempfile.mkdtemp(prefix="thumb_", dir=settings.work_dir))
        try:
            blender.run_script("preview_model.py", ["--image", str(path), "--thumb", str(out / "t.png"), "--size", str(settings.thumbnail_size)])
            if (out / "t.png").exists():
                data = (out / "t.png").read_bytes()
        finally:
            shutil.rmtree(out, ignore_errors=True)
    if data is None:
        return {"thumbnail": False}
    if data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    with session_scope() as db:
        asset = db.get(Asset, ctx.asset_id)
        rel, digest, size = store_bytes(data, ext)
        af = next((f for f in asset.files if f.role == "thumbnail" and f.label == "image"), None)
        if af is None:
            af = AssetFile(role="thumbnail", storage="store", path=rel, name=f"thumbnail{ext}", format=ext[1:], size=size, sha256=digest, label="image")
            asset.files.append(af)
        else:
            af.path, af.sha256, af.size = rel, digest, size
        db.flush()
        if asset.thumbnail_file_id is None or asset.kind in ("texture", "hdri", "material"):
            asset.thumbnail_file_id = af.id
    queue_embeddings()
    return {"thumbnail": True}


# ----------------------------------------------------------------------------- AI tagging

@handler("ai_tag")
def job_ai_tag(ctx: JobContext):
    from . import ai

    if not ai.enabled():
        raise RuntimeError("AI tagging is not configured (set ANTHROPIC_API_KEY on the server)")
    with session_scope() as db:
        asset = db.get(Asset, ctx.asset_id)
        if asset is None:
            return {}
        result = ai.classify(db, asset)
        for name in result["tags"]:
            tag = db.query(Tag).filter(Tag.name == name).one_or_none()
            if tag is None:
                tag = Tag(name=name)
                db.add(tag)
                db.flush()
            if tag not in asset.tags:
                asset.tags.append(tag)
        if result["description"] and not asset.description:
            asset.description = result["description"]
        asset.meta = {**(asset.meta or {}), "ai": result, "category": result["category"]}
        index_asset(db, asset)
    return result


# ----------------------------------------------------------------------------- on-device AI

EMBED_BATCH = 200


def queue_embeddings(db: Session | None = None) -> None:
    """Make sure an embed job is queued (de-duplicated) when local AI is on."""
    from . import vision

    if vision.enabled():
        enqueue("embed_pending", {}, priority=PRIORITY["embed_pending"], db=db)


def _pending_embeddings(db: Session, limit: int):
    from .models import AssetEmbedding

    q = (
        db.query(Asset, AssetFile, AssetEmbedding)
        .join(AssetFile, AssetFile.id == Asset.thumbnail_file_id)
        .outerjoin(AssetEmbedding, AssetEmbedding.asset_id == Asset.id)
        .filter(
            (AssetEmbedding.asset_id.is_(None))
            | (AssetEmbedding.source_sha != AssetFile.sha256)
            | (AssetEmbedding.model != settings.clip_model)
        )
        .limit(limit)
    )
    return q.all()


@handler("embed_pending")
def job_embed_pending(ctx: JobContext):
    """Embed thumbnails that have no (or an outdated) embedding, then suggest tags from them."""
    import numpy as np

    from . import vision
    from .models import AssetEmbedding

    if not vision.enabled():
        return {"skipped": "local AI disabled"}
    ctx.progress(0.0, "loading model" if not vision.model_ready() else "embedding")
    model = vision.clip()
    vision.vocabulary()
    done = failed = 0
    with session_scope() as db:
        batch = [(a.id, resolve(f), f.sha256) for a, f, _e in _pending_embeddings(db, EMBED_BATCH)]
    for i, (asset_id, path, sha) in enumerate(batch):
        ctx.check_cancel()
        try:
            vec = model.embed_image(path)
        except Exception as e:  # unreadable thumbnail - skip it, keep going
            failed += 1
            ctx.log(f"asset {asset_id}: {e}")
            vec = None
        with session_scope() as db:
            asset = db.get(Asset, asset_id)
            if asset is None:
                continue
            row = db.get(AssetEmbedding, asset_id) or AssetEmbedding(asset_id=asset_id)
            row.model = settings.clip_model
            row.source_sha = sha or ""
            row.vector = (vec if vec is not None else np.zeros(model.dim, dtype=np.float32)).astype(np.float16).tobytes()
            db.merge(row)
            if vec is not None:
                channel = (asset.meta or {}).get("channel")
                asset.meta = {**(asset.meta or {}), "auto_tags": vision.suggest_tags(vec, asset.kind, channel)}
                index_asset(db, asset)
                done += 1
        ctx.progress((i + 1) / max(len(batch), 1), f"{i + 1}/{len(batch)}")
    vision.index.invalidate()
    with session_scope() as db:
        more = bool(_pending_embeddings(db, 1))
    if more:
        # the next batch, or thumbnails that arrived while this one ran (their queue_embeddings() was
        # folded into this job); a separate job lets scans/exports run in between batches
        enqueue("embed_pending", {}, priority=PRIORITY["embed_pending"], only_queued=True)
    return {"embedded": done, "failed": failed}


# ----------------------------------------------------------------------------- scheduled rescans

def scheduler_loop(stop_event) -> None:
    """Re-scan sources whose `rescan_hours` option has elapsed (checked every minute)."""
    from datetime import datetime, timedelta, timezone

    last_gc = time.time()
    while not stop_event.wait(60):
        try:
            from . import vision

            vision.unload_if_idle()
        except Exception:
            pass
        if time.time() - last_gc > 24 * 3600:
            last_gc = time.time()
            try:
                from .maintenance import enforce_export_cache, gc_store

                with session_scope() as db:
                    enforce_export_cache(db)
                    gc_store(db)
            except Exception:
                pass
        try:
            with session_scope() as db:
                now = datetime.now(timezone.utc)
                for src in db.query(Source):
                    hours = float((src.options or {}).get("rescan_hours") or 0)
                    if hours <= 0 or src.status == "scanning":
                        continue
                    last = src.last_scan_at
                    if last is None or now - last >= timedelta(hours=hours):
                        enqueue("scan_source", {}, source_id=src.id, priority=PRIORITY["scan_source"], db=db)
        except Exception:  # keep the scheduler alive
            pass


# ----------------------------------------------------------------------------- exports

@handler("export")
def job_export(ctx: JobContext):
    target = ctx.payload["target"]
    with session_scope() as db:
        asset = db.get(Asset, ctx.asset_id)
        if asset is None:
            raise RuntimeError("asset not found")
        if target not in available_targets(asset):
            raise RuntimeError(f"target {target} not available for this asset")
        ctx.progress(0.1, f"exporting {target}")
        af = export_asset(db, asset, target, should_cancel=ctx.cancelled)
        result = {"file_id": af.id, "name": af.name, "size": af.size}
    with session_scope() as db:
        from .maintenance import enforce_export_cache

        enforce_export_cache(db)
    return result


@handler("package_project")
def job_package_project(ctx: JobContext):
    """Zip every asset of a project in one target format."""
    target = ctx.payload["target"]
    project_id = ctx.payload["project_id"]
    with session_scope() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise RuntimeError("project not found")
        asset_ids = [i.asset_id for i in project.items]
        pname = project.name
    out = Path(tempfile.mkdtemp(prefix=f"project_{project_id}_", dir=settings.work_dir))
    zpath = out / f"{''.join(c if c.isalnum() or c in '-_' else '_' for c in pname)}_{target}.zip"
    errors = []
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for i, aid in enumerate(asset_ids):
                ctx.check_cancel()
                ctx.progress(i / max(len(asset_ids), 1), f"{i + 1}/{len(asset_ids)}")
                with session_scope() as db:
                    asset = db.get(Asset, aid)
                    if asset is None:
                        continue
                    t = target if target in available_targets(asset) else ("original" if "original" in available_targets(asset) else None)
                    if t is None:
                        errors.append(f"{asset.name}: no export available")
                        continue
                    try:
                        af = cached_export(asset, t) or export_asset(db, asset, t, should_cancel=ctx.cancelled)
                    except Exception as e:
                        errors.append(f"{asset.name}: {e}")
                        continue
                    z.write(resolve(af), f"{asset.kind}/{af.name}")
            z.writestr("stash_project.json", json.dumps({"project": pname, "target": target, "errors": errors}, indent=2))
        # the zip lives in the store; /api/jobs/{id}/download serves it
        rel, _digest, size = _store_package(zpath)
        with session_scope() as db:
            from .maintenance import enforce_export_cache

            enforce_export_cache(db)
        return {"store_path": rel, "name": zpath.name, "size": size, "errors": errors}
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _store_package(path: Path):
    from .storage import store_file

    return store_file(path)
