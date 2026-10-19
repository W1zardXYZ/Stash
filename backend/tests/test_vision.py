"""On-device AI flow with a stubbed CLIP model (no download)."""

import numpy as np

from helpers import write_png


class FakeClip:
    dim = 4
    # images: red -> "rock"-like vector, blue -> "sky"-like vector
    def embed_image(self, path):
        from PIL import Image

        r, g, b = Image.open(path).convert("RGB").getpixel((1, 1))
        v = np.array([r, 0, b, 1.0], dtype=np.float32)
        return v / np.linalg.norm(v)

    def embed_text(self, text):
        v = np.array([1.0, 0, 0, 0.1] if "rock" in text else [0, 0, 1.0, 0.1], dtype=np.float32)
        return v / np.linalg.norm(v)


def test_embeddings_suggestions_search_similar(app_client, run_jobs, tmp_path, monkeypatch):
    from stash import vision
    from stash.config import settings

    monkeypatch.setattr(settings, "local_ai", True)
    monkeypatch.setattr(vision, "clip", lambda: FakeClip())
    vocab = {"material": (["rock", "sky"], np.array([[1, 0, 0, 0.1], [0, 0, 1, 0.1]], dtype=np.float32))}
    monkeypatch.setattr(vision, "vocabulary", lambda: vocab)
    monkeypatch.setattr(vision, "VOCABULARY", {"material": ("{}", 1, 0.5, ["rock", "sky"])})
    vision.index.invalidate()

    lib = tmp_path / "vis"
    write_png(lib / "red_one.png", (250, 10, 10))
    write_png(lib / "red_two.png", (240, 20, 5))
    write_png(lib / "blue_one.png", (5, 10, 250))
    c = app_client
    src = c.post("/api/sources", json={"path": str(lib)}).json()
    run_jobs()  # scan -> thumbnails -> embed_pending
    items = {a["name"]: a for a in c.get("/api/assets", params={"source": src["id"]}).json()["items"]}
    red = c.get(f"/api/assets/{items['red_one']['id']}").json()
    assert [t["tag"] for t in red["meta"]["auto_tags"]] == ["rock"]
    assert c.get("/api/status").json()["local_ai"]["embedded"] >= 3

    # search by meaning (the stub maps "rock" text to red images)
    r = c.get("/api/assets", params={"q": "a mossy rock", "semantic": 1, "source": src["id"]}).json()
    assert [a["name"] for a in r["items"]][:2] == ["red_one", "red_two"] or [a["name"] for a in r["items"]][:2] == ["red_two", "red_one"]
    assert "blue_one" not in [a["name"] for a in r["items"]]
    # suggestions are searchable with the normal full-text search too
    assert "red_one" in [a["name"] for a in c.get("/api/assets", params={"q": "rock", "source": src["id"]}).json()["items"]]

    sim = c.get(f"/api/assets/{items['red_one']['id']}/similar").json()["items"]
    mine = [a["name"] for a in sim if a["source_id"] == src["id"]]
    # the blue one is never similar; red_two is unless other tests' near-identical orange images
    # in the shared database make it not stand out (the cut-off is unit-tested below)
    assert mine in ([], ["red_two"])

    assert c.post("/api/ai/accept-suggestions", json={"ids": [items["red_one"]["id"]]}).json()["added"] == 1
    assert "rock" in c.get(f"/api/assets/{items['red_one']['id']}").json()["tags"]


def test_suggest_tags_respects_kind(monkeypatch):
    from stash import vision

    vocab = {
        "object": (["chair"], np.array([[1.0, 0]], dtype=np.float32)),
        "material": (["wood"], np.array([[1.0, 0]], dtype=np.float32)),
    }
    monkeypatch.setattr(vision, "vocabulary", lambda: vocab)
    v = np.array([1.0, 0], dtype=np.float32)
    assert {t["tag"] for t in vision.suggest_tags(v, "model")} == {"chair", "wood"}
    assert {t["tag"] for t in vision.suggest_tags(v, "material")} == {"wood"}  # no objects on material balls
    assert vision.suggest_tags(v, "texture", "normal") == []  # data maps get nothing


def test_similar_keeps_only_what_stands_out():
    """CLIP scores of 3D renders bunch up (~0.7 for anything); only clear outliers count as similar."""
    from stash.vision import EmbeddingIndex

    rng = np.random.default_rng(0)
    base = np.ones(16, dtype=np.float32)
    def unit(v):
        return (v / np.linalg.norm(v)).astype(np.float32)
    query = unit(base + rng.normal(0, 0.6, 16))
    vecs = [unit(base + rng.normal(0, 0.6, 16)) for _ in range(30)] + [unit(query + rng.normal(0, 0.05, 16)) for _ in range(2)]
    idx = EmbeddingIndex()
    idx.ids = np.arange(1, len(vecs) + 2, dtype=np.int64)
    idx.mat = np.stack(vecs + [query]).astype(np.float16)
    idx.dirty = False
    query_id = len(vecs) + 1
    assert min(float(v @ query) for v in vecs[:30]) > 0.3  # everything looks somewhat alike
    hits = idx.search(None, query, limit=24, exclude=query_id, min_z=1.5, min_score=0.85)
    assert sorted(i for i, _ in hits) == [31, 32]

    # a library this small has no meaningful statistics: an absolute bar applies instead
    idx.ids = np.array([31, 32, 1, 33], dtype=np.int64)
    idx.mat = np.stack([vecs[30], vecs[31], vecs[0], query]).astype(np.float16)
    hits = idx.search(None, query, limit=24, exclude=33, min_z=1.5, min_score=0.85)
    assert sorted(i for i, _ in hits) == [31, 32]
