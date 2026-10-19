#!/usr/bin/env python3
"""Describe Stash packs (and, if you want, single assets) with a local model in LM Studio.

Runs on any machine that can reach both the Stash server and LM Studio, e.g. a MacBook:

    python3 stash_describe.py models                                 # what LM Studio has loaded
    python3 stash_describe.py packs --stash http://nas:8420          # describe every undescribed pack
    python3 stash_describe.py packs --only "Medieval Village" --dry-run
    python3 stash_describe.py packs --engine clip                    # no LLM: summary from CLIP tags
    python3 stash_describe.py assets --pack "Medieval Village" --kind model --limit 50

How it works: for each pack it asks Stash for a summary (GET /api/packs/<name>: counts per kind,
folders, asset names, CLIP's visual tags, a few thumbnails and the pack's own preview images),
sends that plus the images to LM Studio's OpenAI-compatible /v1/chat/completions endpoint, and
writes the answer back with PUT /api/packs/<name> (description + search keywords). Stash indexes
the pack description with every asset in the pack, so a search for "medieval village" finds all of
its barrels and walls without any of them having its own description.

Start LM Studio's server (Developer tab → Start server, or `lms server start`) and load a model.
Use a vision-capable model (e.g. a Qwen VL / Gemma 3 / other multimodal build) to send pictures;
with a text-only model pass --images 0 and it works from names, folders and CLIP tags only.

Settings can also come from the environment: STASH_URL, STASH_TOKEN (the Stash password, if one is
set), LMSTUDIO_URL (default http://localhost:1234/v1), LMSTUDIO_MODEL.

Only the Python standard library is used (Python 3.9+).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PACK_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["description", "keywords"],
}

ASSET_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["description", "tags"],
}

PACK_SYSTEM = (
    "You catalogue 3D asset packs (game-ready models, materials, textures, Unreal Engine and Blender "
    "content) for a searchable personal asset library. Be factual and concise. Reply with JSON only."
)

PACK_TASK = """Write:
- "description": 1-3 plain sentences: what the pack is, its theme or setting, its art style (e.g. realistic, stylized, low-poly, PBR scanned) and the main kinds of content. No marketing language. Don't invent details that the names, folders or images don't support.
- "keywords": 5-12 short lowercase search keywords (theme, setting, style, typical object types) that someone might type to find these assets. Don't repeat the pack name."""

ASSET_SYSTEM = (
    "You describe single 3D assets (models, materials, textures) for a searchable asset library. "
    "Be factual and concise. Reply with JSON only."
)

ASSET_TASK = """Write:
- "description": one short sentence saying what this asset is (object, material, style), e.g. "Weathered wooden barrel with iron bands, realistic PBR."
- "tags": 2-6 short lowercase tags (object type, material, style)."""


# ----------------------------------------------------------------------------- http

class Http:
    def __init__(self, base: str, token: str = "", timeout: float = 60):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, body=None, raw: bool = False, timeout: float | None = None):
        url = path if path.startswith("http") else self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"{method} {url} -> {e.reason}") from None
        return payload if raw else json.loads(payload or b"null")

    def get(self, path, **kw):
        return self._req("GET", path, **kw)

    def post(self, path, body, **kw):
        return self._req("POST", path, body, **kw)

    def put(self, path, body):
        return self._req("PUT", path, body)

    def patch(self, path, body):
        return self._req("PATCH", path, body)


def q(name: str) -> str:
    return urllib.parse.quote(name, safe="")


# ----------------------------------------------------------------------------- LM Studio

class LMStudio:
    def __init__(self, url: str, model: str | None, temperature: float, max_tokens: int, no_think: bool, timeout: float):
        self.http = Http(url, timeout=timeout)
        self.model = model or self._first_model()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.no_think = no_think
        self.structured = True

    def models(self) -> list[str]:
        return [m["id"] for m in self.http.get("/models").get("data", [])]

    def _first_model(self) -> str:
        models = [m for m in self.models() if "embed" not in m.lower()]
        if not models:
            raise SystemExit("LM Studio has no model loaded – load one or pass --model")
        return models[0]

    def ask(self, system: str, text: str, images: list[bytes], schema: dict) -> dict:
        if self.no_think:
            text += "\n/no_think"
        content: list[dict] = [{"type": "text", "text": text}]
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}})
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.structured:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "strict": True, "schema": schema}}
        try:
            r = self.http.post("/chat/completions", body)
        except RuntimeError as e:
            if self.structured and "HTTP 400" in str(e):
                # older LM Studio builds / some models don't do structured output: ask for plain JSON
                self.structured = False
                return self.ask(system, text.replace("\n/no_think", ""), images, schema)
            raise
        msg = r["choices"][0]["message"]
        return parse_json(msg.get("content") or "")


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError(f"model did not return JSON: {text[:300]!r}") from None


