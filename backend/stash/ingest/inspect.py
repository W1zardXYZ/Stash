"""Lightweight, dependency-free inspectors for common 3D formats.

They extract what the library needs for indexing (mesh/poly counts, bones, animations,
materials and referenced texture files) without loading a full DCC. Anything they can't
figure out is filled in later by the Blender extraction job.
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path
from typing import Any


def inspect_file(path: Path) -> dict[str, Any]:
    ext = path.suffix.lower()
    try:
        if ext in (".gltf", ".glb"):
            return inspect_gltf(path)
        if ext == ".fbx":
            return inspect_fbx(path)
        if ext == ".obj":
            return inspect_obj(path)
        if ext == ".stl":
            return inspect_stl(path)
        if ext == ".ply":
            return inspect_ply(path)
        if ext == ".dae":
            return inspect_dae(path)
    except Exception as e:  # inspectors are best effort
        return {"inspect_error": f"{type(e).__name__}: {e}"}
    return {}


# --------------------------------------------------------------------------- glTF

def _read_gltf_json(path: Path) -> dict:
    data = path.read_bytes() if path.suffix.lower() == ".glb" else None
    if data is None:
        return json.loads(path.read_text("utf-8"))
    magic, _version, _length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise ValueError("not a GLB")
    chunk_len, chunk_type = struct.unpack_from("<I4s", data, 12)
    if chunk_type != b"JSON":
        raise ValueError("GLB first chunk is not JSON")
    return json.loads(data[20 : 20 + chunk_len].decode("utf-8"))


def inspect_gltf(path: Path) -> dict[str, Any]:
    g = _read_gltf_json(path)
    accessors = g.get("accessors", [])
    tris = 0
    verts = 0
    for mesh in g.get("meshes", []):
        for prim in mesh.get("primitives", []):
            pos = prim.get("attributes", {}).get("POSITION")
            if pos is not None and pos < len(accessors):
                verts += accessors[pos].get("count", 0)
            mode = prim.get("mode", 4)
            if mode == 4:
                idx = prim.get("indices")
                if idx is not None and idx < len(accessors):
                    tris += accessors[idx].get("count", 0) // 3
                elif pos is not None and pos < len(accessors):
                    tris += accessors[pos].get("count", 0) // 3
    skins = g.get("skins", [])
    bones = max((len(s.get("joints", [])) for s in skins), default=0)
    anims = []
    for a in g.get("animations", []):
        dur = 0.0
        for ch in a.get("channels", []):
            s = a.get("samplers", [])[ch.get("sampler", 0)] if a.get("samplers") else None
            if s and s.get("input") is not None and s["input"] < len(accessors):
                mx = accessors[s["input"]].get("max")
                if mx:
                    dur = max(dur, float(mx[0]))
        anims.append({"name": a.get("name") or f"Animation {len(anims)}", "duration": round(dur, 3)})
    images = []
    for img in g.get("images", []):
        uri = img.get("uri")
        if uri and not uri.startswith("data:"):
            images.append(uri)
        else:
            images.append(img.get("name") or "(embedded)")
    materials = [m.get("name") or f"Material {i}" for i, m in enumerate(g.get("materials", []))]
    morphs = sum(1 for m in g.get("meshes", []) for p in m.get("primitives", []) if p.get("targets"))
    return {
        "meshes": len(g.get("meshes", [])),
        "nodes": len(g.get("nodes", [])),
        "triangles": tris,
        "vertices": verts,
        "bones": bones,
        "skins": len(skins),
        "animations": anims,
        "materials": materials,
        "textures": images,
        "morph_targets": morphs,
        "generator": g.get("asset", {}).get("generator", ""),
    }


# --------------------------------------------------------------------------- FBX

_FBX_MAGIC = b"Kaydara FBX Binary  \x00"


class _FbxNode:
    __slots__ = ("name", "props", "children")

    def __init__(self, name: str, props: list, children: list):
        self.name = name
        self.props = props
        self.children = children

    def find(self, name: str):
        for c in self.children:
            if c.name == name:
                yield c


def _fbx_read_prop(data: memoryview, off: int, want_arrays: bool) -> tuple[Any, int]:
    t = chr(data[off])
    off += 1
    if t == "Y":
        return struct.unpack_from("<h", data, off)[0], off + 2
    if t == "C":
        return bool(data[off]), off + 1
    if t == "I":
        return struct.unpack_from("<i", data, off)[0], off + 4
    if t == "F":
        return struct.unpack_from("<f", data, off)[0], off + 4
    if t == "D":
        return struct.unpack_from("<d", data, off)[0], off + 8
    if t == "L":
        return struct.unpack_from("<q", data, off)[0], off + 8
    if t in "fdlib":
        length, encoding, comp_len = struct.unpack_from("<III", data, off)
        off += 12
        raw = bytes(data[off : off + comp_len])
        off += comp_len
        if not want_arrays:
            return ("array", length), off
        if encoding == 1:
            raw = zlib.decompress(raw)
        fmt = {"f": "f", "d": "d", "l": "q", "i": "i", "b": "b"}[t]
        return list(struct.unpack(f"<{length}{fmt}", raw[: length * struct.calcsize(fmt)])), off
    if t in "SR":
        length = struct.unpack_from("<I", data, off)[0]
        off += 4
        raw = bytes(data[off : off + length])
        off += length
        if t == "S":
            return raw.decode("utf-8", "replace"), off
        return b"", off
    raise ValueError(f"unknown FBX property type {t!r}")


# Arrays we actually need decoded (everything else is skipped for speed).
_FBX_ARRAY_NODES = {"PolygonVertexIndex"}


def _fbx_read_node(data: memoryview, off: int, version: int) -> tuple[_FbxNode | None, int]:
    if version >= 7500:
        end, nprops, _plen = struct.unpack_from("<QQQ", data, off)
        off += 24
    else:
        end, nprops, _plen = struct.unpack_from("<III", data, off)
        off += 12
    name_len = data[off]
    off += 1
    if end == 0:
        return None, off + name_len
    name = bytes(data[off : off + name_len]).decode("ascii", "replace")
    off += name_len
    want = name in _FBX_ARRAY_NODES
    props = []
    for _ in range(nprops):
        v, off = _fbx_read_prop(data, off, want)
        props.append(v)
    children = []
    while off < end:
        child, off = _fbx_read_node(data, off, version)
        if child is None:
            break
        children.append(child)
    return _FbxNode(name, props, children), end


def _fbx_clean_name(s: str) -> str:
    # Binary FBX names look like "Name\x00\x01Model"
    return s.split("\x00\x01")[0] if isinstance(s, str) else str(s)


def inspect_fbx(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if not raw.startswith(_FBX_MAGIC):
        return inspect_fbx_ascii(raw.decode("utf-8", "replace"))
    version = struct.unpack_from("<I", raw, 23)[0]
    data = memoryview(raw)
    off = 27
    top: list[_FbxNode] = []
    while off < len(data) - 13:
        node, off = _fbx_read_node(data, off, version)
        if node is None:
            break
        top.append(node)
    objects = next((n for n in top if n.name == "Objects"), None)
    res: dict[str, Any] = {"fbx_version": version, "materials": [], "textures": [], "animations": []}
    if objects is None:
        return res
    tris = 0
    meshes = 0
    bones = 0
    skins = 0
    blendshapes = 0
    curves = 0
    models = []
    for n in objects.children:
        if n.name == "Geometry" and len(n.props) >= 3 and n.props[2] == "Mesh":
            meshes += 1
            for pvi in n.find("PolygonVertexIndex"):
                idx = pvi.props[0] if pvi.props else []
                if isinstance(idx, list):
                    poly_len = 0
                    for v in idx:
                        poly_len += 1
                        if v < 0:
                            tris += max(poly_len - 2, 0)
                            poly_len = 0
        elif n.name == "Geometry" and len(n.props) >= 3 and n.props[2] == "Shape":
            blendshapes += 1
        elif n.name == "Model" and len(n.props) >= 3:
            if n.props[2] == "LimbNode":
                bones += 1
            elif n.props[2] == "Mesh":
                models.append(_fbx_clean_name(n.props[1]))
        elif n.name == "Material" and len(n.props) >= 2:
            res["materials"].append(_fbx_clean_name(n.props[1]))
        elif n.name in ("Texture", "Video"):
            for key in ("RelativeFilename", "FileName", "Filename"):
                for c in n.find(key):
                    if c.props and isinstance(c.props[0], str) and c.props[0]:
                        res["textures"].append(c.props[0])
                        break
        elif n.name == "Deformer" and len(n.props) >= 3 and n.props[2] == "Skin":
            skins += 1
        elif n.name == "AnimationStack" and len(n.props) >= 2:
            name = _fbx_clean_name(n.props[1])
            dur = 0.0
            for p70 in n.find("Properties70"):
                for p in p70.find("P"):
                    if p.props and p.props[0] in ("LocalStop", "ReferenceStop") and len(p.props) >= 5:
                        try:
                            dur = max(dur, float(p.props[4]) / 46186158000.0)
                        except (TypeError, ValueError):
                            pass
            res["animations"].append({"name": name, "duration": round(dur, 3)})
        elif n.name == "AnimationCurve":
            curves += 1
    if not curves:
        res["animations"] = []  # the default take Maya/Max/Blender write into static files ("Take 001")
    # de-duplicate texture paths (Texture and Video nodes both reference the file)
    seen = set()
    res["textures"] = [t for t in res["textures"] if not (t in seen or seen.add(t))]
    res.update(
        meshes=meshes,
        triangles=tris,
        bones=bones,
        skins=skins,
        morph_targets=blendshapes,
        mesh_names=models[:200],
    )
    return res


def inspect_fbx_ascii(text: str) -> dict[str, Any]:
    res: dict[str, Any] = {"fbx_version": "ascii"}
    res["meshes"] = len(re.findall(r'Geometry:\s*\d+,\s*"[^"]*",\s*"Mesh"', text))
    res["bones"] = len(re.findall(r'Model:\s*\d+,\s*"[^"]*",\s*"LimbNode"', text))
    res["skins"] = len(re.findall(r'Deformer:\s*\d+,\s*"[^"]*",\s*"Skin"', text))
    res["materials"] = [m.split("::")[-1] for m in re.findall(r'Material:\s*\d+,\s*"([^"]*)"', text)]
    res["textures"] = sorted(set(re.findall(r'RelativeFilename:\s*"([^"]+)"', text)))
    res["animations"] = [{"name": a.split("::")[-1], "duration": 0} for a in re.findall(r'AnimationStack:\s*\d+,\s*"([^"]*)"', text)]
    if not re.search(r"AnimationCurve:\s*\d+", text):
        res["animations"] = []  # only the default take of a static file
    return res


# --------------------------------------------------------------------------- OBJ / STL / PLY / DAE

def inspect_obj(path: Path) -> dict[str, Any]:
    verts = tris = 0
    mtllibs: list[str] = []
    usemtl: set[str] = set()
    objects = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                verts += 1
            elif line.startswith("f "):
                tris += max(len(line.split()) - 3, 0)
            elif line.startswith("mtllib "):
                mtllibs.append(line[7:].strip())
            elif line.startswith("usemtl "):
                usemtl.add(line[7:].strip())
            elif line.startswith(("o ", "g ")):
                objects += 1
    textures: list[str] = []
    for lib in mtllibs:
        mtl = path.parent / lib
        if mtl.exists():
            for line in mtl.read_text("utf-8", "replace").splitlines():
                parts = line.strip().split()
                if parts and parts[0].lower().startswith(("map_", "bump", "disp", "norm")) and len(parts) > 1:
                    textures.append(parts[-1])
    return {
        "vertices": verts,
        "triangles": tris,
        "materials": sorted(usemtl),
        "textures": sorted(set(textures)),
        "mtllibs": mtllibs,
        "objects": objects,
    }


def inspect_stl(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(84)
    if len(head) == 84:
        count = struct.unpack_from("<I", head, 80)[0]
        if 84 + count * 50 == size:
            return {"triangles": count, "stl": "binary"}
    tris = 0
    with open(path, "r", encoding="ascii", errors="replace") as f:
        for line in f:
            if line.lstrip().startswith("facet"):
                tris += 1
    return {"triangles": tris, "stl": "ascii"}


def inspect_ply(path: Path) -> dict[str, Any]:
    res: dict[str, Any] = {}
    with open(path, "rb") as f:
        for _ in range(200):
            line = f.readline().decode("ascii", "replace").strip()
            if line.startswith("element vertex"):
                res["vertices"] = int(line.split()[-1])
            elif line.startswith("element face"):
                res["triangles"] = int(line.split()[-1])
            elif line == "end_header":
                break
    return res


def inspect_dae(path: Path) -> dict[str, Any]:
    text = path.read_text("utf-8", "replace")
    return {
        "meshes": text.count("<geometry "),
        "bones": len(re.findall(r'type="JOINT"', text)),
        "skins": text.count("<skin "),
        "animations": [{"name": n, "duration": 0} for n in re.findall(r'<animation_clip[^>]*name="([^"]+)"', text)],
        "textures": re.findall(r"<init_from>([^<]+\.(?:png|jpg|jpeg|tga|tif|tiff|bmp|exr))</init_from>", text, re.I),
        "materials": re.findall(r'<material id="[^"]*" name="([^"]+)"', text),
    }
