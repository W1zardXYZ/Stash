"""Stash Bridge - browse, import and push assets to a self-hosted Stash library.

Sidebar (N) > Stash in the 3D viewport.
"""

bl_info = {
    "name": "Stash Bridge",
    "author": "Stash",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Stash",
    "description": "Browse, import and push assets to your self-hosted Stash library",
    "category": "Import-Export",
}

import json
import os
import queue
import tempfile
import threading
import time
import webbrowser
import zipfile

import bpy
import bpy.utils.previews
from bpy.props import EnumProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

from .client import StashClient, StashError

ADDON_ID = __package__ or __name__

KIND_ITEMS = [
    ("", "All", "Everything"),
    ("model,skeletal_model", "Models", "Static and rigged models"),
    ("skeletal_model", "Rigged", "Rigged models"),
    ("animation", "Animations", "Actions / animation clips"),
    ("material", "Materials", "Materials and PBR texture sets"),
    ("texture", "Textures", "Single textures"),
    ("hdri", "HDRIs", "Environment maps"),
    ("node_group", "Node groups", "Geometry / shader node groups"),
    ("scene", "Files", "Whole .blend / scene files"),
]

# ----------------------------------------------------------------------------- state

_state = {"results": [], "total": 0, "busy": "", "error": "", "status": None}
_previews = None
_main_q: "queue.Queue" = queue.Queue()


def prefs():
    return bpy.context.preferences.addons[ADDON_ID].preferences


def client() -> StashClient:
    p = prefs()
    return StashClient(p.server_url, p.token)


def cache_dir() -> str:
    p = prefs()
    if p.cache_dir:
        d = bpy.path.abspath(p.cache_dir)
    else:
        try:
            d = os.path.join(bpy.utils.extension_path_user(ADDON_ID, create=True), "cache")
        except Exception:
            d = os.path.join(tempfile.gettempdir(), "stash_cache")
    os.makedirs(d, exist_ok=True)
    return d


def run_async(work, done=None, label="Working"):
    """Run `work()` on a thread; call `done(result, error)` back on Blender's main thread."""
    _state["busy"] = label
    _state["error"] = ""

    def target():
        try:
            res, err = work(), None
        except Exception as e:  # noqa: BLE001
            res, err = None, e
        _main_q.put((done, res, err))

    threading.Thread(target=target, daemon=True).start()


def _pump():
    try:
        _auto_receive_tick()
    except Exception:  # noqa: BLE001 - never kill the timer
        pass
    while True:
        try:
            done, res, err = _main_q.get_nowait()
        except queue.Empty:
            break
        _state["busy"] = ""
        if err is not None:
            _state["error"] = str(err)
            print("[Stash]", err)
        if done is not None:
            try:
                done(res, err)
            except Exception as e:  # noqa: BLE001
                _state["error"] = str(e)
                import traceback

                traceback.print_exc()
        _redraw()
    return 0.2


def _redraw():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# ----------------------------------------------------------------------------- preferences

class StashPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    server_url: StringProperty(name="Server URL", default="http://localhost:8420", description="Address of your Stash server, e.g. http://nas.local:8420")
    token: StringProperty(name="API token / password", default="", subtype="PASSWORD", description="Only needed when the server sets STASH_PASSWORD or STASH_API_TOKEN")
    cache_dir: StringProperty(name="Download cache", default="", subtype="DIR_PATH", description="Where downloaded assets are kept (empty = extension folder)")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "server_url")
        col.prop(self, "token")
        col.prop(self, "cache_dir")
        row = col.row()
        row.operator("stash.check_connection", icon="URL")
        st = _state.get("status")
        if st:
            col.label(text=f"Connected: Stash {st.get('version')} - {st.get('assets')} assets, Blender on server: {'yes' if st.get('blender') else 'no'}", icon="CHECKMARK")
        if not bpy.app.online_access:
            col.label(text="Note: 'Allow Online Access' is off in Preferences > System; Stash still uses your configured server.", icon="INFO")