# ----------------------------------------------------------------------------- prompts

def pack_prompt(p: dict, n_images: int) -> str:
    lines = [f"Pack name: {p['name']}"]
    if p.get("ue_project"):
        lines.append(f"Unreal project: {p['ue_project']}")
    if p.get("sources"):
        lines.append("Library source: " + ", ".join(s["name"] for s in p["sources"]))
    lines.append(f"Assets: {p['count']} (" + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in p["kinds"].items()) + ")")
    if p.get("formats"):
        lines.append("File formats: " + ", ".join(f"{k} {n}" for k, n in list(p["formats"].items())[:10]))
    if p.get("folders"):
        lines.append("Folders: " + ", ".join(f"{f['path']} ({f['count']})" for f in p["folders"][:20]))
    if p.get("tags"):
        lines.append("Existing tags: " + ", ".join(p["tags"][:20]))
    if p.get("auto_tags"):
        lines.append("Visual tags from an image classifier (often right, sometimes wrong): " + ", ".join(t["tag"] for t in p["auto_tags"][:15]))
    names = p.get("names") or []
    if names:
        lines.append(f"Asset names ({min(len(names), 150)} of {len(names)}): " + ", ".join(names[:150]))
    if n_images:
        lines.append(f"{n_images} preview image(s) of the pack and some of its assets are attached.")
    return "\n".join(lines) + "\n\n" + PACK_TASK


def asset_prompt(a: dict, has_image: bool) -> str:
    m = a.get("meta") or {}
    lines = [f"Asset name: {a['name']}", f"Kind: {a['kind'].replace('_', ' ')} ({a.get('format', '')})"]
    if a.get("pack"):
        lines.append(f"From pack: {a['pack']['name']}" + (f" – {a['pack']['description']}" if a["pack"].get("description") else ""))
    lines.append(f"Path: {a.get('source_path', '')}")
    for key, label in (("materials", "Materials"), ("animations", "Animations"), ("channels", "Texture channels")):
        v = m.get(key)
        if v:
            items = list(v)[:15] if not isinstance(v, dict) else list(v)[:15]
            lines.append(f"{label}: " + ", ".join(x.get("name", "") if isinstance(x, dict) else str(x) for x in items))
    if a.get("poly_count"):
        lines.append(f"Polygons: {a['poly_count']}")
    if a.get("is_rigged"):
        lines.append("Rigged (has a skeleton)")
    if a.get("tags"):
        lines.append("Existing tags: " + ", ".join(a["tags"]))
    auto = [t["tag"] for t in m.get("auto_tags") or [] if isinstance(t, dict)]
    if auto:
        lines.append("Visual tags from an image classifier: " + ", ".join(auto[:8]))
    if has_image:
        lines.append("A thumbnail of the asset is attached.")
    return "\n".join(lines) + "\n\n" + ASSET_TASK


# ----------------------------------------------------------------------------- commands

def fetch_images(stash: Http, urls: list[str], limit: int, size: int) -> list[bytes]:
    out = []
    for u in urls:
        if len(out) >= limit:
            break
        if not u:
            continue
        sep = "&" if "?" in u else "?"
        try:
            out.append(stash.get(f"{u}{sep}max={size}", raw=True))
        except RuntimeError as e:
            print(f"    (skipped image: {e})", file=sys.stderr)
    return out


def clean_keywords(words, pack_name: str = "") -> list[str]:
    seen: list[str] = []
    for w in words or []:
        w = str(w).strip().lower().strip(".#")
        if w and w != pack_name.lower() and w not in seen and len(w) <= 40:
            seen.append(w)
    return seen[:12]


