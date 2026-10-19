"""Convert any Blender-importable 3D file into another format.

  blender -b [--factory-startup] [input.blend] --python convert.py -- --input FILE --output FILE --format FMT [options]

Formats: glb gltf fbx fbx_unreal fbx_unity obj stl ply usd usdz abc blend
Options:
  --objects A,B           (when input is .blend) only keep these objects (+children/armatures)
  --collection NAME       (when input is .blend) only keep objects of that collection
  --search-dir DIR        repeatable; folders used to relink missing textures
  --pbr JSON              {"base_color": path, "normal": path, ..., "_name": name}, or a list of those:
                          build PBR materials for slots without image textures (see apply_pbr_sets)
  --material-only         with --pbr: output a preview sphere using that material
  --scale F               uniform scale applied before export
  --append-blend FILE:Type:Name   append a datablock (used for Blender-native material/action assets)
"""

import json
import os
import re
import sys
import traceback

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stash_common as sc  # noqa: E402


def parse_args():
    a = sc.script_args()
    o = {"search_dirs": [], "objects": None, "collection": None, "pbr": None, "material_only": False, "scale": 1.0, "append": []}
    i = 0
    while i < len(a):
        k = a[i]
        v = a[i + 1] if i + 1 < len(a) else None
        if k == "--input":
            o["input"] = v; i += 1
        elif k == "--output":
            o["output"] = v; i += 1
        elif k == "--format":
            o["format"] = v; i += 1
        elif k == "--objects":
            o["objects"] = [x for x in v.split(",") if x]; i += 1
        elif k == "--collection":
            o["collection"] = v; i += 1
        elif k == "--search-dir":
            o["search_dirs"].append(v); i += 1
        elif k == "--pbr":
            o["pbr"] = json.loads(v); i += 1
        elif k == "--scale":
            o["scale"] = float(v); i += 1
        elif k == "--append-blend":
            o["append"].append(v); i += 1
        elif k == "--material-only":
            o["material_only"] = True
        i += 1
    return o


def import_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path, use_anim=True, automatic_bone_orientation=False, ignore_leaf_bones=False)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext in (".gltf", ".glb"):
        # no "Icosphere" bone-shape helper: it would end up in every per-asset .blend and export
        opts = {"disable_bone_shape": True} if "disable_bone_shape" in bpy.ops.import_scene.gltf.get_rna_type().properties else {}
        bpy.ops.import_scene.gltf(filepath=path, **opts)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    elif ext in (".usd", ".usda", ".usdc", ".usdz"):
        bpy.ops.wm.usd_import(filepath=path)
    elif ext == ".abc":
        bpy.ops.wm.alembic_import(filepath=path, as_background_job=False)
    elif ext == ".dae":
        if hasattr(bpy.ops.wm, "collada_import"):
            bpy.ops.wm.collada_import(filepath=path)
        else:
            raise RuntimeError("Collada import was removed in Blender 5.0")
    elif ext in (".x3d", ".wrl"):
        bpy.ops.import_scene.x3d(filepath=path)
    else:
        raise RuntimeError(f"cannot import {ext}")


def keep_only(objects=None, collection=None):
    keep = set()
    if collection:
        coll = bpy.data.collections.get(collection)
        if coll:
            keep.update(coll.all_objects)
    if objects:
        def add(o):
            keep.add(o)
            for c in o.children:
                add(c)
            for m in getattr(o, "modifiers", []):
                if m.type == "ARMATURE" and m.object:
                    keep.add(m.object)
        for name in objects:
            o = bpy.data.objects.get(name)
            if o:
                add(o)
        # meshes deformed by a kept armature come along too
        for o in bpy.data.objects:
            for m in getattr(o, "modifiers", []):
                if m.type == "ARMATURE" and m.object in keep:
                    keep.add(o)
    if not keep:
        return
    for o in list(bpy.data.objects):
        if o not in keep:
            bpy.data.objects.remove(o)


def relink_textures(dirs):
    for d in dirs:
        if d and os.path.isdir(d):
            try:
                bpy.ops.file.find_missing_files(directory=d, find_all=True)
            except Exception as e:
                print("find_missing_files failed:", e)


