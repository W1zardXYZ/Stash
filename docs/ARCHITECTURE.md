# Architecture

```
                        ┌──────────────────── Docker container ─────────────────────┐
 Browser ──HTTP──▶ FastAPI (backend/stash/main.py)                                   │
  React + three.js │   /api/*  REST       ── SQLite (WAL) + FTS5  ── /data/stash.db  │
                   │   /*      built UI                                               │
                   │   worker threads ◀── jobs table (persistent queue)               │
                   │      ├─ scanner (pure Python: walk, inspect, .uasset headers)    │
                   │      ├─ Pillow thumbnails                                        │
                   │      └─ headless Blender (blender_scripts/*.py)                  │
                   │            extract_blend · preview_model · convert               │
                   │   /data/store  content-addressed derived files (sha256)          │
 Blender add-on ───┤   /library/... your asset folders (mounted read-only)            │
 Unreal plugin ────┘                                                                   │
                        └───────────────────────────────────────────────────────────┘
```

## Data model (`backend/stash/models.py`)

* **Source** – a folder / Unreal project / `.blend` / uploads / bundles pushed by plugins.
  `mode=linked` indexes in place; `mode=import` mirrors it into `/data/library/<id>`.
  `options`: `group_mode`, `render_thumbnails`, `extract_archives`, `ignored` (removed paths).
* **Asset** – one browsable thing. `kind` ∈ model, skeletal_model, animation, material, texture,
  hdri, node_group, scene, blueprint, unreal_asset, addon, audio, other. Identity is
  `(source_id, source_path, sub_key)`; `sub_key` addresses things inside a container
  (`object:Chair`, `material:Wood`, `action:Walk` inside a `.blend`). `parent_id` = container.
  Filterable flags as columns (`is_rigged`, `is_animated`, `is_procedural`, `has_textures`,
  `poly_count`), everything else in `meta` JSON (inspector output, node graphs, Unreal class/deps…).
* **AssetFile** – files with a role: `source`, `sidecar` (.mtl, .bin, .uexp/.ubulk), `texture`
  (label = channel), `preview` (GLB for the viewer), `thumbnail`, `blend` (self-contained per-asset
  .blend), `gallery` (preview images shipped with packs), `ue_export` (FBX pushed from Unreal),
  `export` (cached conversions, label = target). `storage=source` → absolute path on disk,
  `storage=store` → `/data/store/ab/cd/<sha256>.<ext>`.
* **AssetLink** – `uses_material`, `uses_texture`, `uses_skeleton`, `animates`, `depends_on`.
* **Tag**, **Project** (+ProjectAsset), **Job**, **Delivery** (Send-to-Blender/Unreal inbox).

Search: an FTS5 table `asset_fts(name, tags, kind, path, description, extra)` maintained by
`search.index_asset`; names are also indexed split on CamelCase/underscores so `SM_WoodenChair_01`
matches "wooden chair". Queries are prefix matches, `-word` excludes, `"phrase"` is exact.

## Storage

* Your files are only read (sources are mounted `:ro` in the TrueNAS profile; tests run the
  Blender pipeline against a read-only library).
* Everything Stash generates lives in `/data/store/ab/cd/<sha256>.<ext>`, so identical files are
  stored once no matter how many assets reference them.
* `.blend` splitting writes each packed/generated texture to the store **once** and the per-asset
  `.blend` files reference it (a 40-object kitbash file sharing one 2K texture: 981 MB → 36 MB).
  Textures are packed only when a `.blend` is downloaded, so downloads still open anywhere.
* Web-preview GLBs carry WebP textures capped at `STASH_PREVIEW_TEXTURE_SIZE` (1024 px).
* Conversions/downloads are cached as `export` files up to `STASH_EXPORT_CACHE_GB` (oldest evicted);
  project packages expire after 7 days; a daily garbage collector deletes store files no row
  references (replaced thumbnails, removed assets).

## On-device AI (`vision.py`)