def clip_summary(p: dict) -> dict:
    """No-LLM fallback: a plain summary from counts and CLIP's aggregated visual tags."""
    kinds = ", ".join(f"{n} {k.replace('_', ' ')}{'s' if n != 1 and not k.endswith('s') else ''}" for k, n in list(p["kinds"].items())[:4])
    top = [t["tag"] for t in p.get("auto_tags", [])[:8]]
    desc = f"{p['count']} assets: {kinds}."
    if top:
        desc += " Looks like: " + ", ".join(top[:6]) + "."
    return {"description": desc, "keywords": top}


def cmd_packs(args, stash: Http):
    packs = stash.get("/api/packs" + ("" if args.overwrite or args.only else "?described=false"))
    if args.only:
        wanted = set(args.only)
        packs = [p for p in packs if p["name"] in wanted]
        missing = wanted - {p["name"] for p in packs}
        if missing:
            print("unknown pack(s): " + ", ".join(sorted(missing)), file=sys.stderr)
    if args.min_assets:
        packs = [p for p in packs if p["count"] >= args.min_assets]
    if args.limit:
        packs = packs[: args.limit]
    if not packs:
        print("Nothing to do – every pack has a description (use --overwrite to redo them).")
        return
    llm = None if args.engine == "clip" else make_llm(args)
    print(f"{len(packs)} pack(s) · engine: {'CLIP tags' if llm is None else 'LM Studio / ' + llm.model}")
    done = failed = 0
    for i, summary in enumerate(packs, 1):
        name = summary["name"]
        t0 = time.time()
        try:
            p = stash.get(f"/api/packs/{q(name)}?samples={max(args.images, 1) * 2}")
            if llm is None:
                result = clip_summary(p)
                meta = {"by": "clip"}
            else:
                urls = p.get("gallery", [])[:2] + [s["thumbnail"] for s in p.get("samples", [])]
                images = fetch_images(stash, urls, args.images, args.image_size)
                result = llm.ask(PACK_SYSTEM, pack_prompt(p, len(images)), images, PACK_SCHEMA)
                meta = {"by": "lmstudio", "model": llm.model}
            desc = str(result.get("description", "")).strip()
            keywords = clean_keywords(result.get("keywords"), name)
            if not desc:
                raise RuntimeError("empty description")
            print(f"[{i}/{len(packs)}] {name} ({p['count']} assets, {time.time() - t0:.1f}s)\n    {desc}\n    keywords: {', '.join(keywords)}")
            if not args.dry_run:
                stash.put(f"/api/packs/{q(name)}", {"description": desc, "keywords": keywords, "meta": meta})
            done += 1
        except Exception as e:  # keep going: one odd pack shouldn't stop a batch of hundreds
            failed += 1
            print(f"[{i}/{len(packs)}] {name}: FAILED – {e}", file=sys.stderr)
    print(f"\n{done} described{' (dry run, nothing saved)' if args.dry_run else ''}, {failed} failed.")


def cmd_assets(args, stash: Http):
    params = {k: v for k, v in {"pack": args.pack, "kind": args.kind, "q": args.q, "tags": args.tags, "source": args.source,
                                 "parent": "root" if args.root else None, "sort": "name", "order": "asc"}.items() if v}
    if not (args.pack or args.kind or args.q or args.tags or args.source):
        raise SystemExit("pick some assets: --pack, --kind, --q, --tags or --source (describing a whole library one by one is rarely worth it)")
    llm = make_llm(args)
    todo: list[dict] = []
    page = 1
    while True:
        r = stash.get("/api/assets?" + urllib.parse.urlencode({**params, "page": page, "per_page": 200}))
        todo += r["items"]
        if page * 200 >= r["total"] or (args.limit and len(todo) >= args.limit * 4):
            break
        page += 1
    print(f"{len(todo)} matching asset(s) · LM Studio / {llm.model}")
    done = skipped = failed = 0
    for brief in todo:
        if args.limit and done >= args.limit:
            break
        try:
            a = stash.get(f"/api/assets/{brief['id']}")
            if a.get("description") and not args.overwrite:
                skipped += 1
                continue
            images = fetch_images(stash, [a.get("thumbnail")], 1 if args.images else 0, args.image_size)
            result = llm.ask(ASSET_SYSTEM, asset_prompt(a, bool(images)), images, ASSET_SCHEMA)
            desc = str(result.get("description", "")).strip()
            tags = clean_keywords(result.get("tags"))[:6]
            print(f"{a['name']}: {desc}" + (f"  [{', '.join(tags)}]" if args.apply_tags and tags else ""))
            if not args.dry_run:
                stash.patch(f"/api/assets/{a['id']}", {"description": desc})
                if args.apply_tags and tags:
                    stash.post(f"/api/assets/{a['id']}/tags", {"tags": tags})
            done += 1
        except Exception as e:
            failed += 1
            print(f"{brief['name']}: FAILED – {e}", file=sys.stderr)
    print(f"\n{done} described, {skipped} already had a description, {failed} failed.")


