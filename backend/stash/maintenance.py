"""Storage housekeeping: cap the export cache and delete store files nothing references."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import settings
from .models import AssetFile, Job

log = logging.getLogger("stash.maintenance")

PACKAGE_MAX_AGE_DAYS = 7  # project packages are one-off downloads


def _job_packages(db: Session) -> list[tuple[Job, str]]:
    out = []
    for j in db.query(Job).filter(Job.type == "package_project", Job.status == "done"):
        rel = (j.result or {}).get("store_path")
        if rel:
            out.append((j, rel))
    return out


def _referenced(db: Session) -> set[str]:
    refs = {p for (p,) in db.query(AssetFile.path).filter(AssetFile.storage == "store")}
    refs.update(rel for _, rel in _job_packages(db))
    return refs


def _unlink_if_unused(db: Session, rel: str) -> int:
    """Delete a store file unless another row still points at it (the store de-duplicates)."""
    still_used = db.query(AssetFile.id).filter(AssetFile.storage == "store", AssetFile.path == rel).first()
    if still_used or any(r == rel for _, r in _job_packages(db)):
        return 0
    p = settings.store_dir / rel
    try:
        size = p.stat().st_size
        p.unlink()
        return size
    except OSError:
        return 0


def enforce_export_cache(db: Session) -> dict:
    """Evict the oldest cached exports until the cache is under STASH_EXPORT_CACHE_GB."""
    freed = removed = 0
    cutoff = time.time() - PACKAGE_MAX_AGE_DAYS * 86400
    for job, rel in _job_packages(db):
        if job.finished_at and job.finished_at.timestamp() < cutoff:
            job.result = {**job.result, "store_path": None, "expired": True}
            db.flush()
            freed += _unlink_if_unused(db, rel)
            removed += 1
    cap = int(settings.export_cache_gb * 1024**3)
    if cap > 0:
        total = db.query(func.coalesce(func.sum(AssetFile.size), 0)).filter(AssetFile.role == "export").scalar()
        if total > cap:
            target = int(cap * 0.9)
            for f in db.query(AssetFile).filter(AssetFile.role == "export").order_by(AssetFile.created_at.asc()):
                if total <= target:
                    break
                rel, size = f.path, f.size
                db.delete(f)
                db.flush()
                freed += _unlink_if_unused(db, rel) if f.storage == "store" else 0
                total -= size
                removed += 1
    if removed:
        log.info("export cache: removed %d entries, freed %.1f MB", removed, freed / 1e6)
    return {"removed": removed, "freed_bytes": freed}


def gc_store(db: Session, min_age_seconds: int = 3600) -> dict:
    """Delete store files no row references (replaced thumbnails, removed assets, old exports).

    Files younger than `min_age_seconds` are kept: a running job may have written them already
    without having committed the row that points at them yet.
    """
    refs = _referenced(db)
    now = time.time()
    freed = removed = kept = 0
    root = settings.store_dir
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if rel in refs:
                kept += 1
                continue
            try:
                st = p.stat()
                if now - st.st_mtime < min_age_seconds:
                    continue
                p.unlink()
                freed += st.st_size
                removed += 1
            except OSError:
                pass
    for d in sorted(root.glob("*/*"), reverse=True):
        try:
            d.rmdir()  # only succeeds when empty
        except OSError:
            pass
    if removed:
        log.info("store gc: removed %d files, freed %.1f MB", removed, freed / 1e6)
    return {"removed": removed, "freed_bytes": freed, "kept": kept}


def storage_stats(db: Session) -> dict:
    by_role = dict(
        db.query(AssetFile.role, func.coalesce(func.sum(AssetFile.size), 0))
        .filter(AssetFile.storage == "store")
        .group_by(AssetFile.role)
        .all()
    )
    on_disk = 0
    for dirpath, _dirs, files in os.walk(settings.store_dir):
        for fn in files:
            try:
                on_disk += (Path(dirpath) / fn).stat().st_size
            except OSError:
                pass
    db_size = sum(p.stat().st_size for p in settings.data_dir.glob("stash.db*") if p.exists())
    return {
        "store_bytes": on_disk,
        "by_role": by_role,
        "export_cache_cap_bytes": int(settings.export_cache_gb * 1024**3),
        "database_bytes": db_size,
    }
