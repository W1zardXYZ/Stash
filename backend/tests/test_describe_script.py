"""integrations/tools/stash_describe.py against a live Stash server and a fake LM Studio.

Uses the library test_api.py scanned (same session, files run in name order)."""

import importlib.util
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "integrations" / "tools" / "stash_describe.py"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def stash_url(app_client):
    import uvicorn

    from stash.main import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


@pytest.fixture()
def lmstudio():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._json({"data": [{"id": "text-embedding-nomic"}, {"id": "qwen-test-vl"}]})

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(req)
            if "response_format" in req and len(calls) == 1:
                return self._json({"error": "structured output not supported"}, 400)  # exercise the fallback
            text = req["messages"][1]["content"][0]["text"]
            if "Pack name: Crate" in text:
                out = '<think>crates…</think>```json\n{"description": "Wooden shipping crates for docks.", "keywords": ["Cargo", "crate", "harbor"]}\n```'
            elif "Pack name:" in text:
                out = json.dumps({"description": "Some pack.", "keywords": ["misc"]})
            else:
                out = json.dumps({"description": "A barrel.", "tags": ["Barrel", "wood"]})
            self._json({"choices": [{"message": {"role": "assistant", "content": out}}]})

    srv = HTTPServer(("127.0.0.1", _free_port()), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1", calls
    srv.shutdown()


def _script():
    spec = importlib.util.spec_from_file_location("stash_describe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_describe_packs(app_client, stash_url, lmstudio, capsys):
    url, calls = lmstudio
    c = app_client
    for p in c.get("/api/packs", params={"described": True}).json():
        c.delete(f"/api/packs/{p['name']}")
    mod = _script()

    mod.main(["--stash", stash_url, "--lmstudio", url, "--dry-run", "packs", "--only", "Crate"])
    assert c.get("/api/packs/Crate").json()["description"] == ""  # dry run wrote nothing

    calls.clear()
    mod.main(["--stash", stash_url, "--lmstudio", url, "packs", "--only", "Crate"])
    req = calls[-1]
    assert req["model"] == "qwen-test-vl"  # skipped the embedding model
    assert "response_format" not in req  # fell back after the 400
    content = req["messages"][1]["content"]
    assert "SM_Crate" in content[0]["text"]
    assert any(p["type"] == "image_url" and p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in content)
    pack = c.get("/api/packs/Crate").json()
    assert pack["description"] == "Wooden shipping crates for docks."
    assert pack["keywords"] == ["cargo", "harbor"]  # lower-cased, pack name dropped
    assert pack["meta"] == {"by": "lmstudio", "model": "qwen-test-vl"}

    # default run: only undescribed packs; Crate is left alone
    calls.clear()
    mod.main(["--stash", stash_url, "--lmstudio", url, "--images", "0", "packs"])
    assert calls and all("Pack name: Crate\n" not in r["messages"][1]["content"][0]["text"] for r in calls)
    assert all(len(r["messages"][1]["content"]) == 1 for r in calls)  # --images 0: text only
    assert c.get("/api/packs", params={"described": False}).json() == []

    # CLIP engine needs no LLM at all
    mod.main(["--stash", stash_url, "--lmstudio", "http://127.0.0.1:9/v1", "packs", "--engine", "clip", "--only", "Surfaces"])
    assert c.get("/api/packs/Surfaces").json()["meta"]["by"] == "clip"


def test_describe_assets(app_client, stash_url, lmstudio):
    url, _ = lmstudio
    c = app_client
    mod = _script()
    with pytest.raises(SystemExit):
        mod.main(["--stash", stash_url, "--lmstudio", url, "assets"])  # refuses to run on everything
    mod.main(["--stash", stash_url, "--lmstudio", url, "assets", "--pack", "barrels", "--apply-tags"])
    barrel = next(a for a in c.get("/api/assets", params={"pack": "barrels"}).json()["items"] if a["name"] == "Barrel")
    d = c.get(f"/api/assets/{barrel['id']}").json()
    assert d["description"] == "A barrel." and {"barrel", "wood"} <= set(d["tags"])


def test_script_is_served(app_client):
    r = app_client.get("/api/integrations/stash_describe.py")
    assert r.status_code == 200 and b"def cmd_packs" in r.content
