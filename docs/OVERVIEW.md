# Stash – codebase overview

A map of the whole repository: what each part does, where data lives on disk and in the
database, and how the web UI, the background jobs and the Blender/Unreal plugins read and
write it. [ARCHITECTURE.md](ARCHITECTURE.md) is the shorter design summary; this file is the
reference.

```
Stash/
├── backend/                  Python 3.12 · FastAPI server, job workers, scanners, Blender scripts
│   ├── stash/                the application package
│   ├── blender_scripts/      scripts executed *inside* headless Blender
│   ├── tests/                pytest suite (+ fixtures generator)
│   └── requirements.txt
├── frontend/                 React 18 + TypeScript + three.js web UI (built into frontend/dist)
├── integrations/
│   ├── blender_addon/stash_bridge/        Blender 4.2+ extension
│   └── unreal/StashBridge/                Unreal Engine 5 editor plugin (Python)
├── deploy/truenas/stash.yaml  TrueNAS / HexOS app definition (N100 profile)
├── Dockerfile · docker-compose.yml
├── .github/workflows/        ci.yml (tests, Blender pipeline, UI build) · docker.yml (GHCR image)
└── docs/                     this file, ARCHITECTURE, TRUENAS, BLENDER, UNREAL, RESEARCH
```

---

## 1. The big picture

```
                         ┌─────────────────────────── one container ───────────────────────────┐
 Browser (React UI) ───▶ │ FastAPI  /api/*  ──────────┐                                         │
 Blender add-on ───────▶ │          /*  (built UI)    │ SQLAlchemy                              │
 Unreal plugin  ───────▶ │                            ▼                                         │
                         │                  /data/stash.db  (SQLite, WAL, FTS5)                 │
                         │                            ▲                                         │
                         │ worker threads ── jobs table (persistent queue)                      │
                         │   ├─ scanner          reads  /library/...  (your files, read-only)   │
                         │   ├─ Pillow           thumbnails                                     │
                         │   ├─ ONNX Runtime     CLIP embeddings (on-device AI)                 │
                         │   └─ blender -b ...   extract / preview / convert  (subprocess)      │
                         │                            │ writes                                  │
                         │                            ▼                                         │
                         │                  /data/store/ab/cd/<sha256>.<ext>  (generated files) │
                         └──────────────────────────────────────────────────────────────────────┘
```

* **One process** serves the API and the UI and runs the background workers (threads).
  Blender is the only extra process, started per job.
* **Your files are never modified.** Sources are indexed where they are (`linked`, the default)
  or mirrored once into `/data/library` (`import` mode). Everything Stash creates goes to
  `/data`.
* **SQLite is the only database.** It holds the catalogue, the job queue and the search index.

---

## 2. Where data is stored

### 2.1 The data directory (`STASH_DATA_DIR`, `/data` in Docker)

