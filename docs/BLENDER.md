# Blender

## Add-on: Stash Bridge (Blender 4.2+)

Install: web UI → **Integrations** → *Blender add-on → Download*, then in Blender
*Edit → Preferences → Get Extensions → ⌄ → Install from Disk…* and pick `stash_bridge.zip`.
In the add-on preferences set **Server URL** (e.g. `http://nas.local:8420`) and, if the server has
`STASH_PASSWORD`/`STASH_API_TOKEN`, the token. *Test connection* confirms it.

3D Viewport → Sidebar (`N`) → **Stash**:

* **Search** as you type, filter by type, page through results with thumbnails.
* **Import** picks the best format automatically:
  | Asset | What happens |
  |---|---|
  | Model / rig / scene | per-asset `.blend` appended into a new collection at the 3D cursor (rig, modifiers, materials, actions included) |
  | Material (Blender or PBR set) | appended and assigned to the selected objects' active slot (procedural node setups intact) |
  | Geometry node group | appended and added as a Geometry Nodes modifier on the active mesh |
  | Action | appended and assigned to the active armature |
  | HDRI | set as the world's environment texture |
  | Texture | loaded as an image |
  | Anything without a `.blend` | GLB / FBX / OBJ … imported with Blender's importers |
* **Receive** imports everything you clicked *Send to Blender* on in the web UI; toggle the link
  icon for **auto-receive** (polls every 5 s).
* **Options & push** → *Push selection to Stash* uploads the selected objects (plus armatures and
  children) as a new asset (GLB preview + `.blend`); *Upload this .blend* sends the whole saved file,
  which the server splits into objects, materials, actions and node groups. *Import by ID* accepts the
  ID shown on an asset page.

Downloads are cached in the extension's user folder (or the cache folder you set).

## What the server does with `.blend` files

`backend/blender_scripts/extract_blend.py`, per source option *.blend splitting*:

* **auto** – Blender-marked assets if the file has any, otherwise one asset per root object
  hierarchy; meshes deformed by an armature are grouped with it.
* **objects** / **collections** / **marked** – force one of the strategies.

For each object group: a self-contained `.blend` (images packed), a GLB with all compatible actions
for the web viewer, a Cycles thumbnail, stats (tris, verts, bones, shape keys, modifiers, geometry
nodes). Materials, images, actions, geometry/shader node groups and environment HDRIs become their
own assets, linked to what uses them. Asset-browser descriptions and tags are imported.

## Blender add-on collection

Every add-on found in a source (extension zips with `blender_manifest.toml`, legacy zips/folders
with `bl_info`, single-file `.py` add-ons) is catalogued under kind *Blender add-on*. Extensions are
also published as a **remote extension repository**:

*Preferences → Get Extensions → Repositories → + → Add Remote Repository*
URL `http://<nas>:8420/api/blender/extensions/index.json` (enable *Authentication* with your token
if the server is password protected).