CLIP ViT-B-32 (OpenAI weights, ONNX exports published by Immich) in ONNX Runtime on the CPU.
`embed_pending` jobs embed thumbnails (re-run when a thumbnail changes) into `asset_embeddings`
(float16). Tag suggestions are zero-shot: the image embedding is compared to text embeddings of a
fixed vocabulary per category (objects, materials, styles, colours, HDRI environments), with a
softmax per category and per-kind category sets (no objects for material balls, nothing for
normal/roughness maps); they're stored in `meta.auto_tags`, full-text indexed, and become real
tags only when accepted. Smart search embeds the query text and ranks by cosine similarity
against an in-memory matrix, boosted by ordinary full-text matches and cut at a window below the
best score.

## Pipeline

1. **scan_source** (`ingest/scanner.py`)
   * walks the tree, skipping `Intermediate/Saved/DerivedDataCache/...`
   * folders with a `.uproject` or a `Content/` with `.uasset`s → `scan_unreal_content`
     (header parse per package, links from imports, thumbnails from the package)
   * add-on folders / zips / single-file add-ons → `addon` assets (zipped into the store)
   * zip packs → unpacked to `/data/extracted/<fingerprint>` and scanned with a `pack.zip!/` prefix
   * images grouped into PBR sets by `split_texture_name`; sets with ≥2 channels → `material`
   * models inspected (`ingest/inspect.py`: glTF, binary/ASCII FBX, OBJ+MTL, STL, PLY, DAE),
     referenced textures resolved, models linked to matching sets in their folder
   * `.blend` files → `scene` container + `extract_blend` job; embedded preview read without Blender
   * queues follow-up jobs (preview, thumbnail, extract) and marks vanished files `missing`
2. **extract_blend** – `blender -b file.blend --python extract_blend.py`: groups objects
   (marked assets → collections → root hierarchies; meshes join the armature that deforms them),
   writes per-asset `.blend` via `libraries.write` (images packed), GLB via the glTF exporter (run
   from the file's own scene – exporting from freshly created scenes segfaults in 4.5), Cycles
   thumbnails in throw-away scenes, node graphs as JSON, actions matched to rigs by bone names.
   `processing.ingest_manifest` turns the manifest into child assets + links.
3. **preview** – non-GLB models are imported into Blender, textures relinked (or a matched PBR set
   applied), exported to GLB for the browser and rendered to a thumbnail.
4. **thumbnail** – Pillow (or Blender for EXR/HDR/TGA/PSD) for textures and texture sets.
5. **export** (`convert/exporter.py`) – chooses an input per asset (per-asset `.blend` > Unreal
   export > source file + matched PBR set) and runs `convert.py`; packages multi-file outputs as zips;
   `unreal` adds textures + `stash_manifest.json`; `unreal_native` walks the dependency closure.
   Results are cached as `export` files keyed by target and source fingerprint.
6. **package_project** – exports every project asset to one target and zips it.

Jobs are rows in `jobs` claimed by worker threads (`jobs.py`), survive restarts, report progress
(Blender scripts print `[stash-progress] n/total`), can be cancelled (the Blender process is killed)
and retried. Identical queued jobs are de-duplicated.

## Plugins ↔ server

* Blender add-on & Unreal plugin share `client.py` (stdlib only).
* Pull: `POST /api/assets/{id}/export {target}` → job → `GET /api/files/{id}`.
  Blender prefers `blend`; Unreal prefers `unreal_native` when the engine version allows, else
  `unreal`.
* Push: zip with `manifest.json` → `POST /api/ingest/bundle`. Unreal entries carry `ue_package`
  and are matched to the scanned package of the same project.
* Send-to-app: `POST /api/deliveries` from the UI; plugins poll `GET /api/deliveries?app=…` and ack.

## Frontend (`frontend/src`)

React 18 + react-router + three.js, no UI framework. `pages/Library.tsx` (facets, infinite grid,
bulk actions), `pages/AssetPage.tsx` (viewer, metadata, tags, projects, related, files, node graph,
export), `components/Viewer.tsx` (model/PBR/HDRI viewer, animation mixer, skeleton helper, capture),
`components/NodeGraph.tsx` (read-only node tree SVG).
