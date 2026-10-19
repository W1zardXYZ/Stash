from helpers import write_obj, write_png


def test_sibling_meshes_and_textures_folders(app_client, run_jobs, tmp_path):
    """UE-style pack layout: Pack/Meshes/SM_X.obj + Pack/Textures/T_X_*.png, no MTL references."""
    pack = tmp_path / "RockPack"
    for n in ("Rock_01", "Rock_02"):
        write_obj(pack / "Meshes" / f"SM_{n}.obj")
        for ch in ("BaseColor", "Normal", "ORM"):
            write_png(pack / "Textures" / f"T_{n}_{ch}.png")
    src = app_client.post("/api/sources", json={"path": str(tmp_path)}).json()
    run_jobs()
    items = {a["name"]: a for a in app_client.get("/api/assets", params={"source": src["id"], "per_page": 100}).json()["items"]}
    for n in ("Rock_01", "Rock_02"):
        model = app_client.get(f"/api/assets/{items['SM_' + n]['id']}").json()
        mats = [l["asset"]["name"] for l in model["links"] if l["relation"] == "uses_material"]
        assert mats == [n], (n, mats)
        assert {f["label"] for f in model["files"] if f["role"] == "texture"} == {"base_color", "normal", "orm"}
        assert model["meta"]["pack"] == "RockPack"


def test_megascans_sidecar_json(app_client, run_jobs, tmp_path):
    import json

    d = tmp_path / "Megascans" / "Mossy_Rock_vkpgcfd"
    write_obj(d / "vkpgcfd_LOD0.obj")
    for ch in ("Albedo", "Normal", "Roughness"):
        write_png(d / f"vkpgcfd_4K_{ch}.png")
    (d / "vkpgcfd.json").write_text(json.dumps({
        "id": "vkpgcfd", "name": "Mossy Rock", "tags": ["rock", "moss"], "categories": ["3d", "nature"],
        "semanticTags": {"asset_type": "3D asset", "color": ["green", "gray"], "environment": ["forest"]},
        "meta": [{"key": "height", "name": "Height", "value": "1.2 m"}],
    }))
    src = app_client.post("/api/sources", json={"path": str(tmp_path / "Megascans")}).json()
    run_jobs()
    items = {a["name"]: a for a in app_client.get("/api/assets", params={"source": src["id"]}).json()["items"]}
    model = app_client.get(f"/api/assets/{items['vkpgcfd_LOD0']['id']}").json()
    assert {"rock", "moss", "nature", "forest", "green"} <= set(model["tags"])
    assert model["meta"]["sidecar"]["name"] == "Mossy Rock"
    assert model["meta"]["sidecar"]["properties"]["Height"] == "1.2 m"
    assert "forest" in items["vkpgcfd"]["tags"]  # the texture set too


def test_blend_render_beats_embedded_preview(app_client, run_jobs, tmp_path, monkeypatch):
    """The parent .blend shows its rendered model, not the small embedded preview, also after a rescan."""
    import io

    from PIL import Image

    from stash.db import session_scope
    from stash.ingest import scanner
    from stash.models import Asset
    from stash.processing import ingest_manifest

    def png(color):
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), color).save(buf, "PNG")
        return buf.getvalue()

    monkeypatch.setattr(scanner, "_blend_thumbnail", lambda path: png((0, 0, 255)))
    (tmp_path / "chair.blend").write_bytes(b"BLENDER-v405")
    src = app_client.post("/api/sources", json={"path": str(tmp_path)}).json()
    run_jobs()
    parent_id = app_client.get("/api/assets", params={"source": src["id"]}).json()["items"][0]["id"]

    out = tmp_path.parent / "extract_out"
    out.mkdir()
    (out / "chair.png").write_bytes(png((255, 0, 0)))
    manifest = {"assets": [{"key": "OB:Chair", "name": "Chair", "kind": "model", "files": {"thumbnail": "chair.png"}}]}
    with session_scope() as db:
        ingest_manifest(db, db.get(Asset, parent_id), manifest, out)

    rendered = app_client.get(f"/api/assets/{parent_id}").json()["thumbnail"]
    child = next(a for a in app_client.get("/api/assets", params={"source": src["id"]}).json()["items"] if a["name"] == "Chair")
    assert rendered == child["thumbnail"]
    app_client.post(f"/api/sources/{src['id']}/scan")
    run_jobs()
    assert app_client.get(f"/api/assets/{parent_id}").json()["thumbnail"] == rendered


