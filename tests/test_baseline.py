import hashlib
import json
import os
import re
from pathlib import Path

import pytest

import rcm_ai
from rcm_ai import baseline as bl
from rcm_ai import extract as ex
from rcm_ai.codes import is_icd10cm
from rcm_ai.extract import Extraction
from rcm_ai.graph import build_graph
from rcm_ai.schemas import OUTCOMES, load_case

DATA = Path(__file__).resolve().parents[1] / "data"
EVAL = DATA / "eval" / "cases"
GRAPH = build_graph(DATA)
POLICY = {"L33822": GRAPH.criteria("L33822")}
EVIDENCE = {"diagnoses": [], "criteria_met": [], "criteria_not_met": []}


def _walk(criteria):
    return [x for c in criteria for x in (c, *_walk(c.any_of))]


def test_the_request_gives_the_model_the_claim_the_note_and_the_lcd_criteria():
    case = load_case(EVAL / "ev-19.json")  # two billed codes

    content = bl.build_request(case.claim, case.note, POLICY)["messages"][0]["content"]

    for text in (case.claim.hcpcs, *case.claim.icd10, "CARC 50", case.claim.coverage, case.note, "L33822"):
        assert text in content
    for c in _walk(POLICY["L33822"]):
        assert c.id in content and c.quote in content


def test_the_request_carries_nothing_the_graph_decides():
    case = load_case(EVAL / "ev-12.json")  # R73.03: not covered, and Excludes1 with the diabetes codes

    content = bl.build_request(case.claim, case.note, POLICY)["messages"][0]["content"]

    # No covered-list codes, no linked article, no terminology: only the claim's own codes.
    codes = {w for w in re.findall(r"\b[A-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?\b", content) if is_icd10cm(w)}
    assert codes <= set(case.claim.icd10)
    assert "A52464" not in content
    assert "Excludes1" not in content


def test_the_request_pins_the_extractors_model_settings_and_its_own_prompt_file():
    case = load_case(EVAL / "ev-01.json")

    req = bl.build_request(case.claim, case.note, POLICY)

    assert (req["model"], req["max_tokens"], req["output_config"]["effort"]) == (ex.MODEL, ex.MAX_TOKENS, ex.EFFORT)
    assert "temperature" not in req
    assert req["system"] == bl.PROMPT.read_text()
    assert bl.PROMPT.parent == ex.PROMPT.parent and bl.PROMPT != ex.PROMPT


def test_the_output_schema_is_the_extractors_plus_a_verdict_and_reasons():
    case = load_case(EVAL / "ev-01.json")
    extractor = json.loads(ex.SCHEMA.read_text())

    schema = bl.build_request(case.claim, case.note, POLICY)["output_config"]["format"]["schema"]

    assert {k: schema["properties"][k] for k in extractor["properties"]} == extractor["properties"]
    assert schema["properties"]["verdict"] == {"type": "string", "enum": list(OUTCOMES)}
    assert schema["properties"]["reasons"] == {"type": "array", "items": {"type": "string"}}
    assert set(schema["required"]) == set(extractor["required"]) | {"verdict", "reasons"}
    assert schema["additionalProperties"] is False


def _answer(raw, stop_reason="end_turn"):
    return Extraction(raw, stop_reason, {})


@pytest.mark.parametrize("outcome", ["uphold", "needs_human"])
def test_the_models_verdict_and_reasons_become_the_arms_verdict(outcome):
    verdict = bl.verdict_from(_answer({**EVIDENCE, "verdict": outcome, "reasons": ["first", "second"]}))

    assert verdict.outcome == outcome
    assert [(r.kind, r.statement) for r in verdict.reasons] == [("model_reason", "first"), ("model_reason", "second")]
    assert verdict.path == ()


def test_an_overturnable_verdict_carries_the_models_reasons_as_unsourced_hops():
    reasons = ["E11.65 is a covered diagnosis", "the note documents criteria 1-5"]

    verdict = bl.verdict_from(_answer({**EVIDENCE, "verdict": "overturnable", "reasons": reasons}))

    assert verdict.outcome == "overturnable"
    assert [h.statement for h in verdict.path] == reasons
    assert all(h.anchor is None and h.source is None for h in verdict.path)


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_a_refused_or_truncated_response_hands_the_claim_off(stop_reason):
    verdict = bl.verdict_from(_answer(None, stop_reason))

    assert verdict.outcome == "needs_human"
    assert [r.kind for r in verdict.reasons] == ["no_verdict"]
    assert stop_reason in verdict.reasons[0].statement


@pytest.mark.parametrize("raw", [
    {**EVIDENCE, "verdict": "approve", "reasons": ["x"]},
    {**EVIDENCE, "verdict": "uphold", "reasons": "x"},
    {**EVIDENCE, "verdict": "uphold", "reasons": [1]},
    {**EVIDENCE, "verdict": "overturnable", "reasons": []},  # an overturnable needs a path
    ["uphold"],
])
def test_a_response_that_is_not_a_usable_verdict_hands_the_claim_off(raw):
    verdict = bl.verdict_from(_answer(raw))

    assert verdict.outcome == "needs_human"
    assert [r.kind for r in verdict.reasons] == ["no_verdict"]


