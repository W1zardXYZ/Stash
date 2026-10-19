# Hand-off: run and test Stash natively on a MacBook

**For:** a coding agent (e.g. Claude Code) working in a local clone of this repository on the
owner's MacBook. This file is self-contained; read [OVERVIEW.md](OVERVIEW.md) when you need to
know where something lives in the code.

## Goal

Run Stash as a plain dev setup – Python backend + Vite dev server, **no Docker** – under the best
conditions available (Apple silicon, a real Blender 4.5 LTS, on-device CLIP enabled), run every
test, exercise the app end to end, fix what breaks, and write a short report. So far Stash has only
been run on Linux (CI and a cloud container); macOS has never been tried.

## Ground rules

* Work on branch `claude/zealous-thompson-fv7gqn`. Commit fixes there, with a test when the bug is
  testable. **Ask the owner before pushing.**
* Stash must never write into an asset library. Use the sample library (`dev-library/`), and if the
  owner points you at their real assets, check afterwards that nothing in that folder changed. A
  write there is a serious bug: stop and report it.
* Don't touch the owner's real Blender preferences: `scripts/dev.sh addon-smoke` installs the add-on
  into a throw-away `BLENDER_USER_RESOURCES` folder; keep it that way.
* Everything Stash generates goes to `dev-data/` (git-ignored, safe to delete with
  `scripts/dev.sh reset`). Python packages go to `backend/.venv`.
* Ask before installing anything system-wide (Homebrew packages, a second Blender, LM Studio models).

## Prerequisites

| Need | Check | If missing |
|---|---|---|
| Python **3.11+** | `python3 --version` | macOS's `/usr/bin/python3` is 3.9 and won't do: `brew install python@3.12` (ask first), or set `PYTHON=/path/to/python3.12` |
| Node **20+** | `node --version` | `brew install node` (ask first) |
| Blender **4.5 LTS** | `ls /Applications/Blender*.app` | Download from blender.org (ask first). Found automatically in `/Applications` or `~/Applications`, or set `STASH_BLENDER=/Applications/Blender.app/Contents/MacOS/Blender`. Blender 5.x is untested (Collada import was removed in 5.0; other API changes are possible). If that's all the owner has, test with it and report the differences. |
| Network | – | pip, npm, Poly Haven + GitHub (sample assets), Hugging Face (CLIP model, ~600 MB, once) |

## Steps

Run from the repo root. Expected results are what the same steps give on Linux.

1. **Update:** `git fetch origin && git checkout claude/zealous-thompson-fv7gqn && git pull`
2. **Setup:** `scripts/dev.sh setup` – creates `backend/.venv`, installs Python + npm packages,
   prints the Python/Node/Blender it found.
3. **Unit/API tests:** `scripts/dev.sh test` → `44 passed, 1 skipped`, then the frontend
   typecheck + build succeed.