def apply_pbr_sets(sets):
    """Give mesh slots without image textures a PBR material. With several sets (a mesh textured in
    Substance Painter: SM_Arco_LP_lambert1, SM_Arco_LP_lambert2) each slot gets the set named after it
    ("lambert1"), otherwise the first set."""
    if isinstance(sets, dict):
        sets = [sets]
    built = {}

    def mat(i):
        if i not in built:
            built[i] = build_pbr_material(sets[i].get("_name", "StashMaterial"), sets[i])
        return built[i]

    def for_slot(m):
        slot = re.sub(r"\.\d{3}$", "", m.name).lower() if m is not None else ""
        return next((i for i, s in enumerate(sets) if slot and s.get("_name", "").lower().endswith("_" + slot)), 0)

    for ob in bpy.data.objects:
        if ob.type != "MESH":
            continue
        slots = ob.data.materials
        if len(slots) == 0:
            slots.append(mat(0))
            continue
        for i, m in enumerate(slots):
            if m is None or not any(n.bl_idname == "ShaderNodeTexImage" for n in (m.node_tree.nodes if m.use_nodes else [])):
                slots[i] = mat(for_slot(m))


def build_pbr_material(name, maps):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    x = -700
    y = 400

    def tex(path, non_color):
        nonlocal y
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = bpy.data.images.load(path, check_existing=True)
        if non_color:
            n.image.colorspace_settings.name = "Non-Color"
        n.location = (x, y)
        y -= 300
        return n

    if maps.get("base_color"):
        n = tex(maps["base_color"], False)
        if maps.get("ao"):
            ao = tex(maps["ao"], True)
            mix = nt.nodes.new("ShaderNodeMix")
            mix.data_type = "RGBA"
            mix.blend_type = "MULTIPLY"
            mix.inputs["Factor"].default_value = 1.0
            nt.links.new(n.outputs["Color"], mix.inputs[6])
            nt.links.new(ao.outputs["Color"], mix.inputs[7])
            nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
        else:
            nt.links.new(n.outputs["Color"], bsdf.inputs["Base Color"])
        if maps.get("opacity") is None and n.image and n.image.depth == 32:
            pass
    if maps.get("orm"):
        n = tex(maps["orm"], True)
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        nt.links.new(n.outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
        nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
    for ch, socket in (("roughness", "Roughness"), ("metallic", "Metallic"), ("opacity", "Alpha")):
        if maps.get(ch):
            n = tex(maps[ch], True)
            nt.links.new(n.outputs["Color"], bsdf.inputs[socket])
    if maps.get("glossiness") and not maps.get("roughness"):
        n = tex(maps["glossiness"], True)
        inv = nt.nodes.new("ShaderNodeInvert")
        nt.links.new(n.outputs["Color"], inv.inputs["Color"])
        nt.links.new(inv.outputs["Color"], bsdf.inputs["Roughness"])
    if maps.get("emissive"):
        n = tex(maps["emissive"], False)
        nt.links.new(n.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = 1.0
    if maps.get("normal"):
        n = tex(maps["normal"], True)
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(n.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    if maps.get("height"):
        n = tex(maps["height"], True)
        disp = nt.nodes.new("ShaderNodeDisplacement")
        disp.inputs["Scale"].default_value = 0.05
        nt.links.new(n.outputs["Color"], disp.inputs["Height"])
        out = nt.nodes.get("Material Output")
        nt.links.new(disp.outputs["Displacement"], out.inputs["Displacement"])
    if maps.get("opacity"):
        try:
            mat.surface_render_method = "DITHERED"
        except Exception:
            pass
    return mat


def preview_sphere(mat):
    import bmesh

    me = bpy.data.meshes.new(mat.name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=64, v_segments=32, radius=0.5, calc_uvs=True)
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    me.materials.append(mat)
    ob = bpy.data.objects.new(mat.name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def append_datablock(spec):
    # spec format: /path/file.blend::materials::Name (double colon so Windows paths survive)
    path, kind, name = spec.split("::")
    with bpy.data.libraries.load(path, link=False) as (src, dst):
        setattr(dst, kind, [name] if name in getattr(src, kind) else list(getattr(src, kind))[:1])
    items = getattr(dst, kind)
    return items[0] if items else None


def export(path, fmt):
    # importers name actions "Armature|Walk"; strip that so re-exports don't stack prefixes
    for act in bpy.data.actions:
        if "|" in act.name:
            act.name = act.name.rsplit("|", 1)[-1]
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    for o in bpy.context.view_layer.objects:
        o.select_set(True)
    if fmt in ("glb", "gltf"):
        bpy.ops.export_scene.gltf(
            filepath=path,
            export_format="GLB" if fmt == "glb" else "GLTF_SEPARATE",
            export_apply=True,
            export_animations=True,
            export_animation_mode="ACTIONS",
            export_yup=True,
        )
    elif fmt in ("fbx", "fbx_unreal", "fbx_unity"):
        kw = dict(filepath=path, use_selection=False, add_leaf_bones=False, bake_anim=True, path_mode="COPY", embed_textures=False)
        if fmt == "fbx_unreal":
            kw.update(
                apply_scale_options="FBX_SCALE_UNITS",
                mesh_smooth_type="FACE",
                use_tspace=True,
                armature_nodetype="NULL",
                primary_bone_axis="Y",
                secondary_bone_axis="X",
                bake_anim_use_nla_strips=False,
                bake_anim_use_all_actions=True,
                bake_anim_force_startend_keying=True,
                bake_anim_simplify_factor=0.0,
            )
            # Unreal treats an armature object named "Armature" as an extra root bone
            for o in bpy.data.objects:
                if o.type == "ARMATURE" and o.name == "Armature":
                    o.name = "root"
        elif fmt == "fbx_unity":
            kw.update(apply_scale_options="FBX_SCALE_ALL", apply_unit_scale=True, bake_space_transform=True, axis_forward="-Z", axis_up="Y", mesh_smooth_type="FACE")
        else:
            kw.update(embed_textures=True)
        bpy.ops.export_scene.fbx(**kw)
    elif fmt == "obj":
        bpy.ops.wm.obj_export(filepath=path, export_selected_objects=False, path_mode="COPY", export_materials=True)
    elif fmt == "stl":
        bpy.ops.wm.stl_export(filepath=path, export_selected_objects=False, apply_modifiers=True)
    elif fmt == "ply":
        bpy.ops.wm.ply_export(filepath=path, export_selected_objects=False, apply_modifiers=True)
    elif fmt in ("usd", "usdz"):
        bpy.ops.wm.usd_export(filepath=path, selected_objects_only=False, export_animation=True, export_materials=True)
    elif fmt == "abc":
        bpy.ops.wm.alembic_export(filepath=path, selected=False, as_background_job=False)
    elif fmt == "blend":
        try:
            bpy.ops.file.pack_all()
        except Exception as e:
            print("pack_all failed:", e)
        bpy.ops.wm.save_as_mainfile(filepath=path, compress=True, copy=True)
    else:
        raise RuntimeError(f"unknown format {fmt}")


def main():
    o = parse_args()
    inp = o.get("input")
    if inp and not inp.lower().endswith(".blend"):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        import_file(inp)
    elif inp and inp.lower().endswith(".blend") and bpy.data.filepath != os.path.abspath(inp):
        bpy.ops.wm.open_mainfile(filepath=inp)
    elif not inp:
        bpy.ops.wm.read_factory_settings(use_empty=True)

    if o["objects"] or o["collection"]:
        keep_only(o["objects"], o["collection"])

    for spec in o["append"]:
        item = append_datablock(spec)
        if isinstance(item, bpy.types.Material):
            preview_sphere(item)
        elif isinstance(item, bpy.types.Action):
            item.use_fake_user = True

    relink_textures(o["search_dirs"] + ([os.path.dirname(inp)] if inp else []))

    if o["pbr"]:
        if o["material_only"]:
            preview_sphere(build_pbr_material(o["pbr"].get("_name", "StashMaterial"), o["pbr"]))
        else:
            apply_pbr_sets(o["pbr"])

    if o["scale"] != 1.0:
        for ob in bpy.data.objects:
            if ob.parent is None:
                ob.scale = [s * o["scale"] for s in ob.scale]

    export(o["output"], o["format"])
    print("[stash] converted ->", o["output"])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
