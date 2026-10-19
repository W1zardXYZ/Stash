"""Read just enough of an editor (uncooked) Unreal .uasset/.umap to catalog it.

What we get without Unreal installed:
  * the asset class (StaticMesh, SkeletalMesh, AnimSequence, Material, Texture2D, ...)
  * package dependencies (other /Game/... packages it imports or soft-references)
  * the engine version that saved it (needed: uassets only open in that version or newer)
  * the embedded editor thumbnail (PNG/JPEG) that Unreal stores in every saved package

Mesh/texture *data* inside uncooked packages is stored in engine-internal, often
Oodle-compressed bulk formats, so geometry export is done by the Unreal companion
script (integrations/unreal) which runs inside the editor and pushes FBX/glTF here.

The summary layout changes between engine versions; instead of hard-coding every
version we try the plausible layouts and validate each against the file contents.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_TAG = 0x9E2A83C1
PKG_FILTER_EDITOR_ONLY = 0x80000000

# Main-class priority: the first class found among the package's class imports wins.
CLASS_PRIORITY = [
    "World",
    "AnimBlueprint",
    "WidgetBlueprint",
    "ControlRigBlueprint",
    "Blueprint",
    "NiagaraSystem",
    "NiagaraEmitter",
    "LevelSequence",
    "AnimMontage",
    "BlendSpace",
    "BlendSpace1D",
    "AimOffsetBlendSpace",
    "PoseAsset",
    "AnimComposite",
    "AnimSequence",
    "SkeletalMesh",
    "StaticMesh",
    "GeometryCollection",
    "PhysicsAsset",
    "Skeleton",
    "IKRigDefinition",
    "IKRetargeter",
    "MaterialFunction",
    "MaterialParameterCollection",
    "MaterialInstanceConstant",
    "Material",
    "TextureCube",
    "TextureRenderTarget2D",
    "Texture2DArray",
    "Texture2D",
    "VolumeTexture",
    "SoundCue",
    "SoundWave",
    "MetaSoundSource",
    "DataTable",
    "CurveFloat",
    "CurveTable",
    "PaperSprite",
    "Font",
]

# Unreal class -> Stash asset kind
CLASS_TO_KIND = {
    "StaticMesh": "model",
    "GeometryCollection": "model",
    "SkeletalMesh": "skeletal_model",
    "AnimSequence": "animation",
    "AnimMontage": "animation",
    "AnimComposite": "animation",
    "BlendSpace": "animation",
    "BlendSpace1D": "animation",
    "AimOffsetBlendSpace": "animation",
    "PoseAsset": "animation",
    "Material": "material",
    "MaterialInstanceConstant": "material",
    "MaterialFunction": "material",
    "Texture2D": "texture",
    "TextureCube": "hdri",
    "Texture2DArray": "texture",
    "VolumeTexture": "texture",
    "World": "scene",
    "Blueprint": "blueprint",
    "AnimBlueprint": "blueprint",
    "WidgetBlueprint": "blueprint",
    "ControlRigBlueprint": "blueprint",
    "SoundWave": "audio",
    "SoundCue": "audio",
    "MetaSoundSource": "audio",
}

# Naming-convention fallback (Epic's recommended prefixes, used by most Fab content)
PREFIX_TO_CLASS = {
    "SM": "StaticMesh",
    "SK": "SkeletalMesh",
    "SKM": "SkeletalMesh",
    "SKEL": "Skeleton",
    "PHYS": "PhysicsAsset",
    "PA": "PhysicsAsset",
    "A": "AnimSequence",
    "AS": "AnimSequence",
    "ANIM": "AnimSequence",
    "AM": "AnimMontage",
    "BS": "BlendSpace",
    "ABP": "AnimBlueprint",
    "BP": "Blueprint",
    "WBP": "WidgetBlueprint",
    "M": "Material",
    "MI": "MaterialInstanceConstant",
    "MF": "MaterialFunction",
    "T": "Texture2D",
    "TC": "TextureCube",
    "HDR": "TextureCube",
    "NS": "NiagaraSystem",
    "NE": "NiagaraEmitter",
    "S": "SoundWave",
    "SC": "SoundCue",
    "DT": "DataTable",
}

_ENGINE_RE = re.compile(rb"\+\+UE([45])\+Release-(\d+\.\d+)")
_PNG = b"\x89PNG\r\n\x1a\n"
_JPG = b"\xff\xd8\xff"


@dataclass
class UAssetInfo:
    path: Path
    package_name: str = ""
    object_name: str = ""
    asset_class: str = ""
    class_source: str = ""  # header | thumbnail | prefix | extension
    kind: str = "unreal_asset"
    engine_version: str = ""
    file_version_ue4: int = 0
    file_version_ue5: int = 0
    names: list[str] = field(default_factory=list)
    class_imports: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    engine_dependencies: list[str] = field(default_factory=list)
    thumbnail: bytes | None = None
    thumbnail_format: str = ""
    error: str = ""

    def to_meta(self) -> dict:
        return {
            "ue_class": self.asset_class,
            "ue_class_source": self.class_source,
            "ue_package": self.package_name,
            "ue_engine_version": self.engine_version,
            "ue_file_version": [self.file_version_ue4, self.file_version_ue5],
            "ue_dependencies": self.dependencies[:500],
            "ue_engine_dependencies": self.engine_dependencies[:200],
            "ue_classes": self.class_imports[:50],
            **({"ue_parse_error": self.error} if self.error else {}),
        }


class _Reader:
    def __init__(self, data: bytes, off: int = 0):
        self.d = data
        self.o = off

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.d, self.o)[0]
        self.o += 4
        return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.d, self.o)[0]
        self.o += 4
        return v

    def skip(self, n: int) -> None:
        self.o += n

    def fstring(self) -> str:
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            if n > 4096:
                raise ValueError("FString too long")
            s = self.d[self.o : self.o + n - 1].decode("latin-1")
            self.o += n
            return s
        n = -n
        if n > 4096:
            raise ValueError("FString too long")
        s = self.d[self.o : self.o + (n - 1) * 2].decode("utf-16-le", "replace")
        self.o += n * 2
        return s


def _parse_custom_versions(r: _Reader, legacy: int) -> None:
    count = r.i32()
    if count < 0 or count > 1000:
        raise ValueError("bad custom version count")
    if legacy == -2:
        r.skip(count * 8)
    elif legacy in (-3, -4, -5):
        for _ in range(count):
            r.skip(20)
            r.fstring()
    else:
        r.skip(count * 20)


def _read_names(data: bytes, offset: int, count: int, ue4: int) -> list[str]:
    r = _Reader(data, offset)
    names = []
    for _ in range(count):
        names.append(r.fstring())
        if ue4 >= 504 or ue4 == 0:
            r.skip(4)
    return names


def _plausible_name(s: str) -> bool:
    return 0 < len(s) < 1024 and all(31 < ord(c) < 0x10000 for c in s)


def _parse_header(data: bytes, info: UAssetInfo) -> None:
    r = _Reader(data)
    if r.u32() != PACKAGE_TAG:
        raise ValueError("not an Unreal package (bad tag)")
    legacy = r.i32()
    if legacy >= 0:
        raise ValueError("UE3 packages are not supported")
    if legacy != -4:
        r.i32()  # LegacyUE3Version
    ue4 = r.i32()
    ue5 = r.i32() if legacy <= -8 else 0
    r.i32()  # licensee
    if legacy <= -2:
        _parse_custom_versions(r, legacy)
    info.file_version_ue4, info.file_version_ue5 = ue4, ue5
    after_custom = r.o
    size = len(data)

    last_err: Exception | None = None
    # Candidate layouts: with/without the UE 5.5+ SavedHash (20 bytes) before TotalHeaderSize.
    for saved_hash in ((ue5 >= 1016, ue5 < 1016) if ue5 else (False,)):
        try:
            r.o = after_custom
            if saved_hash:
                r.skip(20)
            r.i32()  # TotalHeaderSize
            package_name = r.fstring()
            flags = r.u32()
            name_count = r.i32()
            name_offset = r.i32()
            if not (0 < name_count < 2_000_000 and 0 < name_offset < size):
                raise ValueError("bad name table")
            names = _read_names(data, name_offset, name_count, ue4)
            if not all(_plausible_name(n) for n in names[: min(50, len(names))]):
                raise ValueError("implausible names")
            info.names = names
            info.package_name = package_name if package_name.startswith("/") else ""
            editor_only_filtered = bool(flags & PKG_FILTER_EDITOR_ONLY)
            if ue5 >= 1008:
                r.skip(8)  # soft object paths count/offset
            if not editor_only_filtered and ue4 >= 516:
                r.fstring()  # LocalizationId
            if ue4 >= 459:
                r.skip(8)  # gatherable text data
            r.i32()  # export count
            r.i32()  # export offset
            import_count = r.i32()
            import_offset = r.i32()
            _parse_imports(data, info, names, import_count, import_offset, ue4, ue5, editor_only_filtered)
            return
        except Exception as e:  # try the next layout
            last_err = e
    raise ValueError(f"header parse failed: {last_err}")


def _fname(data: bytes, off: int, names: list[str]) -> str:
    idx, num = struct.unpack_from("<ii", data, off)
    if not (0 <= idx < len(names)):
        raise IndexError("name index out of range")
    base = names[idx]
    return f"{base}_{num - 1}" if num > 0 else base


def _parse_imports(data, info, names, count, offset, ue4, ue5, filtered):
    if not (0 <= count < 1_000_000) or not (0 < offset < len(data)):
        raise ValueError("bad import table")
    expected = 28 + (8 if ue4 >= 520 and not filtered else 0) + (4 if ue5 >= 1003 else 0)
    sizes = [expected] + [s for s in (28, 36, 32, 40) if s != expected]
    for entry_size in sizes:
        try:
            imports = []
            for i in range(count):
                base = offset + i * entry_size
                class_package = _fname(data, base, names)
                class_name = _fname(data, base + 8, names)
                outer = struct.unpack_from("<i", data, base + 16)[0]
                obj = _fname(data, base + 20, names)
                if not (-count <= outer <= 10_000_000):
                    raise ValueError("bad outer")
                imports.append((class_package, class_name, outer, obj))
            # sanity: Unreal imports always reference /Script/ packages for classes
            if count and not any(cp.startswith("/Script/") for cp, *_ in imports):
                raise ValueError("implausible imports")
            break
        except (ValueError, IndexError, struct.error):
            imports = None
    if imports is None:
        raise ValueError("could not decode import table")
    classes, deps, engine_deps = [], [], []
    for class_package, class_name, _outer, obj in imports:
        if class_name in ("Class", "BlueprintGeneratedClass", "WidgetBlueprintGeneratedClass", "AnimBlueprintGeneratedClass"):
            classes.append(obj)
        elif class_name == "Package" and obj.startswith("/") and not obj.startswith("/Script/"):
            (engine_deps if obj.startswith("/Engine/") else deps).append(obj)
    info.class_imports = classes
    info.dependencies = deps
    info.engine_dependencies = engine_deps


def _extract_thumbnail(data: bytes) -> tuple[bytes | None, str]:
    """Find the embedded editor thumbnail: [int32 w][int32 h][int32 size][PNG|JPEG bytes]."""
    best: tuple[bytes, str] | None = None
    for magic, fmt in ((_PNG, "png"), (_JPG, "jpg")):
        start = 0
        while True:
            pos = data.find(magic, start)
            if pos < 0:
                break
            start = pos + 1
            if pos < 12:
                continue
            w, h, n = struct.unpack_from("<iii", data, pos - 12)
            if 0 < w <= 4096 and 0 < abs(h) <= 4096 and 0 < n <= len(data) - pos:
                blob = data[pos : pos + n]
                if best is None or len(blob) > len(best[0]):
                    best = (blob, fmt)
                break  # first valid hit per format is the thumbnail
    return best if best else (None, "")


def _class_from_prefix(stem: str) -> str:
    head = stem.split("_", 1)[0]
    return PREFIX_TO_CLASS.get(head.upper(), "") if "_" in stem else ""


def parse_uasset(path: Path, read_limit: int = 64 * 1024 * 1024) -> UAssetInfo:
    path = Path(path)
    info = UAssetInfo(path=path, object_name=path.stem)
    try:
        with open(path, "rb") as f:
            data = f.read(read_limit)
    except OSError as e:
        info.error = str(e)
        return info

    m = _ENGINE_RE.search(data[: 256 * 1024])
    if m:
        info.engine_version = m.group(2).decode()

    try:
        _parse_header(data, info)
    except Exception as e:
        info.error = str(e)

    # soft references live only in the name map
    for n in info.names:
        if n.startswith("/Game/") and n not in info.dependencies and n != info.package_name:
            pkg = n.split(".", 1)[0]
            if pkg != info.package_name and pkg not in info.dependencies:
                info.dependencies.append(pkg)

    info.thumbnail, info.thumbnail_format = _extract_thumbnail(data)

    if path.suffix.lower() == ".umap":
        info.asset_class, info.class_source = "World", "extension"
    else:
        cls = next((c for c in CLASS_PRIORITY if c in info.class_imports), "")
        if cls:
            info.asset_class, info.class_source = cls, "header"
        elif info.class_imports:
            info.asset_class, info.class_source = info.class_imports[0], "header"
        else:
            # header unreadable: fall back to names then naming conventions
            cls = next((c for c in CLASS_PRIORITY if c in info.names), "")
            pre = _class_from_prefix(path.stem)
            info.asset_class = pre or cls
            info.class_source = "prefix" if pre else ("names" if cls else "")
    info.kind = CLASS_TO_KIND.get(info.asset_class, "unreal_asset")
    return info


def content_root_for(path: Path) -> Path | None:
    """Return the nearest enclosing 'Content' directory (project or plugin)."""
    for parent in Path(path).parents:
        if parent.name == "Content":
            return parent
    return None


def package_path_for(path: Path, content_root: Path, mount: str = "/Game") -> str:
    rel = Path(path).relative_to(content_root).with_suffix("")
    return f"{mount}/{rel.as_posix()}"


def package_files(path: Path) -> list[Path]:
    """All files belonging to one package (.uasset + .uexp/.ubulk/.uptnl sidecars)."""
    path = Path(path)
    out = [path]
    for ext in (".uexp", ".ubulk", ".uptnl"):
        p = path.with_suffix(ext)
        if p.exists():
            out.append(p)
    return out