# ----------------------------------------------------------------------------- search

def _load_thumbs(items):
    global _previews
    c = client()
    d = os.path.join(cache_dir(), "thumbs")
    os.makedirs(d, exist_ok=True)
    out = {}
    for it in items:
        if not it.get("thumbnail"):
            continue
        path = os.path.join(d, f"{it['id']}_{abs(hash(it['thumbnail'])) % 10**8}.png")
        if not os.path.exists(path):
            try:
                c.download(it["thumbnail"], path)
            except StashError:
                continue
        out[it["id"]] = path
    return out


def _apply_thumbs(paths, _err):
    if not paths:
        return
    for aid, path in paths.items():
        key = str(aid)
        if key not in _previews:
            try:
                _previews.load(key, path, "IMAGE")
            except Exception:
                pass


def do_search(context=None, page=1):
    wm = bpy.context.window_manager
    q, kind = wm.stash_query, wm.stash_kind

    def work():
        return client().search(q=q, kind=kind, page=page, per_page=wm.stash_per_page)

    def done(res, err):
        if err:
            return
        _state["results"] = res["items"]
        _state["total"] = res["total"]
        wm.stash_page = page
        run_async(lambda: _load_thumbs(res["items"]), _apply_thumbs, "Loading thumbnails")

    run_async(work, done, "Searching")


def _on_query_change(self, context):
    do_search(context, 1)


class STASH_OT_check_connection(Operator):
    bl_idname = "stash.check_connection"
    bl_label = "Test connection"

    def execute(self, context):
        try:
            _state["status"] = client().status()
            self.report({"INFO"}, f"Connected to Stash {_state['status'].get('version')}")
        except StashError as e:
            self.report({"ERROR"}, str(e))
        return {"FINISHED"}


class STASH_OT_search(Operator):
    bl_idname = "stash.search"
    bl_label = "Search Stash"
    page: IntProperty(default=1)

    def execute(self, context):
        do_search(context, self.page)
        return {"FINISHED"}


class STASH_OT_open_web(Operator):
    bl_idname = "stash.open_web"
    bl_label = "Open in browser"
    asset_id: IntProperty()

    def execute(self, context):
        webbrowser.open(f"{prefs().server_url.rstrip('/')}/asset/{self.asset_id}" if self.asset_id else prefs().server_url)
        return {"FINISHED"}


# ----------------------------------------------------------------------------- import

def _choose_target(asset):
    targets = asset.get("targets", [])
    kind = asset["kind"]
    if kind in ("texture", "hdri") and "original" in targets:
        return "original"
    for t in ("blend", "glb", "original"):
        if t in targets:
            return t
    return None


def _ensure_collection(name):
    coll = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(coll)
    return coll


def _append_objects(path, asset_name):
    with bpy.data.libraries.load(path, link=False) as (src, dst):
        dst.objects = list(src.objects)
    coll = _ensure_collection(asset_name)
    objs = [o for o in dst.objects if o is not None]
    cursor = bpy.context.scene.cursor.location.copy()
    for o in objs:
        coll.objects.link(o)
    roots = [o for o in objs if o.parent is None]
    if roots and prefs_place_at_cursor():
        # keep the asset's layout but move it to the 3D cursor
        from mathutils import Vector

        center = sum((o.matrix_world.translation for o in roots), Vector()) / len(roots)
        for o in roots:
            o.location += cursor - center
    for o in bpy.context.view_layer.objects:
        o.select_set(o in objs)
    if roots:
        bpy.context.view_layer.objects.active = roots[0]
    return objs


def prefs_place_at_cursor():
    return bpy.context.window_manager.stash_at_cursor


