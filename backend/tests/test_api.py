"""End-to-end API tests on a generated asset folder (no Blender required)."""

import io
import json
import zipfile

from helpers import write_glb, write_obj, write_png, write_stl


def _library(root):
    # an asset pack: model + its PBR set in a Textures folder
    write_obj(root / "Crate" / "SM_Crate.obj", {"map_Kd": "Textures/T_Crate_BaseColor.png"})
    write_png(root / "Crate" / "Textures" / "T_Crate_BaseColor.png")
    write_png(root / "Crate" / "Textures" / "T_Crate_Normal.png", (128, 128, 255))
    write_png(root / "Crate" / "Textures" / "T_Crate_Roughness.png", (90, 90, 90))
    write_png(root / "Crate" / "preview.png", (10, 200, 10))
    # a standalone texture set (Poly Haven style naming)
    for ch in ("diff", "nor_gl", "rough"):
        write_png(root / "Surfaces" / "wood_floor" / f"wood_floor_{ch}_2k.png")
    # a rigged animated glb, an STL, a loose decal
    write_glb(root / "Characters" / "Hero.glb")
    write_stl(root / "Print" / "bracket.stl", 12)
    write_png(root / "Decals" / "graffiti.png")
    # a Blender extension zip and a legacy add-on file
    with zipfile.ZipFile(root / "Addons" / "my_tool.zip" if (root / "Addons").mkdir() is None else None, "w") as z:
        z.writestr(
            "blender_manifest.toml",
            'schema_version = "1.0.0"\nid = "my_tool"\nversion = "1.2.0"\nname = "My Tool"\ntagline = "Does things"\n'
            'maintainer = "Me"\ntype = "add-on"\nblender_version_min = "4.2.0"\nlicense = ["SPDX:GPL-3.0-or-later"]\ntags = ["Mesh"]\n',
        )
        z.writestr("__init__.py", "def register(): pass\ndef unregister(): pass\n")
    (root / "Addons" / "old_addon.py").write_text('bl_info = {"name": "Old Addon", "version": (0, 3), "blender": (3, 6, 0), "category": "Object"}\n')
    # an asset pack zip with a model inside
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Barrel/Barrel.obj", "o Barrel\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    (root / "Packs").mkdir()
    (root / "Packs" / "barrels.zip").write_bytes(buf.getvalue())


def _assets(client, **params):
    r = client.get("/api/assets", params={"per_page": 200, **params})
    assert r.status_code == 200, r.text
    return {a["name"]: a for a in r.json()["items"]}