| Path | Contents | Created by | Safe to delete? |
|---|---|---|---|
| `stash.db` (+ `-wal`, `-shm`) | Everything in §2.2: sources, assets, files, links, tags, projects, jobs, deliveries, embeddings, search index | `db.init_engine()` | **No** – this is the library. Back it up / snapshot it. |
| `store/ab/cd/<sha256>.<ext>` | Generated files: thumbnails, preview GLBs, per-asset `.blend` files, textures extracted from `.blend` files, cached exports, project packages, add-on zips made from folders | `storage.store_file/store_bytes`, the extractor (textures) | Regenerable by rescanning / re-extracting; GC removes unreferenced files |
| `models/<model>/` | CLIP ONNX model (~600 MB) + cached vocabulary embeddings `vocab_*.npz` | `vision.download_model()` | Yes – downloaded again on next use |
| `extracted/<fingerprint16>/` | Unpacked `.zip` asset packs found in sources (scanned like folders, prefix `pack.zip!/`) | `Scanner.scan_archive` | Yes – re-extracted on rescan |
| `uploads/<date_time>/…` | Files dropped onto the web UI (the *Uploads* source) | `POST /api/upload` | No (they're your files now) |
| `bundles/<origin>/` | Placeholder root of the *Unreal exports* / *Blender pushes* sources; the pushed files themselves go to `store/` | `api/integrations._bundle_source` | – |
| `library/<id>-<name>/` | Mirror of a source added with `mode=import` | `Scanner._import_copy` | Only together with that source |
| `work/` | Temporary directories for running jobs | jobs | Yes (when nothing runs) |

**Content addressing.** `storage.store_file(path)` hashes the file (SHA-256) and stores it as
`store/<h[0:2]>/<h[2:4]>/<h><ext>`. The same bytes are stored once, however many rows point at
them. Rows reference store files by that relative path (`AssetFile.storage = "store"`).
`storage.resolve(asset_file)` turns any row into a real path:

```python
store  → settings.store_dir / f.path          # generated file
source → Path(f.path)                         # absolute path inside your library mount
```

### 2.2 Database schema (`backend/stash/models.py`)

```
sources 1──n assets 1──n asset_files
                │  └──1 asset_embeddings
                ├── n──m tags            (asset_tags)
                ├── n──m projects        (project_assets, with note)
                ├── parent_id → assets   (container → contents, e.g. .blend → objects)
                └── asset_links          (typed graph between assets)
jobs        (queue; optional asset_id / source_id)
deliveries  ("Send to Blender/Unreal" inbox)
asset_fts   (FTS5 virtual table, rowid = assets.id)
```

| Table | Key columns | Notes |
|---|---|---|
| **sources** | `kind` folder/unreal_project/blender_file/uploads/bundles · `path` · `mode` linked/import · `status` idle/scanning/error · `options` JSON · `meta` JSON | `options`: `group_mode` (`.blend` splitting), `render_thumbnails`, `extract_archives`, `rescan_hours`, `ignored` (paths the user removed). `meta`: `unreal_projects`, `last_result`. |
| **assets** | `uid` (stable public id) · `name` · `kind` · `source_id` · `source_path` · `sub_key` · `parent_id` · `format` · `status` ok/missing · flags `is_rigged/is_animated/is_procedural/has_textures` · `poly_count` · `size` · `fingerprint` · `favorite` · `rating` · `description` · `thumbnail_file_id` · `preview_file_id` · `meta` JSON | **Identity** = `(source_id, source_path, sub_key)` – rescans update the same row, so your tags/ratings survive. `sub_key` addresses things inside a container: `object:Chair`, `material:Wood`, `action:Walk`, `image:T_Wood`, `node_group:Scatter`. |
| **asset_files** | `asset_id` · `role` · `storage` store/source · `path` · `name` · `format` · `size` · `sha256` · `label` · `meta` | Roles: `source`, `sidecar` (.mtl/.bin/.uexp/.ubulk), `texture` (label = channel), `preview` (GLB), `thumbnail` (label render/embedded/image/viewer), `blend` (per-asset .blend), `gallery`, `ue_export` (FBX pushed from Unreal), `export` (cached download, label = target). |
| **asset_links** | `from_id` · `to_id` · `relation` | `uses_material`, `uses_texture`, `uses_skeleton`, `animates`, `depends_on`. |
| **tags / asset_tags** | `name` (lower-case, unique) · `color` | |
| **projects / project_assets** | `name` · `description` · `color` · `note` | Your collections ("Game A"). |
| **jobs** | `type` · `status` queued/running/done/error/cancelled · `priority` · `progress` · `message` · `payload` · `result` · `log` · `dedupe_key` | The queue *and* its history. |
| **deliveries** | `app` blender/unreal · `asset_id` · `target` · `status` pending/delivered/failed | Polled by the plugins. |
| **asset_embeddings** | `asset_id` (PK) · `model` · `vector` (float16 bytes) · `source_sha` | `source_sha` = SHA of the thumbnail it came from; a new thumbnail → re-embed. |
| **asset_fts** | `name`, `tags`, `kind`, `path`, `description`, `extra` | Rebuilt per asset by `search.index_asset`. `extra` holds Unreal class, materials, animations, channels, pack, category, AI suggestions… |

Tables are created by `Base.metadata.create_all` on start-up (no migration tool yet – new
columns need a migration step before the schema changes in a released version).

### 2.3 What lives in `assets.meta`

`meta` is a free-form JSON object; filterable facts are promoted to real columns. Common keys:

| Key | Written by | Meaning |
|---|---|---|
| `inspect` | scanner (`ingest/inspect.py`) | Triangles, bones, animations, materials, referenced textures from glTF/FBX/OBJ/STL/PLY/DAE |
| `folder`, `pack` | scanner | Folder of the file; top-level folder/zip/`/Game/<Pack>` it came from |
| `pbr`, `channels`, `resolution` | scanner | Texture-set material: channel → file |
| `image`, `channel` | scanner / extractor | Image size; which material input a texture drives |
| `sidecar` | scanner | Megascans/Fab metadata JSON (name, id, properties) |
| `ue_class`, `ue_package`, `ue_engine_version`, `ue_dependencies`, `ue_project`, `ue_mount` | `ingest/uasset.py` | Unreal package header facts |
| `stats`, `graph`, `materials`, `actions`, `frame_range`, `targets`, `tree_type` | `.blend` extractor | Per object/material/action/node-group data; `graph` = serialized node tree |
| `extracted`, `scene_stats`, `blender_version`, `extract_errors` | extractor (on the `.blend` container) | |
| `preview_fingerprint`, `blender_stats` | preview job | Detects when a preview is stale |
| `addon` | scanner | Blender add-on / extension manifest |
| `auto_tags` | `embed_pending` | On-device AI suggestions `[{tag, category, score}]` |
| `ai`, `category` | `ai_tag` (Claude) | Category, tags, description, model used |
| `renamed` | API | You renamed it – scans won't overwrite the name |
| `origin`, `bundle_origin`, `blend_file` | ingest | Where the asset came from |

### 2.4 Files the plugins keep

| Where | What |
|---|---|
| Blender: extension user dir `…/extensions/.user/user_default/stash_bridge/cache/<uid>/` (or the folder set in preferences) | Downloaded assets and thumbnails |
| Unreal: `<Project>/Config/StashBridge.json` | `server_url`, `token`, `import_root`, `auto_receive`, `poll_seconds` |
| Unreal: `<Project>/Saved/StashCache/<uid>/` | Downloaded packages |
| Unreal: `/Game/Stash/...` | Imported assets; `/Game/Stash/_Shared/M_StashMaster` master material |

---

## 3. How data is accessed

### 3.1 Inside the server

* **Sessions:** `db.get_db()` (one per request, FastAPI dependency) and `db.session_scope()`
  (context manager, commit on success) for jobs. SQLite runs in WAL mode with a 30 s busy
  timeout, so the UI keeps reading while a job writes. Long scans commit periodically
  (`Scanner.checkpoint`) so job progress updates aren't blocked.
* **Search:** `search.ranked_subquery(q)` joins `asset_fts` (bm25 ranking in SQL); query syntax:
  every word is a prefix match, `-word` excludes, `"exact phrase"`. Smart search
  (`semantic=1`) goes through `vision.text_search` + `vision.hybrid_rank` instead.
* **Facets** (`GET /api/facets`) are `GROUP BY` counts over the same filtered query.
* **Vectors:** `vision.index` keeps all embeddings in one in-memory float16 matrix
  (~100 MB per 100k assets), reloaded when embeddings change; cosine search is a matrix product.

### 3.2 REST API (all under `/api`, JSON; OpenAPI docs at `/docs`)

Auth: if `STASH_PASSWORD`/`STASH_API_TOKEN` is set, every endpoint except `/api/status` and
`/api/auth/*` needs the session cookie (from `POST /api/auth/login`) or
`Authorization: Bearer <token>` (or `?token=`).

| Area | Endpoints |
|---|---|
| Browse | `GET /assets` (filters: `q`, `semantic`, `kind`, `tags`, `flags` rigged/animated/procedural/textured/favorite/untagged/duplicate/previewable, `source`, `project`, `pack`, `format`, `ue_class`, `parent=root\|<id>`, `status`, `sort`, `order`, `page`, `per_page`) · `GET /facets` · `GET /assets/{id}` (files, links, children, projects, export targets, recent jobs) · `GET /assets/{id}/similar` |
| Edit | `PATCH /assets/{id}` (name, description, rating, favorite, kind) · `POST /assets/{id}/tags` · `DELETE /assets/{id}/tags/{name}` · `POST /assets/bulk` (tags, project, favorite, kind, delete) · `DELETE /assets/{id}` (forget; rescans skip it) · `POST /assets/{id}/thumbnail` (data URL from the viewer) · `POST /assets/{id}/thumbnail/{file_id}` |
| Files | `GET /files/{id}[/{name}]` (`?download=1` for attachment) · `GET /assets/{id}/raw/{relative path}` (serves files next to a linked source so `.gltf`/`.obj`/`.fbx` find their textures) |
| Convert | `POST /assets/{id}/export {target, force}` → cached file or `job_id` · `POST /assets/{id}/reprocess` |
| AI | `POST /ai/accept-suggestions {ids, min_score}` (on-device) · `POST /ai/tag {ids}` (Claude) |
| Library | `GET/POST /sources` · `PATCH/DELETE /sources/{id}` · `POST /sources/{id}/scan` · `POST /sources/{id}/extract-all` · `GET /fs?path=` (folder picker, limited to `STASH_BROWSE_ROOTS`) · `POST /upload` |
| Organise | `GET /tags` · `PATCH /tags/{id}` (rename, colour, `merge_into`) · `DELETE /tags/{id}` · `GET/POST /projects` · `GET/PATCH/DELETE /projects/{id}` · `POST /projects/{id}/assets` · `DELETE /projects/{id}/assets/{asset_id}` · `POST /projects/{id}/package {target}` |
| Jobs | `GET /jobs` · `GET /jobs/{id}` (with log) · `GET /jobs/{id}/download` · `POST /jobs/{id}/cancel` · `POST /jobs/{id}/retry` · `DELETE /jobs` (clear finished) |
| Plugins | `POST /ingest/bundle` (zip with `manifest.json`) · `POST/GET /deliveries` · `POST /deliveries/{id}/ack` · `DELETE /deliveries` · `GET /integrations/blender-addon.zip` · `GET /integrations/unreal-plugin.zip` · `GET /blender/extensions/index.json` |
| Server | `GET /status` · `GET /status/blender` · `GET /maintenance/storage` · `POST /maintenance/cleanup` · `POST /maintenance/reindex` |

Export targets per asset are listed in `GET /assets/{id}` → `targets`: `original`, `blend`,
`glb`, `gltf`, `fbx`, `fbx_unity`, `unreal` (FBX + textures + manifest), `unreal_native`
(`.uasset` + dependency closure), `obj`, `usd`, `usdz`, `stl`, `ply`, `abc`, and `png/jpg/tga/tif`
for textures.

### 3.3 Who calls what

| Client | Reads | Writes |
|---|---|---|
| Web UI (`frontend/src/api.ts`) | everything above | edits, tags, projects, sources, exports, uploads, deliveries |
| Blender add-on (`stash_bridge/client.py`) | `/assets` search, `/assets/{id}`, `/export` → `/jobs/{id}` → `/files/{id}`, `/deliveries?app=blender` | `/ingest/bundle` (push selection), `/upload` (push .blend), `/deliveries/{id}/ack` |
| Unreal plugin (`stash_unreal/client.py`, same file) | same pattern with `app=unreal` | `/ingest/bundle` (push Content Browser selection) |
| Blender's extension system | `/blender/extensions/index.json`, `/files/{id}` | – |
| Your own scripts (e.g. a batch tagger) | `/assets?…&per_page=500`, thumbnails via `/files/{thumbnail id}` | `PATCH /assets/{id}`, `POST /assets/{id}/tags`, `/assets/bulk` |

---

## 4. How data gets in (and what happens to it)

### 4.1 Job system (`stash/jobs.py`, handlers in `stash/processing.py`)

`enqueue(type, payload, asset_id, source_id, priority)` inserts a `jobs` row (identical queued
jobs are de-duplicated). `STASH_WORKERS` threads claim the highest-priority queued job, run its
handler with a `JobContext` (progress, log, cancellation) and store the result. Running jobs are
re-queued after a restart. A scheduler thread starts due rescans (`rescan_hours`), unloads an idle
CLIP model and runs the daily cleanup.

| Job | Priority | Does | Queued by |
|---|---|---|---|
| `scan_source` | 50 | Walk a source, upsert assets, queue follow-ups | Add source, Rescan, upload, scheduler |
| `export` | 40 | Produce a download (`convert/exporter.py`) | Export button, plugins |
| `package_project` | 30 | Export every project asset into one zip | Project → Export |
| `thumbnail` | 20 | Pillow (or Blender for EXR/HDR/TGA) thumbnail of textures/sets/HDRIs | scanner, extractor |
| `extract_blend` | 10 | Split a `.blend` into assets | scanner (new/changed `.blend`) |
| `preview` | 5 | Model file → web GLB + Cycles thumbnail | scanner, bundles |
| `ai_tag` | 2 | Claude category/tags/description | *Claude tag* |
| `embed_pending` | 1 | CLIP-embed up to 200 thumbnails, suggest tags, re-queue if more | after scans/previews/thumbnails |

### 4.2 Scanning a folder (`ingest/scanner.py`)

1. **Walk** the tree (skipping `Intermediate`, `Saved`, `DerivedDataCache`, `.git`, …). Folders
   with a `.uproject` or a `Content/` full of `.uasset`s go to the Unreal path; add-on folders
   are catalogued as add-ons.
2. **Images** are split by name into PBR **texture sets** (`ingest/formats.split_texture_name`:
   `Rock_01_4K_BaseColor` → (`Rock_01`, base_color)). Sets with ≥ 2 channels become `material`
   assets; preview images (`preview.jpg`, `thumb.png`) become gallery images.
3. **Models** are inspected (`ingest/inspect.py`, pure Python), their referenced textures
   resolved, and linked to texture sets by file reference, name, or "only model in this folder"
   – with `Meshes/` + `Textures/` sibling layouts understood.
4. Loose images → `texture`/`hdri`; audio; Substance files; single-file add-ons; zips (add-on or
   asset pack, which is unpacked and scanned).
5. **`.blend`** files → `scene` container + `extract_blend` job; the thumbnail Blender embedded in
   the file is read without Blender.
6. **Megascans/Fab JSON sidecars** add tags and properties.
7. Assets not seen this time → `status = missing`; removed ones (`options.ignored`) are dropped.

### 4.3 Unreal content (`ingest/uasset.py`)

Reads each package header in pure Python: summary (version-tolerant, incl. cooked/unversioned and
the UE 5.6+ layout), name table, imports → **class** (StaticMesh, SkeletalMesh, Material…),
**dependencies** (`/Game/...` packages, incl. soft references), **engine version**, and the
embedded **editor thumbnail**. The scanner turns dependencies into links (mesh → material →
texture, skeletal mesh → skeleton, animation → meshes on that skeleton). Mesh *data* isn't read –
the Unreal plugin pushes FBX/glTF exports, which are matched back by `/Game` path.

### 4.4 `.blend` splitting (`blender_scripts/extract_blend.py`)

Runs inside `blender -b file.blend`. Groups objects (Blender-marked assets → collections →
root hierarchies; deformed meshes join their armature). Packed/generated textures are written
once into the store and referenced. Per group it writes a self-contained-by-reference `.blend`
(`bpy.data.libraries.write`), a preview GLB (textures ≤ 1024 px WebP) and a Cycles thumbnail;
it also emits materials (node graph as JSON, procedural flag), images (with the shader input they
drive), actions (matched to rigs by bone names) and node groups. Output is a `manifest.json` that
`processing.ingest_manifest` turns into child assets (`parent_id` = the `.blend`) and links.

### 4.5 Exports (`convert/exporter.py` → `blender_scripts/convert.py`)

Chooses the best input per asset (per-asset `.blend` > Unreal-pushed FBX > source file + matched
texture set), runs Blender's importers/exporters with presets (Unreal: cm units, no leaf bones;
Unity: axis/scale), packs textures for `.blend` downloads, zips multi-file outputs, and caches the
result as an `export` file (capped by `STASH_EXPORT_CACHE_GB`, see `maintenance.py`).

