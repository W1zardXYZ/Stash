#!/usr/bin/env bash
# Stash dev environment without Docker (macOS or Linux). Run from anywhere:
#
#   scripts/dev.sh setup          venv (backend/.venv) + Python deps + npm deps
#   scripts/dev.sh test           backend tests, frontend typecheck + build
#   scripts/dev.sh test-blender   the real-Blender pipeline test (needs Blender 4.2+, 4.5 LTS tested)
#   scripts/dev.sh samples        download a ~16 MB free sample library into dev-library/
#   scripts/dev.sh serve          API + workers on :8420 and the Vite dev UI on :5173 (foreground, Ctrl-C stops both)
#   scripts/dev.sh start | stop | status | logs    the same in the background (logs in dev-data/logs/)
#   scripts/dev.sh smoke          API smoke test against the running server (adds dev-library as a source)
#   scripts/dev.sh addon-smoke    drive the Blender add-on headlessly against the running server
#   scripts/dev.sh reset          delete dev-data/ (database, thumbnails, previews, exports)
#
# Environment: STASH_PORT (default 8420), STASH_BLENDER (path to the Blender binary),
# PYTHON (interpreter to build the venv with), plus any STASH_* setting from backend/stash/config.py.
# Written for bash 3.2 (the macOS default).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
VENV="$BACKEND/.venv"
DATA="${STASH_DATA_DIR:-$ROOT/dev-data}"
LIB="$ROOT/dev-library"
PORT="${STASH_PORT:-8420}"
LOGS="$DATA/logs"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for p in python3.13 python3.12 python3.11 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$p"; return
    fi
  done
  die "need Python 3.11+ (macOS: brew install python@3.12, or set PYTHON=/path/to/python3.12)"
}

find_blender() {
  if [ -n "${STASH_BLENDER:-}" ]; then echo "$STASH_BLENDER"; return; fi
  for app in /Applications/Blender*.app "$HOME"/Applications/Blender*.app; do
    if [ -x "$app/Contents/MacOS/Blender" ]; then echo "$app/Contents/MacOS/Blender"; return; fi
  done
  if command -v blender >/dev/null 2>&1; then command -v blender; return; fi
  if [ -x /opt/blender/blender ]; then echo /opt/blender/blender; return; fi
  echo ""
}

py() { "$VENV/bin/python" "$@"; }

need_venv() { [ -x "$VENV/bin/python" ] || die "run: scripts/dev.sh setup"; }

server_env() {
  export STASH_DATA_DIR="$DATA"
  export STASH_BROWSE_ROOTS="${STASH_BROWSE_ROOTS:-$LIB,$HOME}"
  export STASH_WORKERS="${STASH_WORKERS:-2}"
  local b; b="$(find_blender)"
  if [ -n "$b" ]; then export STASH_BLENDER="$b"; fi
}

