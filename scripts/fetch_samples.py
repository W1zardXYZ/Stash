#!/usr/bin/env python3
"""Download a small, free (CC0 / CC-BY) sample library for dev testing (~16 MB).

    python3 scripts/fetch_samples.py dev-library

Layout (one top-level folder per "pack", like a real asset share):

    PolyHaven/wooden_stool_02/…blend + textures   (Blender file with external textures)
    PolyHaven/marble_bust_01/…fbx + textures      (FBX + PBR set)
    PolyHaven/textures/rock_wall_08/…             (loose PBR texture set)
    PolyHaven/hdri/…hdr                            (HDRI)
    Khronos/Fox/Fox.glb                           (rigged + animated glTF binary)
    Khronos/DamagedHelmet/…gltf + bin + jpgs      (glTF with external textures)

Sources: Poly Haven (CC0, api.polyhaven.com) and the Khronos glTF sample assets (see their
licenses on GitHub). Already-downloaded files are skipped. Standard library only.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

POLYHAVEN_API = "https://api.polyhaven.com/files/"
KHRONOS = "https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Assets/main/Models/"

KHRONOS_FILES = {
    "Fox": ["glTF-Binary/Fox.glb"],
    "DamagedHelmet": [
        "glTF/DamagedHelmet.gltf",
        "glTF/DamagedHelmet.bin",
        "glTF/Default_albedo.jpg",
        "glTF/Default_AO.jpg",
        "glTF/Default_emissive.jpg",
        "glTF/Default_metalRoughness.jpg",
        "glTF/Default_normal.jpg",
    ],
}


def fetch(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "stash-dev-samples"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
    tmp.rename(dest)
    print(f"  {dest}")


def api(asset_id: str) -> dict:
    req = urllib.request.Request(POLYHAVEN_API + asset_id, headers={"User-Agent": "stash-dev-samples"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def polyhaven_model(asset_id: str, fmt: str, root: Path) -> None:
    entry = api(asset_id)[fmt]["1k"][fmt]
    folder = root / "PolyHaven" / asset_id
    fetch(entry["url"], folder / entry["url"].rsplit("/", 1)[1])
    for rel, inc in (entry.get("include") or {}).items():
        fetch(inc["url"], folder / rel)


def polyhaven_texture(asset_id: str, root: Path) -> None:
    files = api(asset_id)
    folder = root / "PolyHaven" / "textures" / asset_id
    for channel in ("Diffuse", "nor_gl", "Rough", "AO", "Displacement"):
        if channel in files:
            url = files[channel]["1k"]["jpg"]["url"]
            fetch(url, folder / url.rsplit("/", 1)[1])


def polyhaven_hdri(asset_id: str, root: Path) -> None:
    url = api(asset_id)["hdri"]["1k"]["hdr"]["url"]
    fetch(url, root / "PolyHaven" / "hdri" / url.rsplit("/", 1)[1])


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "dev-library").resolve()
    print(f"Sample library -> {root}")
    polyhaven_model("wooden_stool_02", "blend", root)
    polyhaven_model("marble_bust_01", "fbx", root)
    polyhaven_texture("rock_wall_08", root)
    polyhaven_hdri("kloofendal_48d_partly_cloudy_puresky", root)
    for name, files in KHRONOS_FILES.items():
        for rel in files:
            fetch(f"{KHRONOS}{name}/{rel}", root / "Khronos" / name / rel.rsplit("/", 1)[1])
    print("done")


if __name__ == "__main__":
    main()