### 4.6 On-device AI (`stash/vision.py`)

`embed_pending` embeds each thumbnail with CLIP ViT-B-32 (ONNX Runtime, CPU) into
`asset_embeddings`, computes zero-shot suggestions against a vocabulary per category and asset
kind (stored in `meta.auto_tags`, searchable, accepted on click). Smart search embeds the query
text; similar-assets uses nearest neighbours of the asset's vector.

---

## 5. Module reference

### Backend (`backend/stash/`)

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app, auth middleware, login, routers, serves `frontend/dist`, starts workers |
| `__main__.py` | CLI: `serve`, `scan PATH`, `reindex`, `run-jobs` |
| `config.py` | All `STASH_*` settings |
| `db.py` | Engine (WAL pragmas), sessions, FTS5 table creation |
| `models.py` | ORM tables (§2.2) |
| `storage.py` | Content-addressed store, `resolve()`, add stored/linked files |
| `search.py` | FTS indexing and query building |
| `jobs.py` | Queue, worker pool, job context |
| `processing.py` | Job handlers, `.blend` manifest ingest, scheduler |
| `blender.py` | Runs Blender scripts (inactivity timeout, nice level, progress parsing) |
| `images.py` | Pillow thumbnails, image info, conversions |
| `vision.py` | CLIP model, vocabulary, suggestions, vector index |
| `ai.py` | Optional Claude tagging |
| `maintenance.py` | Export-cache cap, package expiry, store GC, storage stats |
| `ingest/scanner.py` | Folder/Unreal/archive/add-on scanning, texture-set matching |
| `ingest/formats.py` | Extension classes, texture channel naming |
| `ingest/inspect.py` | glTF/FBX/OBJ/STL/PLY/DAE inspectors |
| `ingest/uasset.py` | Unreal package header reader |
| `ingest/addons.py` | Blender add-on / extension manifests |
| `convert/exporter.py` | Export targets and packaging |
| `api/assets.py` | Browse, edit, tags, bulk, files, exports, smart search, similar |
| `api/library.py` | Status, sources, folder picker, uploads, tags, projects, jobs, maintenance |
| `api/integrations.py` | Bundles, deliveries, Blender extension repository, plugin downloads |
| `api/common.py` | Auth check, serializers |

