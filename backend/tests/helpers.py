"""Build small asset folders on the fly (no Blender needed)."""

import json
import struct
from pathlib import Path

from PIL import Image


def write_png(path: Path, color=(200, 120, 40), size=32):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (size, size), color).save(path)
    return path


def write_obj(path: Path, mtl_textures: dict | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if mtl_textures is not None:
        mtl = path.with_suffix(".mtl")
        mtl_lines = ["newmtl Mat"] + [f"{k} {v}" for k, v in mtl_textures.items()]
        mtl.write_text("\n".join(mtl_lines) + "\n")
        lines += [f"mtllib {mtl.name}", "usemtl Mat"]
    lines += ["o Cube", "v 0 0 0", "v 1 0 0", "v 1 1 0", "v 0 1 0", "f 1 2 3 4"]
    path.write_text("\n".join(lines) + "\n")
    return path


def write_stl(path: Path, triangles=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"\0" * 80 + struct.pack("<I", triangles) + b"\0" * (50 * triangles)
    path.write_bytes(data)
    return path


def write_glb(path: Path, with_skin=True, animations=("Walk", "Run")):
    """A tiny but valid-enough GLB: one triangle, optional skin + animations (JSON chunk only matters)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    gltf = {
        "asset": {"version": "2.0", "generator": "stash-tests"},
        "buffers": [{"byteLength": 0}],
        "accessors": [
            {"count": 3, "type": "VEC3", "componentType": 5126},
            {"count": 3, "type": "SCALAR", "componentType": 5123},
            {"count": 2, "type": "SCALAR", "componentType": 5126, "max": [1.5], "min": [0]},
        ],
        "meshes": [{"name": "Tri", "primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "materials": [{"name": "Skin"}],
        "nodes": [{"name": "Root"}, {"name": "Bone1"}, {"name": "Bone2"}],
    }
    if with_skin:
        gltf["skins"] = [{"joints": [1, 2]}]
    if animations:
        gltf["animations"] = [{"name": n, "channels": [{"sampler": 0, "target": {"node": 1, "path": "rotation"}}], "samplers": [{"input": 2, "output": 2}]} for n in animations]
    js = json.dumps(gltf).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    total = 12 + 8 + len(js)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(js), b"JSON") + js)
    return path
