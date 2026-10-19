"""Split a .blend file into individual Stash assets.

Usage:
  blender -b file.blend --factory-startup --python extract_blend.py -- --out DIR [options]

Options:
  --group auto|objects|collections|marked   how objects are grouped into assets (default auto:
                                           Blender-marked assets if the file has any, else objects)
  --thumbs                                 render Cycles thumbnails (CPU)
  --size N --samples N                     thumbnail size / samples
  --no-glb / --no-blend                    skip per-asset GLB previews / .blend files
  --no-pack                                don't pack textures into the per-asset .blend files

Writes DIR/manifest.json describing every extracted asset; file paths in the manifest are
relative to DIR unless they point at existing external texture files (absolute).
"""

import json
import os
import sys
import time
import traceback

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stash_common as sc  # noqa: E402

GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME", "GPENCIL", "GREASEPENCIL"}
EXPORTABLE_ROOT_TYPES = GEOMETRY_TYPES | {"ARMATURE", "EMPTY"}


def parse_args():
    a = sc.script_args()
    opts = {"out": None, "group": "auto", "thumbs": False, "size": 384, "samples": 24, "glb": True, "blend": True, "pack": True}
    i = 0
    while i < len(a):
        k = a[i]
        if k == "--out":
            opts["out"] = a[i + 1]; i += 1
        elif k == "--group":
            opts["group"] = a[i + 1]; i += 1
        elif k == "--size":
            opts["size"] = int(a[i + 1]); i += 1
        elif k == "--samples":
            opts["samples"] = int(a[i + 1]); i += 1
        elif k == "--thumbs":
            opts["thumbs"] = True
        elif k == "--no-glb":
            opts["glb"] = False
        elif k == "--no-blend":
            opts["blend"] = False
        elif k == "--no-pack":
            opts["pack"] = False
        i += 1
    if not opts["out"]:
        raise SystemExit("--out is required")
    return opts


def log(msg):
    print(f"[stash] {msg}", flush=True)


def progress(done, total):
    print(f"[stash-progress] {done}/{total}", flush=True)


# ----------------------------------------------------------------------------- grouping

def descendants(obj):
    out = [obj]
    for c in obj.children:
        out.extend(descendants(c))
    return out


def scene_objects():
    seen = set()
    out = []
    for s in bpy.data.scenes:
        for o in s.objects:
            if o.name not in seen:
                seen.add(o.name)
                out.append(o)
    return out


def armature_of(obj):
    for m in getattr(obj, "modifiers", []):
        if m.type == "ARMATURE" and m.object is not None:
            return m.object
    return None


def group_objects_mode():
    """Each root hierarchy becomes an asset; meshes deformed by an armature join the armature."""
    objs = [o for o in scene_objects() if o.type in EXPORTABLE_ROOT_TYPES or o.type in GEOMETRY_TYPES]
    roots = [o for o in objs if o.parent is None]
    groups = {}
    attach = {}
    for r in roots:
        arm = armature_of(r) if r.type in GEOMETRY_TYPES else None
        if arm is not None and arm.parent is None and arm != r:
            attach.setdefault(arm.name, []).append(r)
        else:
            groups[r.name] = [r]
    for arm_name, extra in attach.items():
        groups.setdefault(arm_name, [bpy.data.objects[arm_name]]).extend(extra)
    result = []
    for root_name, members in groups.items():
        objs_all = []
        for m in members:
            objs_all.extend(descendants(m))
        # skip groups without any geometry (lone empties, cameras parented to empties, ...)
        if not any(o.type in GEOMETRY_TYPES or o.instance_collection is not None for o in objs_all):
            continue
        root = bpy.data.objects[root_name]
        result.append({"key": f"object:{root_name}", "name": root_name, "objects": objs_all, "root": root})
    return result


