"""AI tagging with a stubbed Anthropic client (no network)."""

import json
import types


class _FakeMessages:
    calls = []

    def create(self, **kw):
        self.calls.append(kw)
        text = json.dumps({"category": "Furniture", "tags": ["chair", "wood", "Medieval", "chair", "3d"], "description": "A rustic wooden chair."})
        return types.SimpleNamespace(stop_reason="end_turn", model=kw["model"], content=[types.SimpleNamespace(type="text", text=text)])


class _FakeClient:
    def __init__(self, *a, **k):
        self.beta = types.SimpleNamespace(messages=_FakeMessages())


def test_ai_tag_job(app_client, run_jobs, monkeypatch, tmp_path):
    import anthropic

    from helpers import write_obj

    monkeypatch.setenv("STASH_AI", "true")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    write_obj(tmp_path / "ai" / "Chair.obj")
    src = app_client.post("/api/sources", json={"path": str(tmp_path / "ai")}).json()
    run_jobs()
    chair = app_client.get("/api/assets", params={"source": src["id"]}).json()["items"][0]
    assert app_client.get("/api/status").json()["ai"] is True
    r = app_client.post("/api/ai/tag", json={"ids": [chair["id"]]})
    assert r.status_code == 200, r.text
    run_jobs()
    a = app_client.get(f"/api/assets/{chair['id']}").json()
    assert a["tags"] == ["furniture", "chair", "wood", "medieval"]
    assert a["description"] == "A rustic wooden chair."
    assert a["meta"]["category"] == "furniture"
    call = _FakeMessages.calls[-1]
    assert call["fallbacks"] == "default" and call["output_config"]["format"]["type"] == "json_schema"
    assert "Chair" in call["messages"][0]["content"][-1]["text"]
    # pack facet comes from the top-level folder
    assert "packs" in app_client.get("/api/facets").json()
