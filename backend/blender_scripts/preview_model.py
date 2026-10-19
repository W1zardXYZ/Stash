"""Make a web preview (GLB) and/or a rendered thumbnail for a single model file.

  blender -b --factory-startup --python preview_model.py -- --input FILE [--glb OUT.glb] [--thumb OUT.png]
          [--size N] [--samples N] [--search-dir DIR ...] [--pbr JSON]
  blender -b --factory-startup --python preview_model.py -- --image IN.exr --thumb OUT.png --size N
"""

import json
import os
import sys
import traceback

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stash_common as sc  # noqa: E402
from convert import build_pbr_material, import_file, relink_textures  # noqa: E402


def parse():
    a = sc.script_args()
    o = {"size": 384, "samples": 24, "search_dirs": [], "glb": None, "thumb": None, "input": None, "image": None, "pbr": None, "preview_max": 1024}
    i = 0
    while i < len(a):
        k, v = a[i], (a[i + 1] if i + 1 < len(a) else None)
        if k in ("--input", "--glb", "--thumb", "--image"):
            o[k[2:]] = v; i += 1
        elif k == "--size":
            o["size"] = int(v); i += 1
        elif k == "--samples":
            o["samples"] = int(v); i += 1
        elif k == "--search-dir":
            o["search_dirs"].append(v); i += 1
        elif k == "--preview-max":
            o["preview_max"] = int(v); i += 1
        elif k == "--pbr":
            o["pbr"] = json.loads(v); i += 1
        i += 1
    return o


def image_thumb(src, dest, size):
    img = bpy.data.images.load(src)
    w, h = img.size
    if w and h and max(w, h) > size:  # only ever shrink
        s = size / max(w, h)
        img.scale(max(1, int(w * s)), max(1, int(h * s)))
    scene = bpy.context.scene
    scene.render.image_settings.file_format = "PNG"
    scene.view_settings.view_transform = "Standard"
    img.save_render(dest, scene=scene)


def main():
    o = parse()
    if o["image"]:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        image_thumb(o["image"], o["thumb"], o["size"])
        print("[stash] image thumbnail done")
        return
    inp = o["input"]
    if inp.lower().endswith(".blend"):
        bpy.ops.wm.open_mainfile(filepath=inp)
    else:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        import_file(inp)
    relink_textures(o["search_dirs"] + [os.path.dirname(inp)])
    if o["pbr"]:
        mat = build_pbr_material(o["pbr"].get("_name", "StashMaterial"), o["pbr"])
        for ob in bpy.data.objects:
            if ob.type == "MESH":
                mats = ob.data.materials
                if len(mats) == 0:
                    mats.append(mat)
                else:
                    for i, m in enumerate(mats):
                        if m is None or not (m.use_nodes and any(n.bl_idname == "ShaderNodeTexImage" for n in m.node_tree.nodes)):
                            mats[i] = mat
    scene = bpy.context.scene
    # skip bone custom-shape helpers (the glTF importer adds an "Icosphere") and hidden objects
    shapes = {pb.custom_shape for ob in scene.objects if ob.type == "ARMATURE" for pb in ob.pose.bones if pb.custom_shape}
    objs = [ob for ob in scene.objects if ob.type not in ("CAMERA", "LIGHT") and ob not in shapes and not ob.hide_render and ob.visible_get()]
    for ob in shapes:
        ob.hide_render = True
    stats = {"objects": len(objs), "bones": sum(len(ob.data.bones) for ob in objs if ob.type == "ARMATURE"), "actions": [a.name for a in bpy.data.actions]}
    if o["glb"]:
        # previews only need screen-sized textures; the source files are untouched
        sc.downscale_images(o["preview_max"])
        for ob in bpy.context.view_layer.objects:
            ob.select_set(ob in objs)
        bpy.ops.export_scene.gltf(
            filepath=o["glb"], export_format="GLB", use_selection=True, export_apply=True,
            export_animations=True, export_animation_mode="ACTIONS", export_yup=True, export_lights=False, export_cameras=False,
            export_image_format="WEBP" if o["preview_max"] else "AUTO", export_image_quality=85,
        )
    if o["thumb"]:
        for ob in scene.objects:
            if ob.type in ("CAMERA", "LIGHT"):
                ob.hide_render = True
        sc.setup_render(scene, o["size"], o["samples"])
        scene.world = sc.studio_world()
        mn, mx = sc.world_bbox(objs)
        cam, center, radius = sc.frame_camera(scene, mn, mx)
        sc.add_lights(scene, center, radius)
        sc.render_to(scene, o["thumb"])
    print("[stash-stats] " + json.dumps(stats))
    print("[stash] preview done")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