def cmd_models(args, _stash):
    llm_http = Http(args.lmstudio, timeout=10)
    for m in llm_http.get("/models").get("data", []):
        print(m["id"])


def make_llm(args) -> LMStudio:
    return LMStudio(args.lmstudio, args.model, args.temperature, args.max_tokens, args.no_think, args.timeout)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stash", default=os.environ.get("STASH_URL", "http://localhost:8420"), help="Stash server URL (env STASH_URL)")
    ap.add_argument("--token", default=os.environ.get("STASH_TOKEN", ""), help="Stash password/API token if one is set (env STASH_TOKEN)")
    ap.add_argument("--lmstudio", default=os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1"), help="LM Studio OpenAI-compatible base URL (env LMSTUDIO_URL)")
    ap.add_argument("--model", default=os.environ.get("LMSTUDIO_MODEL"), help="model id as listed by `models` (default: first loaded model)")
    ap.add_argument("--images", type=int, default=4, help="images per request (0 for text-only models; default 4 for packs)")
    ap.add_argument("--image-size", type=int, default=512, help="longest image side sent to the model (default 512)")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--max-tokens", type=int, default=2048, help="room for the answer, and for thinking on reasoning models")
    ap.add_argument("--no-think", action="store_true", help="append /no_think (turns off thinking on Qwen3-style models – faster)")
    ap.add_argument("--timeout", type=float, default=600, help="seconds to wait for one model answer")
    ap.add_argument("--dry-run", action="store_true", help="print what would be written, change nothing")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("packs", help="describe packs (default: only those without a description)")
    sp.add_argument("--only", nargs="+", metavar="PACK", help="just these packs (also re-describes them)")
    sp.add_argument("--overwrite", action="store_true", help="redo packs that already have a description")
    sp.add_argument("--engine", choices=["lmstudio", "clip"], default="lmstudio", help="clip = no LLM, summarise Stash's CLIP tags")
    sp.add_argument("--min-assets", type=int, default=0, help="skip packs with fewer assets")
    sp.add_argument("--limit", type=int, default=0, help="stop after this many packs")
    sp.set_defaults(func=cmd_packs)

    sa = sub.add_parser("assets", help="describe individual assets matching a filter")
    sa.add_argument("--pack")
    sa.add_argument("--kind", help="e.g. model,skeletal_model,material")
    sa.add_argument("--q", help="search query")
    sa.add_argument("--tags", help="comma separated")
    sa.add_argument("--source", type=int)
    sa.add_argument("--root", action="store_true", help="skip assets inside other assets (objects inside a .blend)")
    sa.add_argument("--overwrite", action="store_true", help="redo assets that already have a description")
    sa.add_argument("--apply-tags", action="store_true", help="also add the suggested tags to the asset")
    sa.add_argument("--limit", type=int, default=0)
    sa.set_defaults(func=cmd_assets)

    sm = sub.add_parser("models", help="list models LM Studio has available")
    sm.set_defaults(func=cmd_models)

    args = ap.parse_args(argv)
    stash = Http(args.stash, args.token)
    try:
        args.func(args, stash)
    except RuntimeError as e:
        raise SystemExit(str(e)) from None
    except KeyboardInterrupt:
        raise SystemExit("\ninterrupted – everything printed above was already saved") from None


if __name__ == "__main__":
    main()
