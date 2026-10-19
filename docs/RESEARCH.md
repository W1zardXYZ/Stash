# Research: existing tools, and how Unreal & Blender store assets

Done October 2026 before writing Stash. Goal: one self-hosted library for Fab/Unreal content,
`.blend` files, generic asset packs and Blender add-ons, running on a NAS in Docker.

## Existing projects

| Project | What it is | Why not fork it |
|---|---|---|
| [Manyfold](https://github.com/manyfold3d/manyfold) (AGPL-3.0, Rails, ~2.2k★) | Mature self-hosted DAM for **3D-printing** files (STL/3MF/OBJ), Docker, tags, creators, ActivityPub | Model = "printable parts". No rigs, animations, materials, texture sets, Unreal or Blender internals. Ruby/Mittsu stack can't reuse Blender/Unreal Python. |
| [VanDAM](https://github.com/Millerboy3/van_dam) | Early fork of Manyfold | Same scope, less mature. |
| [Modelibr](https://github.com/Papyszoo/Modelibr) (.NET 9 + React, ~10★) | Game asset library: models, texture sets, versions, WebDAV for `.blend` | **Business Source License**, not open source; no rigs/animation/Unreal; very young. Good idea borrowed: WebDAV-style "save into library". |
| [UE-AssetManager](https://github.com/RmaNMetaverse/UE-AssetManager) | Node web app + UE plugin that imports from links | Link catalogue, doesn't read your projects or convert. |
| [Multi-Engine Asset Browser](https://github.com/ogconz/Multi-Engine-Asset-Browser) | Windows desktop browser for installed game files | Desktop-only, game-rip oriented. |
| STL Shelf, Printventory, GyroidVault, Makers Vault | Print-file managers | 3D-printing only. |
| [3d-asset-server](https://github.com/arielshad/3d-asset-server) | Search aggregator for online asset sites (+MCP) | Not a local library. |
| AtroDAM, Phraseanet | Generic DAMs | No 3D understanding at all. |
| Commercial: Eagle, Connecter, Quixel Bridge, Kitbash Cargo | Desktop asset browsers | Not self-hosted / closed / vendor-locked. |

Reverse-engineering tools for Unreal packages:

* [CUE4Parse](https://github.com/FabianFG/CUE4Parse) / [FModel](https://github.com/4sval/FModel) (C#) – excellent for **cooked** game paks (IoStore, Oodle, mappings).
* [UE Viewer / umodel](https://github.com/gildor2/UEViewer) – cooked meshes → glTF/PSK.
* [UAssetAPI](https://github.com/atenfyr/UAssetAPI) (C#) – low-level `.uasset` read/write. Its test assets were used to validate Stash's header reader.

None of them exports meshes from **uncooked editor content** (what Fab gives you) reliably: the
source mesh lives in `FMeshDescription` bulk data, frequently Oodle-compressed and versioned per
engine build. So Stash reads package *headers* in pure Python (class, deps, engine version,
thumbnail) and uses the Unreal Editor itself, via a Python plugin, for geometry export.

**Conclusion:** no project covered more than ~20% of the requirements, the closest ones are
print-focused or not open source, and both target DCCs script in Python. Stash is a fresh
MIT project in Python (FastAPI) + React/three.js, using Blender as the conversion engine.

## How Unreal stores content

* A **project** = `Name.uproject` (JSON: `EngineAssociation`, enabled plugins) + `Content/` +
  `Config/` + optional `Plugins/*/Content`. Content paths map to mount points: `Content/X/Y.uasset`
  is the package `/Game/X/Y`; plugin content mounts at `/<PluginName>/…`.
* A **package** = `.uasset` (header + exports) and optionally `.uexp` (export data, split by the
  cooker or by editor settings), `.ubulk`/`.uptnl` (bulk data). `.umap` = level.
* The **package summary** (`FPackageFileSummary`) starts with tag `0x9E2A83C1`, a negative
  *legacy file version* (−7 = UE4.27, −8 = UE5.0–5.5, −9 = UE5.6+), UE4/UE5 object versions, custom
  versions, then the name table, import/export tables, thumbnail table. Since 5.6 the saved hash and
  header size moved *before* the custom versions. Cooked "unversioned" packages store 0 for all
  versions – a reader has to try plausible layouts and validate them (what Stash does).
* **Imports** reference other packages (`ClassName == "Package"`) and classes
  (`ClassName == "Class"`) – enough to recover the asset's class and its dependency graph without
  touching export data. Soft references only appear as names in the name map.
* Every editor-saved package embeds a **thumbnail** (PNG in UE4, JPEG in recent UE5) prefixed by
  width/height/size – Stash shows it as the asset's preview.
* **Engine compatibility:** packages open in the engine version that saved them or newer, never
  older. "Migrate" = copy the package plus its dependency closure keeping `/Game/...` paths – that's
  what Stash's *Unreal native* export produces.
* **Fab** delivers Unreal content into a project or the launcher **VaultCache**
  (`VaultCache/<Pack>/data/Content`), and for many listings also as downloadable "additional
  formats" (FBX/OBJ/glTF/Blend/USD zips). Fab's website has no public download API and blocks bots, so
  Stash doesn't try to log in to Fab – it indexes what you've already downloaded.
* Editor-side export: `AssetExportTask` with `StaticMeshExporterFBX`, `SkeletalMeshExporterFBX`,
  `AnimSequenceExporterFBX`, texture exporters chosen by file extension, and
  `GLTFExporter.export_to_gltf` when the glTF Exporter plugin is enabled. Import: `AssetImportTask`
  + `FbxImportUI`; materials via `MaterialEditingLibrary`.

## How Blender stores content

* A `.blend` is a datablock database: objects, meshes, armatures, materials (node trees), images
  (external or packed), actions, node groups (geometry/shader), collections, worlds, scenes.
* **Asset marks** (`id.asset_data`) flag datablocks for the Asset Browser, with description, tags and
  catalog – Stash respects them (`--group marked`/`auto`).
* `bpy.data.libraries.write(path, datablocks)` writes a subset of datablocks plus everything they
  depend on into a new `.blend` – how Stash makes one self-contained file per object/material/action
  /node group (images packed first). `bpy.data.libraries.load(...)` appends them back – how the add-on
  imports.
* **Procedural materials and geometry nodes can't survive glTF/FBX** (they're Blender node graphs).
  Stash therefore always keeps the `.blend` version for Blender, serializes the node graph to JSON for
  the web view, and renders thumbnails with Cycles so the real look is visible.
* Actions are matched to armatures by the bone names in their F-curve paths; Blender 4.4+ "slotted
  actions" are handled alongside legacy `action.fcurves`.
* Blender 4.2+ **extensions** are zips with `blender_manifest.toml`; remote repositories are a static
  `index.json` (`version: v1`, entries with `archive_url`, `archive_hash`), which Stash generates.

## Format conversion engine

Blender (headless) imports FBX, OBJ, glTF, STL, PLY, USD/USDZ, Alembic, X3D (and Collada until 5.0)
and exports all of those plus `.blend`. Running it in the container gives one well-tested converter
for every path, including rig/animation round-trips and texture relinking
(`file.find_missing_files`). Assimp was considered but loses Blender materials/animation fidelity
and can't produce `.blend`.
