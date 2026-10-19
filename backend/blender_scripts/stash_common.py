"""Helpers shared by the Stash Blender scripts. Runs inside Blender (bpy available)."""

import math
import os
import sys

import bpy
from mathutils import Vector


def script_args():
    argv = sys.argv
    return argv[argv.index("--") + 1 :] if "--" in argv else []


def action_fcurves(action):
    """Iterate F-Curves for both legacy and slotted (4.4+/5.x) actions."""
    try:
        fcs = list(action.fcurves)
        if fcs:
            return fcs
    except AttributeError:
        pass
    out = []
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                out.extend(bag.fcurves)
    return out


def action_bone_names(action):
    names = set()
    for fc in action_fcurves(action):
        dp = fc.data_path
        if dp.startswith('pose.bones["'):
            names.add(dp[12 : dp.index('"', 12)])
    return names


def socket_value(sock):
    if not hasattr(sock, "default_value"):
        return None
    v = sock.default_value
    try:
        if hasattr(v, "__len__") and not isinstance(v, str):
            return [round(float(x), 5) for x in v]
        if isinstance(v, float):
            return round(v, 5)
        if isinstance(v, (int, bool, str)):
            return v
    except Exception:
        return None
    return None


PROCEDURAL_NODES = {
    "ShaderNodeTexNoise", "ShaderNodeTexVoronoi", "ShaderNodeTexWave", "ShaderNodeTexMusgrave",
    "ShaderNodeTexMagic", "ShaderNodeTexGradient", "ShaderNodeTexChecker", "ShaderNodeTexBrick",
    "ShaderNodeTexWhiteNoise", "ShaderNodeTexGabor", "ShaderNodeTexSky", "ShaderNodeValToRGB",
}


def serialize_node_tree(tree, depth=0):
    """Serialize a node tree to JSON (nodes, non-default inputs, links). Recurse into groups once."""
    if tree is None:
        return None
    nodes = []
    images = []
    procedural = False
    for n in tree.nodes:
        entry = {"name": n.name, "type": n.bl_idname, "label": n.label or "", "location": [round(n.location.x), round(n.location.y)]}
        inputs = {}
        for s in n.inputs:
            if not s.is_linked:
                v = socket_value(s)
                if v is not None:
                    inputs[s.identifier] = v
        if inputs:
            entry["inputs"] = inputs
        if n.bl_idname in PROCEDURAL_NODES:
            procedural = True
        if n.bl_idname in ("ShaderNodeTexImage", "ShaderNodeTexEnvironment") and getattr(n, "image", None):
            entry["image"] = n.image.name
            images.append(n.image)
        if getattr(n, "node_tree", None) is not None:
            entry["group"] = n.node_tree.name
            if depth < 3:
                sub = serialize_node_tree(n.node_tree, depth + 1)
                images.extend(sub["_images"])
                procedural = procedural or sub["procedural"]
        for attr in ("blend_type", "operation", "data_type", "interpolation", "projection", "distribution", "subsurface_method"):
            if hasattr(n, attr):
                try:
                    entry[attr] = str(getattr(n, attr))
                except Exception:
                    pass
        nodes.append(entry)
    links = [
        {"from": l.from_node.name, "from_socket": l.from_socket.identifier, "to": l.to_node.name, "to_socket": l.to_socket.identifier}
        for l in tree.links
    ]
    return {"nodes": nodes, "links": links, "procedural": procedural and not images, "_images": images}


def ensure_dir(p):
    os.makedirs(p, exist_ok=True)
    return p


def safe_name(name, maxlen=80):
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return (keep or "item")[:maxlen]


def world_bbox(objects):
    """World-space bounding box over evaluated geometry of given objects."""
    deps = bpy.context.evaluated_depsgraph_get()
    mn = Vector((math.inf,) * 3)
    mx = Vector((-math.inf,) * 3)
    found = False
    for o in objects:
        if o.type not in {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME"}:
            continue
        ev = o.evaluated_get(deps)
        for c in ev.bound_box:
            w = ev.matrix_world @ Vector(c)
            mn = Vector(map(min, mn, w))
            mx = Vector(map(max, mx, w))
            found = True
    if not found:
        for o in objects:
            w = o.matrix_world.translation
            mn = Vector(map(min, mn, w))
            mx = Vector(map(max, mx, w))
        mn -= Vector((1, 1, 1))
        mx += Vector((1, 1, 1))
    return mn, mx


def setup_render(scene, size=384, samples=24, transparent=True):
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = samples
    try:
        scene.cycles.use_denoising = True
        scene.cycles.denoiser = "OPENIMAGEDENOISE"
    except Exception:
        pass
    scene.cycles.use_adaptive_sampling = True
    scene.render.resolution_x = size
    scene.render.resolution_y = size
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = transparent
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform = "AgX" if "AgX" in [v.identifier for v in type(scene.view_settings).bl_rna.properties["view_transform"].enum_items] else "Filmic"


def studio_world(name="STASH_World"):
    w = bpy.data.worlds.get(name) or bpy.data.worlds.new(name)
    w.use_nodes = True
    nt = w.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    bg = nt.nodes.new("ShaderNodeBackground")
    sky = nt.nodes.new("ShaderNodeTexGradient")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    coord = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Rotation"].default_value[1] = math.radians(-90)
    ramp.color_ramp.elements[0].color = (0.18, 0.18, 0.2, 1)
    ramp.color_ramp.elements[1].color = (0.9, 0.9, 0.92, 1)
    nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], sky.inputs["Vector"])
    nt.links.new(sky.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bg.inputs["Color"])
    bg.inputs["Strength"].default_value = 1.0
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    return w


def add_lights(scene, center, radius):
    coll = scene.collection
    made = []
    for name, energy, direction in (("Key", 4.0, (1.0, -1.2, 1.4)), ("Rim", 2.5, (-1.2, 1.0, 1.0))):
        ld = bpy.data.lights.new(f"STASH_{name}", "SUN")
        ld.energy = energy
        ld.angle = math.radians(10)
        lo = bpy.data.objects.new(f"STASH_{name}", ld)
        d = Vector(direction).normalized()
        lo.location = center + d * radius * 3
        lo.rotation_euler = d.to_track_quat("Z", "Y").to_euler()
        coll.objects.link(lo)
        made.append(lo)
    return made


def frame_camera(scene, mn, mx, fov_deg=35.0, direction=(1.0, -1.3, 0.8)):
    center = (mn + mx) / 2
    radius = max((mx - mn).length / 2, 1e-3)
    cd = bpy.data.cameras.new("STASH_Cam")
    cd.lens_unit = "FOV"
    cd.angle = math.radians(fov_deg)
    cd.clip_start = radius / 100
    cd.clip_end = radius * 100
    cam = bpy.data.objects.new("STASH_Cam", cd)
    d = Vector(direction).normalized()
    dist = radius / math.sin(math.radians(fov_deg) / 2) * 1.05
    cam.location = center + d * dist
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(cam)
    scene.camera = cam
    return cam, center, radius


def render_to(scene, path):
    scene.render.filepath = path
    with bpy.context.temp_override(scene=scene):
        bpy.ops.render.render(write_still=True, scene=scene.name)
    return os.path.exists(path)
