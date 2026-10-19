"""Drive the Stash Blender add-on headlessly against a running server.

    blender -b --factory-startup --python scripts/blender_addon_smoke.py -- http://localhost:8420 [token]

Installs the add-on zip the server hands out, then: search, import one asset of each kind it
finds (model, rigged model, material, texture set, HDRI, animation), and push the imported
objects back as a bundle. Prints PASS/FAIL per step and exits non-zero if any step failed.

Run it with BLENDER_USER_RESOURCES pointing at a temp folder (scripts/dev.sh does) so the add-on
is installed there and not into your real Blender preferences.
"""

import os
import sys
import tempfile
import traceback
import urllib.request

import bpy

args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SERVER = (args[0] if args else "http://localhost:8420").rstrip("/")
TOKEN = args[1] if len(args) > 1 else ""
results = []


def step(name, fn):
    try:
        detail = fn()
        results.append((name, True, detail))
        print(f"PASS {name}: {detail}")
        return detail
    except Exception as e:  # report and carry on
        results.append((name, False, str(e)))
        print(f"FAIL {name}: {e}")
        traceback.print_exc()
        return None


def install():
    zip_path = os.path.join(tempfile.mkdtemp(), "stash_bridge.zip")
    req = urllib.request.Request(f"{SERVER}/api/integrations/blender-addon.zip")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req) as r, open(zip_path, "wb") as f:
        f.write(r.read())
    bpy.ops.extensions.package_install_files(filepath=zip_path, repo="user_default", enable_on_install=True)
    mod = sys.modules.get("bl_ext.user_default.stash_bridge")
    if mod is None:
        raise RuntimeError("add-on module not loaded after install")
    p = bpy.context.preferences.addons["bl_ext.user_default.stash_bridge"].preferences
    p.server_url = SERVER
    p.token = TOKEN
    return mod


mod = step("install add-on from server", install)
if mod is None:
    sys.exit(1)
c = mod.client()
step("server status", lambda: c.status()["version"])

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.name = "StashSmokeCube"


def first(kind, **extra):
    items = c.search(kind=kind, per_page=20, **extra)["items"]
    return items[0] if items else None


def import_one(kind, label, select_cube=False, **extra):
    brief = first(kind, **extra)
    if brief is None:
        results.append((f"import {label}", None, "none in library"))
        print(f"SKIP import {label}: none in library")
        return
    a = c.asset(brief["id"])

    def run():
        if select_cube:  # materials/textures/HDRIs apply to the selection / world
            for o in bpy.context.view_layer.objects:
                o.select_set(o == cube)
            bpy.context.view_layer.objects.active = cube
        if kind == "animation":
            arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
            if arm:
                bpy.context.view_layer.objects.active = arm
        target = mod._choose_target(a)
        if target is None:
            raise RuntimeError(f"no import target in {a['targets']}")
        path = c.export(a["id"], target, os.path.join(mod.cache_dir(), a["uid"]))
        out = mod._import_file(path, a)
        return f"{a['name']} as {target} -> {out}"

    step(f"import {label}", run)


import_one("skeletal_model", "rigged model")
import_one("model", "model")
import_one("material", "material", select_cube=True)
import_one("hdri", "HDRI")
import_one("animation", "animation")
print("OBJECTS", sorted(o.name for o in bpy.data.objects))
print("CUBE MATERIALS", [m.name for m in cube.data.materials if m])


def push():
    for o in bpy.context.view_layer.objects:
        o.select_set(o == cube)
    bpy.context.view_layer.objects.active = cube
    z = mod._bundle_selection(bpy.context, ["smoke-test"])
    r = c.upload_bundle(z)
    return r


step("push selection back to Stash", push)

failed = [r for r in results if r[1] is False]
print(f"\nSUMMARY: {sum(1 for r in results if r[1])} passed, {len(failed)} failed, {sum(1 for r in results if r[1] is None)} skipped")
sys.exit(1 if failed else 0)