def _replying(raw, stop_reason="end_turn", seen=None):
    def stand_in(claim, note, policy, client):
        if seen is not None:
            seen.append(policy)
        sent = bl.build_request(claim, note, policy)
        return Extraction(raw, stop_reason, {
            "request_id": "req_stand_in", "http_request_body": json.dumps(sent), "response": {"model": sent["model"]}})
    return stand_in


def _eval(tmp_path, monkeypatch, stand_in, arms="llm-only"):
    monkeypatch.setattr(rcm_ai, "_client", lambda: None)
    monkeypatch.setattr(bl, "ask", stand_in)
    code = rcm_ai.main(["eval", "--arms", arms, "--data", str(DATA), "--out", str(tmp_path)])
    (record,) = tmp_path.glob("eval-*.json")
    return code, json.loads(record.read_text())


def test_the_llm_only_arm_asks_the_model_on_every_case_with_the_lcd_criteria(tmp_path, monkeypatch):
    seen = []

    code, record = _eval(tmp_path, monkeypatch, _replying(
        {**EVIDENCE, "verdict": "needs_human", "reasons": ["r"]}, seen=seen))

    arm = record["arms"]["llm-only"]
    assert code == 0
    assert (arm["model"], arm["n"]) == (ex.MODEL, 20)
    # Nothing is decided before the call: the scope and coding checks come from the graph.
    assert (arm["calls"], arm["responses"]) == (20, 20)
    assert all(r["model_called"] for r in record["results"])
    assert seen == [POLICY] * 20
    assert arm["prompt_sha256"] == hashlib.sha256(bl.PROMPT.read_text().encode()).hexdigest()
    assert len(list(tmp_path.glob("eval-*-llm-only/*.json"))) == 20


def test_the_llm_only_and_extracted_arms_keep_separate_run_records(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "extract", lambda note, criteria, client: Extraction(None, "refusal", {
        "request_id": "req_extract", "http_request_body": json.dumps(ex.build_request(note, criteria)),
        "response": {"model": ex.MODEL}}))

    code, record = _eval(tmp_path, monkeypatch, _replying(
        {**EVIDENCE, "verdict": "uphold", "reasons": ["r"]}), arms="extracted,llm-only")

    arms = record["arms"]
    assert code == 0
    assert (arms["extracted"]["calls"], arms["llm-only"]["calls"]) == (15, 20)
    assert arms["extracted"]["prompt_sha256"] != arms["llm-only"]["prompt_sha256"]
    ids = {r["arm"]: set() for r in record["results"]}
    for r in record["results"]:
        ids[r["arm"]].add(r["request_id"])
    assert ids == {"extracted": {"req_extract", None}, "llm-only": {"req_stand_in"}}
    assert len(list(tmp_path.glob("eval-*-extractions/*.json"))) == 15
    assert len(list(tmp_path.glob("eval-*-llm-only/*.json"))) == 20


def test_eval_prints_and_records_every_case_where_the_arms_disagree(tmp_path, monkeypatch, capsys):
    code, record = _eval(tmp_path, monkeypatch, _replying(
        {**EVIDENCE, "verdict": "uphold", "reasons": ["not covered"]}), arms="graph,llm-only")

    out = capsys.readouterr().out
    graph = {r["case"]: r["outcome"] for r in record["results"] if r["arm"] == "graph"}
    differing = sorted(case for case, outcome in graph.items() if outcome != "uphold")
    assert code == 0
    assert [d["case"] for d in record["disagreements"]] == differing
    ev01 = next(d for d in record["disagreements"] if d["case"] == "ev-01")
    assert ev01["gold"] == "overturnable"
    assert ev01["arms"]["llm-only"] == {"outcome": "uphold", "correct": False, "why": ["model_reason: not covered"]}
    assert ev01["arms"]["graph"]["outcome"] == "overturnable" and ev01["arms"]["graph"]["correct"]
    assert f"arms disagree on {len(differing)} of 20 cases" in out
    assert re.search(r"ev-01 gold overturnable: graph overturnable, llm-only uphold", out)
    assert "llm-only: model_reason: not covered" in out


# Live: calls the model. Needs credentials and is skipped, visibly, without them.
_has_credentials = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                        or (Path.home() / ".config" / "anthropic").exists())


@pytest.mark.skipif(not _has_credentials, reason="needs Anthropic credentials (live model call)")
def test_live_baseline_returns_a_verdict_and_logs_the_wire_request():
    import anthropic

    case = load_case(EVAL / "ev-01.json")

    response = bl.ask(case.claim, case.note, POLICY, anthropic.Anthropic())

    sent = json.loads(response.record["http_request_body"])
    assert sent["model"] == ex.MODEL
    assert sent["output_config"]["effort"] == ex.EFFORT
    assert "temperature" not in sent
    assert response.record["request_id"]
    assert bl.verdict_from(response).outcome in OUTCOMES
