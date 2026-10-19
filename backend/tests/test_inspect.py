from stash.ingest.inspect import inspect_file

from helpers import write_glb, write_obj, write_png, write_stl


def test_glb(tmp_path):
    info = inspect_file(write_glb(tmp_path / "rig.glb"))
    assert info["bones"] == 2
    assert info["triangles"] == 1
    assert [a["name"] for a in info["animations"]] == ["Walk", "Run"]
    assert info["animations"][0]["duration"] == 1.5


def test_obj_with_mtl(tmp_path):
    write_png(tmp_path / "tex" / "crate_albedo.png")
    info = inspect_file(write_obj(tmp_path / "crate.obj", {"map_Kd": "tex/crate_albedo.png"}))
    assert info["triangles"] == 2
    assert info["textures"] == ["tex/crate_albedo.png"]
    assert info["materials"] == ["Mat"]


def test_stl(tmp_path):
    assert inspect_file(write_stl(tmp_path / "part.stl", 7))["triangles"] == 7


def test_broken_file_is_reported_not_raised(tmp_path):
    p = tmp_path / "broken.glb"
    p.write_bytes(b"not a glb")
    assert "inspect_error" in inspect_file(p)


def test_fbx_default_take_is_not_an_animation():
    """Static FBX from Maya/Blender carry an empty "Take 001" stack: only stacks with curves count."""
    from stash.ingest.inspect import inspect_fbx_ascii

    static = 'Objects:  {\n  Geometry: 1, "Geometry::", "Mesh" {}\n  AnimationStack: 2, "AnimStack::Take 001", "" {}\n}\n'
    animated = static + '  AnimationCurve: 3, "AnimCurve::", "" {}\n'
    assert inspect_fbx_ascii(static)["animations"] == []
    assert [a["name"] for a in inspect_fbx_ascii(animated)["animations"]] == ["Take 001"]