4. **Real-Blender pipeline:** `scripts/dev.sh test-blender` → `1 passed` (builds `.blend`/FBX/GLB
   fixtures with Blender, splits them, converts to blend/fbx/unreal/glb/stl, and checks the
   read-only library wasn't written to).
5. **Run it:** `scripts/dev.sh samples` (≈16 MB of CC0 assets in `dev-library/`), then
   `scripts/dev.sh start`. API + built UI: http://localhost:8420 · Vite dev UI with hot reload:
   http://localhost:5173. Logs: `dev-data/logs/api.log`, `ui.log`. `scripts/dev.sh serve` does the
   same in the foreground (Ctrl-C stops both).
6. **API smoke test:** `scripts/dev.sh smoke` → ends with `SMOKE OK`. It adds `dev-library` as a
   source, waits for all jobs (the first `embed_pending` run downloads the CLIP model), and checks
   indexing, `.blend` splitting, thumbnails, a GLB export, packs and search.
7. **Blender add-on smoke test:** `scripts/dev.sh addon-smoke` → `7 passed, 0 failed, 1 skipped`
   (installs the add-on from the server, imports a rigged model, a model, a material onto a cube
   and an HDRI as the world, then pushes the cube back).
8. **UI pass.** Use a browser (Playwright/Chromium if you have it, otherwise ask the owner to click
   through) on http://localhost:5173 and check:
   * Library: thumbnails for all sample assets; filters (Type, Packs, Features) change the grid.
   * `Fox`: the viewer plays the *Survey/Walk/Run* animations; *Skeleton* shows bones.
   * `DamagedHelmet`: textured, emissive; the material shows on the PBR sphere.
   * The HDRI previews as an environment; `rock_wall_08` shows as a material with its maps.
   * `wooden_stool_02_1k.blend` has child assets (object, material, images) and a node graph.
   * **Smart** search: "wooden chair" ranks the stool first; "statue" finds the bust.
   * Asset pages show *Suggested* tags (CLIP); *Accept suggestions* works on a selection.
   * **Packs** page: describe `PolyHaven` by hand, then search a word from the description.
   * **Export** tab: GLB, FBX, `.blend`, USDZ, Unreal downloads work and open (re-import them into
     Blender headlessly if you want proof).
   * Jobs, Sources (add/remove/rescan), Tags, Projects (create, add assets, *Export project*),
     Integrations (all downloads), light/dark theme.
9. **LM Studio (only if the owner has it running with a model loaded):**
   ```sh
   python3 integrations/tools/stash_describe.py --stash http://localhost:8420 models
   python3 integrations/tools/stash_describe.py --stash http://localhost:8420 --dry-run packs
   python3 integrations/tools/stash_describe.py --stash http://localhost:8420 packs --overwrite
   ```
   This script has only been tested against a stand-in server, never a real model. Report the
   seconds per pack, whether images were accepted, whether structured JSON output worked (the
   script falls back to plain JSON if LM Studio rejects `response_format`), and the quality of the
   descriptions. Try `--no-think` with a Qwen3-style model.
10. **Real library (optional, ask the owner for a folder):** first save a listing of it with sizes
    and modification times (`find <dir> -type f -exec stat -f '%z %m %N' {} + | sort > before.txt`).
    Add it as a source and let the jobs finish. Then record asset counts, errors, total time, and
    peak memory of the `python` and `Blender` processes (Activity Monitor or `ps -o rss`). Finally
    compare a new listing against `before.txt`: it must be identical.

## Known issues and gaps (fix if cheap, otherwise report)

* Importing the `Fox` `.blend` into Blender also brings an `Icosphere` object: the glTF importer's
  bone-shape helper ends up in the per-asset `.blend` (`backend/blender_scripts/extract_blend.py` /
  `preview_model.py`).
* Animations inside `.glb`/`.fbx` files don't become separate `animation` assets (only `.blend`
  actions do), so `addon-smoke` skips its animation step on the sample library.
* macOS-specific code that has never run on a Mac: Blender discovery in
  `backend/stash/config.py` (`_find_blender`), `os.setpriority` for Blender jobs
  (`backend/stash/blender.py`), onnxruntime/tokenizers on arm64 (`backend/stash/vision.py`), and
  paths with spaces (e.g. `Blender 4.5.app`, a library folder with spaces – please test one).
* Not covered here: the Unreal plugin in a live editor (optional if UE 5.x is installed – see
  [UNREAL.md](UNREAL.md)), and Claude tagging (needs an API key; skip unless the owner gives one).

## Report

Write `docs/reports/mac-dev-<YYYY-MM-DD>.md` and commit it with your fixes:

* Environment: macOS version, chip, RAM, Python, Node, Blender version(s), LM Studio model if used.
* Each step above: pass/fail, the key output lines, timings (setup, tests, smoke scan, CLIP
  download + embedding, real-library scan).
* Each bug: how to reproduce it, the cause, and the fixing commit (or a proposed fix if it's out of
  scope).
* Anything the owner should look at by hand (UI quirks, description quality).

## Quick reference

| | |
|---|---|
| `scripts/dev.sh setup \| test \| test-blender \| samples \| serve \| start \| stop \| status \| logs \| smoke \| addon-smoke \| reset` | everything above |
| `STASH_PORT=8421 scripts/dev.sh start` | another port if 8420 is taken (the Vite proxy follows it) |
| `STASH_LOCAL_AI=false` | skip CLIP (no model download) |
| `STASH_WORKERS=1`, `STASH_RENDER_THUMBNAILS=false` | lighter background work |
| `GET /api/jobs?status=error`, `GET /api/jobs/{id}` | job errors with logs (also on the Jobs page) |
| http://localhost:8420/docs | OpenAPI docs for every endpoint |
| `backend/tests/` | `cd backend && .venv/bin/python -m pytest -q tests/test_api.py -k packs` for a single test |
