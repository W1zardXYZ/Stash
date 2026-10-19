"""File type classification and PBR texture channel detection."""

from __future__ import annotations

import re
from pathlib import Path

MODEL_EXTS = {
    ".fbx", ".obj", ".gltf", ".glb", ".stl", ".ply", ".dae", ".3ds", ".abc",
    ".usd", ".usda", ".usdc", ".usdz", ".3mf", ".x3d", ".wrl", ".ma", ".mb", ".max", ".c4d", ".lwo", ".dxf",
}
BLEND_EXTS = {".blend"}
IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".bmp", ".webp", ".exr", ".hdr", ".psd", ".dds",
    ".tx", ".ktx2",
}
AUDIO_EXTS = {".wav", ".ogg", ".mp3", ".flac"}
ARCHIVE_EXTS = {".zip"}
UNREAL_EXTS = {".uasset", ".umap"}
SUBSTANCE_EXTS = {".sbsar", ".sbs", ".spp"}

# Formats Blender can import out of the box (4.x). Used to decide what can be converted.
BLENDER_IMPORTABLE = {".fbx", ".obj", ".gltf", ".glb", ".stl", ".ply", ".dae", ".abc", ".usd", ".usda", ".usdc", ".usdz", ".blend", ".x3d", ".wrl"}
# Formats three.js can show directly in the browser.
WEB_VIEWABLE = {".glb", ".gltf", ".fbx", ".obj", ".stl", ".ply", ".dae", ".3mf", ".usdz"}

EXPORT_TARGETS = {
    "glb": "glTF binary (.glb) - web, Godot, three.js",
    "gltf": "glTF separate (.gltf + .bin + textures)",
    "fbx": "FBX (.fbx) - generic",
    "fbx_unreal": "FBX tuned for Unreal Engine (cm scale, no leaf bones) + textures",
    "fbx_unity": "FBX tuned for Unity",
    "obj": "Wavefront OBJ + MTL",
    "usdz": "USDZ (Apple / Omniverse)",
    "usd": "USD (.usdc)",
    "stl": "STL - 3D printing",
    "ply": "PLY",
    "abc": "Alembic cache",
    "blend": "Blender file with packed textures",
}

# Channel name -> suffix tokens commonly used by Megascans, Poly Haven, ambientCG, Substance, UE
CHANNEL_TOKENS: dict[str, tuple[str, ...]] = {
    "base_color": ("basecolor", "base_color", "albedo", "diffuse", "diff", "col", "color", "colour", "d", "bc", "alb"),
    "normal": ("normal", "nor", "nrm", "nml", "norm", "n", "normalgl", "normaldx", "nor_gl", "nor_dx", "normal_gl", "normal_dx"),
    "roughness": ("roughness", "rough", "rgh", "r"),
    "metallic": ("metallic", "metalness", "metal", "met", "m"),
    "ao": ("ao", "ambientocclusion", "ambient_occlusion", "occlusion", "occ"),
    "height": ("height", "displacement", "disp", "displ", "h", "bump", "parallax"),
    "opacity": ("opacity", "alpha", "transparency", "mask", "o"),
    "emissive": ("emissive", "emission", "emit", "e", "glow"),
    "specular": ("specular", "spec", "s"),
    "glossiness": ("glossiness", "gloss", "g"),
    "orm": ("orm", "arm", "rma", "mra", "occlusionroughnessmetallic", "orm_packed"),
    "subsurface": ("sss", "subsurface", "translucency", "trans"),
    "cavity": ("cavity", "cav"),
}

_TOKEN_TO_CHANNEL: dict[str, str] = {}
for _ch, _toks in CHANNEL_TOKENS.items():
    for _t in _toks:
        _TOKEN_TO_CHANNEL.setdefault(_t, _ch)

_RES_RE = re.compile(r"^(\d+k|\d{3,5}(x\d{3,5})?|lod\d|var\d+|png|jpe?g|exr|tga|tiff?)$", re.I)


def kind_for_path(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in MODEL_EXTS:
        return "model"
    if ext in BLEND_EXTS:
        return "blend"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in UNREAL_EXTS:
        return "unreal"
    if ext in SUBSTANCE_EXTS:
        return "substance"
    return None


def split_texture_name(stem: str) -> tuple[str, str | None]:
    """Split "Rock_01_4K_BaseColor" into ("Rock_01", "base_color").

    Tries the last 1-3 tokens (separated by _ - . or space), skipping resolution tokens like 4K.
    Also understands UE-style prefixes: T_Rock_D -> ("Rock", "base_color").
    """
    raw = stem
    tokens = [t for t in re.split(r"[_\-.\s]+", stem) if t]
    if not tokens:
        return stem, None
    prefix_ue = tokens[0].lower() == "t" and len(tokens) > 1
    work = tokens[1:] if prefix_ue else tokens[:]
    # strip trailing resolution / variant tokens
    trailing_res = []
    while len(work) > 1 and _RES_RE.match(work[-1]):
        trailing_res.insert(0, work.pop())
    for n in (2, 1):
        if len(work) > n:
            cand = "".join(work[-n:]).lower()
            cand_us = "_".join(work[-n:]).lower()
            ch = _TOKEN_TO_CHANNEL.get(cand) or _TOKEN_TO_CHANNEL.get(cand_us)
            if ch:
                base_tokens = work[:-n]
                while len(base_tokens) > 1 and _RES_RE.match(base_tokens[-1]):
                    base_tokens.pop()
                return "_".join(base_tokens), ch
    # CamelCase suffix like "RockAlbedo"
    m = re.match(r"^(.*?)(BaseColor|Albedo|Diffuse|Normal|Roughness|Metallic|Metalness|AO|Height|Displacement|Opacity|Emissive|Specular|ORM)$", work[-1]) if work else None
    if m and m.group(1):
        return "_".join(work[:-1] + [m.group(1)]), _TOKEN_TO_CHANNEL.get(m.group(2).lower())
    return raw, None


def is_probably_hdri(path: Path, width: int | None = None, height: int | None = None) -> bool:
    ext = path.suffix.lower()
    if ext not in (".hdr", ".exr"):
        return False
    if width and height:
        return abs(width / max(height, 1) - 2.0) < 0.05
    name = path.stem.lower()
    return any(t in name for t in ("hdri", "sky", "env", "panorama", "studio", "_4k", "_2k", "_8k", "_1k"))
