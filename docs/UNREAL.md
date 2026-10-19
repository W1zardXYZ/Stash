# Unreal Engine

## Indexing projects and Fab content (no Unreal needed)

Add as a source any of:

* a project folder (contains `*.uproject`) – `Content/` and every `Plugins/*/Content` are indexed;
* a bare `Content` folder, e.g. the Epic Games Launcher vault cache
  `…/Epic Games/Launcher/VaultCache/<PackName>/data/Content` (where Fab/Marketplace packs land before
  you add them to a project);
* a folder containing many of the above.

For every `.uasset`/`.umap` Stash reads the package header: asset **class** (StaticMesh,
SkeletalMesh, AnimSequence, Material, MaterialInstanceConstant, Texture2D, Blueprint, Niagara…),
**engine version**, **dependencies** (imports + soft references) and the **editor thumbnail**. From
the dependencies it links meshes → materials → textures, skeletal meshes → skeletons, and
animations → the meshes sharing their skeleton. Assets are tagged with their top-level pack folder
(`/Game/<Pack>/…`).

Downloads available right away:

* **Original** – the package files.
* **Unreal native** – the package *plus its whole dependency closure* laid out as `Content/…` (and
  `Plugins/<X>/Content/…`), ready to merge into another project. Works in the same or a newer engine
  version only.

## StashBridge plugin (UE 5.1+, Python)

Install: web UI → **Integrations** → *Unreal Engine plugin → Download*, unzip to
`YourProject/Plugins/StashBridge`, restart the editor (it enables *Python Editor Script Plugin* and
*Editor Scripting Utilities*; *glTF Exporter* is used when present). Then **Stash → Edit settings…**
opens `Config/StashBridge.json`:

```json
{ "server_url": "http://nas.local:8420", "token": "", "import_root": "/Game/Stash", "auto_receive": false, "poll_seconds": 5 }
```

(`STASH_URL` / `STASH_TOKEN` environment variables override the file.)

### Push (Unreal → Stash)

Select assets in the Content Browser → right-click → **Stash → Push to Stash** (or the main menu).
Meshes and animations are exported as FBX (+ GLB with the glTF Exporter plugin), textures as PNG,
materials as the textures they use (channel guessed from parameter/texture names). The server
matches each entry to the package it already indexed from the same project (by `/Game` path), so
that asset gains a 3D preview, Blender-ready `.blend`/glTF conversions and a rendered thumbnail.
This is the way to get Fab meshes into Blender.

### Pull (Stash → Unreal)

* **Stash → Receive sent assets** imports everything you clicked *Send to Unreal* on in the web UI
  (*Toggle auto-receive* polls in the background).
* **Stash → Import package from file…** imports a downloaded package zip.
* Python: `stash_unreal.import_asset("<asset id or uid>")`.

Format choice: *Unreal native* when the asset came from Unreal with an engine version ≤ yours,
otherwise the **Unreal package**: FBX (cm units, no leaf bones, actions baked) + textures +
`stash_manifest.json`. The importer brings textures in with correct sRGB/compression (normal maps
→ `TC_NORMALMAP`, masks → `TC_MASKS`), creates `/Game/Stash/_Shared/M_StashMaster` once (BaseColor,
Normal, ORM *or* separate Roughness/Metallic/AO, Emissive) and one material instance per material,
then assigns them to the imported mesh's slots. Skeletal meshes import with their animations.

> The plugin is written against the UE 5.x editor Python API (`AssetExportTask`, `AssetImportTask`,
> `FbxImportUI`, `MaterialEditingLibrary`, `ToolMenus`) but has not been run inside a live Unreal
> Editor yet – expect a few API-name fixes on first use, and please note the engine version when
> reporting them.

## Why geometry export needs the editor

Uncooked (editor) packages store meshes as `FMeshDescription` bulk data and textures as source
art, usually Oodle-compressed and serialized with engine-version-specific layouts; tools like
CUE4Parse/UE Viewer target *cooked* game data. The editor's own exporters are the only faithful
path, which is why pushing from Unreal is a one-time step per asset you want outside Unreal.
