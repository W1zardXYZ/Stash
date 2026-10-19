"""Runtime configuration, read from environment variables (all prefixed with STASH_)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"STASH_{name}", default)


def _find_blender() -> str | None:
    explicit = _env("BLENDER")
    if explicit:
        return explicit if explicit.lower() not in ("", "none", "off", "false") and Path(explicit).exists() else None
    for candidate in ("/opt/blender/blender", shutil.which("blender")):
        if candidate and Path(candidate).exists():
            return candidate
    # Dev convenience: any unpacked Blender under /opt/blender
    opt = Path("/opt/blender")
    if opt.is_dir():
        for sub in sorted(opt.glob("blender-*/blender"), reverse=True):
            return str(sub)
    return None


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")).resolve())
    # Roots the web UI may browse when picking a source folder. Comma separated.
    browse_roots: list[str] = field(
        default_factory=lambda: [p for p in (_env("BROWSE_ROOTS", "") or "").split(",") if p.strip()]
    )
    blender: str | None = field(default_factory=_find_blender)
    workers: int = field(default_factory=lambda: int(_env("WORKERS", "2")))
    password: str | None = field(default_factory=lambda: _env("PASSWORD") or None)
    api_token: str | None = field(default_factory=lambda: _env("API_TOKEN") or None)
    thumbnail_size: int = field(default_factory=lambda: int(_env("THUMBNAIL_SIZE", "384")))
    render_thumbnails: bool = field(default_factory=lambda: (_env("RENDER_THUMBNAILS", "true") or "").lower() == "true")
    blender_timeout: int = field(default_factory=lambda: int(_env("BLENDER_TIMEOUT", "1800")))
    # CPU priority for Blender jobs (0-19, higher = nicer) so a small NAS stays responsive
    blender_nice: int = field(default_factory=lambda: int(_env("BLENDER_NICE", "10")))
    # Blender render/convert threads (0 = all cores)
    blender_threads: int = field(default_factory=lambda: int(_env("BLENDER_THREADS", "0")))
    # Longest texture edge inside preview GLBs (0 = keep full resolution)
    preview_texture_size: int = field(default_factory=lambda: int(_env("PREVIEW_TEXTURE_SIZE", "1024")))
    # Size cap for cached exports/packages in GB; oldest are evicted first (0 = unlimited)
    export_cache_gb: float = field(default_factory=lambda: float(_env("EXPORT_CACHE_GB", "10")))
    # On-device AI (CLIP via ONNX Runtime): tag suggestions, semantic search, similar assets
    local_ai: bool = field(default_factory=lambda: (_env("LOCAL_AI", "true") or "").lower() == "true")
    clip_model: str = field(default_factory=lambda: _env("CLIP_MODEL", "ViT-B-32__openai") or "ViT-B-32__openai")
    model_base_url: str = field(default_factory=lambda: (_env("MODEL_URL", "https://huggingface.co/immich-app") or "").rstrip("/"))
    ai_threads: int = field(default_factory=lambda: int(_env("AI_THREADS", "2")))
    ai_idle_unload: int = field(default_factory=lambda: int(_env("AI_IDLE_UNLOAD", "1800")))
    frontend_dir: Path = field(
        default_factory=lambda: Path(
            _env("FRONTEND_DIR", str(Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"))
        )
    )
    public_url: str = field(default_factory=lambda: (_env("PUBLIC_URL", "") or "").rstrip("/"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "stash.db"

    @property
    def store_dir(self) -> Path:
        return self.data_dir / "store"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def extracted_dir(self) -> Path:
        return self.data_dir / "extracted"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.store_dir, self.work_dir, self.uploads_dir, self.extracted_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
