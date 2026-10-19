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