def test_full_flow(app_client, run_jobs, tmp_path):
    c = app_client
    root = tmp_path / "lib"
    root.mkdir()
    _library(root)

    r = c.post("/api/sources", json={"path": str(root), "name": "Test library"})
    assert r.status_code == 200, r.text
    source = r.json()
    assert source["kind"] == "folder"
    run_jobs()

    assets = _assets(c, source=source["id"])
    names = set(assets)
    assert {"SM_Crate", "Hero", "bracket", "graffiti", "My Tool", "Old Addon", "Barrel"} <= names, names
    assert assets["Hero"]["kind"] == "skeletal_model"
    assert assets["Hero"]["is_animated"] and assets["Hero"]["is_rigged"]
    assert assets["Hero"]["has_preview"]  # glb is its own preview
    assert assets["bracket"]["poly_count"] == 12

    # PBR sets became materials, the crate model is linked to its set
    mats = {n: a for n, a in assets.items() if a["kind"] == "material"}
    assert {"Crate", "wood_floor"} <= set(mats), mats.keys()
    crate = c.get(f"/api/assets/{assets['SM_Crate']['id']}").json()
    assert any(l["relation"] == "uses_material" and l["asset"]["name"] == "Crate" for l in crate["links"])
    assert any(f["role"] == "gallery" for f in crate["files"])
    assert crate["has_textures"]
    wood = c.get(f"/api/assets/{mats['wood_floor']['id']}").json()
    assert set(wood["meta"]["channels"]) == {"base_color", "normal", "roughness"}
    assert wood["thumbnail"]  # pillow thumbnail job ran

    # textures that belong to a set are not duplicated as loose texture assets
    assert "T_Crate_Normal" not in names
    assert assets["graffiti"]["kind"] == "texture"

    # search
    hits = _assets(c, q="crate")
    assert "SM_Crate" in hits
    assert _assets(c, q="anim")["Hero"]
    assert "Hero" in _assets(c, flags="rigged")

    # tags + bulk + facets
    hero_id = assets["Hero"]["id"]
    assert c.post(f"/api/assets/{hero_id}/tags", json={"tags": ["Character", "fantasy"]}).json()["tags"] == ["character", "fantasy"]
    assert "Hero" in _assets(c, tags="character")
    assert "Hero" in _assets(c, q="fantasy")
    c.post("/api/assets/bulk", json={"ids": [assets["bracket"]["id"], assets["Barrel"]["id"]], "add_tags": ["props"], "favorite": True})
    assert set(_assets(c, tags="props")) == {"bracket", "Barrel"}
    facets = c.get("/api/facets").json()
    assert facets["kinds"]["material"] >= 2
    assert facets["flags"]["favorite"] == 2
    tag_list = {t["name"]: t for t in c.get("/api/tags").json()}
    assert tag_list["props"]["count"] == 2

    # rename sticks across rescans
    c.patch(f"/api/assets/{hero_id}", json={"name": "Hero Knight", "rating": 4})
    c.post(f"/api/sources/{source['id']}/scan")
    run_jobs()
    assert "Hero Knight" in _assets(c, source=source["id"])

    # projects
    p = c.post("/api/projects", json={"name": "Game A", "color": "#7c5cff"}).json()
    c.post(f"/api/projects/{p['id']}/assets", json={"ids": [hero_id, assets["SM_Crate"]["id"]]})
    assert set(_assets(c, project=p["id"])) == {"Hero Knight", "SM_Crate"}
    assert c.get("/api/projects").json()[0]["count"] == 2

    # export of original files (no Blender needed) + project package
    r = c.post(f"/api/assets/{assets['SM_Crate']['id']}/export", json={"target": "original"}).json()
    run_jobs()
    job = c.get(f"/api/jobs/{r['job_id']}").json()
    assert job["status"] == "done", job
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/files/{job['result']['file_id']}").content))
    assert {"SM_Crate/SM_Crate.obj", "SM_Crate/SM_Crate.mtl", "SM_Crate/Textures/T_Crate_BaseColor.png"} <= set(z.namelist())
    r = c.post(f"/api/projects/{p['id']}/package", json={"target": "original"}).json()
    run_jobs()
    pkg = c.get(f"/api/jobs/{r['job_id']}/download")
    assert pkg.status_code == 200
    assert "stash_project.json" in zipfile.ZipFile(io.BytesIO(pkg.content)).namelist()

    # removing an asset hides it from future rescans too
    c.delete(f"/api/assets/{assets['graffiti']['id']}")
    c.post(f"/api/sources/{source['id']}/scan")
    run_jobs()
    assert "graffiti" not in _assets(c, source=source["id"])

    # relative file serving for viewers (obj -> mtl -> texture)
    rr = c.get(f"/api/assets/{assets['SM_Crate']['id']}/raw/Textures/T_Crate_BaseColor.png")
    assert rr.status_code == 200 and rr.headers["content-type"] == "image/png"
    assert c.get(f"/api/assets/{assets['SM_Crate']['id']}/raw/../../../etc/passwd").status_code in (403, 404)


def test_blender_extension_repository(app_client, run_jobs):
    idx = app_client.get("/api/blender/extensions/index.json").json()
    assert idx["version"] == "v1"
    ext = {e["id"]: e for e in idx["data"]}
    assert "my_tool" in ext
    e = ext["my_tool"]
    assert e["version"] == "1.2.0" and e["archive_hash"].startswith("sha256:")
    archive = app_client.get(e["archive_url"].split("://", 1)[1].split("/", 1)[1].join(["/", ""]))
    assert archive.status_code == 200