cmd_setup() {
  local PY; PY="$(pick_python)"
  say "Python: $PY ($("$PY" --version 2>&1))"
  [ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
  py -m pip install -q --upgrade pip
  py -m pip install -q -r "$BACKEND/requirements.txt" pytest httpx
  command -v npm >/dev/null 2>&1 || die "need Node.js 20+ (macOS: brew install node)"
  say "Node: $(node --version)"
  (cd "$FRONTEND" && npm ci --no-audit --no-fund)
  local b; b="$(find_blender)"
  if [ -n "$b" ]; then say "Blender: $b ($("$b" -b --factory-startup -noaudio --version 2>/dev/null | head -1))"
  else say "Blender: not found – indexing works, .blend splitting/previews/conversions don't (install Blender 4.5 LTS or set STASH_BLENDER)"; fi
}

cmd_test() {
  need_venv
  say "backend tests"
  (cd "$BACKEND" && py -m pytest -q)
  say "frontend typecheck + build"
  (cd "$FRONTEND" && npx tsc --noEmit -p . && npm run build)
}

cmd_test_blender() {
  need_venv
  local b; b="$(find_blender)"
  [ -n "$b" ] || die "Blender not found (set STASH_BLENDER)"
  say "pipeline test with $b"
  (cd "$BACKEND" && STASH_TEST_BLENDER=1 STASH_BLENDER="$b" py -m pytest -q tests/test_blender_pipeline.py)
}

cmd_samples() {
  py_any="$( [ -x "$VENV/bin/python" ] && echo "$VENV/bin/python" || pick_python )"
  "$py_any" "$ROOT/scripts/fetch_samples.py" "$LIB"
}

wait_up() {
  local i=0
  while [ $i -lt 60 ]; do
    if curl -fs "http://localhost:$PORT/api/status" >/dev/null 2>&1; then return 0; fi
    sleep 1; i=$((i + 1))
  done
  die "server didn't come up on :$PORT – see $LOGS/api.log"
}

cmd_serve() {
  need_venv; server_env
  mkdir -p "$DATA"
  say "API http://localhost:$PORT · dev UI http://localhost:5173 · data $DATA · blender ${STASH_BLENDER:-none}"
  if curl -fs "http://localhost:$PORT/api/status" >/dev/null 2>&1; then die "something already answers on :$PORT (scripts/dev.sh stop?)"; fi
  (cd "$BACKEND" && exec "$VENV/bin/python" -m stash serve --host 127.0.0.1 --port "$PORT") &
  local api=$!
  (cd "$FRONTEND" && STASH_PORT="$PORT" exec "$FRONTEND/node_modules/.bin/vite" --port 5173 --strictPort) &
  local ui=$!
  trap 'kill $api $ui 2>/dev/null; wait 2>/dev/null; exit 0' INT TERM
  wait
}

cmd_start() {
  need_venv; server_env
  mkdir -p "$LOGS"
  if [ -f "$DATA/api.pid" ] && kill -0 "$(cat "$DATA/api.pid")" 2>/dev/null; then die "already running (scripts/dev.sh stop)"; fi
  if curl -fs "http://localhost:$PORT/api/status" >/dev/null 2>&1; then die "something already answers on :$PORT"; fi
  # exec, so the saved pid is the server itself and not a wrapper shell
  (cd "$BACKEND" && exec nohup "$VENV/bin/python" -m stash serve --host 127.0.0.1 --port "$PORT") >"$LOGS/api.log" 2>&1 &
  echo $! >"$DATA/api.pid"
  (cd "$FRONTEND" && STASH_PORT="$PORT" exec nohup "$FRONTEND/node_modules/.bin/vite" --port 5173 --strictPort) >"$LOGS/ui.log" 2>&1 &
  echo $! >"$DATA/ui.pid"
  wait_up
  say "running · API http://localhost:$PORT (also serves the built UI) · dev UI http://localhost:5173 · logs $LOGS"
}

cmd_stop() {
  for n in api ui; do
    if [ -f "$DATA/$n.pid" ]; then
      kill "$(cat "$DATA/$n.pid")" 2>/dev/null || true
      rm -f "$DATA/$n.pid"
    fi
  done
  say "stopped"
}

cmd_status() {
  curl -fs "http://localhost:$PORT/api/status" && echo || die "not running on :$PORT"
}

cmd_logs() { tail -n 100 -f "$LOGS/api.log"; }

jobs_left() {
  curl -fs "http://localhost:$PORT/api/status" | "$VENV/bin/python" -c 'import json,sys; j=json.load(sys.stdin)["jobs"]; print(j.get("queued",0)+j.get("running",0))'
}

cmd_smoke() {
  need_venv
  [ -d "$LIB" ] || cmd_samples
  curl -fs "http://localhost:$PORT/api/status" >/dev/null || die "start the server first (scripts/dev.sh start)"
  "$VENV/bin/python" - "$PORT" "$LIB" <<'EOF'
import json, sys, time, urllib.request, urllib.parse
port, lib = sys.argv[1], sys.argv[2]
base = f"http://localhost:{port}/api"
def call(method, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)
ok = True
def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f": {detail}" if detail else ""))
src = next((s for s in call("GET", "/sources") if s["path"] == lib), None) or call("POST", "/sources", {"path": lib, "name": "Dev samples"})
call("POST", f"/sources/{src['id']}/scan")
t0 = time.time()
while True:
    st = call("GET", "/status")
    if not (st["jobs"].get("queued", 0) + st["jobs"].get("running", 0)) or time.time() - t0 > 1800:
        break
    time.sleep(3)
