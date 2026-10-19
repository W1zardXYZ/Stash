"""Scan a Source (folder, Unreal project, single .blend) and index everything in it."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from .. import blender
from ..config import settings
from ..images import image_info, normalize_thumbnail
from ..models import Asset, AssetFile, AssetLink, Source, Tag
from ..search import index_asset
from ..storage import add_linked_file, add_stored_file, quick_fingerprint, store_bytes
from . import addons as addon_mod
from .formats import BLENDER_IMPORTABLE, IMAGE_EXTS, is_probably_hdri, kind_for_path, split_texture_name
from .inspect import inspect_file
from .uasset import CLASS_TO_KIND, content_root_for, package_files, package_path_for, parse_uasset

log = logging.getLogger("stash.scan")

SKIP_DIRS = {
    ".git", ".svn", "__MACOSX", "__pycache__", "node_modules", "Intermediate", "Saved", "DerivedDataCache",
    "Binaries", ".vs", ".idea", "Build", "$RECYCLE.BIN", "@eaDir", "#recycle", ".Trash-1000", ".stfolder",
}
TEXTURE_DIR_NAMES = {"textures", "texture", "tex", "maps", "materials", "images", "sourceimages", "textures_4k", "textures_2k", "pbr"}
PREVIEW_WORDS = ("preview", "thumb", "thumbnail", "render", "screenshot", "cover", "icon", "promo", "showcase")


def _now():
    return datetime.now(timezone.utc)


class Scanner:
    def __init__(self, db: Session, source: Source, ctx=None):
        self.db = db
        self.source = source
        self.ctx = ctx
        self.scan_started = _now()
        self.root = Path(source.path)
        self.followups: list[tuple[str, int, dict]] = []  # (job type, asset id, payload)
        self.counts: dict[str, int] = defaultdict(int)
        self._asset_cache: dict[tuple[str, str], Asset] = {}

    # ------------------------------------------------------------------ helpers
    def checkpoint(self):
        """Commit so the job context (separate connection) can write progress without
        waiting on our open SQLite write transaction."""
        self.db.commit()

    def log(self, msg: str):
        log.info(msg)
        if self.ctx:
            self.checkpoint()
            self.ctx.log(msg)

    def rel(self, path: Path, prefix: str = "") -> str:
        try:
            r = Path(path).relative_to(self.base_dir).as_posix()
        except ValueError:
            r = str(path)
        return f"{prefix}{r}"

    def upsert(self, source_path: str, sub_key: str = "", **fields) -> Asset:
        key = (source_path, sub_key)
        a = self._asset_cache.get(key)
        if a is None:
            a = (
                self.db.query(Asset)
                .filter(Asset.source_id == self.source.id, Asset.source_path == source_path, Asset.sub_key == sub_key)
                .one_or_none()
            )
        if a is None:
            a = Asset(source_id=self.source.id, source_path=source_path, sub_key=sub_key, name=fields.pop("name", Path(source_path).stem))
            self.db.add(a)
            self.counts["new"] += 1
        else:
            if (a.meta or {}).get("renamed"):
                fields.pop("name", None)  # keep names the user edited
        for k, v in fields.items():
            if k == "meta":
                merged = dict(a.meta or {})
                merged.update(v)
                a.meta = merged
            else:
                setattr(a, k, v)
        if "pack" not in (a.meta or {}):
            a.meta = {**(a.meta or {}), "pack": _pack_for(source_path)}
        a.seen_at = self.scan_started
        a.status = "ok"
        self._asset_cache[key] = a
        return a

    def link(self, a: Asset, b: Asset, relation: str):
        if a.id is None or b.id is None:
            self.db.flush()
        if a.id == b.id:
            return
        exists = (
            self.db.query(AssetLink.id)
            .filter(AssetLink.from_id == a.id, AssetLink.to_id == b.id, AssetLink.relation == relation)
            .first()
        )
        if not exists:
            self.db.add(AssetLink(from_id=a.id, to_id=b.id, relation=relation))

    def set_thumbnail_bytes(self, a: Asset, data: bytes, ext: str, label: str = "") -> None:
        rel, digest, size = store_bytes(data, ext)
        for f in a.files:
            if f.role == "thumbnail" and f.label == label:
                f.path, f.sha256, f.size, f.storage = rel, digest, size, "store"
                af = f
                break
        else:
            af = AssetFile(role="thumbnail", storage="store", path=rel, name=f"thumbnail{ext}", format=ext.lstrip("."), size=size, sha256=digest, label=label)
            a.files.append(af)
        self.db.flush()
        if a.thumbnail_file_id is None or label in ("", "embedded", "render"):
            a.thumbnail_file_id = af.id

    def add_tags(self, a: Asset, names):
        for n in names:
            n = n.strip().lower()
            if not n:
                continue
            tag = self.db.query(Tag).filter(Tag.name == n).one_or_none()
            if tag is None:
                tag = Tag(name=n)
                self.db.add(tag)
                self.db.flush()
            if tag not in a.tags:
                a.tags.append(tag)

    def queue(self, job_type: str, asset: Asset, payload: dict | None = None):
        self.followups.append((job_type, asset, payload or {}))

    # ------------------------------------------------------------------ entry
    def run(self) -> dict:
        src = self.source
        root = self.root
        if not root.exists():
            raise FileNotFoundError(f"source path does not exist: {root}")
        if src.mode == "import":
            root = self._import_copy(root)
        self.base_dir = root if root.is_dir() else root.parent

        if root.is_file():
            ext = root.suffix.lower()
            if ext == ".blend":
                self.scan_blend(root)
            elif ext == ".uproject":
                self.base_dir = root.parent
                self.scan_unreal_project(root.parent)
            elif ext == ".zip":
                self.scan_folder_files([root], root.parent)
            else:
                self.scan_folder_files([root], root.parent)
        else:
            uprojects = list(root.glob("*.uproject"))
            if src.kind == "unreal_project" or uprojects or (root.name == "Content" and any(root.rglob("*.uasset"))):
                if uprojects or root.name != "Content":
                    self.scan_unreal_project(root)
                else:
                    self.scan_unreal_content(root, "/Game", root.parent.name)
            else:
                self.scan_folder(root)
        self.db.flush()
        self._drop_ignored()
        missing = self._mark_missing()
        src.last_scan_at = _now()
        return {"counts": dict(self.counts), "missing": missing}

    def _import_copy(self, root: Path) -> Path:
        """Mirror the source into the Stash library folder (mode=import)."""
        dest_root = settings.data_dir / "library" / f"{self.source.id:04d}-{re.sub(r'[^A-Za-z0-9_-]+', '_', self.source.name)[:40]}"
        dest_root.mkdir(parents=True, exist_ok=True)
        if root.is_file():
            dest = dest_root / root.name
            if not dest.exists() or dest.stat().st_size != root.stat().st_size:
                shutil.copy2(root, dest)
            return dest
        copied = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                s = Path(dirpath) / fn
                d = dest_root / s.relative_to(root)
                try:
                    if d.exists() and d.stat().st_size == s.stat().st_size and d.stat().st_mtime >= s.stat().st_mtime:
                        continue
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s, d)
                    copied += 1
                except OSError as e:
                    self.log(f"copy failed {s}: {e}")
        self.log(f"imported {copied} new/changed files into {dest_root}")
        return dest_root

    def _drop_ignored(self) -> None:
        """Assets the user removed from the library stay removed on rescans."""
        ignored = set((self.source.options or {}).get("ignored", []))
        if not ignored:
            return
        from ..search import unindex_asset

        for a in self.db.query(Asset).filter(Asset.source_id == self.source.id, Asset.source_path.in_(ignored), Asset.parent_id.is_(None)):
            for child in self.db.query(Asset).filter(Asset.parent_id == a.id):
                unindex_asset(self.db, child.id)
            unindex_asset(self.db, a.id)
            self.db.delete(a)
        self.followups = [f for f in self.followups if f[1].source_path not in ignored]
        self.db.flush()

    def _mark_missing(self) -> int:
        q = self.db.query(Asset).filter(
            Asset.source_id == self.source.id,
            Asset.parent_id.is_(None),
            Asset.sub_key == "",
            (Asset.seen_at.is_(None)) | (Asset.seen_at < self.scan_started),
            Asset.status != "missing",
        )
        n = 0
        for a in q:
            if a.meta.get("origin") == "bundle":
                continue
            a.status = "missing"
            n += 1
        return n

    # ------------------------------------------------------------------ folders
    def scan_folder(self, root: Path, prefix: str = ""):
        files_by_dir: dict[Path, list[Path]] = defaultdict(list)
        addon_dirs: list[Path] = []
        unreal_roots: list[Path] = []
        n = 0
        for dirpath, dirnames, filenames in os.walk(root):
            d = Path(dirpath)
            dirnames[:] = sorted(x for x in dirnames if x not in SKIP_DIRS and not x.startswith("."))
            if any(f.endswith(".uproject") for f in filenames):
                unreal_roots.append(d)
                dirnames[:] = []
                continue
            if d.name == "Content" and self._has_uassets(d):
                unreal_roots.append(d)
                dirnames[:] = []
                continue
            if "__init__.py" in filenames or "blender_manifest.toml" in filenames:
                info = addon_mod.inspect_addon_dir(d)
                if info:
                    addon_dirs.append(d)
                    self._register_addon(d, info, prefix)
                    dirnames[:] = []
                    continue
            for fn in filenames:
                p = d / fn
                if kind_for_path(p) or p.suffix.lower() == ".py":
                    files_by_dir[d].append(p)
                    n += 1
        self.log(f"found {n} candidate files in {len(files_by_dir)} folders, {len(unreal_roots)} Unreal roots, {len(addon_dirs)} add-on folders")
        all_files = [p for fs in files_by_dir.values() for p in fs]
        self.scan_folder_files(all_files, root, prefix)
        for ur in unreal_roots:
            if ur.name == "Content":
                self.scan_unreal_content(ur, "/Game", ur.parent.name, prefix)
            else:
                self.scan_unreal_project(ur, prefix)

    @staticmethod
    def _has_uassets(d: Path) -> bool:
        return next(d.rglob("*.uasset"), None) is not None

    def scan_folder_files(self, files: list[Path], root: Path, prefix: str = ""):
        models, blends, images, audio, archives, pyfiles, substance = [], [], [], [], [], [], []
        for p in files:
            k = kind_for_path(p)
            if k == "model":
                models.append(p)
            elif k == "blend":
                blends.append(p)
            elif k == "image":
                images.append(p)
            elif k == "audio":
                audio.append(p)
            elif k == "archive":
                archives.append(p)
            elif k == "substance":
                substance.append(p)
            elif p.suffix.lower() == ".py":
                pyfiles.append(p)

        total = max(len(files), 1)
        done = 0

        def tick():
            nonlocal done
            done += 1
            if self.ctx and done % 25 == 0:
                self.checkpoint()
                self.ctx.progress(done / total * 0.9, f"indexed {done}/{total}")
                self.ctx.check_cancel()

        # --- images -> PBR sets / previews / loose textures
        sets: dict[tuple[Path, str], dict[str, Path]] = defaultdict(dict)
        loose: list[Path] = []
        previews: dict[Path, list[Path]] = defaultdict(list)
        for img in images:
            stem_l = img.stem.lower()
            if any(w in stem_l for w in PREVIEW_WORDS):
                previews[img.parent].append(img)
                continue
            base, ch = split_texture_name(img.stem)
            if ch:
                group_dir = img.parent
                if group_dir.name.lower() in TEXTURE_DIR_NAMES or re.fullmatch(r"\d+k", group_dir.name.lower()):
                    group_dir = group_dir.parent
                existing = sets[(group_dir, base.lower())]
                # prefer png/exr/tif over jpg when both exist
                if ch not in existing or existing[ch].suffix.lower() in (".jpg", ".jpeg"):
                    existing[ch] = img
            else:
                loose.append(img)
        claimed: set[Path] = set()

        # --- models
        model_assets: list[tuple[Asset, Path, dict]] = []
        models_per_dir: dict[Path, int] = defaultdict(int)
        for m in models:
            d = m.parent
            if d.name.lower() in TEXTURE_DIR_NAMES:
                d = d.parent
            models_per_dir[d] += 1
        for m in models:
            info = inspect_file(m) if m.stat().st_size < 600 * 1024 * 1024 else {}
            rigged = bool(info.get("bones") or info.get("skins"))
            anims = info.get("animations") or []
            kind = "skeletal_model" if rigged else "model"
            if not info.get("triangles") and not info.get("meshes") and anims and rigged:
                kind = "animation"
            fp = quick_fingerprint(m)
            a = self.upsert(
                self.rel(m, prefix),
                name=m.stem,
                kind=kind,
                format=m.suffix.lstrip(".").lower(),
                size=m.stat().st_size,
                fingerprint=fp,
                is_rigged=rigged,
                is_animated=bool(anims),
                poly_count=int(info.get("triangles") or 0),
                meta={"inspect": info, "folder": self.rel(m.parent, prefix)},
            )
            add_linked_file(a, m, "source")
            if m.suffix.lower() == ".obj":
                for lib in info.get("mtllibs", []):
                    mtl = m.parent / lib
                    if mtl.exists():
                        add_linked_file(a, mtl, "sidecar")
            if m.suffix.lower() == ".gltf":
                self._attach_gltf_buffers(a, m)
            # referenced textures
            refs = []
            for t in info.get("textures", []) or []:
                found = self._resolve_texture(m, t, images)
                if found:
                    refs.append(found)
            a.has_textures = bool(refs)
            for r in refs:
                claimed.add(r)
                base, ch = split_texture_name(r.stem)
                add_linked_file(a, r, "texture", label=ch or "")
            model_assets.append((a, m, info))
            if m.suffix.lower() in (".glb", ".gltf"):
                a.preview_file_id = None  # source itself is web-viewable; set below after flush
            tick()
        self.db.flush()

        # --- material assets from PBR sets
        set_assets: dict[tuple[Path, str], Asset] = {}
        for (gdir, base), chans in sets.items():
            if len(chans) < 2:
                loose.extend(chans.values())
                continue
            first = next(iter(chans.values()))
            nice = next(iter(chans.values())).stem
            nice = split_texture_name(nice)[0] or base
            folder_rel = self.rel(gdir, prefix)
            a = self.upsert(
                f"{folder_rel}#{base}" if folder_rel not in (".", "") else f"#{base}",
                name=nice,
                kind="material",
                format="pbr",
                has_textures=True,
                size=sum(p.stat().st_size for p in chans.values()),
                meta={"pbr": {ch: self.rel(p, prefix) for ch, p in chans.items()}, "channels": sorted(chans), "folder": self.rel(gdir, prefix)},
            )
            for ch, p in chans.items():
                add_linked_file(a, p, "texture", label=ch)
                claimed.add(p)
            res = re.search(r"(\d+)[kK]", first.stem) or re.search(r"(\d+)[kK]", str(first.parent.name))
            if res:
                a.meta = {**a.meta, "resolution": f"{res.group(1)}K"}
            set_assets[(gdir, base)] = a
            self.queue("thumbnail", a)
            tick()
        self.db.flush()

        # --- associate models with sets / textures in their folder
        for a, m, info in model_assets:
            d = m.parent
            if d.name.lower() in TEXTURE_DIR_NAMES:
                d = d.parent
            stem = re.sub(r"^(sm|sk|skm|s)_", "", m.stem.lower())
            stem_core = re.sub(r"[_\-.\s]*(lod\d+|low|high|hi|lo|\d+)$", "", stem)
            linked_any = False
            texture_files = {f.path for f in a.files if f.role == "texture"}
            for (gdir, base), sa in set_assets.items():
                if gdir != d and gdir.parent != d and d not in gdir.parents:
                    continue
                base_l = re.sub(r"^t_", "", base.lower())
                related = base_l and (base_l in stem or stem_core in base_l or stem_core.startswith(base_l))
                set_files = {f.path for f in sa.files if f.role == "texture"}
                if related or set_files & texture_files or (models_per_dir[d] == 1 and sum(1 for k in set_assets if k[0] == gdir) <= 3):
                    self.link(a, sa, "uses_material")
                    linked_any = True
                    if not texture_files:
                        for f in sa.files:
                            if f.role == "texture":
                                add_linked_file(a, Path(f.path), "texture", label=f.label)
            if linked_any:
                a.has_textures = True
            for pv in previews.get(m.parent, []) + previews.get(d, []):
                add_linked_file(a, pv, "gallery")
            self._queue_preview(a, m)

        # --- loose images -> texture / hdri assets
        for img in loose:
            if img in claimed:
                continue
            info = image_info(img)
            kind = "hdri" if is_probably_hdri(img, info.get("width"), info.get("height")) else "texture"
            base, ch = split_texture_name(img.stem)
            a = self.upsert(
                self.rel(img, prefix),
                name=img.stem,
                kind=kind,
                format=img.suffix.lstrip(".").lower(),
                size=img.stat().st_size,
                meta={"image": info, "channel": ch, "folder": self.rel(img.parent, prefix)},
            )
            add_linked_file(a, img, "source")
            self.queue("thumbnail", a)
            tick()

        # --- previews for folders that had no models: keep as plain images? attach to sets
        for d, pvs in previews.items():
            for (gdir, _), sa in set_assets.items():
                if gdir == d:
                    for pv in pvs:
                        add_linked_file(sa, pv, "gallery")

        # --- blend files
        for b in blends:
            if re.search(r"\.blend\d+$", b.name):
                continue
            self.scan_blend(b, prefix)
            tick()

        # --- audio
        for au in audio:
            a = self.upsert(self.rel(au, prefix), name=au.stem, kind="audio", format=au.suffix.lstrip(".").lower(), size=au.stat().st_size)
            add_linked_file(a, au, "source")
            tick()

        # --- substance materials (indexed, not convertible)
        for sb in substance:
            a = self.upsert(self.rel(sb, prefix), name=sb.stem, kind="material", format=sb.suffix.lstrip(".").lower(), size=sb.stat().st_size, is_procedural=sb.suffix.lower() == ".sbsar", meta={"substance": True})
            add_linked_file(a, sb, "source")
            tick()

        # --- single-file legacy add-ons
        for py in pyfiles:
            info = addon_mod.inspect_addon_py(py)
            if info:
                self._register_addon(py, info, prefix)
            tick()

        # --- archives
        for z in archives:
            self.scan_archive(z, prefix)
            tick()

        self.db.flush()
        for a, _, _ in model_assets:
            index_asset(self.db, a)
        for a in set_assets.values():
            index_asset(self.db, a)
        for a in self._asset_cache.values():
            if a.id is not None:
                index_asset(self.db, a)

    def _attach_gltf_buffers(self, a: Asset, path: Path):
        try:
            g = json.loads(path.read_text("utf-8"))
        except Exception:
            return
        for b in g.get("buffers", []):
            uri = b.get("uri")
            if uri and not uri.startswith("data:"):
                p = path.parent / uri
                if p.exists():
                    add_linked_file(a, p, "sidecar")

    def _resolve_texture(self, model: Path, ref: str, images: list[Path]) -> Path | None:
        ref_norm = ref.replace("\\", "/")
        cand = (model.parent / ref_norm)
        if cand.exists():
            return cand.resolve() if cand.resolve() in images else cand
        name = Path(ref_norm).name.lower()
        if not name:
            return None
        # same basename anywhere under the model's folder (or parent) - asset packs move files around
        best = None
        for img in images:
            if img.name.lower() == name:
                if model.parent in img.parents or img.parent == model.parent:
                    return img
                if model.parent.parent in img.parents:
                    best = best or img
        return best

    def _queue_preview(self, a: Asset, m: Path):
        ext = m.suffix.lower()
        if ext in (".glb", ".gltf"):
            self.db.flush()
            src = next((f for f in a.files if f.role == "source"), None)
            if src is not None:
                a.preview_file_id = src.id
            if blender.available() and settings.render_thumbnails and not a.thumbnail_file_id:
                self.queue("preview", a, {"render_only": True})
        elif ext in BLENDER_IMPORTABLE and blender.available():
            if not a.preview_file_id or a.meta.get("preview_fingerprint") != a.fingerprint:
                self.queue("preview", a)

    def _register_addon(self, path: Path, info: dict, prefix: str = ""):
        name = info.get("name") or path.stem
        a = self.upsert(
            self.rel(path, prefix),
            name=name,
            kind="addon",
            format="zip" if path.suffix.lower() == ".zip" else ("py" if path.suffix.lower() == ".py" else "dir"),
            description=info.get("description", "") or info.get("tagline", ""),
            size=path.stat().st_size if path.is_file() else 0,
            meta={"addon": info, "category": info.get("category", "")},
        )
        if path.is_file():
            add_linked_file(a, path, "source")
        else:
            # zip the folder into the store so it can be downloaded/installed
            data = addon_mod.zip_dir(path)
            rel, digest, size = store_bytes(data, ".zip")
            for f in a.files:
                if f.role == "source":
                    f.storage, f.path, f.sha256, f.size, f.name = "store", rel, digest, size, f"{path.name}.zip"
                    break
            else:
                a.files.append(AssetFile(role="source", storage="store", path=rel, name=f"{path.name}.zip", format="zip", size=size, sha256=digest))
            a.size = size
        self.add_tags(a, ["blender-addon"] + ([info["category"].split(",")[0].lower()] if info.get("category") else []))
        self.counts["addons"] += 1

    # ------------------------------------------------------------------ archives
    def scan_archive(self, z: Path, prefix: str = ""):
        info = addon_mod.inspect_addon_zip(z)
        if info:
            self._register_addon(z, info, prefix)
            return
        if not self.source.options.get("extract_archives", True):
            return
        try:
            fp = quick_fingerprint(z)
            dest = settings.extracted_dir / fp[:16]
            marker = dest / ".stash_extracted"
            if not marker.exists():
                with zipfile.ZipFile(z) as zf:
                    names = zf.namelist()
                    interesting = [n for n in names if kind_for_path(Path(n)) not in (None, "archive")]
                    if not interesting:
                        return
                    total = sum(i.file_size for i in zf.infolist())
                    if total > 50 * 1024**3:
                        self.log(f"skipping huge archive {z}")
                        return
                    dest.mkdir(parents=True, exist_ok=True)
                    for member in zf.infolist():
                        target = (dest / member.filename).resolve()
                        if not str(target).startswith(str(dest.resolve())):
                            continue  # path traversal
                        if member.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as srcf, open(target, "wb") as out:
                            shutil.copyfileobj(srcf, out)
                marker.write_text(str(z))
            self.log(f"scanning archive {z.name}")
            zip_prefix = self.rel(z, prefix) + "!/"
            saved_base = self.base_dir
            self.base_dir = dest
            try:
                self.scan_folder(dest, prefix=zip_prefix)
            finally:
                self.base_dir = saved_base
        except (zipfile.BadZipFile, OSError) as e:
            self.log(f"cannot read archive {z}: {e}")

    # ------------------------------------------------------------------ blender
    def scan_blend(self, b: Path, prefix: str = ""):
        fp = quick_fingerprint(b)
        a = self.upsert(
            self.rel(b, prefix),
            name=b.stem,
            kind="scene",
            format="blend",
            size=b.stat().st_size,
            meta={"folder": self.rel(b.parent, prefix), "container": True},
        )
        changed = a.fingerprint != fp
        a.fingerprint = fp
        add_linked_file(a, b, "source")
        thumb = _blend_thumbnail(b)
        if thumb:
            self.set_thumbnail_bytes(a, thumb, ".png", label="embedded")
        if changed or not a.meta.get("extracted"):
            if blender.available():
                self.queue("extract_blend", a)
            else:
                a.meta = {**a.meta, "extract_pending": "Blender not available on the server"}
        self.counts["blend"] += 1

    # ------------------------------------------------------------------ unreal
    def scan_unreal_project(self, root: Path, prefix: str = ""):
        uproject = next(iter(root.glob("*.uproject")), None)
        project_name = uproject.stem if uproject else root.name
        meta = {}
        if uproject:
            try:
                up = json.loads(uproject.read_text("utf-8", "replace"))
                meta = {
                    "engine_association": up.get("EngineAssociation", ""),
                    "plugins": [p.get("Name") for p in up.get("Plugins", []) if p.get("Enabled")],
                    "modules": [m.get("Name") for m in up.get("Modules", [])],
                }
            except Exception:
                pass
        projects = dict(self.source.meta or {}).get("unreal_projects", {})
        projects[project_name] = {"path": str(root), **meta}
        self.source.meta = {**(self.source.meta or {}), "unreal_projects": projects}
        content = root / "Content"
        self._engine_hint = meta.get("engine_association", "") if re.fullmatch(r"\d+\.\d+", meta.get("engine_association", "")) else ""
        if content.is_dir():
            self.scan_unreal_content(content, "/Game", project_name, prefix)
        plugins = root / "Plugins"
        if plugins.is_dir():
            for upl in plugins.rglob("*.uplugin"):
                pc = upl.parent / "Content"
                if pc.is_dir():
                    self.scan_unreal_content(pc, f"/{upl.stem}", project_name, prefix)

    def scan_unreal_content(self, content: Path, mount: str, project_name: str, prefix: str = ""):
        files = [p for p in content.rglob("*") if p.suffix.lower() in (".uasset", ".umap") and not any(s in SKIP_DIRS for s in p.parts)]
        self.log(f"Unreal content {content} ({mount}): {len(files)} packages")
        by_package: dict[str, Asset] = {}
        parsed = []
        for i, f in enumerate(files):
            info = parse_uasset(f, read_limit=16 * 1024 * 1024)
            pkg = package_path_for(f, content, mount)
            info.package_name = pkg
            info.dependencies = [d for d in info.dependencies if d != pkg]
            parts = Path(pkg).parts
            pack = parts[2] if len(parts) > 3 else ""
            fp = f"{f.stat().st_size}-{int(f.stat().st_mtime)}"
            a = self.upsert(
                self.rel(f, prefix),
                name=f.stem,
                kind=info.kind,
                format=f.suffix.lstrip(".").lower(),
                size=sum(p.stat().st_size for p in package_files(f)),
                fingerprint=fp,
                is_rigged=info.asset_class in ("SkeletalMesh", "Skeleton"),
                is_animated=info.kind == "animation",
                meta={
                    **info.to_meta(),
                    "ue_engine_version": info.engine_version or getattr(self, "_engine_hint", ""),
                    "ue_project": project_name,
                    "ue_mount": mount,
                    "pack": pack,
                    "folder": str(Path(pkg).parent),
                },
            )
            for pf in package_files(f):
                add_linked_file(a, pf, "source" if pf == f else "sidecar")
            if info.thumbnail:
                try:
                    data, ext = normalize_thumbnail(info.thumbnail)
                    self.set_thumbnail_bytes(a, data, ext, label="embedded")
                except Exception:
                    pass
            if pack:
                self.add_tags(a, [pack.lower()])
            by_package[pkg] = a
            parsed.append((a, info))
            self.counts["uasset"] += 1
            if self.ctx and i % 50 == 0:
                self.checkpoint()
                self.ctx.progress(i / max(len(files), 1) * 0.9, f"Unreal {i}/{len(files)}")
                self.ctx.check_cancel()
        self.db.flush()
        # relationships
        skel_users: dict[int, list[Asset]] = defaultdict(list)
        for a, info in parsed:
            for dep in info.dependencies:
                b = by_package.get(dep)
                if b is None:
                    continue
                cls = (b.meta or {}).get("ue_class", "")
                if cls.startswith("Material"):
                    rel = "uses_material"
                elif cls.startswith("Texture"):
                    rel = "uses_texture"
                    a.has_textures = True
                elif cls == "Skeleton":
                    rel = "uses_skeleton"
                    if info.asset_class == "SkeletalMesh":
                        skel_users[b.id].append(a)
                else:
                    rel = "depends_on"
                self.link(a, b, rel)
        for a, info in parsed:
            if a.kind == "animation":
                for dep in info.dependencies:
                    b = by_package.get(dep)
                    if b is not None and (b.meta or {}).get("ue_class") == "Skeleton":
                        for mesh in skel_users.get(b.id, []):
                            self.link(a, mesh, "animates")
        for a, _ in parsed:
            index_asset(self.db, a)


def _pack_for(source_path: str) -> str:
    """Top-level folder (or zip) an asset lives in - usually the asset pack it came from."""
    head = source_path.split("!/", 1)[0] if "!/" in source_path else source_path
    parts = [p for p in head.replace("\\", "/").split("/") if p and p != "."]
    if "!/" in source_path:
        return Path(parts[-1]).stem if parts else ""
    return parts[0] if len(parts) > 1 else ""


def _blend_thumbnail(path: Path) -> bytes | None:
    """Read the embedded preview (TEST block) of a .blend without Blender."""
    import gzip
    import struct
    import zlib

    from PIL import Image

    try:
        with open(path, "rb") as f:
            head = f.read(4)
        opener = open
        if head[:2] == b"\x1f\x8b":
            opener = gzip.open
        elif head == b"\x28\xb5\x2f\xfd":
            try:
                import zstandard  # type: ignore

                def opener(p, mode="rb"):  # noqa: E306
                    return zstandard.ZstdDecompressor().stream_reader(open(p, "rb"))
            except ImportError:
                return None
        with opener(path, "rb") as f:
            header = f.read(12)
            if not header.startswith(b"BLENDER"):
                return None
            # Blender 5.0 introduced a new header ("BLENDER17-01v0500")
            if header[7:9] == b"17":
                rest = f.read(5)
                header = header + rest
                ptr_size, endian = 8, "<"
                bhead_fmt = "<4siQQqq"  # code, sdna, old, len(int64), nr
                bhead_size = struct.calcsize(bhead_fmt)
                code_len_order = "v1"
            else:
                ptr_size = 8 if header[7:8] == b"-" else 4
                endian = "<" if header[8:9] == b"v" else ">"
                bhead_fmt = f"{endian}4si{'Q' if ptr_size == 8 else 'I'}ii"
                bhead_size = struct.calcsize(bhead_fmt)
                code_len_order = "v0"
            for _ in range(200000):
                raw = f.read(bhead_size)
                if len(raw) < bhead_size:
                    return None
                if code_len_order == "v0":
                    code, length, _old, _sdna, _nr = struct.unpack(bhead_fmt, raw)
                else:
                    code, _sdna, _old, length, _nr = struct.unpack(bhead_fmt, raw)
                if code == b"TEST":
                    data = f.read(length)
                    w, h = struct.unpack(f"{endian}ii", data[:8])
                    if w <= 0 or h <= 0 or w * h * 4 + 8 > len(data):
                        return None
                    img = Image.frombytes("RGBA", (w, h), data[8 : 8 + w * h * 4]).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                    import io

                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                    return buf.getvalue()
                if code in (b"ENDB", b"DNA1"):
                    return None
                f.read(length) if opener is not open else f.seek(length, 1)
    except Exception:
        return None
    return None


def scan_source(db: Session, source: Source, ctx=None) -> tuple[dict, list]:
    sc = Scanner(db, source, ctx)
    result = sc.run()
    return result, sc.followups
