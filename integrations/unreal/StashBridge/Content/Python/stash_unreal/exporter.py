"""Unreal -> Stash: export selected Content Browser assets and upload them as a bundle.

Meshes and animations go out as FBX (and glTF/GLB when the GLTFExporter plugin is on),
textures as PNG, materials as the textures they reference. The server matches every entry to
the .uasset it already indexed from the same project (by /Game path), so the scanned package
gains a real preview, conversions and Blender-ready files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile

import unreal

from . import client, notify

CLASS_KIND = {
    "StaticMesh": "model",
    "SkeletalMesh": "skeletal_model",
    "AnimSequence": "animation",
    "AnimMontage": "animation",
    "Material": "material",
    "MaterialInstanceConstant": "material",
    "Texture2D": "texture",
    "TextureCube": "hdri",
}

CHANNEL_HINTS = [
    ("normal", ("normal", "nrm", "_n", "nor")),
    ("orm", ("orm", "arm", "rma", "mra", "occlusionroughnessmetal")),
    ("roughness", ("rough",)),
    ("metallic", ("metal",)),
    ("ao", ("ao", "occlusion")),
    ("emissive", ("emiss", "emit", "glow")),
    ("opacity", ("opacity", "alpha", "mask")),
    ("height", ("height", "disp", "bump")),
    ("base_color", ("base", "albedo", "diffuse", "color", "colour", "_d", "_bc", "col")),
]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def guess_channel(*names: str) -> str:
    for n in names:
        low = (n or "").lower()
        for ch, hints in CHANNEL_HINTS:
            if any(h in low for h in hints):
                return ch
    return "base_color"


def _run_export(obj, filename: str, exporter=None, options=None) -> bool:
    task = unreal.AssetExportTask()
    task.set_editor_property("object", obj)
    task.set_editor_property("filename", filename)
    task.set_editor_property("automated", True)
    task.set_editor_property("prompt", False)
    task.set_editor_property("replace_identical", True)
    if exporter is not None:
        task.set_editor_property("exporter", exporter)
    if options is not None:
        task.set_editor_property("options", options)
    ok = unreal.Exporter.run_asset_export_task(task)
    return bool(ok) and os.path.exists(filename)


def _fbx_options():
    opts = unreal.FbxExportOption()
    for prop, value in (("collision", False), ("level_of_detail", False), ("vertex_color", True), ("export_morph_targets", True)):
        try:
            opts.set_editor_property(prop, value)
        except Exception:
            pass
    return opts


def _export_glb(obj, filename: str) -> bool:
    exporter = getattr(unreal, "GLTFExporter", None)
    if exporter is None:
        return False
    try:
        res = exporter.export_to_gltf(obj, filename, unreal.GLTFExportOptions(), set())
        ok = res[0] if isinstance(res, tuple) else res
        return bool(ok) and os.path.exists(filename)
    except Exception as e:
        notify(f"glTF export failed for {obj.get_name()}: {e}")
        return False


def _material_textures(material) -> dict:
    """{channel: Texture} for a Material or MaterialInstance."""
    lib = unreal.MaterialEditingLibrary
    out = {}
    try:
        if isinstance(material, unreal.MaterialInstance):
            for pname in lib.get_texture_parameter_names(material):
                tex = lib.get_material_instance_texture_parameter_value(material, pname)
                if tex:
                    out.setdefault(guess_channel(str(pname), tex.get_name()), tex)
        base = material.get_base_material() if hasattr(material, "get_base_material") else material
        if isinstance(base, unreal.Material):
            for tex in lib.get_used_textures(base):
                if tex and not tex.get_path_name().startswith("/Engine/"):
                    out.setdefault(guess_channel(tex.get_name()), tex)
    except Exception as e:
        notify(f"could not read textures of {material.get_name()}: {e}")
    return out


def _mesh_materials(mesh):
    mats = []
    try:
        if isinstance(mesh, unreal.StaticMesh):
            for sm in mesh.get_editor_property("static_materials"):
                mats.append(sm.get_editor_property("material_interface"))
        elif isinstance(mesh, unreal.SkeletalMesh):
            for sm in mesh.get_editor_property("materials"):
                mats.append(sm.get_editor_property("material_interface"))
    except Exception:
        pass
    return [m for m in mats if m is not None]


def _export_textures(textures: dict, folder: str, rel_prefix: str) -> dict:
    files = {}
    os.makedirs(folder, exist_ok=True)
    for ch, tex in textures.items():
        fn = os.path.join(folder, f"{_safe(tex.get_name())}.png")
        if os.path.exists(fn) or _run_export(tex, fn):
            files[ch] = f"{rel_prefix}/{os.path.basename(fn)}"
    return files


def _export_one(obj, root: str) -> dict | None:
    cls = obj.get_class().get_name()
    kind = CLASS_KIND.get(cls)
    name = obj.get_name()
    pkg = obj.get_path_name().split(".")[0]
    entry = {"name": name, "kind": kind or "unreal_asset", "ue_package": pkg, "ue_class": cls, "files": {}, "stats": {}}
    sub = _safe(name)
    d = os.path.join(root, sub)
    os.makedirs(d, exist_ok=True)

    if cls in ("StaticMesh", "SkeletalMesh", "AnimSequence"):
        exporter = {
            "StaticMesh": getattr(unreal, "StaticMeshExporterFBX", None),
            "SkeletalMesh": getattr(unreal, "SkeletalMeshExporterFBX", None),
            "AnimSequence": getattr(unreal, "AnimSequenceExporterFBX", None),
        }[cls]
        fbx = os.path.join(d, f"{sub}.fbx")
        if _run_export(obj, fbx, exporter() if exporter else None, _fbx_options()):
            entry["files"]["mesh"] = f"{sub}/{sub}.fbx"
        if cls != "AnimSequence":
            glb = os.path.join(d, f"{sub}.glb")
            if _export_glb(obj, glb):
                entry["files"]["glb"] = f"{sub}/{sub}.glb"
            textures = {}
            for m in _mesh_materials(obj):
                for ch, tex in _material_textures(m).items():
                    textures.setdefault(ch, tex)
            entry["files"]["textures"] = _export_textures(textures, os.path.join(d, "Textures"), f"{sub}/Textures")
            if cls == "StaticMesh":
                try:
                    entry["stats"]["triangles"] = int(obj.get_num_triangles(0))
                except Exception:
                    pass
    elif cls in ("Material", "MaterialInstanceConstant"):
        entry["files"]["textures"] = _export_textures(_material_textures(obj), os.path.join(d, "Textures"), f"{sub}/Textures")
    elif cls in ("Texture2D", "TextureCube"):
        ext = ".hdr" if cls == "TextureCube" else ".png"
        fn = os.path.join(d, f"{sub}{ext}")
        if _run_export(obj, fn):
            entry["files"]["textures"] = {guess_channel(name): f"{sub}/{sub}{ext}"}
            entry["files"]["extra"] = [f"{sub}/{sub}{ext}"]
    else:
        notify(f"{name}: {cls} is indexed by Stash but has no exportable payload")
    return entry


def push_selected():
    assets = unreal.EditorUtilityLibrary.get_selected_assets()
    if not assets:
        unreal.EditorDialog.show_message("Stash", "Select assets in the Content Browser first.", unreal.AppMsgType.OK)
        return None
    root = tempfile.mkdtemp(prefix="stash_ue_")
    entries = []
    try:
        with unreal.ScopedSlowTask(len(assets) + 1, "Pushing to Stash") as task:
            task.make_dialog(True)
            for obj in assets:
                if task.should_cancel():
                    break
                task.enter_progress_frame(1, f"Exporting {obj.get_name()}")
                try:
                    e = _export_one(obj, root)
                    if e:
                        entries.append(e)
                except Exception as ex:
                    notify(f"export of {obj.get_name()} failed: {ex}", error=True)
            task.enter_progress_frame(1, "Uploading")
            manifest = {
                "stash_bundle": 1,
                "origin": "unreal",
                "project": unreal.SystemLibrary.get_game_name(),
                "engine_version": ".".join(unreal.SystemLibrary.get_engine_version().split(".")[:2]),
                "assets": entries,
            }
            with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=1)
            zpath = os.path.join(tempfile.gettempdir(), f"stash_bundle_{os.getpid()}.zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                for dirpath, _, files in os.walk(root):
                    for fn in files:
                        full = os.path.join(dirpath, fn)
                        z.write(full, os.path.relpath(full, root).replace("\\", "/"))
            res = client().upload_bundle(zpath)
            os.remove(zpath)
        matched = sum(1 for a in res.get("assets", []) if a.get("matched"))
        notify(f"pushed {len(entries)} asset(s) ({matched} matched to scanned packages)")
        unreal.EditorDialog.show_message("Stash", f"Pushed {len(entries)} asset(s) to Stash.\n{matched} matched to already-indexed packages.", unreal.AppMsgType.OK)
        return res
    except Exception as e:
        notify(f"push failed: {e}", error=True)
        unreal.EditorDialog.show_message("Stash", f"Push failed:\n{e}", unreal.AppMsgType.OK)
        return None
    finally:
        shutil.rmtree(root, ignore_errors=True)
