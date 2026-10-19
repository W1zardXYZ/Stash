# TrueNAS SCALE / HexOS

Tested image settings target an **Intel N100 with 8 GB RAM**; anything bigger just gets faster.
TrueNAS SCALE 24.10 ("Electric Eel") and later run apps on Docker and accept Compose YAML.
HexOS is built on TrueNAS SCALE: if its own app screen doesn't offer custom apps, open the TrueNAS
web UI it runs on and follow the same steps.

## 1. Create datasets

In **Datasets**, create:

| Dataset | Purpose | Permissions |
|---|---|---|
| `tank/apps/stash` | Stash's database, previews, thumbnails, exports (keep on SSD if you can) | owner **apps** (568), group **apps** |
| your existing asset shares | Fab/Unreal projects, `.blend` files, packs, add-ons | leave as they are – mounted read-only; *apps* needs read access |

For the asset shares, give the **apps** user read + execute via an ACL entry (Edit Permissions →
Add Item → User `apps`, Read). Stash never writes to them.

## 2. Install

**Apps → Discover Apps → ⋮ (top right) → Install via YAML**, name it `stash`, and paste
[`deploy/truenas/stash.yaml`](../deploy/truenas/stash.yaml). Change the lines marked `CHANGE ME`:

* `TZ`
* `/mnt/tank/apps/stash` → your dataset from step 1
* one `- /mnt/<pool>/<share>:/library/<name>:ro` line per asset share

Open `http://<nas-ip>:8420` → **Sources → Add source** → pick a folder under `/library`.

The image is published by this repository's GitHub Actions to `ghcr.io/w1zardxyz/stash`.
**New GHCR packages start private**: on GitHub open *Your profile → Packages → stash → Package
settings → Change visibility → Public* once, or TrueNAS can't pull it (alternatively add GHCR
credentials under *Apps → Configuration → Manage Container Registries*).

Updating: *Apps → stash → Update* if TrueNAS shows one, or Edit → Save to re-pull `:latest`.
Every build is also tagged with its commit (`:sha-466790c` – see *Packages → stash* on GitHub);
use one of those instead of `:latest` to pin a version. Your database is upgraded in place on
start (new tables are added; nothing is dropped).

## 3. Test it (10 minutes)

Do this once after installing, with a small folder first (e.g. one asset pack), before pointing it
at the whole library.

1. **It's up:** *Apps* shows `stash` as *Running*, and `http://<nas-ip>:8420/api/status` returns
   JSON with `"blender": true`. (Shell alternative: `sudo docker logs stash` should end with
   `Uvicorn running on http://0.0.0.0:8420`.)
2. **It can read your files:** **Sources → Add source** → browse `/library/...` → pick a folder
   with a few packs → *Add*. If the folder browser is empty or the scan fails with *Permission
   denied*, the *apps* user lacks read access to that share (step 1).
3. **Indexing works:** the **Jobs** page (spinner icon) shows `scan_source`, then `preview`,
   `thumbnail`, `extract_blend` and – with on-device AI – `embed_pending` (its first run also
   downloads the ~600 MB CLIP model). Nothing should end in *error*; click a job to see its log.
4. **Library:** models show thumbnails; open one and the 3D viewer loads it; texture sets appear as
   *Material*. Search for a word from an asset name.
5. **Downloads/conversion:** on an asset page open the *Export* tab and pick *GLB* (or
   *.blend*) – it runs a job and then offers the file.
6. **Packs:** the **Packs** page lists the top-level folders you added. Describe one with ✎, then
   search for a word from the description – every asset of that pack is found.
7. **On-device AI** (after the model download): an asset page shows *Suggested* tags; the
   **Smart** toggle next to search finds "wooden chair" style descriptions.
8. **Your files were not touched:** the share is mounted `:ro`, so Stash *can't* write to it – the
   TrueNAS snapshot/size of the share stays unchanged.

Then add the rest of your library as sources. A first scan of a big library runs for hours
because of the Blender work (§4); browsing works while it runs.

Optional, from your Mac: describe all packs with LM Studio – see [BATCH.md](BATCH.md).

## 4. What the N100 profile does

| Setting | Value | Why |
|---|---|---|
| `STASH_WORKERS` | `1` | One background job at a time. Splitting a large `.blend` file loads it fully into Blender (1–3 GB). |
| `mem_limit` | `4g` | Leaves ~4 GB for TrueNAS and the ZFS ARC. |
| `cpus` / `STASH_BLENDER_THREADS` | `3.5` / `3` | Keeps a core free for SMB and the UI. |
| `STASH_BLENDER_NICE` | `15` | Blender runs at low CPU priority. |
| `STASH_THUMBNAIL_SIZE` | `256` | Cycles CPU renders take ~2–6 s each at this size on an N100 (estimate). |
| `STASH_EXPORT_CACHE_GB` | `5` | Downloads/conversions are cached for re-use; the oldest are evicted above this. Project packages expire after 7 days. |
| `STASH_RENDER_THUMBNAILS` | `true` | Set `false` to skip renders entirely; use the viewer's *Set thumbnail* button for the ones you care about. Texture/HDRI/Unreal thumbnails don't need Blender either way. |

First scans of big libraries are dominated by Blender work (`.blend` splitting, previews of
FBX/OBJ). It runs in the background; the library is browsable immediately and fills in as jobs
finish (see **Jobs**). Indexing itself (folders, texture sets, Unreal headers) handles thousands of
files per second.

## 5. On-device AI on the N100

With `STASH_LOCAL_AI=true` (default) Stash downloads a CLIP model (ViT-B-32, ~600 MB, once, into
`/data/models`) on the first scan and embeds every thumbnail on the CPU. Measured on a desktop
CPU: ~0.1 s per image; expect a few times that on an N100, i.e. roughly 10–20 minutes per
5,000 assets on first indexing (estimate). With the model loaded the container uses ~850 MB in
total; it unloads after 30 idle minutes (`STASH_AI_IDLE_UNLOAD`), and the next Smart search or new
thumbnail reloads it in ~10–20 s.

What you get: suggested tags on each asset page (click to add, or select many → *Accept
suggestions*), the **Smart** toggle next to the search box, and *Visually similar* under Related.
No internet access is needed after the model download; set `STASH_LOCAL_AI=false` to turn it off.

## 6. Optional

* **Password:** set `STASH_PASSWORD`; the Blender add-on and Unreal plugin use it as their token.
* **Reverse proxy / remote access:** set `STASH_PUBLIC_URL` so the Blender extension repository
  hands out correct download links.
* **Running as root instead:** delete the `user:` line (then dataset ownership doesn't matter).
* **Storage:** *Integrations → Server* shows what Stash uses and has a *Clean up storage* button;
  the same cleanup runs daily on its own.
* **Backups:** snapshot `tank/apps/stash`. Everything in it except `stash.db` can be regenerated by
  rescanning.
