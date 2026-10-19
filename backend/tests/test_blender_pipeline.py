"""Full pipeline with real Blender: STASH_TEST_BLENDER=1 STASH_BLENDER=/path/to/blender pytest tests/test_blender_pipeline.py"""

import io
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from PIL import Image

pytestmark = pytest.mark.skipif(os.environ.get("STASH_TEST_BLENDER") != "1", reason="set STASH_TEST_BLENDER=1 (needs Blender)")

FIXTURES = Path(__file__).parent / "fixtures" / "make_fixtures.py"


def _glb_json(data: bytes) -> dict:
    n = int.from_bytes(data[12:16], "little")
    return json.loads(data[20 : 20 + n])


def test_blend_extraction_and_exports(app_client, run_jobs, tmp_path):
    from stash.config import settings

    fx = tmp_path / "fx"
    subprocess.run([settings.blender, "-b", "--factory-startup", "-noaudio", "--python", str(FIXTURES), "--", str(fx)], check=True, capture_output=True)
    c = app_client
    before = sorted(p.relative_to(fx) for p in fx.rglob("*"))
    for p in [fx, *fx.rglob("*")]:  # the library is read-only for Stash, like a :ro mount
        p.chmod(p.stat().st_mode & ~0o222)
    try:
        src = c.post("/api/sources", json={"path": str(fx), "options": {"render_thumbnails": False}}).json()
        run_jobs()
    finally:
        for p in [fx, *fx.rglob("*")]:
            p.chmod(p.stat().st_mode | 0o200)
    assert sorted(p.relative_to(fx) for p in fx.rglob("*")) == before, "Stash wrote into the source library"
    items = {(a["name"], a["kind"]): a for a in c.get("/api/assets", params={"source": src["id"], "per_page": 200}).json()["items"]}
    names = {k[0] for k in items}
    assert {"NoiseCube", "Crate", "Worm", "ScatterPlane", "ProceduralMarble", "Wiggle", "Bend", "ScatterCubes"} <= names, names
    worm = items[("Worm", "skeletal_model")]
    assert worm["is_rigged"] and worm["is_animated"] and worm["has_preview"]
    marble = c.get(f"/api/assets/{items[('ProceduralMarble', 'material')]['id']}").json()
    assert marble["is_procedural"] and any(n["type"] == "ShaderNodeTexNoise" for n in marble["meta"]["graph"]["nodes"])
    wiggle = c.get(f"/api/assets/{items[('Wiggle', 'animation')]['id']}").json()
    assert any(l["relation"] == "animates" and l["asset"]["name"] == "Worm" for l in wiggle["links"])
    fbx = items[("worm", "skeletal_model")]  # worm.fbx gets a GLB preview through Blender
    assert fbx["has_preview"]

    for asset, target, check in (
        (worm, "blend", lambda d: d.startswith((b"BLENDER", b"\x28\xb5\x2f\xfd", b"\x1f\x8b"))),
        (worm, "fbx", lambda d: d.startswith(b"Kaydara FBX Binary")),
        (items[("Crate", "model")], "unreal", lambda d: "Crate/stash_manifest.json" in zipfile.ZipFile(io.BytesIO(d)).namelist()),
        (items[("ProceduralMarble", "material")], "glb", lambda d: d[:4] == b"glTF"),
        (items[("cube", "model")], "stl", lambda d: len(d) > 84),
        # a material split from a .blend carries its textures through links, not files of its own
        (items[("CrateMat", "material")], "unreal", lambda d: any(m["maps"] for m in json.loads(zipfile.ZipFile(io.BytesIO(d)).read("CrateMat/stash_manifest.json"))["materials"])),
        # EXR is decoded by Blender, then written in the requested format at its own size
        # one Painter texture set per material slot, not the first set everywhere
        (items[("SM_Arch_LP", "model")], "glb", lambda d: {"SM_Arch_LP_lambert1", "SM_Arch_LP_lambert2"} <= {m["name"] for m in _glb_json(d)["materials"]}),
        # the glTF importer's bone-shape helper ("Icosphere") must not come along
        (next(a for a in c.get("/api/assets", params={"source": src["id"], "q": "worm", "per_page": 50}).json()["items"] if a["format"] == "glb"), "fbx", lambda d: d.startswith(b"Kaydara FBX Binary") and b"Icosphere" not in d),
        (next(a for (n, _), a in items.items() if n == "sky"), "jpg", lambda d: Image.open(io.BytesIO(d)).format == "JPEG" and Image.open(io.BytesIO(d)).size == (64, 32)),
    ):
        r = c.post(f"/api/assets/{asset['id']}/export", json={"target": target}).json()
        run_jobs()
        job = c.get(f"/api/jobs/{r['job_id']}").json()
        assert job["status"] == "done", (target, job["message"])
        data = c.get(f"/api/files/{job['result']['file_id']}").content
        assert check(data), target