print(f"jobs settled after {time.time() - t0:.0f}s: {st['jobs']}")
errors = call("GET", "/jobs?status=error&limit=50")["items"]
check("no failed jobs", not errors, "; ".join(f"{j['type']}: {j['message'][:120]}" for j in errors))
assets = call("GET", f"/assets?source={src['id']}&per_page=500")["items"]
items = {a["name"]: a for a in assets}  # names repeat (a .blend's object and material); use `assets` to scan all
print("assets:", sorted(items))
for name in ("Fox", "DamagedHelmet", "marble_bust_01_1k", "rock_wall_08"):
    check(f"indexed {name}", name in items)
fox = items.get("Fox")
check("Fox is rigged + animated", fox and fox["is_rigged"] and fox["is_animated"])
st = call("GET", "/status")
if st.get("blender"):
    check("blend split into objects", any(a["parent_id"] for a in assets), "children of wooden_stool_02_1k.blend")
    with_thumb = [a for a in assets if a["thumbnail"]]
    check("thumbnails", len(with_thumb) >= len(assets) // 2, f"{len(with_thumb)}/{len(assets)}")
    target = next((a for a in assets if a["kind"] == "model" and a["parent_id"]), fox)
    job = call("POST", f"/assets/{target['id']}/export", {"target": "glb"})
    while "job_id" in job:
        j = call("GET", f"/jobs/{job['job_id']}")
        if j["status"] in ("done", "error", "cancelled"):
            job = j["result"] if j["status"] == "done" else {"error": j["message"]}
            break
        time.sleep(2)
    check(f"export {target['name']} to GLB", "file_id" in job, str(job)[:200])
else:
    print("SKIP Blender checks (no Blender found)")
packs = [p["name"] for p in call("GET", "/packs")]
check("packs listed", {"PolyHaven", "Khronos"} <= set(packs), packs)
hits = call("GET", "/assets?q=fox")["items"]
check("search 'fox'", any(a["name"] == "Fox" for a in hits))
print("\nSMOKE " + ("OK" if ok else "FAILED"))
sys.exit(0 if ok else 1)
EOF
}

cmd_addon_smoke() {
  local b; b="$(find_blender)"
  [ -n "$b" ] || die "Blender not found (set STASH_BLENDER)"
  curl -fs "http://localhost:$PORT/api/status" >/dev/null || die "start the server first (scripts/dev.sh start)"
  local res; res="$(mktemp -d)"
  say "Blender user resources in $res (your real Blender preferences are not touched)"
  BLENDER_USER_RESOURCES="$res" "$b" -b --factory-startup -noaudio --python "$ROOT/scripts/blender_addon_smoke.py" -- "http://localhost:$PORT" "${STASH_PASSWORD:-}"
}

cmd_reset() {
  cmd_stop >/dev/null 2>&1 || true
  rm -rf "$DATA"
  say "removed $DATA"
}

case "${1:-}" in
  setup) cmd_setup ;;
  test) cmd_test ;;
  test-blender) cmd_test_blender ;;
  samples) cmd_samples ;;
  serve) cmd_serve ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  smoke) cmd_smoke ;;
  addon-smoke) cmd_addon_smoke ;;
  reset) cmd_reset ;;
  *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
