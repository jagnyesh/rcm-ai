"""Live mode's gates and its event stream. The model calls are stand-ins built from each case's
own evidence; no test here calls the model or the SDK."""

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcm_ai import live as lv
from rcm_ai import web
from rcm_ai.extract import Extraction
from rcm_ai.graph import build_graph
from rcm_ai.schemas import load_case

DATA = Path(__file__).resolve().parents[1] / "data"
EVAL = DATA / "eval" / "cases"
GRAPH = build_graph(DATA)
ON = {"LIVE_MODE": "on", "DEMO_ACCESS_CODE": "open-sesame", "ANTHROPIC_API_KEY": "sk-test-not-used"}

client = TestClient(web.app)


def _ok(raw) -> Extraction:
    return Extraction(raw, "end_turn", {"response": {"model": "stand-in"}})


def _raw(evidence) -> dict:
    return {"diagnoses": [{"code": d.code, "span": d.span} for d in evidence.diagnoses],
            "criteria_met": [{"criterion": c.criterion, "span": c.span} for c in evidence.criteria],
            "criteria_not_met": [{"criterion": c.criterion, "span": c.span} for c in evidence.criteria_not_met]}


def _backend(case_id: str, calls: list, letter: str = "We ask that the denial be overturned.") -> lv.Backend:
    evidence = load_case(EVAL / f"{case_id}.json").evidence

    def extract(note, criteria):
        calls.append("extract")
        return _ok(_raw(evidence))

    def check(pairs):
        calls.append("check")
        return _ok({"items": [{"item": n, "supported": True, "reason": "stand-in"} for n in range(1, len(pairs) + 1)]})

    def draft(claim, path, quotes, findings):
        calls.append("draft")
        return _ok({"letter": letter})

    return lv.Backend(extract, check, draft)


def _no_calls() -> lv.Backend:
    def fail(*_):
        raise AssertionError("a gate let a model call through")
    return lv.Backend(fail, fail, fail)


@pytest.fixture
def live_on(monkeypatch):
    for k, v in ON.items():
        monkeypatch.setenv(k, v)


def _events(r) -> list[dict]:
    return [json.loads(line) for line in r.text.splitlines() if line]


@pytest.mark.parametrize("env,expected", [
    ({}, lv.OFF),
    ({**ON, "LIVE_MODE": "off"}, lv.OFF),
    ({**ON, "LIVE_MODE": ""}, lv.OFF),
    ({**ON, "DEMO_ACCESS_CODE": ""}, lv.NOT_SET_UP),
    ({k: v for k, v in ON.items() if k != "ANTHROPIC_API_KEY"}, lv.NOT_SET_UP),
])
def test_live_mode_runs_only_with_the_switch_on_and_both_secrets_set(env, expected):
    assert lv.status(env) == (False, expected)


def test_live_mode_is_on_with_the_switch_on_and_both_secrets_set():
    assert lv.status(ON) == (True, None)
    assert lv.status({**ON, "LIVE_MODE": " ON "}) == (True, None)


def test_the_access_code_must_match_exactly():
    assert lv.code_ok("open-sesame", ON)
    for wrong in (None, "", "open-sesam", "open-sesame ", "OPEN-SESAME"):
        assert not lv.code_ok(wrong, ON)
    assert not lv.code_ok("", {})


def test_the_off_switch_stops_a_run_before_any_call(monkeypatch, live_on):
    monkeypatch.setenv("LIVE_MODE", "off")
    monkeypatch.setattr(web, "_backend", lambda _case: _no_calls())
    r = client.post("/api/live", json={"case": "ev-01"}, headers={"X-Access-Code": "open-sesame"})
    assert r.status_code == 503 and r.json()["detail"] == lv.OFF
    assert client.get("/api/health").json()["live_mode"] == "off"


@pytest.mark.parametrize("code", [None, "wrong"])
def test_a_wrong_access_code_is_refused_before_any_call(monkeypatch, live_on, code):
    monkeypatch.setattr(web, "_backend", lambda _case: _no_calls())
    headers = {"X-Access-Code": code} if code else {}
    r = client.post("/api/live", json={"case": "ev-01"}, headers=headers)
    assert r.status_code == 403


@pytest.mark.parametrize("body,status", [
    ({"case": "ev-99"}, 404),
    ({"case": "ev-01", "note": "   "}, 422),
    ({"case": "ev-01", "note": "x" * (lv.NOTE_LIMIT + 1)}, 422),
])
def test_an_unknown_case_or_a_bad_note_is_refused_before_any_call(monkeypatch, live_on, body, status):
    monkeypatch.setattr(web, "_backend", lambda _case: _no_calls())
    r = client.post("/api/live", json=body, headers={"X-Access-Code": "open-sesame"})
    assert r.status_code == status


def test_health_says_live_mode_is_on_when_it_can_run(live_on):
    assert client.get("/api/health").json()["live_mode"] == "on"


