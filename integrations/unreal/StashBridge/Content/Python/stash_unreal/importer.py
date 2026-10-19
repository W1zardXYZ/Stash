"""Stash -> Unreal: download assets in the best format for this engine and import them.

* Assets that came from an Unreal project of the same or an older engine version are pulled
  as native .uasset packages (with their dependencies) and dropped into Content/.
* Everything else is pulled as a "Stash Unreal package": FBX (cm, no leaf bones) + textures
  + stash_manifest.json. Textures are imported with the right compression/sRGB settings and a
  material instance of M_StashMaster is created per material and assigned to the mesh.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile

import unreal

from . import cache_dir, client, load_config, notify
from .client import StashError

MASTER_PATH = "/Game/Stash/_Shared"
MASTER_NAME = "M_StashMaster"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "Asset"


def _engine_version() -> tuple[int, int]:
    v = unreal.SystemLibrary.get_engine_version().split(".")
    return int(v[0]), int(v[1])


def _choose_target(asset: dict) -> str | None:
    targets = asset.get("targets", [])
    meta = asset.get("meta") or {}
    if "unreal_native" in targets:
        ver = meta.get("ue_engine_version") or ""
        try:
            major, minor = (int(x) for x in ver.split(".")[:2])
            if (major, minor) <= _engine_version():
                return "unreal_native"
        except ValueError:
            pass  # unknown version - prefer a converted package when we have one
        if "unreal" not in targets:
            return "unreal_native"
    for t in ("unreal", "fbx", "glb", "original"):
        if t in targets:
            return t
    return None


# ----------------------------------------------------------------------------- low level import

def _import_files(files: list[str], dest: str, options=None) -> list[str]:
    tasks = []
    for f in files:
        t = unreal.AssetImportTask()
        t.set_editor_property("filename", f)
        t.set_editor_property("destination_path", dest)
        t.set_editor_property("automated", True)
        t.set_editor_property("replace_existing", True)
        t.set_editor_property("save", True)
        if options is not None:
            t.set_editor_property("options", options)
        tasks.append(t)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(tasks)
    paths = []
    for t in tasks:
        paths.extend(str(p) for p in t.get_editor_property("imported_object_paths"))
    return paths


def _load(path: str):
    return unreal.EditorAssetLibrary.load_asset(path.split(".")[0] if path.count(".") > 1 else path)


def _configure_texture(tex, channel: str) -> None:
    try:
        if channel == "normal":
            tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
            tex.set_editor_property("srgb", False)
        elif channel in ("base_color", "emissive"):
            tex.set_editor_property("srgb", True)
        else:
            tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
            tex.set_editor_property("srgb", False)
        unreal.EditorAssetLibrary.save_loaded_asset(tex)
    except Exception as e:
        notify(f"texture settings failed for {tex.get_name()}: {e}")


# ----------------------------------------------------------------------------- master material

def ensure_master_material():
    """Create /Game/Stash/_Shared/M_StashMaster once (BaseColor/Normal/ORM or separate maps)."""
    full = f"{MASTER_PATH}/{MASTER_NAME}"
    if unreal.EditorAssetLibrary.does_asset_exist(full):
        return unreal.EditorAssetLibrary.load_asset(full)
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = tools.create_asset(MASTER_NAME, MASTER_PATH, unreal.Material, unreal.MaterialFactoryNew())
    lib = unreal.MaterialEditingLibrary
    white = unreal.EditorAssetLibrary.load_asset("/Engine/EngineResources/WhiteSquareTexture")
    flat = unreal.EditorAssetLibrary.load_asset("/Engine/EngineMaterials/DefaultNormal")

    def tex_param(name, x, y, default, sampler=None):
        e = lib.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, x, y)
        e.set_editor_property("parameter_name", name)
        if default:
            e.set_editor_property("texture", default)
        if sampler is not None:
            e.set_editor_property("sampler_type", sampler)
        return e

    def scalar(name, value, x, y):
        e = lib.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, x, y)
        e.set_editor_property("parameter_name", name)
        e.set_editor_property("default_value", value)
        return e

    def mul(a, a_out, b, b_out, x, y):
        m = lib.create_material_expression(mat, unreal.MaterialExpressionMultiply, x, y)
        lib.connect_material_expressions(a, a_out, m, "A")
        lib.connect_material_expressions(b, b_out, m, "B")
        return m

    def switch(name, true_expr, true_out, false_expr, false_out, x, y):
        s = lib.create_material_expression(mat, unreal.MaterialExpressionStaticSwitchParameter, x, y)
        s.set_editor_property("parameter_name", name)
        s.set_editor_property("default_value", False)
        lib.connect_material_expressions(true_expr, true_out, s, "True")
        lib.connect_material_expressions(false_expr, false_out, s, "False")
        return s

    base = tex_param("BaseColor", -900, -300, white)
    tint = lib.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -900, -100)
    tint.set_editor_property("parameter_name", "BaseColorTint")
    tint.set_editor_property("default_value", unreal.LinearColor(1, 1, 1, 1))
    lib.connect_material_property(mul(base, "RGB", tint, "", -600, -250), "", unreal.MaterialProperty.MP_BASE_COLOR)

    normal = tex_param("Normal", -900, 100, flat, unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    lib.connect_material_property(normal, "RGB", unreal.MaterialProperty.MP_NORMAL)

    orm = tex_param("ORM", -1300, 400, white, unreal.MaterialSamplerType.SAMPLERTYPE_MASKS)
    rough_map = tex_param("RoughnessMap", -1300, 700, white, unreal.MaterialSamplerType.SAMPLERTYPE_MASKS)
    metal_map = tex_param("MetallicMap", -1300, 1000, white, unreal.MaterialSamplerType.SAMPLERTYPE_MASKS)
    ao_map = tex_param("AOMap", -1300, 1300, white, unreal.MaterialSamplerType.SAMPLERTYPE_MASKS)
    rough = mul(switch("UseORM", orm, "G", rough_map, "R", -900, 600), "", scalar("RoughnessScale", 1.0, -900, 750), "", -600, 650)
    metal = mul(switch("UseORM", orm, "B", metal_map, "R", -900, 900), "", scalar("MetallicScale", 0.0, -900, 1050), "", -600, 950)
    ao = switch("UseORM", orm, "R", ao_map, "R", -900, 1250)
    lib.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)
    lib.connect_material_property(metal, "", unreal.MaterialProperty.MP_METALLIC)
    lib.connect_material_property(ao, "", unreal.MaterialProperty.MP_AMBIENT_OCCLUSION)

    emissive = tex_param("Emissive", -900, 1500, None)
    emissive.set_editor_property("texture", unreal.EditorAssetLibrary.load_asset("/Engine/EngineResources/Black"))
    lib.connect_material_property(mul(emissive, "RGB", scalar("EmissiveStrength", 0.0, -900, 1700), "", -600, 1550), "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)

    lib.recompile_material(mat)
    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    return mat


def _make_instance(name: str, folder: str, maps: dict) -> object:
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    lib = unreal.MaterialEditingLibrary
    mi_name = f"MI_{_safe(name)}"
    full = f"{folder}/{mi_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(full):
        mi = unreal.EditorAssetLibrary.load_asset(full)
    else:
        mi = tools.create_asset(mi_name, folder, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
    lib.set_material_instance_parent(mi, ensure_master_material())
    params = {"base_color": "BaseColor", "normal": "Normal", "orm": "ORM", "roughness": "RoughnessMap", "metallic": "MetallicMap", "ao": "AOMap", "emissive": "Emissive"}
    for ch, tex in maps.items():
        p = params.get(ch)
        if p:
            lib.set_material_instance_texture_parameter_value(mi, p, tex)
    if "orm" in maps:
        lib.set_material_instance_static_switch_parameter_value(mi, "UseORM", True)
    if "metallic" in maps or "orm" in maps:
        lib.set_material_instance_scalar_parameter_value(mi, "MetallicScale", 1.0)
    if "emissive" in maps:
        lib.set_material_instance_scalar_parameter_value(mi, "EmissiveStrength", 1.0)
    unreal.EditorAssetLibrary.save_loaded_asset(mi)
    return mi


def _assign_materials(mesh, instances: dict) -> None:
    if not instances:
        return
    only = next(iter(instances.values())) if len(instances) == 1 else None
    if isinstance(mesh, unreal.StaticMesh):
        slots = list(mesh.get_editor_property("static_materials"))
        for i, sm in enumerate(slots):
            slot = str(sm.get_editor_property("material_slot_name"))
            mi = instances.get(slot) or next((v for k, v in instances.items() if k.lower() in slot.lower() or slot.lower() in k.lower()), None) or only
            if mi:
                mesh.set_material(i, mi)
    elif isinstance(mesh, unreal.SkeletalMesh):
        mats = list(mesh.get_editor_property("materials"))
        for sm in mats:
            slot = str(sm.get_editor_property("material_slot_name"))
            mi = instances.get(slot) or next((v for k, v in instances.items() if k.lower() in slot.lower() or slot.lower() in k.lower()), None) or only
            if mi:
                sm.set_editor_property("material_interface", mi)
        mesh.set_editor_property("materials", mats)
    unreal.EditorAssetLibrary.save_loaded_asset(mesh)


# ----------------------------------------------------------------------------- package import

def import_package(zip_path: str, dest_root: str | None = None) -> list[str]:
    """Import a Stash 'unreal' package zip (FBX + textures + stash_manifest.json)."""
    dest_root = dest_root or load_config().get("import_root", "/Game/Stash")
    out = os.path.splitext(zip_path)[0]
    shutil.rmtree(out, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out)
    manifest_path = next((os.path.join(r, "stash_manifest.json") for r, _, fs in os.walk(out) if "stash_manifest.json" in fs), None)
    if not manifest_path:
        raise StashError("not a Stash Unreal package (stash_manifest.json missing)")
    root = os.path.dirname(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        m = json.load(f)
    folder = f"{dest_root}/{_safe(m['name'])}"
    imported: list[str] = []

    instances = {}
    for mat in m.get("materials", []):
        maps = {}
        for ch, rel in mat.get("maps", {}).items():
            paths = _import_files([os.path.join(root, rel)], f"{folder}/Textures")
            for p in paths:
                tex = _load(p)
                if isinstance(tex, unreal.Texture):
                    _configure_texture(tex, ch)
                    maps[ch] = tex
            imported += paths
        if maps:
            instances[mat["name"]] = _make_instance(mat["name"], folder, maps)

    mesh_rel = (m.get("files") or {}).get("mesh")
    if mesh_rel:
        ui = unreal.FbxImportUI()
        skeletal = bool(m.get("skeletal"))
        ui.set_editor_property("import_mesh", True)
        ui.set_editor_property("import_as_skeletal", skeletal)
        ui.set_editor_property("import_animations", skeletal)
        ui.set_editor_property("import_materials", not instances)
        ui.set_editor_property("import_textures", False)
        ui.set_editor_property("automated_import_should_detect_type", False)
        ui.set_editor_property("mesh_type_to_import", unreal.FBXImportType.FBXIT_SKELETAL_MESH if skeletal else unreal.FBXImportType.FBXIT_STATIC_MESH)
        try:
            ui.static_mesh_import_data.set_editor_property("combine_meshes", True)
            ui.skeletal_mesh_import_data.set_editor_property("import_morph_targets", True)
        except Exception:
            pass
        paths = _import_files([os.path.join(root, mesh_rel)], folder, ui)
        imported += paths
        for p in paths:
            asset = _load(p)
            if isinstance(asset, (unreal.StaticMesh, unreal.SkeletalMesh)):
                _assign_materials(asset, instances)
    elif instances:
        imported += [mi.get_path_name() for mi in instances.values()]
    return imported


def import_native(zip_path: str) -> list[str]:
    """Unzip a Content/... package set into this project (existing files are never overwritten)."""
    content = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
    plugins = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_plugins_dir())
    project = os.path.dirname(os.path.normpath(content))
    written, skipped = [], []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if info.is_dir() or not (name.startswith("Content/") or name.startswith("Plugins/")):
                continue
            target = os.path.normpath(os.path.join(project, name))
            if not (target.startswith(os.path.normpath(content)) or target.startswith(os.path.normpath(plugins))):
                continue
            if os.path.exists(target):
                skipped.append(name)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(name)
    unreal.AssetRegistryHelpers.get_asset_registry().scan_paths_synchronous(["/Game"], True)
    if skipped:
        notify(f"{len(skipped)} package(s) already existed and were left untouched")
    return written


def _import_by_extension(path: str, asset: dict) -> list[str]:
    ext = os.path.splitext(path)[1].lower()
    folder = f"{load_config().get('import_root', '/Game/Stash')}/{_safe(asset['name'])}"
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
        if any(n.endswith("stash_manifest.json") for n in names):
            return import_package(path)
        if any(n.startswith("Content/") for n in names):
            return import_native(path)
        out = os.path.splitext(path)[0]
        with zipfile.ZipFile(path) as z:
            z.extractall(out)
        files = [os.path.join(r, f) for r, _, fs in os.walk(out) for f in fs if f.lower().endswith((".fbx", ".glb", ".gltf", ".obj"))]
        return _import_files(files[:1], folder)
    return _import_files([path], folder)


def import_asset(id_or_uid) -> list[str]:
    c = client()
    if isinstance(id_or_uid, str) and not id_or_uid.isdigit():
        found = c.find_by_uid(id_or_uid)
        if not found:
            raise StashError(f"no asset {id_or_uid}")
        id_or_uid = found["id"]
    asset = c.asset(int(id_or_uid))
    target = _choose_target(asset)
    if not target:
        raise StashError(f"{asset['name']}: nothing importable into Unreal")
    with unreal.ScopedSlowTask(2, f"Stash: {asset['name']}") as task:
        task.make_dialog(False)
        task.enter_progress_frame(1, f"Downloading ({target})")
        path = c.export(asset["id"], target, os.path.join(cache_dir(), asset["uid"]))
        task.enter_progress_frame(1, "Importing")
        result = import_native(path) if target == "unreal_native" else _import_by_extension(path, asset)
    notify(f"imported {asset['name']} ({target}): {len(result)} item(s)")
    return result


def receive() -> int:
    """Import everything sent to Unreal from the Stash web UI."""
    c = client()
    try:
        pending = c.get("/api/deliveries", app="unreal")
    except StashError as e:
        notify(str(e), error=True)
        return 0
    n = 0
    for d in pending:
        try:
            import_asset(d["asset_id"])
            c.post(f"/api/deliveries/{d['id']}/ack", {"status": "delivered", "client": f"Unreal {unreal.SystemLibrary.get_engine_version()}"})
            n += 1
        except Exception as e:
            notify(f"delivery {d['id']} failed: {e}", error=True)
            c.post(f"/api/deliveries/{d['id']}/ack", {"status": "failed", "message": str(e)})
    if n:
        notify(f"received {n} asset(s)")
    return n


def import_file_dialog() -> list[str]:
    """Pick a downloaded Stash package with a file dialog (tkinter ships with UE's Python on Windows)."""
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        path = filedialog.askopenfilename(title="Stash package", filetypes=[("Stash package", "*.zip"), ("3D files", "*.fbx *.glb *.gltf *.obj")])
        root.destroy()
    except Exception as e:
        unreal.EditorDialog.show_message("Stash", f"No file dialog available ({e}).\nUse: stash_unreal.importer.import_package(r'path\\to\\package.zip')", unreal.AppMsgType.OK)
        return []
    if not path:
        return []
    return _import_by_extension(path, {"name": os.path.splitext(os.path.basename(path))[0]})