def _import_file(path, asset):
    kind = asset["kind"]
    ext = os.path.splitext(path)[1].lower()
    name = asset["name"]
    if ext == ".zip":
        out = os.path.splitext(path)[0]
        with zipfile.ZipFile(path) as z:
            z.extractall(out)
        cands = [os.path.join(r, f) for r, _, fs in os.walk(out) for f in fs if os.path.splitext(f)[1].lower() in (".blend", ".glb", ".gltf", ".fbx", ".obj", ".usdz", ".usd", ".stl", ".ply", ".abc")]
        if not cands:
            raise StashError("archive contains nothing importable")
        return _import_file(sorted(cands, key=lambda p: (not p.endswith(".blend"), len(p)))[0], asset)
    if ext == ".blend":
        if kind == "material":
            with bpy.data.libraries.load(path, link=False) as (src, dst):
                dst.materials = [m for m in src.materials if m == name] or list(src.materials)[:1]
            mat = dst.materials[0] if dst.materials else None
            if mat is None:
                raise StashError("no material in downloaded file")
            targets = [o for o in bpy.context.selected_objects if o.type in ("MESH", "CURVE", "SURFACE", "META", "FONT")]
            for o in targets:
                if o.active_material_index < len(o.material_slots) and len(o.material_slots):
                    o.material_slots[o.active_material_index].material = mat
                else:
                    o.data.materials.append(mat)
            mat.use_fake_user = not targets
            return f"material '{mat.name}'" + (f" applied to {len(targets)} object(s)" if targets else " added (no selection)")
        if kind == "node_group":
            with bpy.data.libraries.load(path, link=False) as (src, dst):
                dst.node_groups = [g for g in src.node_groups if g == name] or list(src.node_groups)[:1]
            ng = dst.node_groups[0] if dst.node_groups else None
            if ng is None:
                raise StashError("no node group in downloaded file")
            ob = bpy.context.active_object
            if ng.bl_idname == "GeometryNodeTree" and ob is not None and ob.type == "MESH":
                mod = ob.modifiers.new(ng.name, "NODES")
                mod.node_group = ng
                return f"geometry nodes '{ng.name}' added to {ob.name}"
            ng.use_fake_user = True
            return f"node group '{ng.name}' appended"
        if kind == "animation":
            with bpy.data.libraries.load(path, link=False) as (src, dst):
                dst.actions = [a for a in src.actions if a == name]
            if dst.actions:
                act = dst.actions[0]
                arm = bpy.context.active_object
                if arm is not None and arm.type == "ARMATURE":
                    arm.animation_data_create()
                    arm.animation_data.action = act
                    return f"action '{act.name}' assigned to {arm.name}"
                act.use_fake_user = True
                return f"action '{act.name}' appended (select an armature to assign it)"
            # whole rig came with the export - fall through to object append
        objs = _append_objects(path, name)
        return f"{len(objs)} object(s) appended"
    if kind == "hdri":
        world = bpy.context.scene.world or bpy.data.worlds.new("World")
        bpy.context.scene.world = world
        world.use_nodes = True
        nt = world.node_tree
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(path, check_existing=True)
        bg = next((n for n in nt.nodes if n.type == "BACKGROUND"), None) or nt.nodes.new("ShaderNodeBackground")
        nt.links.new(env.outputs["Color"], bg.inputs["Color"])
        out = next((n for n in nt.nodes if n.type == "OUTPUT_WORLD"), None) or nt.nodes.new("ShaderNodeOutputWorld")
        nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
        return f"HDRI '{name}' set as world"
    if kind == "texture":
        img = bpy.data.images.load(path, check_existing=True)
        img.use_fake_user = True
        return f"image '{img.name}' loaded"
    before = set(bpy.data.objects)
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    elif ext in (".usd", ".usdz", ".usdc", ".usda"):
        bpy.ops.wm.usd_import(filepath=path)
    elif ext == ".abc":
        bpy.ops.wm.alembic_import(filepath=path, as_background_job=False)
    else:
        raise StashError(f"don't know how to import {ext}")
    new = [o for o in bpy.data.objects if o not in before]
    return f"{len(new)} object(s) imported"