def test_deliveries_and_bundle_ingest(app_client, run_jobs, tmp_path):
    c = app_client
    any_asset = c.get("/api/assets", params={"per_page": 1}).json()["items"][0]
    ids = c.post("/api/deliveries", json={"app": "blender", "asset_ids": [any_asset["id"]]}).json()["ids"]
    pending = c.get("/api/deliveries", params={"app": "blender"}).json()
    assert [d["id"] for d in pending] == ids
    c.post(f"/api/deliveries/{ids[0]}/ack", json={"status": "delivered", "client": "pytest"})
    assert c.get("/api/deliveries", params={"app": "blender"}).json() == []

    # a bundle pushed by the Unreal plugin creates a new asset with its files
    buf = io.BytesIO()
    glb = write_glb(tmp_path / "SM_Rock.glb", with_skin=False, animations=())
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({
            "stash_bundle": 1, "origin": "unreal", "project": "MyGame", "engine_version": "5.4",
            "assets": [{"name": "SM_Rock", "kind": "model", "ue_package": "/Game/Env/SM_Rock", "ue_class": "StaticMesh",
                        "files": {"glb": "SM_Rock/SM_Rock.glb", "textures": {"base_color": "SM_Rock/T_Rock_D.png"}}, "tags": ["rocks"]}],
        }))
        z.write(glb, "SM_Rock/SM_Rock.glb")
        z.write(write_png(tmp_path / "T_Rock_D.png"), "SM_Rock/T_Rock_D.png")
    r = c.post("/api/ingest/bundle", files={"file": ("bundle.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 200, r.text
    res = r.json()["assets"][0]
    assert res["matched"] is False
    a = c.get(f"/api/assets/{res['id']}").json()
    assert a["meta"]["ue_package"] == "/Game/Env/SM_Rock"
    assert a["preview"] is not None and a["has_textures"]
    assert "rocks" in a["tags"]


def test_auth_required_when_password_set(app_client, monkeypatch):
    from stash.config import settings

    monkeypatch.setattr(settings, "password", "secret")
    assert app_client.get("/api/assets").status_code == 401
    assert app_client.get("/api/assets", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert app_client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert app_client.post("/api/auth/login", json={"password": "secret"}).status_code == 200
    assert app_client.get("/api/assets").status_code == 200  # cookie now set
    app_client.cookies.clear()


def test_storage_cleanup(app_client, run_jobs, monkeypatch, tmp_path):
    import os
    import time

    from stash.config import settings

    c = app_client
    # an orphaned file older than the grace period is collected, a referenced one is kept
    orphan = settings.store_dir / "zz" / "zz" / "orphan.bin"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"x" * 1000)
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    before = c.get("/api/maintenance/storage").json()
    assert before["store_bytes"] >= 1000
    r = c.post("/api/maintenance/cleanup").json()
    assert r["gc"]["removed"] >= 1 and not orphan.exists()
    assert r["gc"]["kept"] > 0  # thumbnails etc. from earlier tests survive

    # export cache cap: with a tiny cap the oldest export is evicted after the next export
    monkeypatch.setattr(settings, "export_cache_gb", 1e-9)
    asset = next(a for a in c.get("/api/assets", params={"per_page": 200}).json()["items"] if a["format"] == "stl")
    r = c.post(f"/api/assets/{asset['id']}/export", json={"target": "original", "force": True}).json()
    run_jobs()
    files = c.get(f"/api/assets/{asset['id']}").json()["exports"]
    assert files == []  # evicted straight away by the 1-byte cap


def test_packs(app_client, run_jobs):
    c = app_client
    packs = {p["name"]: p for p in c.get("/api/packs").json()}
    assert {"Crate", "Characters", "barrels"} <= set(packs), packs.keys()
    assert packs["Crate"]["count"] >= 1 and packs["Crate"]["description"] == ""

    detail = c.get("/api/packs/Crate").json()
    assert "SM_Crate" in detail["names"] and detail["kinds"]["model"] == 1
    assert detail["sources"] and detail["gallery"]  # preview.png shipped next to the crate
    assert c.get("/api/packs/Surfaces").json()["samples"][0]["name"] == "wood_floor"
    assert c.get("/api/packs/NoSuchPack").status_code == 404

    # a pack description makes every asset of the pack findable, without touching the assets
    assert "SM_Crate" not in _assets(c, q="splintered")
    r = c.put("/api/packs/Crate", json={"description": "Splintered wooden shipping crates.", "keywords": ["Cargo", "cargo", "dock"],
                                         "meta": {"by": "lmstudio", "model": "test"}})
    assert r.status_code == 200 and r.json()["keywords"] == ["cargo", "dock"]
    assert "SM_Crate" in _assets(c, q="splintered")
    assert "SM_Crate" in _assets(c, q="dock")
    assert "Hero Knight" not in _assets(c, q="dock")
    assert {p["name"] for p in c.get("/api/packs", params={"described": True}).json()} == {"Crate"}
    asset = c.get(f"/api/assets/{_assets(c, q='SM_Crate')['SM_Crate']['id']}").json()
    assert asset["pack"]["description"].startswith("Splintered")

    # survives a full reindex, goes away when deleted
    c.post("/api/maintenance/reindex")
    run_jobs()
    assert "SM_Crate" in _assets(c, q="splintered")
    c.delete("/api/packs/Crate")
    assert "SM_Crate" not in _assets(c, q="splintered")


def test_scaled_image(app_client):
    c = app_client
    detail = c.get("/api/packs/Crate").json()
    r = c.get(detail["gallery"][0], params={"max": 32})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    from PIL import Image

    assert max(Image.open(io.BytesIO(r.content)).size) <= 32