def group_collections_mode():
    result = []
    for s in bpy.data.scenes:
        for coll in s.collection.children:
            objs = list(coll.all_objects)
            if not any(o.type in GEOMETRY_TYPES for o in objs):
                continue
            result.append({"key": f"collection:{coll.name}", "name": coll.name, "objects": objs, "root": None, "collection": coll.name})
        loose = [o for o in s.collection.objects if o.parent is None]
        for o in loose:
            objs = descendants(o)
            if any(x.type in GEOMETRY_TYPES for x in objs):
                result.append({"key": f"object:{o.name}", "name": o.name, "objects": objs, "root": o})
    return result


def group_marked_mode():
    result = []
    for o in bpy.data.objects:
        if o.asset_data is not None:
            objs = descendants(o)
            arm = armature_of(o)
            if arm is not None and arm not in objs:
                objs.append(arm)
            result.append({"key": f"object:{o.name}", "name": o.name, "objects": objs, "root": o, "marked": True})
    for c in bpy.data.collections:
        if c.asset_data is not None:
            result.append({"key": f"collection:{c.name}", "name": c.name, "objects": list(c.all_objects), "root": None, "collection": c.name, "marked": True})
    return result


def has_marked_assets():
    for coll in (bpy.data.objects, bpy.data.collections, bpy.data.materials, bpy.data.node_groups, bpy.data.actions, bpy.data.worlds):
        if any(i.asset_data is not None for i in coll):
            return True
    return False


# ----------------------------------------------------------------------------- stats

def object_stats(objs):
    deps = bpy.context.evaluated_depsgraph_get()
    tris = verts = 0
    bones = 0
    shape_keys = 0
    modifiers = set()
    geo_nodes = False
    materials = []
    for o in objs:
        if o.type == "ARMATURE":
            bones += len(o.data.bones)
        if o.type == "MESH":
            me = o.data
            if me.shape_keys:
                shape_keys += max(len(me.shape_keys.key_blocks) - 1, 0)
        for m in getattr(o, "modifiers", []):
            modifiers.add(m.type)
            if m.type == "NODES":
                geo_nodes = True
        for slot in getattr(o, "material_slots", []):
            if slot.material and slot.material not in materials:
                materials.append(slot.material)
        if o.type in GEOMETRY_TYPES:
            try:
                ev = o.evaluated_get(deps)
                me = ev.to_mesh()
                if me is not None:
                    me.calc_loop_triangles()
                    tris += len(me.loop_triangles)
                    verts += len(me.vertices)
                ev.to_mesh_clear()
            except Exception:
                pass
    return {
        "triangles": tris,
        "vertices": verts,
        "bones": bones,
        "shape_keys": shape_keys,
        "modifiers": sorted(modifiers),
        "geometry_nodes": geo_nodes,
        "object_count": len(objs),
        "object_types": sorted({o.type for o in objs}),
    }, materials


def compatible_actions(arm_obj):
    if arm_obj is None or arm_obj.type != "ARMATURE":
        return []
    bone_names = {b.name for b in arm_obj.data.bones}
    out = []
    for act in bpy.data.actions:
        names = sc.action_bone_names(act)
        if names and len(names & bone_names) >= max(1, int(len(names) * 0.6)):
            out.append(act)
    ad = arm_obj.animation_data
    if ad and ad.action and ad.action not in out:
        out.append(ad.action)
    return out


def object_actions(objs):
    acts = []
    for o in objs:
        if o.type == "ARMATURE":
            for a in compatible_actions(o):
                if a not in acts:
                    acts.append(a)
        ad = o.animation_data
        if ad:
            if ad.action and ad.action not in acts:
                acts.append(ad.action)
            for tr in ad.nla_tracks:
                for st in tr.strips:
                    if st.action and st.action not in acts:
                        acts.append(st.action)
    return acts


# ----------------------------------------------------------------------------- images / materials

CHANNEL_BY_SOCKET = {
    "Base Color": "base_color", "Color": "base_color", "Roughness": "roughness", "Metallic": "metallic",
    "Alpha": "opacity", "Emission Color": "emissive", "Emission": "emissive", "Normal": "normal",
    "Height": "height", "Displacement": "height", "Specular IOR Level": "specular", "Specular": "specular",
    "Subsurface Weight": "subsurface", "Coat Weight": "coat",
}