### Blender scripts (`backend/blender_scripts/`, run inside Blender)

`extract_blend.py` (split files) · `preview_model.py` (any model → GLB + thumbnail; HDR/EXR → PNG)
· `convert.py` (any → any, PBR material builder) · `stash_common.py` (args, node-tree
serialization, framing/lighting/rendering, texture externalizing/downscaling).

### Frontend (`frontend/src/`)

`main.tsx` (shell, routes, drag-and-drop upload, login gate) · `api.ts` (typed client, labels)
· `pages/Library.tsx` (facets, grid, Smart search, bulk actions) · `pages/AssetPage.tsx`
(viewer, details, suggestions, related/similar, files, node graph, export/send) ·
`pages/Sources.tsx` · `pages/Projects.tsx` · `pages/Misc.tsx` (tags, jobs, integrations/server,
login) · `components/Viewer.tsx` (three.js) · `components/NodeGraph.tsx` · `components/ui.tsx`
· `components/Icon.tsx` · `styles.css` (dark/light themes, responsive).

### Integrations

* `integrations/blender_addon/stash_bridge/` – `__init__.py` (UI panel, import logic per asset
  kind, push, deliveries, threaded HTTP with main-thread callbacks), `client.py`,
  `blender_manifest.toml`.
* `integrations/unreal/StashBridge/` – `StashBridge.uplugin`, `Content/Python/init_unreal.py`
  (startup), `stash_unreal/` (`__init__` config, `exporter.py` push, `importer.py`
  packages/native/master material, `menus.py` menus + auto-receive tick, `client.py` – identical
  copy of the Blender one, enforced by a test).

