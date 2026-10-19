"""On-device image understanding with CLIP (ONNX Runtime, CPU).

* Every asset thumbnail gets a 512-d embedding (stored in `asset_embeddings`).
* Tag suggestions: the embedding is compared with an asset-specific vocabulary (objects,
  materials, styles, settings, colours) - zero-shot, nothing leaves the NAS.
* Semantic search: a text query is embedded with the same model and matched by cosine
  similarity, so "mossy stone wall" finds assets nobody tagged that way.
* Similar assets: nearest neighbours of an asset's embedding.

Models are the ONNX exports Immich publishes (OpenAI CLIP weights, MIT licensed) and are
downloaded on first use into $STASH_DATA_DIR/models (~600 MB for ViT-B-32).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.request
from pathlib import Path

import numpy as np

from .config import settings

log = logging.getLogger("stash.vision")

MODEL_FILES = (
    "config.json",
    "visual/model.onnx",
    "visual/preprocess_cfg.json",
    "textual/model.onnx",
    "textual/tokenizer.json",
)

# (category, prompt template, max tags kept, min probability within the category)
VOCABULARY: dict[str, tuple[str, int, float, list[str]]] = {
    "object": ("a 3D render of a {}", 3, 0.12, [
        "chair", "table", "sofa", "bed", "cabinet", "shelf", "desk", "lamp", "door", "window", "wall", "floor",
        "stairs", "fence", "gate", "column", "arch", "roof", "house", "building", "castle", "tower", "bridge",
        "barrel", "crate", "box", "treasure chest", "bottle", "cup", "plate", "bowl", "vase", "pot", "book",
        "candle", "lantern", "clock", "mirror", "painting", "statue", "bust", "sculpture", "skull", "sword",
        "axe", "hammer", "knife", "spear", "bow", "shield", "helmet", "armor", "gun", "rifle", "pistol",
        "tank", "car", "truck", "bus", "motorcycle", "bicycle", "train", "boat", "ship", "airplane",
        "helicopter", "spaceship", "robot", "drone", "computer", "phone", "television", "keyboard", "camera",
        "wrench", "pipe", "cable", "machine", "engine", "gear", "generator", "dumpster", "trash can", "sign",
        "street light", "bench", "stool", "cone", "barrier", "rock", "boulder", "cliff", "mountain", "terrain",
        "tree", "pine tree", "palm tree", "bush", "plant", "flower", "leaf", "log", "tree stump", "branch",
        "mushroom", "cactus", "grass", "person", "man", "woman", "child", "soldier", "knight", "zombie",
        "monster", "creature", "dragon", "dinosaur", "dog", "cat", "horse", "fox", "wolf", "bear", "bird",
        "fish", "insect", "spider", "hand", "head", "food", "fruit", "furniture", "weapon", "vehicle",
        "jewelry", "ring", "crown", "coin", "key", "rope", "chain", "bag", "backpack", "clothing", "shoe",
        "hat", "glasses", "sphere", "cube", "cylinder",
    ]),
    "material": ("a close-up of a {} surface", 2, 0.22, [
        "wood", "wood planks", "bark", "metal", "rusty metal", "steel", "gold", "silver", "copper", "iron",
        "stone", "marble", "concrete", "brick", "tiles", "plaster", "glass", "plastic", "rubber", "leather",
        "fabric", "carpet", "paper", "cardboard", "ceramic", "crystal", "ice", "snow", "sand", "dirt", "mud",
        "gravel", "asphalt", "moss", "grass", "rock", "skin", "fur", "water", "lava",
    ]),
    "style": ("a {} 3D model", 1, 0.3, [
        "low poly", "stylized", "cartoon", "realistic", "photoscanned", "sci-fi", "futuristic", "cyberpunk",
        "medieval", "fantasy", "ancient", "modern", "industrial", "military", "post-apocalyptic", "horror",
        "victorian", "rustic", "damaged", "old", "voxel",
    ]),
    "environment": ("a panorama of a {}", 2, 0.2, [
        "clear sky", "cloudy sky", "sunset", "night sky", "studio", "indoor room", "forest", "city street",
        "beach", "mountains", "desert", "snowy landscape", "field", "industrial building", "church", "garage",
    ]),
    "color": ("a {} object", 1, 0.25, [
        "red", "green", "blue", "yellow", "white", "black", "brown", "gray", "orange", "purple", "pink",
        "colorful",
    ]),
}


def enabled() -> bool:
    return settings.local_ai


def model_dir() -> Path:
    return settings.data_dir / "models" / settings.clip_model


def model_ready() -> bool:
    d = model_dir()
    return all((d / f).exists() for f in MODEL_FILES)


def download_model() -> Path:
    d = model_dir()
    for rel in MODEL_FILES:
        dest = d / rel
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"{settings.model_base_url}/{settings.clip_model}/resolve/main/{rel}"
        log.info("downloading %s", url)
        tmp = dest.with_suffix(dest.suffix + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": "stash"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(tmp, dest)
    return d


class Clip:
    def __init__(self, path: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, settings.ai_threads)
        opts.inter_op_num_threads = 1
        providers = ["CPUExecutionProvider"]
        self.visual = ort.InferenceSession(str(path / "visual/model.onnx"), opts, providers=providers)
        self.textual = ort.InferenceSession(str(path / "textual/model.onnx"), opts, providers=providers)
        pre = json.loads((path / "visual/preprocess_cfg.json").read_text())
        cfg = json.loads((path / "config.json").read_text())
        self.size = int(pre["size"][0] if isinstance(pre["size"], list) else pre["size"])
        self.mean = np.array(pre["mean"], dtype=np.float32).reshape(3, 1, 1)
        self.std = np.array(pre["std"], dtype=np.float32).reshape(3, 1, 1)
        self.context = int(cfg["text_cfg"]["context_length"])
        self.dim = int(cfg["embed_dim"])
        self.tokenizer = Tokenizer.from_file(str(path / "textual/tokenizer.json"))
        self.text_dtype = np.int64 if "int64" in self.textual.get_inputs()[0].type else np.int32
        self.lock = threading.Lock()

    def _preprocess(self, image_path: Path) -> np.ndarray:
        from PIL import Image

        with Image.open(image_path) as im:
            im = im.convert("RGBA")
            # thumbnails are rendered on transparent backgrounds; CLIP expects an opaque image
            bg = Image.new("RGBA", im.size, (128, 128, 128, 255))
            im = Image.alpha_composite(bg, im).convert("RGB")
            w, h = im.size
            s = self.size / min(w, h)
            im = im.resize((max(self.size, round(w * s)), max(self.size, round(h * s))), Image.Resampling.BICUBIC)
            w, h = im.size
            left, top = (w - self.size) // 2, (h - self.size) // 2
            im = im.crop((left, top, left + self.size, top + self.size))
            arr = np.asarray(im, dtype=np.float32).transpose(2, 0, 1) / 255.0
        return ((arr - self.mean) / self.std)[None]

    def embed_image(self, image_path: Path) -> np.ndarray:
        x = self._preprocess(image_path)
        with self.lock:
            v = self.visual.run(None, {self.visual.get_inputs()[0].name: x})[0][0]
        return (v / (np.linalg.norm(v) + 1e-8)).astype(np.float32)

    def embed_text(self, text: str) -> np.ndarray:
        ids = self.tokenizer.encode(text.lower().strip()).ids[: self.context]
        if len(ids) == self.context:
            ids[-1] = 49407  # keep the end-of-text token CLIP pools from
        tokens = np.zeros((1, self.context), dtype=self.text_dtype)
        tokens[0, : len(ids)] = ids
        with self.lock:
            v = self.textual.run(None, {self.textual.get_inputs()[0].name: tokens})[0][0]
        return (v / (np.linalg.norm(v) + 1e-8)).astype(np.float32)


_clip: Clip | None = None
_clip_lock = threading.Lock()
_last_used = [0.0]


def clip() -> Clip:
    global _clip
    import time

    with _clip_lock:
        if _clip is None:
            _clip = Clip(download_model())
        _last_used[0] = time.time()
        return _clip


def unload_if_idle() -> bool:
    """Free the model (~700 MB) when unused for STASH_AI_IDLE_UNLOAD seconds; reloads on demand."""
    global _clip
    import gc
    import time

    idle = settings.ai_idle_unload
    with _clip_lock:
        if _clip is not None and idle > 0 and time.time() - _last_used[0] > idle:
            _clip = None
            gc.collect()
            log.info("unloaded idle CLIP model")
            return True
    return False


# ----------------------------------------------------------------------------- vocabulary

_vocab: dict[str, tuple[list[str], np.ndarray]] | None = None


def vocabulary() -> dict[str, tuple[list[str], np.ndarray]]:
    """Text embeddings of the tag vocabulary, cached on disk per model + vocabulary version."""
    global _vocab
    if _vocab is not None:
        return _vocab
    key = hashlib.sha256(json.dumps(VOCABULARY, sort_keys=True).encode()).hexdigest()[:12]
    cache = model_dir() / f"vocab_{key}.npz"
    if cache.exists():
        data = np.load(cache, allow_pickle=False)
        _vocab = {cat: (list(VOCABULARY[cat][3]), data[cat]) for cat in VOCABULARY}
        return _vocab
    c = clip()
    out = {}
    for cat, (template, _k, _p, terms) in VOCABULARY.items():
        out[cat] = (list(terms), np.stack([c.embed_text(template.format(t)) for t in terms]))
    np.savez(cache, **{cat: m for cat, (_t, m) in out.items()})
    _vocab = out
    return _vocab


CATEGORIES_FOR_KIND = {
    "hdri": ("environment",),
    "texture": ("material", "color"),
    "material": ("material", "style", "color"),
}
DEFAULT_CATEGORIES = ("object", "material", "style", "color")
DATA_CHANNELS = {"normal", "roughness", "metallic", "ao", "height", "orm", "opacity", "specular", "glossiness", "cavity"}


def suggest_tags(vec: np.ndarray, kind: str = "model", channel: str | None = None) -> list[dict]:
    """Zero-shot tags: softmax over each vocabulary category that makes sense for this kind of
    asset (no objects for a material ball, nothing for a normal map), keep confident winners."""
    if channel in DATA_CHANNELS:
        return []
    out = []
    cats = CATEGORIES_FOR_KIND.get(kind, DEFAULT_CATEGORIES)
    for cat, (terms, mat) in vocabulary().items():
        if cat not in cats:
            continue
        _template, keep, min_p, _ = VOCABULARY[cat]
        logits = 100.0 * (mat @ vec)
        p = np.exp(logits - logits.max())
        p /= p.sum()
        for i in np.argsort(-p)[:keep]:
            if p[i] >= min_p:
                out.append({"tag": terms[i], "category": cat, "score": round(float(p[i]), 3)})
    return out


# ----------------------------------------------------------------------------- search index

class EmbeddingIndex:
    """All embeddings in one matrix for brute-force cosine search (100k assets ~ 100 MB fp16)."""

    def __init__(self):
        self.ids = np.zeros(0, dtype=np.int64)
        self.mat = np.zeros((0, 512), dtype=np.float16)
        self.dirty = True
        self.lock = threading.Lock()

    def invalidate(self) -> None:
        self.dirty = True

    def _load(self, db) -> None:
        from .models import AssetEmbedding

        rows = db.query(AssetEmbedding.asset_id, AssetEmbedding.vector).filter(AssetEmbedding.model == settings.clip_model).all()
        if rows:
            self.ids = np.array([r[0] for r in rows], dtype=np.int64)
            self.mat = np.stack([np.frombuffer(r[1], dtype=np.float16) for r in rows])
        else:
            self.ids = np.zeros(0, dtype=np.int64)
            self.mat = np.zeros((0, 512), dtype=np.float16)
        self.dirty = False

    def search(self, db, vec: np.ndarray, limit: int = 500, min_score: float = 0.0, exclude: int | None = None) -> list[tuple[int, float]]:
        with self.lock:
            if self.dirty:
                self._load(db)
            if not len(self.ids):
                return []
            scores = self.mat.astype(np.float32) @ vec.astype(np.float32)
        order = np.argsort(-scores)[: limit + 1]
        return [(int(self.ids[i]), float(scores[i])) for i in order if scores[i] >= min_score and int(self.ids[i]) != exclude][:limit]


index = EmbeddingIndex()


def text_search(db, query: str, limit: int = 5000) -> list[tuple[int, float]]:
    """Raw CLIP scores (text vs. thumbnail cosine) for the best `limit` assets."""
    return index.search(db, clip().embed_text(f"a 3D render of {query}"), limit=limit, min_score=0.0)


def hybrid_rank(clip_hits: list[tuple[int, float]], text_ids: set[int], window: float = 0.06, floor: float = 0.2) -> dict[int, float]:
    """Blend image similarity with ordinary text matches and keep what's close to the best hit.

    CLIP cosine scores for matching pairs sit around 0.25-0.35 and noise only a little lower, so an
    absolute threshold either floods or starves results; a window below the top score is stabler.
    Assets whose name/tags/path match the words get a small boost (and are kept even without a
    thumbnail embedding).
    """
    boost = 0.05
    scores = {aid: s + (boost if aid in text_ids else 0.0) for aid, s in clip_hits}
    for aid in text_ids:
        scores.setdefault(aid, floor + boost)
    if not scores:
        return {}
    cutoff = max(floor, max(scores.values()) - window)
    return {aid: s for aid, s in scores.items() if s >= cutoff or aid in text_ids}


def similar(db, asset_id: int, limit: int = 24) -> list[tuple[int, float]]:
    from .models import AssetEmbedding

    row = db.get(AssetEmbedding, asset_id)
    if row is None:
        return []
    return index.search(db, np.frombuffer(row.vector, dtype=np.float16).astype(np.float32), limit=limit, min_score=0.5, exclude=asset_id)
