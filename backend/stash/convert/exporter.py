"""Turn an asset into a downloadable file for a target app/format.

Targets
  original        the untouched source file(s) (zip when there are several)
  glb gltf fbx fbx_unity obj usd usdz stl ply abc blend   via headless Blender
  unreal          zip: FBX (cm, no leaf bones) + textures + stash_manifest.json for the
                  Stash Unreal importer (builds material instances automatically)
  unreal_native   zip of .uasset packages incl. their dependency closure, laid out as
                  Content/... so it can be copied straight into another project
  png jpg tga tif texture conversion
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from .. import blender
from ..config import settings
from ..models import Asset, AssetFile, AssetLink
from ..storage import add_stored_file, resolve

IMAGE_TARGETS = {"png", "jpg", "tga", "tif"}
BLENDER_TARGETS = {"glb", "gltf", "fbx", "fbx_unreal", "fbx_unity", "obj", "usd", "usdz", "stl", "ply", "abc", "blend"}
EXT_FOR = {"fbx_unreal": "fbx", "fbx_unity": "fbx", "gltf": "gltf"}
MULTI_FILE = {"gltf", "obj", "fbx_unreal", "fbx_unity"}


class ExportError(RuntimeError):
    pass


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "asset"


def files_by_role(asset: Asset, role: str) -> list[AssetFile]:
    return [f for f in asset.files if f.role == role]


def first(asset: Asset, role: str) -> AssetFile | None:
    return next(iter(files_by_role(asset, role)), None)


def linked(db: Session, asset: Asset, relation: str, reverse: bool = False) -> list[Asset]:
    if reverse:
        ids = [l.from_id for l in db.query(AssetLink).filter(AssetLink.to_id == asset.id, AssetLink.relation == relation)]
    else:
        ids = [l.to_id for l in db.query(AssetLink).filter(AssetLink.from_id == asset.id, AssetLink.relation == relation)]
    return [db.get(Asset, i) for i in ids if db.get(Asset, i) is not None]


def pbr_maps(asset: Asset) -> dict[str, str]:
    maps = {}
    for f in files_by_role(asset, "texture"):
        if f.label and f.label not in maps:
            p = resolve(f)
            if p.exists():
                maps[f.label] = str(p)
    if not maps and asset.kind in ("texture",):
        src = first(asset, "source")
        if src:
            maps["base_color"] = str(resolve(src))
    return maps


def available_targets(asset: Asset) -> list[str]:
    k = asset.kind
    has_blend = first(asset, "blend") is not None
    src = first(asset, "source")
    ue = (asset.meta or {}).get("ue_class")
    out = ["original"] if src else []
    if ue:
        out.append("unreal_native")
        if first(asset, "ue_export") is None:
            return out
    if k in ("texture", "hdri"):
        return out + ["png", "jpg", "tga", "tif"]
    if k == "addon" or k == "audio":
        return out
    if not blender.available():
        return out + (["glb"] if first(asset, "preview") else [])
    if k == "node_group":
        return out + (["blend"] if has_blend else [])
    if k == "material":
        if (asset.meta or {}).get("substance"):
            return out
        return out + ["blend", "glb", "unreal"]
    if k in ("model", "skeletal_model", "animation", "scene"):
        targets = ["blend", "glb", "gltf", "fbx", "unreal", "fbx_unity", "obj", "usdz", "usd", "stl", "ply"]
        if asset.is_animated:
            targets.append("abc")
        return out + targets
    return out


def _zip_dir(d: Path, dest: Path, root_name: str) -> Path:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(d.rglob("*")):
            if f.is_file():
                z.write(f, f"{root_name}/{f.relative_to(d).as_posix()}")
    return dest


def _input_for(db: Session, asset: Asset) -> dict:
    """Figure out how to feed this asset to convert.py."""
    blend = first(asset, "blend")
    if asset.kind == "material" and blend:
        return {"input": None, "append": [f"{resolve(blend)}::materials::{asset.name}"]}
    if asset.kind == "material":
        maps = pbr_maps(asset)
        if not maps:
            raise ExportError("material has no textures to export")
        return {"input": None, "pbr": {**maps, "_name": asset.name}, "material_only": True}
    if asset.kind == "animation" and blend and asset.parent_id:
        # export the rig this action animates (actions travel with their armature)
        targets = linked(db, asset, "animates")
        if targets and first(targets[0], "blend"):
            return {"input": str(resolve(first(targets[0], "blend")))}
    if blend:
        return {"input": str(resolve(blend))}
    ue_exp = first(asset, "ue_export")
    if ue_exp:
        return {"input": str(resolve(ue_exp)), "search_dirs": [str(resolve(ue_exp).parent)]}
    src = first(asset, "source")
    if src is None:
        raise ExportError("asset has no source file")
    p = resolve(src)
    spec: dict = {"input": str(p), "search_dirs": [str(p.parent), str(p.parent.parent)]}
    if asset.kind == "scene" and asset.parent_id is None:
        return spec
    # models: bring along a matched PBR set when the model doesn't reference textures itself
    if not files_by_role(asset, "texture"):
        for m in linked(db, asset, "uses_material"):
            maps = pbr_maps(m)
            if maps:
                spec["pbr"] = {**maps, "_name": m.name}
                break
    return spec


def _texture_channels_for_unreal(db: Session, asset: Asset) -> list[dict]:
    """Materials + their texture files for the Unreal importer manifest."""
    mats = []
    if asset.kind == "material":
        maps = pbr_maps(asset)
        if maps:
            mats.append({"name": asset.name, "maps": maps})
    for m in linked(db, asset, "uses_material"):
        maps = pbr_maps(m)
        if not maps:
            for t in linked(db, m, "uses_texture"):
                src = first(t, "source")
                ch = (t.meta or {}).get("channel") or "base_color"
                if src and resolve(src).exists():
                    maps.setdefault(ch, str(resolve(src)))
        if maps:
            mats.append({"name": m.name, "maps": maps})
    if not mats:
        maps = {}
        for f in files_by_role(asset, "texture"):
            p = resolve(f)
            if p.exists():
                maps.setdefault(f.label or f.name, str(p))
        if maps:
            mats.append({"name": asset.name, "maps": maps})
    return mats


def export_asset(db: Session, asset: Asset, target: str, should_cancel=None) -> AssetFile:
    target = target.lower()
    work = Path(tempfile.mkdtemp(prefix=f"export_{asset.id}_", dir=settings.work_dir))
    name = _safe(asset.name)
    try:
        if target == "original":
            out = _export_original(asset, work, name)
        elif target == "unreal_native":
            out = _export_unreal_native(db, asset, work, name)
        elif target in IMAGE_TARGETS:
            out = _export_image(asset, work, name, target)
        elif target == "unreal":
            out = _export_unreal_package(db, asset, work, name, should_cancel)
        elif target in BLENDER_TARGETS:
            out = _export_blender(db, asset, work, name, target, should_cancel)
        else:
            raise ExportError(f"unknown target {target}")
        af = add_stored_file(asset, out, "export", name=out.name, label=target, meta={"fingerprint": asset.fingerprint})
        db.flush()
        return af
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _export_original(asset: Asset, work: Path, name: str) -> Path:
    files = [f for f in asset.files if f.role in ("source", "sidecar", "texture")]
    if not files:
        raise ExportError("no source files")
    if len(files) == 1:
        p = resolve(files[0])
        dest = work / p.name
        shutil.copyfile(p, dest)
        return dest
    src = resolve(files[0])
    base = src.parent
    dest = work / f"{name}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        seen = set()
        for f in files:
            p = resolve(f)
            if not p.exists():
                continue
            try:
                arc = p.relative_to(base).as_posix()
            except ValueError:
                arc = f"textures/{p.name}"
            if arc in seen:
                continue
            seen.add(arc)
            z.write(p, f"{name}/{arc}")
    return dest


def _export_image(asset: Asset, work: Path, name: str, target: str) -> Path:
    from ..images import convert_image

    src = first(asset, "source")
    if src is None:
        raise ExportError("no image")
    dest = work / f"{name}.{target}"
    try:
        return convert_image(resolve(src), dest)
    except Exception as e:
        if not blender.available():
            raise ExportError(f"cannot convert image: {e}")
        blender.run_script("preview_model.py", ["--image", str(resolve(src)), "--thumb", str(dest.with_suffix(".png")), "--size", "16384"])
        return dest.with_suffix(".png")


def _export_blender(db, asset, work: Path, name: str, target: str, should_cancel=None) -> Path:
    if not blender.available():
        raise ExportError("Blender is not available on the server")
    spec = _input_for(db, asset)
    blend = first(asset, "blend")
    if target == "blend" and blend and asset.kind != "material":
        dest = work / f"{name}.blend"
        shutil.copyfile(resolve(blend), dest)
        return dest
    if target == "glb" and first(asset, "preview") and not spec.get("pbr"):
        p = resolve(first(asset, "preview"))
        if p.suffix.lower() == ".glb" and p.exists():
            dest = work / f"{name}.glb"
            shutil.copyfile(p, dest)
            return dest
    ext = EXT_FOR.get(target, target)
    out_dir = work / name
    out_dir.mkdir()
    out = out_dir / f"{name}.{ext}"
    objects = None
    if spec.get("input") and spec["input"].lower().endswith(".blend") and asset.sub_key.startswith("object:") and not blend:
        objects = [asset.sub_key.split(":", 1)[1]]
    blender.convert(
        Path(spec["input"]) if spec.get("input") else None,
        out,
        target,
        objects=objects,
        search_dirs=spec.get("search_dirs"),
        pbr=spec.get("pbr"),
        material_only=spec.get("material_only", False),
        append=spec.get("append"),
        should_cancel=should_cancel,
    )
    if not out.exists():
        raise ExportError("conversion produced no file")
    produced = [p for p in out_dir.rglob("*") if p.is_file()]
    if target in MULTI_FILE and len(produced) > 1:
        return _zip_dir(out_dir, work / f"{name}_{target}.zip", name)
    return out


def _export_unreal_package(db, asset, work: Path, name: str, should_cancel=None) -> Path:
    pkg = work / "_package" / name
    pkg.mkdir(parents=True)
    tex_dir = pkg / "Textures"
    manifest: dict = {"stash_version": 1, "name": asset.name, "kind": asset.kind, "uid": asset.uid, "materials": [], "files": {}}
    if asset.kind != "material":
        fbx = _export_blender(db, asset, work, name, "fbx_unreal", should_cancel)
        if fbx.suffix == ".zip":
            with zipfile.ZipFile(fbx) as z:
                z.extractall(work / "_fbx")
            fbx = next((work / "_fbx").rglob("*.fbx"))
        shutil.copyfile(fbx, pkg / f"{name}.fbx")
        manifest["files"]["mesh"] = f"{name}.fbx"
        manifest["skeletal"] = asset.kind in ("skeletal_model", "animation") or asset.is_rigged
        manifest["animations"] = [a.get("name", "").split("|")[-1] for a in (asset.meta or {}).get("inspect", {}).get("animations", [])] or (asset.meta or {}).get("actions", [])
    for mat in _texture_channels_for_unreal(db, asset):
        entry = {"name": mat["name"], "maps": {}}
        for ch, path in mat["maps"].items():
            p = Path(path)
            if not p.exists():
                continue
            tex_dir.mkdir(exist_ok=True)
            dest = tex_dir / f"T_{_safe(mat['name'])}_{ch}{p.suffix.lower()}"
            if p.suffix.lower() in (".png", ".tga", ".jpg", ".jpeg", ".exr", ".tif", ".tiff", ".bmp", ".psd"):
                shutil.copyfile(p, dest)
            else:
                from ..images import convert_image

                dest = convert_image(p, dest.with_suffix(".png"))
            entry["maps"][ch] = f"Textures/{dest.name}"
        manifest["materials"].append(entry)
    (pkg / "stash_manifest.json").write_text(json.dumps(manifest, indent=2))
    return _zip_dir(pkg, work / f"{name}_unreal.zip", name)


def unreal_closure(db: Session, asset: Asset) -> list[Asset]:
    seen = {asset.id: asset}
    stack = [asset]
    while stack:
        a = stack.pop()
        for l in db.query(AssetLink).filter(AssetLink.from_id == a.id, AssetLink.relation.in_(("uses_material", "uses_texture", "uses_skeleton", "depends_on"))):
            b = db.get(Asset, l.to_id)
            if b is not None and b.id not in seen and (b.meta or {}).get("ue_package"):
                seen[b.id] = b
                stack.append(b)
    return list(seen.values())


def _export_unreal_native(db, asset, work: Path, name: str) -> Path:
    if not (asset.meta or {}).get("ue_package"):
        raise ExportError("not an Unreal asset")
    closure = unreal_closure(db, asset)
    dest = work / f"{name}_ue_{(asset.meta or {}).get('ue_engine_version') or 'native'}.zip"
    versions = sorted({(a.meta or {}).get("ue_engine_version") for a in closure if (a.meta or {}).get("ue_engine_version")})
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for a in closure:
            pkg = a.meta["ue_package"]
            mount, _, rest = pkg.lstrip("/").partition("/")
            base = "Content" if mount == "Game" else f"Plugins/{mount}/Content"
            for f in a.files:
                if f.role in ("source", "sidecar"):
                    p = resolve(f)
                    if p.exists():
                        z.write(p, f"{base}/{Path(rest).parent.as_posix()}/{p.name}".replace("/./", "/"))
        z.writestr(
            "README_STASH.txt",
            f"Copy the Content folder into your Unreal project (merge).\n"
            f"Root asset: {asset.meta['ue_package']}\nPackages: {len(closure)}\n"
            f"Saved with engine version(s): {', '.join(versions) or 'unknown'} - open with that version or newer.\n",
        )
    return dest