def image_channels(material):
    """Follow links from image nodes downstream to find which shader input each one drives."""
    chans = {}
    if not material or not material.use_nodes or not material.node_tree:
        return chans
    nt = material.node_tree

    def walk(node, depth=0):
        if depth > 6:
            return None
        for out in node.outputs:
            for l in out.links:
                tn = l.to_node
                if tn.bl_idname in ("ShaderNodeBsdfPrincipled", "ShaderNodeOutputMaterial"):
                    return CHANNEL_BY_SOCKET.get(l.to_socket.name, l.to_socket.name.lower())
                if tn.bl_idname == "ShaderNodeNormalMap":
                    return "normal"
                if tn.bl_idname in ("ShaderNodeBump", "ShaderNodeDisplacement"):
                    return "height"
                r = walk(tn, depth + 1)
                if r:
                    return r
        return None

    for n in nt.nodes:
        if n.bl_idname == "ShaderNodeTexImage" and n.image:
            ch = walk(n)
            if n.image.name not in chans and ch:
                chans[n.image.name] = ch
    return chans


def export_image(img, tex_dir):
    """Return path to an image file on disk (writing packed/generated images into tex_dir)."""
    try:
        if img.packed_file is not None:
            ext = os.path.splitext(img.filepath_raw or img.name)[1] or (".png" if img.file_format == "PNG" else ".png")
            path = os.path.join(tex_dir, sc.safe_name(os.path.splitext(img.name)[0]) + ext.lower())
            with open(path, "wb") as f:
                f.write(img.packed_file.data)
            return path, "packed"
        if img.source in ("FILE", "SEQUENCE", "MOVIE"):
            p = bpy.path.abspath(img.filepath, library=img.library)
            p = os.path.normpath(p)
            if os.path.exists(p):
                return p, "external"
            return p, "missing"
        if img.source == "GENERATED":
            path = os.path.join(tex_dir, sc.safe_name(img.name) + ".png")
            img.filepath_raw = path
            img.file_format = "PNG"
            img.save()
            return path, "generated"
    except Exception as e:
        log(f"image export failed for {img.name}: {e}")
    return None, "error"


# ----------------------------------------------------------------------------- export scene

