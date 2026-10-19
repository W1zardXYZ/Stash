"""Generate test fixtures with Blender:

  blender -b --factory-startup --python make_fixtures.py -- OUT_DIR

Creates:
  sample.blend        procedural-material cube, textured plane, rigged + animated "Worm", geo-nodes object
  worm.fbx / worm.glb rigged animated mesh exported from the same scene
  crate.obj (+ .mtl)  textured OBJ
  textures/Crate_*    a tiny PBR texture set
  hdri/sky.exr        a tiny float image (Pillow can't read EXR, so exports go through Blender)
  Painted/Meshes/SM_Arch_LP.fbx + Painted/Textures/SM_Arch_LP_<slot>_*.png
                      two material slots textured Substance-Painter style (one set per slot)
"""

import math
import os
import sys

import bpy

out = sys.argv[sys.argv.index("--") + 1]
os.makedirs(os.path.join(out, "textures"), exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def make_image(name, color, path):
    img = bpy.data.images.new(name, 64, 64)
    px = []
    for y in range(64):
        for x in range(64):
            checker = ((x // 8) + (y // 8)) % 2
            f = 0.6 + 0.4 * checker
            px.extend([color[0] * f, color[1] * f, color[2] * f, 1.0])
    img.pixels = px
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    return img


tex = os.path.join(out, "textures")
col_img = make_image("Crate_BaseColor", (0.8, 0.5, 0.2), os.path.join(tex, "Crate_BaseColor.png"))
nrm_img = make_image("Crate_Normal", (0.5, 0.5, 1.0), os.path.join(tex, "Crate_Normal.png"))
make_image("Crate_Roughness", (0.7, 0.7, 0.7), os.path.join(tex, "Crate_Roughness.png"))
make_image("Planks_4K_Albedo", (0.5, 0.35, 0.2), os.path.join(tex, "Planks_4K_Albedo.png"))
make_image("Planks_4K_Normal", (0.5, 0.5, 1.0), os.path.join(tex, "Planks_4K_Normal.png"))
os.makedirs(os.path.join(out, "hdri"), exist_ok=True)
sky = bpy.data.images.new("sky", 64, 32, float_buffer=True)
sky.pixels = [2.0, 1.5, 1.0, 1.0] * (64 * 32)
sky.filepath_raw = os.path.join(out, "hdri", "sky.exr")
sky.file_format = "OPEN_EXR"
sky.save()

# --- procedural material cube
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
cube = bpy.context.active_object
cube.name = "NoiseCube"
mat = bpy.data.materials.new("ProceduralMarble")
mat.use_nodes = True
nt = mat.node_tree
bsdf = nt.nodes["Principled BSDF"]
noise = nt.nodes.new("ShaderNodeTexNoise")
noise.inputs["Scale"].default_value = 8.0
ramp = nt.nodes.new("ShaderNodeValToRGB")
nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
cube.data.materials.append(mat)

# --- image textured crate
bpy.ops.mesh.primitive_cube_add(size=1.5, location=(4, 0, 0.75))
crate = bpy.context.active_object
crate.name = "Crate"
cm = bpy.data.materials.new("CrateMat")
cm.use_nodes = True
nt = cm.node_tree
bsdf = nt.nodes["Principled BSDF"]
t1 = nt.nodes.new("ShaderNodeTexImage")
t1.image = col_img
nt.links.new(t1.outputs["Color"], bsdf.inputs["Base Color"])
t2 = nt.nodes.new("ShaderNodeTexImage")
t2.image = nrm_img
t2.image.colorspace_settings.name = "Non-Color"
nm = nt.nodes.new("ShaderNodeNormalMap")
nt.links.new(t2.outputs["Color"], nm.inputs["Color"])
nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
crate.data.materials.append(cm)

# --- rigged worm: a cylinder with 3 bones and two actions
bpy.ops.mesh.primitive_cylinder_add(vertices=16, radius=0.3, depth=3, location=(-4, 0, 1.5))
worm = bpy.context.active_object
worm.name = "WormMesh"
import bmesh

bm = bmesh.new()
bm.from_mesh(worm.data)
vertical = [e for e in bm.edges if abs(e.verts[0].co.z - e.verts[1].co.z) > 1.0]
bmesh.ops.subdivide_edges(bm, edges=vertical, cuts=8, use_grid_fill=True)
bm.to_mesh(worm.data)
bm.free()
arm_data = bpy.data.armatures.new("WormRig")
arm = bpy.data.objects.new("Worm", arm_data)
scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
prev = None
for i in range(3):
    b = arm_data.edit_bones.new(f"Bone{i}")
    b.head = (-4, 0, i * 1.0)
    b.tail = (-4, 0, (i + 1) * 1.0)
    if prev:
        b.parent = prev
        b.use_connect = True
    prev = b
bpy.ops.object.mode_set(mode="OBJECT")
worm.select_set(True)
arm.select_set(True)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.parent_set(type="ARMATURE_AUTO")
wm = bpy.data.materials.new("WormSkin")
wm.diffuse_color = (0.9, 0.4, 0.5, 1)
wm.use_nodes = True
wm.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.9, 0.4, 0.5, 1)
worm.data.materials.append(wm)

arm.animation_data_create()
for act_name, amp in (("Wiggle", 30), ("Bend", 60)):
    act = bpy.data.actions.new(act_name)
    act.use_fake_user = True
    arm.animation_data.action = act
    for f, sign in ((1, 0), (10, 1), (20, -1), (30, 0)):
        for i in range(1, 3):
            pb = arm.pose.bones[f"Bone{i}"]
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0, math.radians(amp * sign), 0)
            pb.keyframe_insert("rotation_euler", frame=f)
arm.animation_data.action = bpy.data.actions["Wiggle"]

# --- geometry nodes object
bpy.ops.mesh.primitive_plane_add(size=2, location=(0, 5, 0))
gn_obj = bpy.context.active_object
gn_obj.name = "ScatterPlane"
mod = gn_obj.modifiers.new("Scatter", "NODES")
ng = bpy.data.node_groups.new("ScatterCubes", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
gin = ng.nodes.new("NodeGroupInput")
gout = ng.nodes.new("NodeGroupOutput")
dist = ng.nodes.new("GeometryNodeDistributePointsOnFaces")
dist.inputs["Density"].default_value = 5
inst = ng.nodes.new("GeometryNodeInstanceOnPoints")
cubeg = ng.nodes.new("GeometryNodeMeshCube")
cubeg.inputs["Size"].default_value = (0.1, 0.1, 0.1)
real = ng.nodes.new("GeometryNodeRealizeInstances")
ng.links.new(gin.outputs[0], dist.inputs["Mesh"])
ng.links.new(dist.outputs["Points"], inst.inputs["Points"])
ng.links.new(cubeg.outputs["Mesh"], inst.inputs["Instance"])
ng.links.new(inst.outputs["Instances"], real.inputs["Geometry"])
ng.links.new(real.outputs["Geometry"], gout.inputs[0])
mod.node_group = ng

# camera + light that should be ignored by extraction
bpy.ops.object.camera_add(location=(10, -10, 8))
bpy.ops.object.light_add(type="SUN")

bpy.ops.wm.save_as_mainfile(filepath=os.path.join(out, "sample.blend"))

# --- exports of the worm
for o in bpy.context.view_layer.objects:
    o.select_set(o.name in ("Worm", "WormMesh"))
bpy.ops.export_scene.fbx(filepath=os.path.join(out, "worm.fbx"), use_selection=True, add_leaf_bones=False, bake_anim=True)
bpy.ops.export_scene.gltf(filepath=os.path.join(out, "worm.glb"), use_selection=True, export_format="GLB")

# --- textured crate obj
for o in bpy.context.view_layer.objects:
    o.select_set(o.name == "Crate")
bpy.ops.wm.obj_export(filepath=os.path.join(out, "crate.obj"), export_selected_objects=True, path_mode="RELATIVE")
bpy.ops.wm.stl_export(filepath=os.path.join(out, "cube.stl"), export_selected_objects=True)

# --- two-slot mesh with Painter-style texture sets, alone in the scene
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1)
arch = bpy.context.active_object
arch.name = "SM_Arch_LP"
for slot in ("lambert1", "lambert2"):
    arch.data.materials.append(bpy.data.materials.new(slot))
for i, poly in enumerate(arch.data.polygons):
    poly.material_index = i % 2
os.makedirs(os.path.join(out, "Painted", "Meshes"), exist_ok=True)
os.makedirs(os.path.join(out, "Painted", "Textures"), exist_ok=True)
bpy.ops.export_scene.fbx(filepath=os.path.join(out, "Painted", "Meshes", "SM_Arch_LP.fbx"), use_selection=False)
for slot, color in (("lambert1", (0.8, 0.1, 0.1)), ("lambert2", (0.1, 0.1, 0.8))):
    make_image(f"{slot}_bc", color, os.path.join(out, "Painted", "Textures", f"SM_Arch_LP_{slot}_BaseColor.1001.png"))
    make_image(f"{slot}_n", (0.5, 0.5, 1.0), os.path.join(out, "Painted", "Textures", f"SM_Arch_LP_{slot}_Normal.1001.png"))
print("fixtures written to", out)