### Tests (`backend/tests/`)

`test_api.py` (full flow on a generated library) · `test_scanner.py` (layouts, Megascans
sidecars) · `test_vision.py` (stubbed CLIP) · `test_ai.py` (stubbed Claude) ·
`test_unreal_plugin.py` (plugin vs. a stub `unreal` module) · `test_inspect.py` ·
`test_formats.py` · `test_blender_pipeline.py` (real Blender, read-only library; opt-in via
`STASH_TEST_BLENDER=1`) · `fixtures/make_fixtures.py` (builds the test `.blend`/FBX/GLB).

---

## 6. Extending it

| You want to… | Touch |
|---|---|
| Index a new file type | `ingest/formats.py` (classify), `ingest/inspect.py` (metadata), `Scanner.scan_folder_files` |
| Add an export format | `convert/exporter.py` (`BLENDER_TARGETS`, `available_targets`), `blender_scripts/convert.py` (`export()`), label in `frontend/src/api.ts` `TARGET_LABELS` |
| Add a background step | `@handler("name")` in `processing.py`, a priority in `PRIORITY`, `enqueue(...)` where it should start |
| Store a new per-asset fact | `meta` (no migration) – promote to a column in `models.py` only if you filter on it |
| Make it searchable | add the `meta` key to `search._meta_text` |
| Batch-process assets from outside (e.g. a local LLM) | read `GET /api/assets` + thumbnails, write `PATCH /api/assets/{id}` / `POST /api/assets/{id}/tags`; no server change needed |

### Note on naming: "project" vs "pack"

Two different things can be called a project:

* **Stash Project** – a collection *you* create (`projects` table), e.g. "Game A".
* **Pack** – where a file came from: the top-level folder of a source, the zip it was in, or the
  `/Game/<Pack>` folder of Unreal content. Stored per asset as `meta.pack` (filter
  `?pack=Name`, *Packs* in the sidebar). There is no pack table yet, so a pack has no description
  of its own; a per-pack description would need a small `packs` table (name, source, description,
  tags).