class ExportScene:
    """A throw-away scene per asset. Reusing one scene across glTF exports crashes Blender 4.5,
    so every asset gets a fresh scene that is removed again afterwards."""

    def __init__(self, opts):
        self.opts = opts
        self.world = sc.studio_world()
        self.scene = None
        self.vl = None
        win = bpy.context.window
        self.home_scene = win.scene if win else bpy.context.scene

    def _activate(self, scene):
        win = bpy.context.window
        if win is not None and win.scene != scene:
            win.scene = scene

    def begin(self, objs, name="STASH_Export"):
        self.end()
        self.scene = bpy.data.scenes.new(sc.safe_name(name, 60) or "Asset")
        self.vl = self.scene.view_layers[0]
        self._activate(self.scene)
        if self.opts["thumbs"]:
            sc.setup_render(self.scene, self.opts["size"], self.opts["samples"])
        for o in objs:
            if o.name not in self.scene.collection.objects:
                self.scene.collection.objects.link(o)
            o.hide_viewport = False
            o.hide_render = False
            o.hide_select = False
        for o in objs:
            try:
                o.hide_set(False, view_layer=self.vl)
            except Exception:
                pass
        # make sure the new scene has an evaluated depsgraph before exporters ask for it
        self.vl.update()
        return self.scene

    def end(self):
        if self.scene is not None:
            # the glTF exporter switches window.scene itself; never delete the active scene
            self._activate(self.home_scene)
            bpy.data.scenes.remove(self.scene)
            self.scene = None
            self.vl = None

    def select_only(self, objs):
        for o in self.scene.objects:
            o.select_set(o in objs, view_layer=self.vl)

    def export_glb(self, objs, path, animations=True):
        """Export from the file's own scene: running the glTF exporter on freshly created
        scenes segfaults in Blender 4.5 (depsgraph not yet built for the new view layer)."""
        home = self.home_scene
        self._activate(home)
        vl = home.view_layers[0] if bpy.context.window is None else bpy.context.window.view_layer
        added = []
        for o in objs:
            if o.name not in vl.objects:
                home.collection.objects.link(o)
                added.append(o)
        vl.update()
        try:
            for o in vl.objects:
                o.select_set(o in objs)
            for o in objs:
                try:
                    o.hide_set(False)
                except Exception:
                    pass
            bpy.ops.export_scene.gltf(
                filepath=path,
                export_format="GLB",
                use_selection=True,
                export_apply=True,
                export_animations=animations,
                export_animation_mode="ACTIONS",
                export_yup=True,
                export_lights=False,
                export_cameras=False,
            )
        finally:
            for o in added:
                home.collection.objects.unlink(o)
            if self.scene is not None:
                self._activate(self.scene)
        return os.path.exists(path)

    def write_blend(self, path):
        bpy.data.libraries.write(path, {self.scene}, path_remap="ABSOLUTE", fake_user=True, compress=True)
        return os.path.exists(path)

    def render_objects(self, objs, path):
        self.scene.world = self.world
        mn, mx = sc.world_bbox(objs)
        cam, center, radius = sc.frame_camera(self.scene, mn, mx)
        lights = sc.add_lights(self.scene, center, radius)
        try:
            return sc.render_to(self.scene, path)
        finally:
            self.scene.world = None
            for o in [cam] + lights:
                data = o.data
                bpy.data.objects.remove(o)
                if isinstance(data, bpy.types.Camera):
                    bpy.data.cameras.remove(data)
                else:
                    bpy.data.lights.remove(data)


def material_preview(es, mat, path):
    import bmesh

    me = bpy.data.meshes.new("STASH_Sphere")
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=64, v_segments=32, radius=1.0, calc_uvs=True)
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    ob = bpy.data.objects.new("STASH_Sphere", me)
    me.materials.append(mat)
    es.begin([ob], "STASH_MatPreview")
    try:
        return es.render_objects([ob], path)
    finally:
        es.end()
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)


# ----------------------------------------------------------------------------- main

