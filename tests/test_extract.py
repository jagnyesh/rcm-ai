"""Issue #9: an LLM extracts evidence from the note; anything not grounded in the note or the
graph sends the claim to a human.

Only the live tests call the model. The rest test our own validation and routing, with a
stand-in for our `extract` function (never for the SDK)."""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from rcm_ai.extract import (
    EFFORT, MODEL, PROMPT, SCHEMA, Extraction, build_request, extract, parse_extraction, triage_note,
)
from rcm_ai.graph import build_graph
from rcm_ai.schemas import load_case

DATA = Path(__file__).resolve().parents[1] / "data"
EVAL = DATA / "eval" / "cases"
GRAPH = build_graph(DATA)


def _gold_raw(case_id: str) -> tuple[dict, str]:
    """What a perfect extractor would return for an eval case, in the extraction schema."""
    raw = json.loads((EVAL / f"{case_id}.json").read_text())
    e = raw["evidence"]
    return {
        "diagnoses": [{"code": d["code"], "span": d["span"]} for d in e["diagnoses"]],
        "criteria_met": [{"criterion": c["criterion"], "span": c["span"]} for c in e["criteria"]],
        "criteria_not_met": [{"criterion": c["criterion"], "span": c["span"]} for c in e.get("criteria_not_met", [])],
    }, raw["note"]


def test_a_perfect_extraction_parses_into_the_gold_evidence_with_offsets():
    raw, note = _gold_raw("ev-14")  # has criteria_not_met
    case = load_case(EVAL / "ev-14.json")

    parsed = parse_extraction(raw, note, GRAPH)

    assert parsed.problems == []
    assert parsed.evidence == case.evidence
    for s in parsed.spans:
        assert note[s.start:s.end] == s.span


@pytest.mark.parametrize("field, item, problem", [
    ("diagnoses", {"code": "E11.65", "span": "type 2 diabetes with high sugars"}, "not in the note"),
    ("diagnoses", {"code": "E11.99", "span": None}, "E11.99"),  # code-shaped, but not a code
    ("diagnoses", {"code": "diabetes", "span": None}, "diabetes"),
    ("criteria_met", {"criterion": "cgm-9-bogus", "span": None}, "cgm-9-bogus"),
    ("criteria_met", {"criterion": "cgm-2-training", "span": "CGM"}, "shorter than"),
])
def test_an_ungrounded_item_rejects_the_whole_extraction(field, item, problem):
    raw, note = _gold_raw("ev-01")
    if item.get("span") is None:  # a real span from the note, so only the code or id is wrong
        item = {**item, "span": raw[field][0]["span"]}
    raw[field].append(item)

    parsed = parse_extraction(raw, note, GRAPH)

    assert parsed.evidence is None
    assert any(problem in p for p in parsed.problems), parsed.problems


def test_a_response_that_does_not_match_the_schema_is_rejected():
    parsed = parse_extraction({"diagnoses": "E11.65"}, "note", GRAPH)

    assert parsed.evidence is None and parsed.problems


def _stub(raw, stop_reason="end_turn"):
    return lambda note, criteria: Extraction(raw, stop_reason, {"stub": True})


def test_a_rejected_extraction_routes_the_claim_to_a_human_with_the_problems():
    raw, note = _gold_raw("ev-01")
    raw["diagnoses"][0]["span"] = "invented text"
    case = load_case(EVAL / "ev-01.json")

    verdict, _ = triage_note(case.claim, note, GRAPH, _stub(raw))

    assert verdict.outcome == "needs_human"
    (reason,) = verdict.reasons
    assert reason.kind == "extraction_rejected"
    assert "invented text" in reason.statement


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_a_refused_or_truncated_extraction_routes_the_claim_to_a_human(stop_reason):
    case = load_case(EVAL / "ev-01.json")

    verdict, _ = triage_note(case.claim, case.note, GRAPH, _stub(None, stop_reason))

    assert verdict.outcome == "needs_human"
    assert [r.kind for r in verdict.reasons] == ["extraction_rejected"]
    assert stop_reason in verdict.reasons[0].statement


@pytest.mark.parametrize("case_id", ["ev-01", "ev-14", "ev-19"])
def test_a_perfect_extraction_gives_the_same_verdict_as_gold_evidence(case_id):
    raw, note = _gold_raw(case_id)
    case = load_case(EVAL / f"{case_id}.json")
    from rcm_ai.adjudicate import adjudicate

    verdict, _ = triage_note(case.claim, note, GRAPH, _stub(raw))

    assert verdict == adjudicate(case.claim, case.evidence, GRAPH)


def test_the_extractor_is_offered_every_curated_criterion_with_its_lcd_quote():
    case = load_case(EVAL / "ev-01.json")
    seen = {}

    def spy(note, criteria):
        seen["criteria"] = criteria
        return Extraction(None, "refusal", {})

    triage_note(case.claim, case.note, GRAPH, spy)

    ids = {c.id for c in seen["criteria"]}
    assert ids == GRAPH.criterion_ids()
    assert all(c.quote for c in seen["criteria"])


def test_the_request_pins_the_model_effort_prompt_file_and_schema_file():
    case = load_case(EVAL / "ev-01.json")
    criteria = [c for lcd in ["L33822"] for c in GRAPH.criteria(lcd)]

    req = build_request(case.note, criteria)

    assert req["model"] == MODEL == "claude-opus-5-5"
    assert req["output_config"]["effort"] == EFFORT
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": json.loads(SCHEMA.read_text())}
    assert req["system"] == PROMPT.read_text()
    assert "temperature" not in req  # sampling parameters are rejected by the API since 2026-10-21
    assert "fallbacks" not in req  # one model per run: a refusal goes to a human, not another model
    assert case.note in req["messages"][0]["content"]


def test_a_case_without_evidence_loads_for_extraction(tmp_path):
    raw = json.loads((EVAL / "ev-01.json").read_text())
    del raw["evidence"]
    path = tmp_path / "ev-01.json"
    path.write_text(json.dumps(raw))

    assert load_case(path).evidence is None


# Live: these call the model. They need credentials and are skipped, visibly, without them.
live = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY (live model call)")


@live
def test_live_extraction_of_a_documented_case_is_overturnable_and_logs_the_wire_request(tmp_path):
    import anthropic

    case = load_case(EVAL / "ev-01.json")

    verdict, extraction = triage_note(
        case.claim, case.note, GRAPH, lambda n, c: extract(n, c, anthropic.Anthropic()))

    sent = json.loads(extraction.record["http_request_body"])
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"]["effort"] == EFFORT
    assert "temperature" not in sent
    assert extraction.record["response"]["model"].startswith("claude-opus-5-5")
    assert extraction.record["request_id"]
    assert verdict.outcome == "overturnable", verdict.reasons