def test_unreal_project_loose_source_files(app_client, run_jobs, tmp_path):
    """Fab packs ship FBX/textures next to the packages: indexed like a folder, linked to their package."""
    proj = tmp_path / "Stylized Project"
    (proj / "Stylized.uproject").parent.mkdir(parents=True)
    (proj / "Stylized.uproject").write_text('{"EngineAssociation": "5.8"}')
    meshes = proj / "Content" / "ParisStreet" / "Meshes"
    meshes.mkdir(parents=True)
    (meshes / "SM_Lamp.uasset").write_bytes(b"\0" * 64)  # unreadable header -> class from the SM_ prefix
    write_obj(meshes / "SM_Lamp.obj")
    write_obj(meshes / "SM_Bench.obj")
    for ch in ("BaseColor", "Normal"):
        write_png(proj / "Content" / "ParisStreet" / "Textures" / f"T_Wall_{ch}.png")
    write_obj(proj / "Saved" / "Autosaves" / "SM_Junk.obj")  # editor folders stay ignored

    src = app_client.post("/api/sources", json={"path": str(proj)}).json()
    run_jobs()
    items = app_client.get("/api/assets", params={"source": src["id"], "per_page": 100}).json()["items"]
    by = {(a["name"], a["format"]): a for a in items}
    assert ("SM_Junk", "obj") not in by
    lamp_obj, lamp_pkg, bench = by[("SM_Lamp", "obj")], by[("SM_Lamp", "uasset")], by[("SM_Bench", "obj")]
    assert lamp_obj["kind"] == bench["kind"] == "model"
    assert any(a["kind"] == "material" and a["format"] == "pbr" for a in items)  # T_Wall_* became a set
    for a in (lamp_obj, lamp_pkg, bench):
        assert app_client.get(f"/api/assets/{a['id']}").json()["pack"]["name"] == "ParisStreet"
    links = app_client.get(f"/api/assets/{lamp_obj['id']}").json()["links"]
    assert [(l["relation"], l["direction"], l["asset"]["id"]) for l in links if l["relation"] == "source_of"] == [("source_of", "out", lamp_pkg["id"])]
    assert not [l for l in app_client.get(f"/api/assets/{bench['id']}").json()["links"] if l["relation"] == "source_of"]
    # "Re-extract" only re-renders what Blender can read: the two OBJ files, not the .uasset
    assert app_client.post(f"/api/sources/{src['id']}/extract-all").json()["jobs"] == 2
    assert app_client.post(f"/api/assets/{lamp_pkg['id']}/reprocess").json()["jobs"] == []


def test_substance_painter_texture_sets(app_client, run_jobs, tmp_path):
    """Painter's default export "<mesh>_<texture set>_<channel>.<udim>" belongs to the longest matching mesh."""
    pack = tmp_path / "Street"
    write_obj(pack / "Meshes" / "SM_Arch_LP.obj")
    write_obj(pack / "Meshes" / "SM_Arch.obj")
    for ts in ("lambert1", "lambert2"):
        for ch in ("BaseColor", "Normal", "OcclusionRoughnessMetallic"):
            write_png(pack / "Textures" / f"SM_Arch_LP_{ts}_{ch}.1001.png")
    src = app_client.post("/api/sources", json={"path": str(tmp_path)}).json()
    run_jobs()
    items = {a["name"]: a for a in app_client.get("/api/assets", params={"source": src["id"], "per_page": 100}).json()["items"]}

    def mats(name):
        return sorted(l["asset"]["name"] for l in app_client.get(f"/api/assets/{items[name]['id']}").json()["links"] if l["relation"] == "uses_material")

    assert len(mats("SM_Arch_LP")) == 2, mats("SM_Arch_LP")
    assert mats("SM_Arch") == []
