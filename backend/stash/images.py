"""Image helpers: thumbnails and basic info via Pillow (Blender handles EXR/HDR when present)."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

from .config import settings

Image.MAX_IMAGE_PIXELS = 512 * 1024 * 1024
PIL_FORMATS = {".png", ".jpg", ".jpeg", ".tga", ".tif", ".tiff", ".bmp", ".webp", ".psd", ".dds", ".gif"}


def image_info(path: Path) -> dict:
    if path.suffix.lower() not in PIL_FORMATS:
        if path.suffix.lower() == ".hdr":
            return _hdr_info(path)
        return {}
    try:
        with Image.open(path) as im:
            return {"width": im.width, "height": im.height, "mode": im.mode}
    except Exception as e:
        return {"image_error": str(e)[:200]}


def _hdr_info(path: Path) -> dict:
    # Radiance header: "-Y 1024 +X 2048"
    try:
        with open(path, "rb") as f:
            for _ in range(30):
                line = f.readline().strip()
                if line.startswith((b"-Y", b"+Y")):
                    parts = line.split()
                    return {"height": int(parts[1]), "width": int(parts[3])}
    except Exception:
        pass
    return {}


def make_thumbnail(path: Path, size: int | None = None) -> bytes | None:
    size = size or settings.thumbnail_size
    if path.suffix.lower() not in PIL_FORMATS:
        return None
    try:
        with Image.open(path) as im:
            im.draft("RGB", (size * 2, size * 2))
            if im.mode in ("I;16", "I;16B", "I", "F"):
                im = ImageOps.autocontrast(im.convert("I").point(lambda v: v / 256).convert("L"))
            im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P", "PA") else im.convert("RGB")
            im.thumbnail((size, size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            if im.mode == "RGBA":
                im.save(buf, "PNG", optimize=True)
            else:
                im.save(buf, "JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        return None


def normalize_thumbnail(data: bytes, size: int | None = None) -> tuple[bytes, str]:
    """Re-encode an arbitrary image blob (e.g. a uasset thumbnail or a browser capture)."""
    size = size or settings.thumbnail_size
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGBA")
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue(), ".png"


def convert_image(src: Path, dest: Path) -> Path:
    with Image.open(src) as im:
        fmt = dest.suffix.lower()
        if fmt in (".jpg", ".jpeg"):
            im = im.convert("RGB")
        im.save(dest)
    return dest
