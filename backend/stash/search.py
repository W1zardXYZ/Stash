"""Full-text search over assets using SQLite FTS5."""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import Asset, Pack


def _meta_text(asset: Asset) -> str:
    m = asset.meta or {}
    parts: list[str] = []
    for key in ("ue_class", "materials", "mesh_names", "animations", "textures", "collection", "catalog", "pack", "tree_type", "channel", "channels", "generator", "bl_info_name", "category", "ue_package"):
        v = m.get(key)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            for x in v[:100]:
                parts.append(x.get("name", "") if isinstance(x, dict) else str(x))
    for t in m.get("auto_tags") or []:
        if isinstance(t, dict) and t.get("tag"):
            parts.append(t["tag"])
    if asset.is_rigged:
        parts.append("rigged skeleton armature")
    if asset.is_animated:
        parts.append("animated animation")
    if asset.is_procedural:
        parts.append("procedural")
    return " ".join(parts)


def _split_words(name: str) -> str:
    """Make "SM_WoodenChair_01" also findable as "wooden chair"."""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    s = re.sub(r"[_\-.]+", " ", s)
    return f"{name} {s}"


def pack_text(db: Session, name: str | None) -> str:
    """Description + keywords of a pack, searched along with each of its assets (cached per session)."""
    if not name:
        return ""
    cache = db.info.setdefault("pack_text", {})
    if name not in cache:
        p = db.query(Pack).filter(Pack.name == name).one_or_none()
        cache[name] = f"{p.description} {' '.join(p.keywords or [])}".strip() if p else ""
    return cache[name]


def index_asset(db: Session, asset: Asset) -> None:
    if asset.id is None:
        db.flush()
    db.execute(text("DELETE FROM asset_fts WHERE rowid = :id"), {"id": asset.id})
    db.execute(
        text("INSERT INTO asset_fts(rowid, name, tags, kind, path, description, extra) VALUES (:id, :n, :t, :k, :p, :d, :e)"),
        {
            "id": asset.id,
            "n": _split_words(asset.name),
            "t": " ".join(t.name for t in asset.tags),
            "k": asset.kind.replace("_", " "),
            "p": re.sub(r"[/\\_\-.]+", " ", asset.source_path or ""),
            "d": asset.description or "",
            "e": f"{_meta_text(asset)} {pack_text(db, (asset.meta or {}).get('pack'))}",
        },
    )


def unindex_asset(db: Session, asset_id: int) -> None:
    db.execute(text("DELETE FROM asset_fts WHERE rowid = :id"), {"id": asset_id})


def reindex_pack(db: Session, name: str) -> int:
    db.info.get("pack_text", {}).pop(name, None)
    from sqlalchemy import func

    n = 0
    for a in db.query(Asset).filter(func.json_extract(Asset.meta, "$.pack") == name).yield_per(500):
        index_asset(db, a)
        n += 1
    return n


def reindex_all(db: Session) -> int:
    db.info.pop("pack_text", None)
    db.execute(text("DELETE FROM asset_fts"))
    n = 0
    for a in db.query(Asset).yield_per(500):
        index_asset(db, a)
        n += 1
    return n


def fts_query(q: str) -> str | None:
    """Turn user input into an FTS5 query: every word must match as a prefix.

    Supports -word for exclusion and quoted phrases.
    """
    q = q.strip()
    if not q:
        return None
    terms = re.findall(r'-?"[^"]+"|-?[^\s"]+', q)
    pos, neg = [], []
    for t in terms:
        negative = t.startswith("-")
        t = t.lstrip("-")
        if t.startswith('"'):
            phrase = t.strip('"').replace('"', "")
            expr = f'"{phrase}"'
        else:
            word = re.sub(r"[^\w]", " ", t).strip()
            if not word:
                continue
            expr = " ".join(f'"{w}"*' for w in word.split())
        (neg if negative else pos).append(expr)
    if not pos:
        return None
    out = " AND ".join(pos)
    for n in neg:
        out += f" NOT {n}"
    return out


def ranked_subquery(q: str):
    """(rid, rank) rows for an FTS match - join it to filter and order by relevance in SQL.

    Joining instead of `id IN (...)` keeps huge result sets under SQLite's parameter limit.
    """
    from sqlalchemy import column

    fq = fts_query(q)
    if fq is None:
        return None
    return (
        text(
            "SELECT rowid AS rid, bm25(asset_fts, 10.0, 6.0, 2.0, 1.0, 2.0, 1.0) AS rank "
            "FROM asset_fts WHERE asset_fts MATCH :fq"
        )
        .bindparams(fq=fq)
        .columns(column("rid"), column("rank"))
        .subquery("fts")
    )


def search_ids(db: Session, q: str, limit: int = 100000) -> list[int] | None:
    fq = fts_query(q)
    if fq is None:
        return None
    rows = db.execute(
        text("SELECT rowid FROM asset_fts WHERE asset_fts MATCH :q ORDER BY bm25(asset_fts, 10.0, 6.0, 2.0, 1.0, 2.0, 1.0) LIMIT :l"),
        {"q": fq, "l": limit},
    ).fetchall()
    return [r[0] for r in rows]
