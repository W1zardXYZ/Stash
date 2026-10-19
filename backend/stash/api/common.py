"""Shared API helpers: auth, serialization."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request

from ..config import settings
from ..models import Asset, AssetFile, Job, Source

SESSION_COOKIE = "stash_session"


def session_token() -> str:
    secret = (settings.password or "") + "|stash"
    return hmac.new(secret.encode(), b"session", hashlib.sha256).hexdigest()


def auth_enabled() -> bool:
    return bool(settings.password or settings.api_token)


def is_authorized(request: Request) -> bool:
    if not auth_enabled():
        return True
    if settings.password and hmac.compare_digest(request.cookies.get(SESSION_COOKIE, ""), session_token()):
        return True
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
    for valid in (settings.api_token, settings.password):
        if valid and token and hmac.compare_digest(token, valid):
            return True
    return False


def file_url(f: AssetFile | None) -> str | None:
    if f is None:
        return None
    return f"/api/files/{f.id}/{f.name or 'file'}"


def ser_file(f: AssetFile) -> dict:
    return {
        "id": f.id,
        "role": f.role,
        "name": f.name,
        "format": f.format,
        "size": f.size,
        "label": f.label,
        "storage": f.storage,
        "path": f.path if f.storage == "source" else None,
        "url": file_url(f),
        "meta": f.meta or {},
    }


def ser_asset_brief(a: Asset) -> dict:
    return {
        "id": a.id,
        "uid": a.uid,
        "name": a.name,
        "kind": a.kind,
        "format": a.format,
        "status": a.status,
        "favorite": a.favorite,
        "rating": a.rating,
        "size": a.size,
        "poly_count": a.poly_count,
        "is_rigged": a.is_rigged,
        "is_animated": a.is_animated,
        "is_procedural": a.is_procedural,
        "has_textures": a.has_textures,
        "thumbnail": file_url(a.thumbnail),
        "has_preview": a.preview_file_id is not None,
        "source_id": a.source_id,
        "parent_id": a.parent_id,
        "source_path": a.source_path,
        "tags": [t.name for t in a.tags],
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        "ue_class": (a.meta or {}).get("ue_class"),
        "child_count": None,
    }


def ser_source(s: Source, counts: dict | None = None) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "kind": s.kind,
        "path": s.path,
        "mode": s.mode,
        "options": s.options or {},
        "status": s.status,
        "last_scan_at": s.last_scan_at.isoformat() if s.last_scan_at else None,
        "meta": s.meta or {},
        "asset_count": (counts or {}).get(s.id, 0),
    }


def ser_job(j: Job, with_log: bool = False) -> dict:
    d = {
        "id": j.id,
        "type": j.type,
        "status": j.status,
        "progress": j.progress,
        "message": j.message,
        "payload": j.payload,
        "result": j.result,
        "asset_id": j.asset_id,
        "source_id": j.source_id,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }
    if with_log:
        d["log"] = j.log
    return d


def require(cond, status: int = 404, detail: str = "not found"):
    if not cond:
        raise HTTPException(status, detail)
