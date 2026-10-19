"""Detect and describe Blender add-ons / extensions (zip files, folders or single .py files)."""

from __future__ import annotations

import ast
import io
import tomllib
import zipfile
from pathlib import Path


def _bl_info_from_source(src: str) -> dict | None:
    if "bl_info" not in src:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "bl_info" for t in node.targets):
            try:
                info = ast.literal_eval(node.value)
                return info if isinstance(info, dict) else None
            except Exception:
                return None
    return None


def _normalize_bl_info(info: dict) -> dict:
    def ver(v):
        return ".".join(str(x) for x in v) if isinstance(v, (tuple, list)) else str(v or "")

    return {
        "addon_type": "legacy",
        "name": info.get("name", ""),
        "version": ver(info.get("version")),
        "blender_version_min": ver(info.get("blender")),
        "author": info.get("author", ""),
        "description": info.get("description", ""),
        "category": info.get("category", ""),
        "location": info.get("location", ""),
        "doc_url": info.get("doc_url") or info.get("wiki_url", ""),
    }


def _normalize_manifest(m: dict) -> dict:
    return {
        "addon_type": "extension",
        "id": m.get("id", ""),
        "name": m.get("name", ""),
        "version": str(m.get("version", "")),
        "tagline": m.get("tagline", ""),
        "description": m.get("tagline", ""),
        "maintainer": m.get("maintainer", ""),
        "author": m.get("maintainer", ""),
        "type": m.get("type", "add-on"),
        "tags": m.get("tags", []),
        "category": ", ".join(m.get("tags", [])),
        "blender_version_min": m.get("blender_version_min", ""),
        "blender_version_max": m.get("blender_version_max", ""),
        "license": m.get("license", []),
        "website": m.get("website", ""),
        "permissions": m.get("permissions", {}),
        "platforms": m.get("platforms", []),
        "manifest": m,
    }


def inspect_addon_zip(path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            # extension manifest at the root or one folder deep
            for n in names:
                parts = n.split("/")
                if parts[-1] == "blender_manifest.toml" and len(parts) <= 2:
                    m = tomllib.loads(z.read(n).decode("utf-8", "replace"))
                    return _normalize_manifest(m)
            for n in names:
                parts = n.split("/")
                if parts[-1] == "__init__.py" and len(parts) == 2:
                    info = _bl_info_from_source(z.read(n).decode("utf-8", "replace"))
                    if info:
                        return _normalize_bl_info(info) | {"module": parts[0]}
            py_root = [n for n in names if n.endswith(".py") and "/" not in n]
            if len(py_root) == 1:
                info = _bl_info_from_source(z.read(py_root[0]).decode("utf-8", "replace"))
                if info:
                    return _normalize_bl_info(info) | {"module": py_root[0][:-3]}
    except (zipfile.BadZipFile, OSError, tomllib.TOMLDecodeError):
        return None
    return None


def inspect_addon_dir(path: Path) -> dict | None:
    manifest = path / "blender_manifest.toml"
    if manifest.exists():
        try:
            return _normalize_manifest(tomllib.loads(manifest.read_text("utf-8", "replace")))
        except tomllib.TOMLDecodeError:
            return None
    init = path / "__init__.py"
    if init.exists():
        info = _bl_info_from_source(init.read_text("utf-8", "replace"))
        if info:
            return _normalize_bl_info(info) | {"module": path.name}
    return None


def inspect_addon_py(path: Path) -> dict | None:
    try:
        info = _bl_info_from_source(path.read_text("utf-8", "replace"))
    except OSError:
        return None
    return (_normalize_bl_info(info) | {"module": path.stem}) if info else None


def zip_dir(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(path.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f"{path.name}/{f.relative_to(path).as_posix()}")
    return buf.getvalue()