def main():
    opts = parse_args()
    out = sc.ensure_dir(opts["out"])
    tex_dir = sc.ensure_dir(os.path.join(out, "textures"))
    t0 = time.time()
    src = bpy.data.filepath
    log(f"extracting {src}")

    mode = opts["group"]
    if mode == "auto":
        mode = "marked" if has_marked_assets() else "objects"
    if mode == "marked":
        groups = group_marked_mode()
        if not groups and not has_marked_assets():
            groups = group_objects_mode()
    elif mode == "collections":
        groups = group_collections_mode()
    else:
        groups = group_objects_mode()

    if opts["pack"] and opts["blend"]:
        for img in bpy.data.images:
            try:
                if img.source == "FILE" and img.packed_file is None and os.path.exists(bpy.path.abspath(img.filepath, library=img.library)):
                    img.pack()
            except Exception as e:
                log(f"could not pack {img.name}: {e}")

    es = ExportScene(opts)
    manifest = {
        "source": src,
        "blender_version": bpy.app.version_string,
        "group_mode": mode,
        "scene_stats": {
            "objects": len(bpy.data.objects), "meshes": len(bpy.data.meshes), "materials": len(bpy.data.materials),
            "actions": len(bpy.data.actions), "images": len(bpy.data.images), "node_groups": len(bpy.data.node_groups),
            "collections": len(bpy.data.collections), "scenes": len(bpy.data.scenes) - 1,
        },
        "assets": [],
        "errors": [],
    }
    assets = manifest["assets"]
    used_materials = []
    used_actions = []
    total = len(groups) + 1

    for gi, g in enumerate(groups):
        progress(gi, total)
        key = g["key"]
        safe = f"{gi:04d}_{sc.safe_name(g['name'])}"
        objs = g["objects"]
        entry = {"key": key, "name": g["name"], "files": {}, "objects": [o.name for o in objs]}
        if g.get("collection"):
            entry["collection"] = g["collection"]
        try:
            stats, mats = object_stats(objs)
            acts = object_actions(objs)
            rigged = stats["bones"] > 0
            entry.update(
                kind="skeletal_model" if rigged else "model",
                stats=stats,
                materials=[f"material:{m.name}" for m in mats],
                actions=[f"action:{a.name}" for a in acts],
                is_rigged=rigged,
                is_animated=bool(acts),
                is_procedural=stats["geometry_nodes"],
                marked=bool(g.get("marked")),
            )
            root = g.get("root")
            if root is not None and root.asset_data is not None:
                entry["description"] = root.asset_data.description
                entry["tags"] = [t.name for t in root.asset_data.tags]
                entry["catalog_id"] = root.asset_data.catalog_id
            for m in mats:
                if m not in used_materials:
                    used_materials.append(m)
            for a in acts:
                if a not in used_actions:
                    used_actions.append(a)

            es.begin(objs, g["name"])
            if opts["blend"]:
                p = os.path.join(out, safe + ".blend")
                if es.write_blend(p):
                    entry["files"]["blend"] = os.path.basename(p)
            if opts["glb"]:
                p = os.path.join(out, safe + ".glb")
                try:
                    if es.export_glb(objs, p, animations=bool(acts)):
                        entry["files"]["glb"] = os.path.basename(p)
                except Exception as e:
                    manifest["errors"].append(f"{key}: glb export failed: {e}")
            if opts["thumbs"]:
                p = os.path.join(out, safe + ".png")
                try:
                    if es.render_objects(objs, p):
                        entry["files"]["thumbnail"] = os.path.basename(p)
                except Exception as e:
                    manifest["errors"].append(f"{key}: render failed: {e}")
        except Exception as e:
            traceback.print_exc()
            manifest["errors"].append(f"{key}: {e}")
        finally:
            es.end()
        assets.append(entry)

    # Materials: everything used by extracted objects plus any marked or fake-user materials
    for m in bpy.data.materials:
        if m not in used_materials and (m.asset_data is not None or (m.use_fake_user and m.users <= 1)):
            used_materials.append(m)
    images_seen = {}
    for mi, mat in enumerate(used_materials):
        key = f"material:{mat.name}"
        safe = f"mat_{mi:04d}_{sc.safe_name(mat.name)}"
        entry = {"key": key, "name": mat.name, "kind": "material", "files": {}}
        try:
            graph = sc.serialize_node_tree(mat.node_tree) if mat.use_nodes else None
            imgs = graph.pop("_images") if graph else []
            chans = image_channels(mat)
            entry["graph"] = graph
            entry["is_procedural"] = bool(graph and graph["procedural"])
            entry["textures"] = []
            for img in dict.fromkeys(imgs):
                ik = f"image:{img.name}"
                entry["textures"].append(ik)
                if ik not in images_seen:
                    images_seen[ik] = {"img": img, "channel": chans.get(img.name)}
                elif chans.get(img.name) and not images_seen[ik]["channel"]:
                    images_seen[ik]["channel"] = chans.get(img.name)
            entry["base_color"] = [round(c, 4) for c in mat.diffuse_color]
            if mat.asset_data is not None:
                entry["description"] = mat.asset_data.description
                entry["tags"] = [t.name for t in mat.asset_data.tags]
            if opts["blend"]:
                p = os.path.join(out, safe + ".blend")
                bpy.data.libraries.write(p, {mat}, path_remap="ABSOLUTE", fake_user=True, compress=True)
                entry["files"]["blend"] = os.path.basename(p)
            if opts["thumbs"]:
                p = os.path.join(out, safe + ".png")
                try:
                    if material_preview(es, mat, p):
                        entry["files"]["thumbnail"] = os.path.basename(p)
                except Exception as e:
                    manifest["errors"].append(f"{key}: render failed: {e}")
        except Exception as e:
            traceback.print_exc()
            manifest["errors"].append(f"{key}: {e}")
        assets.append(entry)

    # Images referenced by materials
    for ik, info in images_seen.items():
        img = info["img"]
        path, state = export_image(img, tex_dir)
        entry = {"key": ik, "name": img.name, "kind": "texture", "channel": info["channel"], "state": state, "files": {}}
        try:
            entry["size"] = list(img.size)
        except Exception:
            pass
        if path and os.path.exists(path):
            entry["files"]["image"] = os.path.relpath(path, out) if path.startswith(out) else path
        assets.append(entry)

    # Actions
    for ai, act in enumerate(set(used_actions) | {a for a in bpy.data.actions if a.asset_data is not None or a.use_fake_user}):
        key = f"action:{act.name}"
        safe = f"act_{ai:04d}_{sc.safe_name(act.name)}"
        fr = act.frame_range
        bones = sorted(sc.action_bone_names(act))
        entry = {
            "key": key, "name": act.name, "kind": "animation", "files": {},
            "frame_range": [float(fr[0]), float(fr[1])], "fps": bpy.context.scene.render.fps,
            "bones": len(bones), "fcurves": len(sc.action_fcurves(act)),
        }
        targets = [a["key"] for a in assets if a.get("kind") == "skeletal_model" and key in a.get("actions", [])]
        entry["targets"] = targets
        if opts["blend"]:
            p = os.path.join(out, safe + ".blend")
            bpy.data.libraries.write(p, {act}, path_remap="ABSOLUTE", fake_user=True, compress=True)
            entry["files"]["blend"] = os.path.basename(p)
        assets.append(entry)

    # Node groups (geometry nodes & shader groups) that are reusable on their own
    for ni, ng in enumerate(bpy.data.node_groups):
        if ng.name.startswith(".") or ng.library is not None:
            continue
        if not (ng.asset_data is not None or ng.use_fake_user or ng.bl_idname == "GeometryNodeTree"):
            continue
        key = f"node_group:{ng.name}"
        safe = f"ng_{ni:04d}_{sc.safe_name(ng.name)}"
        graph = sc.serialize_node_tree(ng)
        graph.pop("_images", None)
        entry = {"key": key, "name": ng.name, "kind": "node_group", "tree_type": ng.bl_idname, "graph": graph, "files": {}, "is_procedural": True}
        if ng.asset_data is not None:
            entry["description"] = ng.asset_data.description
            entry["tags"] = [t.name for t in ng.asset_data.tags]
        if opts["blend"]:
            p = os.path.join(out, safe + ".blend")
            bpy.data.libraries.write(p, {ng}, path_remap="ABSOLUTE", fake_user=True, compress=True)
            entry["files"]["blend"] = os.path.basename(p)
        assets.append(entry)

    # Worlds with environment textures -> HDRI references
    for w in bpy.data.worlds:
        if w.name.startswith("STASH_") or not w.use_nodes or not w.node_tree:
            continue
        for n in w.node_tree.nodes:
            if n.bl_idname == "ShaderNodeTexEnvironment" and n.image:
                ik = f"image:{n.image.name}"
                if ik not in images_seen:
                    path, state = export_image(n.image, tex_dir)
                    if path and os.path.exists(path):
                        assets.append({"key": ik, "name": n.image.name, "kind": "hdri", "state": state, "files": {"image": os.path.relpath(path, out) if path.startswith(out) else path}})
                        images_seen[ik] = {"img": n.image, "channel": "environment"}

    progress(total, total)
    manifest["elapsed"] = round(time.time() - t0, 2)
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    log(f"done: {len(assets)} assets in {manifest['elapsed']}s")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