def test_an_overturnable_case_streams_each_step_and_a_checked_letter(monkeypatch, live_on):
    calls = []
    monkeypatch.setattr(web, "_backend", lambda _case: _backend("ev-01", calls))
    r = client.post("/api/live", json={"case": "ev-01"}, headers={"X-Access-Code": "open-sesame"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = _events(r)
    assert [e["event"] for e in events] == [
        "extraction_started", "extraction_done", "support_check_started", "support_check_done",
        "verdict", "draft_started", "draft_done", "done"]
    assert events[1]["problems"] == [] and events[1]["evidence"]["diagnoses"]
    assert events[3]["unsupported"] == []
    assert events[4]["verdict"]["outcome"] == "overturnable" and events[4]["verdict"]["path"]
    assert events[6]["check"]["passed"] is True
    assert events[-1]["model_calls"] == 3 and calls == ["extract", "check", "draft"]


def test_an_uphold_drafts_nothing(monkeypatch, live_on):
    calls = []
    monkeypatch.setattr(web, "_backend", lambda _case: _backend("ev-14", calls))
    events = _events(client.post("/api/live", json={"case": "ev-14"}, headers={"X-Access-Code": "open-sesame"}))
    verdict = next(e for e in events if e["event"] == "verdict")["verdict"]
    assert verdict["outcome"] == "uphold"
    assert "draft" not in calls and events[-1] == {"event": "done", "model_calls": 2}


def test_an_edited_note_is_what_the_model_reads(monkeypatch, live_on):
    seen = []
    backend = _backend("ev-01", [])
    monkeypatch.setattr(web, "_backend", lambda _case: lv.Backend(
        lambda note, criteria: (seen.append(note), backend.extract(note, criteria))[1], backend.check, backend.draft))
    note = load_case(EVAL / "ev-01.json").note + " Seen in clinic today."
    client.post("/api/live", json={"case": "ev-01", "note": note}, headers={"X-Access-Code": "open-sesame"})
    assert seen == [note]


def test_a_letter_citing_what_the_graph_did_not_establish_fails_its_check(monkeypatch, live_on):
    monkeypatch.setattr(web, "_backend", lambda _case: _backend("ev-01", [], letter="Covered under LCD L99999 for E10.9."))
    events = _events(client.post("/api/live", json={"case": "ev-01"}, headers={"X-Access-Code": "open-sesame"}))
    check = next(e for e in events if e["event"] == "draft_done")["check"]
    assert check["passed"] is False and "L99999" in check["unsupported"]


def test_an_api_error_ends_the_run_with_its_reason(monkeypatch, live_on):
    def boom(*_):
        raise RuntimeError("overloaded")
    monkeypatch.setattr(web, "_backend", lambda _case: lv.Backend(boom, boom, boom))
    events = _events(client.post("/api/live", json={"case": "ev-01"}, headers={"X-Access-Code": "open-sesame"}))
    assert events[-1] == {"event": "error", "detail": "RuntimeError: overloaded"}


def test_past_the_deadline_the_claim_goes_to_a_human_and_no_further_call_is_made():
    case = load_case(EVAL / "ev-01.json")
    release, calls = threading.Event(), []
    inner = _backend("ev-01", calls)

    def slow_extract(note, criteria):
        release.wait(5)
        return inner.extract(note, criteria)

    events = [json.loads(b) for b in lv.stream(case.claim, case.note, GRAPH,
                                               lv.Backend(slow_extract, inner.check, inner.draft), deadline=0.2)]
    release.set()
    verdict = events[-1]["verdict"]
    assert verdict["outcome"] == "needs_human" and verdict["reasons"][0]["kind"] == "timed_out"
    threading.Event().wait(0.3)  # let the worker reach its next call, which the cancel stops
    assert "check" not in calls


STAND_IN = {"LIVE_MODE": "stand-in", "DEMO_ACCESS_CODE": "open-sesame"}


def test_a_stand_in_needs_the_code_but_no_key():
    assert lv.status(STAND_IN) == (True, None)
    assert lv.status({"LIVE_MODE": "stand-in"}) == (False, lv.NOT_SET_UP)
    assert lv.mode({"LIVE_MODE": " Stand-In "}) == "stand-in" and lv.mode({"LIVE_MODE": "yes"}) == "off"


@pytest.fixture
def stand_in(monkeypatch):
    for k in ON:
        monkeypatch.delenv(k, raising=False)
    for k, v in STAND_IN.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lv.time, "sleep", lambda _s: None)


def test_health_says_stand_in(stand_in):
    assert client.get("/api/health").json()["live_mode"] == "stand-in"


def test_a_stand_in_still_refuses_a_wrong_code(stand_in):
    assert client.post("/api/live", json={"case": "ev-01"}, headers={"X-Access-Code": "nope"}).status_code == 403


@pytest.mark.parametrize("case_id", sorted(p.stem for p in EVAL.glob("*.json")))
def test_every_case_runs_as_a_stand_in_and_its_letter_cites_only_what_the_graph_established(stand_in, case_id):
    events = _events(client.post("/api/live", json={"case": case_id}, headers={"X-Access-Code": "open-sesame"}))
    assert events[-1]["event"] == "done"
    if (read := next((e for e in events if e["event"] == "extraction_done"), None)) is not None:
        assert read["model"] == lv.STAND_IN and read["problems"] == []
    if (draft := next((e for e in events if e["event"] == "draft_done"), None)) is not None:
        assert draft["check"]["passed"] is True and draft["check"]["citations"]


def test_a_stand_in_flags_a_quote_the_edited_note_no_longer_holds(stand_in):
    case = load_case(EVAL / "ev-01.json")
    span = case.evidence.diagnoses[0].span
    events = _events(client.post("/api/live", json={"case": "ev-01", "note": case.note.replace(span, "")},
                                 headers={"X-Access-Code": "open-sesame"}))
    assert events[1]["problems"]