class STASH_OT_import(Operator):
    bl_idname = "stash.import_asset"
    bl_label = "Import from Stash"
    bl_description = "Download this asset (converted to the best format for Blender) and add it to the scene"
    asset_id: IntProperty()
    target: StringProperty(default="")

    def execute(self, context):
        aid, forced = self.asset_id, self.target

        def work():
            c = client()
            asset = c.asset(aid)
            target = forced or _choose_target(asset)
            if not target:
                raise StashError("this asset has no downloadable format")
            d = os.path.join(cache_dir(), asset["uid"])
            path = c.export(aid, target, d)
            return asset, path

        def done(res, err):
            if err:
                return
            asset, path = res
            msg = _import_file(path, asset)
            _state["error"] = ""
            print(f"[Stash] {asset['name']}: {msg}")
            bpy.context.window_manager.stash_last = f"{asset['name']}: {msg}"

        run_async(work, done, "Downloading")
        return {"FINISHED"}


class STASH_OT_import_uid(Operator):
    bl_idname = "stash.import_uid"
    bl_label = "Import by ID"
    bl_description = "Import an asset by the ID shown on its Stash page"
    uid: StringProperty(name="Asset ID")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        try:
            it = client().find_by_uid(self.uid.strip())
        except StashError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        if not it:
            self.report({"ERROR"}, "No asset with that ID")
            return {"CANCELLED"}
        bpy.ops.stash.import_asset(asset_id=it["id"])
        return {"FINISHED"}


# ----------------------------------------------------------------------------- deliveries ("Send to Blender" in the web UI)

_last_poll = [0.0]


def receive_deliveries(quiet=False):
    def work():
        c = client()
        out = []
        for d in c.get("/api/deliveries", app="blender"):
            try:
                asset = c.asset(d["asset_id"])
                target = d.get("target") or _choose_target(asset)
                path = c.export(asset["id"], target, os.path.join(cache_dir(), asset["uid"]))
                out.append((d, asset, path, None))
            except Exception as e:  # noqa: BLE001
                out.append((d, None, None, str(e)))
        return out

    def done(res, err):
        if err:
            if quiet:
                _state["error"] = ""
            return
        c = client()
        msgs = []
        for d, asset, path, error in res:
            if error is None:
                try:
                    msgs.append(f"{asset['name']}: {_import_file(path, asset)}")
                except Exception as e:  # noqa: BLE001
                    error = str(e)
            try:
                c.post(f"/api/deliveries/{d['id']}/ack", {"status": "failed" if error else "delivered", "message": error or "", "client": f"Blender {bpy.app.version_string}"})
            except StashError:
                pass
            if error:
                _state["error"] = error
        if msgs:
            bpy.context.window_manager.stash_last = "; ".join(msgs)[:200]

    run_async(work, done, "" if quiet else "Receiving")


class STASH_OT_receive(Operator):
    bl_idname = "stash.receive"
    bl_label = "Receive sent assets"
    bl_description = "Import everything sent to Blender from the Stash web UI"

    def execute(self, context):
        receive_deliveries()
        return {"FINISHED"}


def _auto_receive_tick():
    wm = bpy.context.window_manager
    if getattr(wm, "stash_auto_receive", False) and not _state["busy"] and time.time() - _last_poll[0] > 5:
        _last_poll[0] = time.time()
        receive_deliveries(quiet=True)


# ----------------------------------------------------------------------------- push

