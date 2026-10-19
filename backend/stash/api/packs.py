"""Asset packs: the top-level folder / zip / Unreal Content folder assets came from.

Assets carry their pack name in `meta.pack`; the `packs` table only stores what people (or a batch
job like tools/stash_describe.py) wrote about a pack. The description and keywords are full-text
indexed with every asset of the pack, so "medieval village" finds the props of that pack even if
none of them is described individually.
"""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Asset, Pack, Source
from ..search import reindex_pack
from .common import file_url, ser_asset_brief

router = APIRouter()

PACK = func.json_extract(Asset.meta, "$.pack")


def ser_pack(name: str, row: Pack | None, count: int = 0, cover: Asset | None = None) -> dict:
    return {
        "name": name,
        "count": count,
        "description": row.description if row else "",
        "keywords": (row.keywords or []) if row else [],
        "meta": (row.meta or {}) if row else {},
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        "cover": file_url(cover.thumbnail) if cover is not None else None,
    }


def _covers(db: Session, names: list[str]) -> dict[str, Asset]:
    """One thumbnail per pack: the first top-level asset that has one, else any asset."""
    out: dict[str, Asset] = {}
    if not names:
        return out
    for top_level in (True, False):
        missing = [n for n in names if n not in out]
        if not missing:
            break
        q = db.query(PACK, func.min(Asset.id)).filter(PACK.in_(missing), Asset.thumbnail_file_id.isnot(None))
        if top_level:
            q = q.filter(Asset.parent_id.is_(None))
        ids = dict(q.group_by(PACK).all())
        for a in db.query(Asset).filter(Asset.id.in_(ids.values())):
            out[(a.meta or {}).get("pack")] = a
    return out


@router.get("/packs")
def list_packs(q: str = "", described: bool | None = None, source: int | None = None, db: Session = Depends(get_db)):
    query = db.query(PACK, func.count()).filter(PACK.isnot(None), PACK != "")
    if source:
        query = query.filter(Asset.source_id == source)
    if q:
        query = query.filter(PACK.ilike(f"%{q}%"))
    counts = dict(query.group_by(PACK).all())
    rows = {p.name: p for p in db.query(Pack).filter(Pack.name.in_(counts))} if counts else {}
    names = sorted(counts, key=lambda n: n.lower())
    if described is not None:
        names = [n for n in names if bool(rows.get(n) and rows[n].description) == described]
    covers = _covers(db, names)
    return [ser_pack(n, rows.get(n), counts[n], covers.get(n)) for n in names]


def _pack_assets(db: Session, name: str):
    return db.query(Asset).filter(PACK == name)


@router.get("/packs/{name:path}")
def get_pack(name: str, samples: int = 24, db: Session = Depends(get_db)):
    """Everything a person or a model needs to describe a pack: what's in it and how it looks."""
    assets = _pack_assets(db, name).all()
    if not assets:
        raise HTTPException(404, "pack not found")
    row = db.query(Pack).filter(Pack.name == name).one_or_none()
    kinds = Counter(a.kind for a in assets)
    formats = Counter(a.format for a in assets if a.format)
    tags = Counter(t.name for a in assets for t in a.tags)
    auto = Counter()
    folders = Counter()
    for a in assets:
        for t in (a.meta or {}).get("auto_tags") or []:
            if isinstance(t, dict) and t.get("tag"):
                auto[t["tag"]] += float(t.get("score", 1.0))
        folder = (a.meta or {}).get("folder") or str(PurePosixPath(a.source_path.split("!/")[-1]).parent)
        parts = [p for p in folder.split("/") if p and p not in (".", name, "Game")]
        if parts:
            folders["/".join(parts[:2])] += 1
    source_ids = {a.source_id for a in assets if a.source_id}
    sources = db.query(Source).filter(Source.id.in_(source_ids)).all() if source_ids else []

    # samples: assets with thumbnails, round-robin over kinds so a pack of 300 textures and
    # 20 props still shows props; top-level assets first
    pool: dict[str, list[Asset]] = {}
    for a in sorted(assets, key=lambda a: (a.parent_id is not None, a.id)):
        if a.thumbnail_file_id:
            pool.setdefault(a.kind, []).append(a)
    picked: list[Asset] = []
    while len(picked) < samples and any(pool.values()):
        for k in sorted(pool, key=lambda k: -kinds[k]):
            if pool[k] and len(picked) < samples:
                picked.append(pool[k].pop(0))

    def sample(a: Asset) -> dict:
        d = ser_asset_brief(a)
        d["auto_tags"] = [t["tag"] for t in (a.meta or {}).get("auto_tags") or [] if isinstance(t, dict)][:5]
        d["description"] = a.description
        return d

    d = ser_pack(name, row, len(assets), picked[0] if picked else None)
    d.update(
        kinds=dict(kinds.most_common()),
        formats=dict(formats.most_common()),
        tags=[t for t, _ in tags.most_common(30)],
        auto_tags=[{"tag": t, "weight": round(w, 2)} for t, w in auto.most_common(30)],
        folders=[{"path": p, "count": c} for p, c in folders.most_common(30)],
        names=sorted({a.name for a in assets if a.parent_id is None or a.kind != "texture"}, key=str.lower)[:300],
        sources=[{"id": s.id, "name": s.name} for s in sources],
        ue_project=next((a.meta.get("ue_project") for a in assets if (a.meta or {}).get("ue_project")), None),
        samples=[sample(a) for a in picked],
        # preview images shipped with the pack (store screenshots, renders) - the best pictures to describe it by
        gallery=[file_url(f) for a in assets for f in a.files if f.role == "gallery"][:12],
    )
    return d


class PackBody(BaseModel):
    description: str | None = None
    keywords: list[str] | None = None
    meta: dict | None = None


@router.put("/packs/{name:path}")
def put_pack(name: str, body: PackBody, db: Session = Depends(get_db)):
    count = _pack_assets(db, name).count()
    if not count:
        raise HTTPException(404, "pack not found")
    row = db.query(Pack).filter(Pack.name == name).one_or_none()
    if row is None:
        row = Pack(name=name)
        db.add(row)
    if body.description is not None:
        row.description = body.description.strip()
    if body.keywords is not None:
        seen: list[str] = []
        for k in body.keywords:
            k = k.strip().lower()
            if k and k not in seen:
                seen.append(k)
        row.keywords = seen[:40]
    row.meta = {**(row.meta or {}), **(body.meta or {"by": "user"})}
    db.flush()
    reindex_pack(db, name)
    db.commit()
    return ser_pack(name, row, count)


@router.delete("/packs/{name:path}")
def delete_pack(name: str, db: Session = Depends(get_db)):
    row = db.query(Pack).filter(Pack.name == name).one_or_none()
    if row:
        db.delete(row)
        db.flush()
        reindex_pack(db, name)
        db.commit()
    return {"ok": True}
