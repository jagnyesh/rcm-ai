from fastapi.testclient import TestClient

from rcm_ai import web

client = TestClient(web.app)


def test_health_reports_each_loaded_source_and_that_live_mode_is_off(monkeypatch):
    monkeypatch.delenv("LIVE_MODE", raising=False)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["live_mode"] == "off"
    sources = body["graph"]["sources"]
    assert sources and all(s["rejected"] == 0 and s["in_graph"] == s["parsed"] for s in sources)


def test_health_reports_a_graph_that_wont_load(tmp_path, monkeypatch):
    monkeypatch.setenv("RCM_AI_DATA", str(tmp_path))
    web._graph.cache_clear()
    try:
        r = client.get("/api/health")
    finally:
        web._graph.cache_clear()
    assert r.status_code == 503
    assert r.json()["graph"]["loaded"] is False


def test_live_mode_is_off_without_its_switch_and_calls_no_model(monkeypatch):
    monkeypatch.delenv("LIVE_MODE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-be-used")
    monkeypatch.setenv("DEMO_ACCESS_CODE", "code")
    r = client.post("/api/live", json={"case": "ev-01"}, headers={"X-Access-Code": "code"})
    assert r.status_code == 503
    assert "switched off" in r.json()["detail"]


def test_the_local_preview_serves_the_page():
    r = client.get("/")
    assert r.status_code == 200 and "rcm-ai" in r.text
    assert client.get("/demo-cases.json").status_code == 200