def _bundle_selection(context, tags):
    objs = list(context.selected_objects)
    if not objs:
        raise StashError("select something first")
    # bring armatures / children along
    extra = set()
    for o in objs:
        for m in getattr(o, "modifiers", []):
            if m.type == "ARMATURE" and m.object:
                extra.add(m.object)
        extra.update(o.children_recursive)
    objs = list(dict.fromkeys(objs + list(extra)))
    active = context.active_object if context.active_object in objs else objs[0]
    name = active.name
    tmp = tempfile.mkdtemp(prefix="stash_push_")
    glb = os.path.join(tmp, "asset.glb")
    blend = os.path.join(tmp, "asset.blend")
    for o in context.view_layer.objects:
        o.select_set(o in objs)
    bpy.ops.export_scene.gltf(filepath=glb, export_format="GLB", use_selection=True, export_apply=True, export_animations=True, export_animation_mode="ACTIONS")
    bpy.data.libraries.write(blend, set(objs), path_remap="ABSOLUTE", fake_user=True, compress=True)
    bones = sum(len(o.data.bones) for o in objs if o.type == "ARMATURE")
    tris = 0
    deps = context.evaluated_depsgraph_get()
    for o in objs:
        if o.type == "MESH":
            me = o.evaluated_get(deps).to_mesh()
            me.calc_loop_triangles()
            tris += len(me.loop_triangles)
            o.evaluated_get(deps).to_mesh_clear()
    actions = []
    for o in objs:
        ad = o.animation_data
        if ad and ad.action:
            actions.append(ad.action.name)
    manifest = {
        "stash_bundle": 1,
        "origin": "blender",
        "project": bpy.path.basename(bpy.data.filepath) or "unsaved",
        "assets": [{
            "name": name,
            "kind": "skeletal_model" if bones else "model",
            "files": {"glb": "asset.glb", "blend": "asset.blend"},
            "stats": {"triangles": tris, "bones": bones},
            "animations": actions,
            "tags": tags,
            "meta": {"blender_version": bpy.app.version_string, "objects": [o.name for o in objs]},
        }],
    }
    with open(os.path.join(tmp, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    zpath = os.path.join(tmp, "bundle.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in ("manifest.json", "asset.glb", "asset.blend"):
            z.write(os.path.join(tmp, fn), fn)
    return zpath


class STASH_OT_push_selection(Operator):
    bl_idname = "stash.push_selection"
    bl_label = "Push selection to Stash"
    bl_description = "Upload the selected objects (with materials, rig and animations) as a new Stash asset"
    tags: StringProperty(name="Tags", default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        try:
            zpath = _bundle_selection(context, [t.strip() for t in self.tags.split(",") if t.strip()])
        except StashError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        def done(res, err):
            if not err:
                bpy.context.window_manager.stash_last = f"Pushed {len(res.get('assets', []))} asset(s) to Stash"

        run_async(lambda: client().upload_bundle(zpath), done, "Uploading")
        return {"FINISHED"}


class STASH_OT_push_file(Operator):
    bl_idname = "stash.push_file"
    bl_label = "Upload this .blend"
    bl_description = "Upload the saved .blend file; Stash splits it into objects, materials, actions and node groups"

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save the file first")
            return {"CANCELLED"}
        if bpy.data.is_dirty:
            bpy.ops.wm.save_mainfile()
        path = bpy.data.filepath

        def done(res, err):
            if not err:
                bpy.context.window_manager.stash_last = "Uploaded - Stash is extracting it now"

        run_async(lambda: client().upload_files([path]), done, "Uploading")
        return {"FINISHED"}


# ----------------------------------------------------------------------------- UI

class STASH_PT_browser(Panel):
    bl_label = "Stash"
    bl_idname = "STASH_PT_browser"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Stash"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        row = layout.row(align=True)
        row.prop(wm, "stash_query", text="", icon="VIEWZOOM")
        row.operator("stash.search", text="", icon="FILE_REFRESH").page = wm.stash_page
        layout.prop(wm, "stash_kind", text="")
        if _state["busy"]:
            layout.label(text=_state["busy"] + "…", icon="TIME")
        if _state["error"]:
            box = layout.box()
            box.alert = True
            box.label(text=_state["error"][:120], icon="ERROR")
        if wm.stash_last:
            layout.label(text=wm.stash_last[:80], icon="CHECKMARK")
        row = layout.row(align=True)
        row.operator("stash.receive", icon="IMPORT")
        row.prop(wm, "stash_auto_receive", text="", icon="LINKED" if wm.stash_auto_receive else "UNLINKED")

        results = _state["results"]
        if not results:
            layout.label(text="Search to browse your library", icon="INFO")
        grid = layout.grid_flow(row_major=True, columns=wm.stash_columns, even_columns=True, align=False)
        for it in results:
            cell = grid.column(align=True)
            box = cell.box()
            pv = _previews.get(str(it["id"])) if _previews else None
            if pv:
                box.template_icon(icon_value=pv.icon_id, scale=wm.stash_thumb_scale)
            else:
                box.label(text="", icon="MESH_CUBE")
            box.label(text=it["name"][:28])
            sub = box.row(align=True)
            flags = []
            if it.get("is_rigged"):
                flags.append("rig")
            if it.get("is_animated"):
                flags.append("anim")
            sub.label(text=f"{it['kind'].replace('_', ' ')} {' '.join(flags)}")
            r = box.row(align=True)
            op = r.operator("stash.import_asset", text="Import", icon="IMPORT")
            op.asset_id = it["id"]
            r.operator("stash.open_web", text="", icon="URL").asset_id = it["id"]

        total = _state["total"]
        if total > len(results) or wm.stash_page > 1:
            row = layout.row(align=True)
            prev = row.operator("stash.search", text="", icon="TRIA_LEFT")
            prev.page = max(1, wm.stash_page - 1)
            row.label(text=f"Page {wm.stash_page} · {total} assets")
            nxt = row.operator("stash.search", text="", icon="TRIA_RIGHT")
            nxt.page = wm.stash_page + 1


class STASH_PT_options(Panel):
    bl_label = "Options & push"
    bl_parent_id = "STASH_PT_browser"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Stash"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        wm = context.window_manager
        col = self.layout.column()
        col.prop(wm, "stash_at_cursor")
        col.prop(wm, "stash_columns")
        col.prop(wm, "stash_thumb_scale")
        col.prop(wm, "stash_per_page")
        col.separator()
        col.operator("stash.import_uid", icon="PASTEDOWN")
        col.operator("stash.push_selection", icon="EXPORT")
        col.operator("stash.push_file", icon="FILE_BLEND")
        col.operator("stash.open_web", text="Open Stash in browser", icon="URL").asset_id = 0


CLASSES = (
    StashPreferences,
    STASH_OT_check_connection,
    STASH_OT_search,
    STASH_OT_open_web,
    STASH_OT_import,
    STASH_OT_import_uid,
    STASH_OT_receive,
    STASH_OT_push_selection,
    STASH_OT_push_file,
    STASH_PT_browser,
    STASH_PT_options,
)


def register():
    global _previews
    for c in CLASSES:
        bpy.utils.register_class(c)
    wm = bpy.types.WindowManager
    wm.stash_query = StringProperty(name="Search", update=_on_query_change, options={"TEXTEDIT_UPDATE"} if bpy.app.version >= (4, 2, 0) else set())
    wm.stash_kind = EnumProperty(name="Type", items=KIND_ITEMS, update=_on_query_change)
    wm.stash_page = IntProperty(default=1, min=1)
    wm.stash_per_page = IntProperty(name="Results per page", default=24, min=4, max=200)
    wm.stash_columns = IntProperty(name="Columns", default=2, min=1, max=6)
    wm.stash_thumb_scale = bpy.props.FloatProperty(name="Thumbnail size", default=6.0, min=2.0, max=15.0)
    wm.stash_at_cursor = bpy.props.BoolProperty(name="Place at 3D cursor", default=True)
    wm.stash_last = StringProperty(default="")
    wm.stash_auto_receive = bpy.props.BoolProperty(name="Auto-receive", description="Poll Stash every few seconds and import assets sent to Blender", default=False)
    _previews = bpy.utils.previews.new()
    bpy.app.timers.register(_pump, persistent=True)


def unregister():
    global _previews
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
    wm = bpy.types.WindowManager
    for p in ("stash_query", "stash_kind", "stash_page", "stash_per_page", "stash_columns", "stash_thumb_scale", "stash_at_cursor", "stash_last", "stash_auto_receive"):
        if hasattr(wm, p):
            delattr(wm, p)
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
