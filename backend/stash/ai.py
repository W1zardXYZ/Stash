"""Optional AI categorisation: Claude looks at an asset's thumbnail + metadata and suggests
a category, tags and a one-line description.

Enabled when the server has Anthropic credentials (ANTHROPIC_API_KEY, or any credential the
SDK resolves) and STASH_AI is not "false". Model: STASH_AI_MODEL (default claude-opus-5-5;
claude-haiku-5-5 is far cheaper for tagging thousands of assets).
"""

from __future__ import annotations

import base64
import json
import os

from .models import Asset, AssetLink
from .storage import resolve

MODEL = os.environ.get("STASH_AI_MODEL", "claude-opus-5-5")

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "description": "One short lowercase noun phrase, e.g. 'furniture', 'rock', 'sci-fi weapon', 'tree', 'humanoid character'"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "5-12 short lowercase search tags: object type, style, material, era, setting, color, use"},
        "description": {"type": "string", "description": "One sentence describing what the asset is"},
    },
    "required": ["category", "tags", "description"],
    "additionalProperties": False,
}

SYSTEM = (
    "You catalogue 3D assets (game/film/DCC: models, rigs, animations, materials, textures, HDRIs, "
    "Unreal packages, Blender add-ons) for a personal asset library. From the preview image and metadata, "
    "say what the asset is so it can be found by search. Tags are lowercase, singular, 1-3 words, no "
    "duplicates of the file format or of generic words like 'asset', '3d', 'model'. Prefer concrete "
    "nouns and styles (e.g. 'chair', 'medieval', 'wood', 'low poly', 'pbr'). If the image is missing "
    "or unclear, rely on the names and paths."
)


def enabled() -> bool:
    if os.environ.get("STASH_AI", "").lower() in ("0", "false", "off", "no"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE") or os.environ.get("STASH_AI") == "true")


def _context(db, asset: Asset) -> str:
    m = asset.meta or {}
    insp = m.get("inspect") or {}
    stats = m.get("stats") or {}
    lines = [
        f"Name: {asset.name}",
        f"Kind: {asset.kind}",
        f"File: {asset.source_path}",
    ]
    if asset.source is not None:
        lines.append(f"Library source: {asset.source.name}")
    for label, value in (
        ("Unreal class", m.get("ue_class")),
        ("Unreal path", m.get("ue_package")),
        ("Pack", m.get("pack")),
        ("Folder", m.get("folder")),
        ("Triangles", asset.poly_count or None),
        ("Bones", stats.get("bones") or insp.get("bones")),
        ("Materials", ", ".join(insp.get("materials", [])[:20]) or None),
        ("Animations", ", ".join(a.get("name", "") for a in insp.get("animations", [])[:20]) or None),
        ("Texture channels", ", ".join(m.get("channels", [])) or None),
        ("Add-on", (m.get("addon") or {}).get("description")),
        ("Existing tags", ", ".join(t.name for t in asset.tags) or None),
        ("Existing notes", asset.description or None),
    ):
        if value:
            lines.append(f"{label}: {value}")
    related = [
        db.get(Asset, l.to_id).name
        for l in db.query(AssetLink).filter(AssetLink.from_id == asset.id).limit(15)
        if db.get(Asset, l.to_id) is not None
    ]
    if related:
        lines.append(f"Uses: {', '.join(related)}")
    if asset.parent is not None:
        lines.append(f"Part of: {asset.parent.name}")
    return "\n".join(lines)


def _image_block(asset: Asset) -> dict | None:
    f = asset.thumbnail
    if f is None:
        return None
    p = resolve(f)
    if not p.exists() or p.stat().st_size > 4 * 1024 * 1024:
        return None
    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(p.suffix.lstrip(".").lower())
    if media is None:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.standard_b64encode(p.read_bytes()).decode()}}


def classify(db, asset: Asset) -> dict:
    """Ask Claude for category/tags/description. Raises on API errors."""
    import anthropic

    client = anthropic.Anthropic()
    content: list[dict] = []
    img = _image_block(asset)
    if img:
        content.append(img)
    content.append({"type": "text", "text": f"Catalogue this asset.\n\n{_context(db, asset)}"})
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to classify this asset")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("classification was cut off (max_tokens)")
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)
    tags = []
    for t in [data.get("category", "")] + list(data.get("tags", [])):
        t = " ".join(str(t).lower().split())[:60]
        if t and t not in tags and t not in ("asset", "3d", "model", asset.format):
            tags.append(t)
    return {"category": data.get("category", "").lower().strip(), "tags": tags[:14], "description": data.get("description", "").strip(), "model": response.model}
