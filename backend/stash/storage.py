"""Content-addressed file store plus helpers to resolve AssetFile rows to real paths."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from .config import settings
from .models import Asset, AssetFile


def sha256_file(path: Path, limit: int | None = None) -> str:
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
            if limit and read >= limit:
                break
    return h.hexdigest()


def quick_fingerprint(path: Path) -> str:
    """Cheap identity for big linked files: size + first/last MiB."""
    st = path.stat()
    h = hashlib.sha256(str(st.st_size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1024 * 1024))
        if st.st_size > 2 * 1024 * 1024:
            f.seek(-1024 * 1024, os.SEEK_END)
            h.update(f.read())
    return h.hexdigest()


def store_file(src: Path, ext: str | None = None, move: bool = False) -> tuple[str, str, int]:
    """Copy (or move) a file into the store. Returns (relative path, sha256, size)."""
    src = Path(src)
    digest = sha256_file(src)
    ext = (ext if ext is not None else src.suffix).lower()
    rel = f"{digest[:2]}/{digest[2:4]}/{digest}{ext}"
    dest = settings.store_dir / rel
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        if move:
            shutil.move(str(src), tmp)
        else:
            shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    elif move:
        src.unlink(missing_ok=True)
    return rel, digest, dest.stat().st_size


def store_bytes(data: bytes, ext: str) -> tuple[str, str, int]:
    digest = hashlib.sha256(data).hexdigest()
    rel = f"{digest[:2]}/{digest[2:4]}/{digest}{ext.lower()}"
    dest = settings.store_dir / rel
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    return rel, digest, len(data)


def resolve(f: AssetFile) -> Path:
    if f.storage == "store":
        return settings.store_dir / f.path
    return Path(f.path)


def add_stored_file(
    asset: Asset,
    src: Path,
    role: str,
    name: str | None = None,
    label: str = "",
    meta: dict | None = None,
    move: bool = False,
) -> AssetFile:
    """Store `src` and attach it to `asset`, replacing an identical role/label/name entry."""
    rel, digest, size = store_file(src, move=move)
    name = name or Path(src).name
    for existing in asset.files:
        if existing.role == role and existing.label == label and existing.name == name:
            existing.storage, existing.path, existing.sha256, existing.size = "store", rel, digest, size
            existing.meta = meta or existing.meta
            return existing
    af = AssetFile(
        role=role,
        storage="store",
        path=rel,
        name=name,
        format=Path(name).suffix.lstrip(".").lower(),
        size=size,
        sha256=digest,
        label=label,
        meta=meta or {},
    )
    asset.files.append(af)
    return af


def add_linked_file(asset: Asset, path: Path, role: str, label: str = "", meta: dict | None = None) -> AssetFile:
    path = Path(path)
    sp = str(path)
    for existing in asset.files:
        if existing.role == role and existing.storage == "source" and existing.path == sp:
            existing.label = label or existing.label
            try:
                existing.size = path.stat().st_size
            except OSError:
                pass
            if meta:
                existing.meta = meta
            return existing
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    af = AssetFile(
        role=role,
        storage="source",
        path=sp,
        name=path.name,
        format=path.suffix.lstrip(".").lower(),
        size=size,
        label=label,
        meta=meta or {},
    )
    asset.files.append(af)
    return af


def primary_source_file(asset: Asset) -> AssetFile | None:
    for f in asset.files:
        if f.role == "source":
            return f
    return None
