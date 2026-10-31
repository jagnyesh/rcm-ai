"""Issue #9: an LLM extracts evidence from the note; anything not grounded in the note or the
graph sends the claim to a human.

Only the live tests call the model. The rest test our own validation and routing, with a
stand-in for our `extract` function (never for the SDK)."""

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import rcm_ai
from rcm_ai import extract as ex
from rcm_ai.extract import (
    EFFORT, MODEL, PROMPT, SCHEMA, Extraction, build_request, extract, parse_extraction, triage_note,
)
from rcm_ai.graph import build_graph
from rcm_ai.schemas import load_case

DATA = Path(__file__).resolve().parents[1] / "data"
EVAL = DATA / "eval" / "cases"
GRAPH = build_graph(DATA)
CRITERIA = GRAPH.criteria("L33822")


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

    parsed = parse_extraction(raw, note, GRAPH, CRITERIA)

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

    parsed = parse_extraction(raw, note, GRAPH, CRITERIA)

    assert parsed.evidence is None
    assert any(problem in p for p in parsed.problems), parsed.problems


@pytest.mark.parametrize("code, problem", [
    ("E11.99", "not a code in the loaded ICD-10-CM category E11"),  # E11 is loaded whole
    ("Z79.84", "outside the loaded ICD-10-CM slices"),  # a real code the graph can't check
    ("diabetes", "not an ICD-10-CM code"),
])
def test_a_rejected_diagnosis_says_whether_it_is_invalid_or_just_not_loaded(code, problem):
    raw, note = _gold_raw("ev-04")
    raw["diagnoses"].append({"code": code, "span": raw["diagnoses"][0]["span"]})

    parsed = parse_extraction(raw, note, GRAPH, CRITERIA)

    assert parsed.evidence is None
    assert [p for p in parsed.problems if code in p] == [p for p in parsed.problems if problem in p] != []


@pytest.mark.parametrize("case_id, parent, span", [
    ("ev-03", "cgm-4-glycemic-control", "without coma; not on insulin"),  # the note documents a level 3 event
    ("ev-10", "cgm-4b-hypoglycemia", "No hypoglycemia reported."),  # gold evidence may; an extraction may not
])
def test_a_not_met_on_a_criterion_with_alternatives_rejects_the_extraction(case_id, parent, span):
    raw, note = _gold_raw(case_id)
    raw["criteria_met"] = [c for c in raw["criteria_met"] if not c["criterion"].startswith("cgm-4")]
    raw["criteria_not_met"] = [c for c in raw["criteria_not_met"] if c["criterion"] != parent]
    raw["criteria_not_met"].append({"criterion": parent, "span": span})
    case = load_case(EVAL / f"{case_id}.json")

    verdict, _ = triage_note(case.claim, note, GRAPH, _stub(raw))

    assert verdict.outcome == "needs_human"
    assert [r.kind for r in verdict.reasons] == ["extraction_rejected"]
    assert parent in verdict.reasons[0].statement and "each alternative" in verdict.reasons[0].statement


def test_a_criterion_not_offered_for_the_claim_is_rejected():
    raw, note = _gold_raw("ev-01")  # documents cgm-5-visit
    offered = [c for c in CRITERIA if c.id != "cgm-5-visit"]  # as if cgm-5 belonged to another LCD

    parsed = parse_extraction(raw, note, GRAPH, offered)

    assert parsed.evidence is None
    assert any("cgm-5-visit" in p and "not offered" in p for p in parsed.problems), parsed.problems


def test_a_response_that_does_not_match_the_schema_is_rejected():
    parsed = parse_extraction({"diagnoses": "E11.65"}, "note", GRAPH, CRITERIA)

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


@pytest.mark.parametrize("case_id, kind", [
    ("ev-15", "item_not_in_criteria"), ("ev-16", "denial_out_of_scope"), ("ev-17", "coverage_out_of_scope"),
])
def test_an_out_of_scope_claim_is_handed_off_for_its_scope_reason_without_calling_the_model(case_id, kind):
    case = load_case(EVAL / f"{case_id}.json")

    def must_not_call(note, criteria):
        raise AssertionError("the model was called for an out-of-scope claim")

    verdict, extraction = triage_note(case.claim, case.note, GRAPH, must_not_call)

    assert extraction is None
    assert verdict.outcome == "needs_human"
    assert [r.kind for r in verdict.reasons] == [kind]


def test_an_uncovered_diagnosis_is_upheld_on_extracted_evidence_only_when_the_note_rules_out_the_covered_condition():
    case = load_case(EVAL / "ev-13.json")  # billed E16.2, not covered; the note says "no diabetes diagnosis"
    raw, note = _gold_raw("ev-13")

    silent, _ = triage_note(case.claim, note, GRAPH, _stub(raw))
    raw["criteria_not_met"].append({"criterion": "cgm-1-diabetes", "span": "no diabetes diagnosis"})
    ruled_out, _ = triage_note(case.claim, note, GRAPH, _stub(raw))

    # The model may simply have missed a covered diagnosis the note documents.
    assert silent.outcome == "needs_human"
    assert [r.kind for r in silent.reasons] == ["covered_diagnosis_not_ruled_out"]
    assert ruled_out.outcome == "uphold"
    assert [r.kind for r in ruled_out.reasons] == ["diagnosis_not_covered"]


def test_a_criterion_decided_from_the_diagnosis_code_cannot_be_extracted_as_met():
    case = load_case(EVAL / "ev-13.json")
    raw, note = _gold_raw("ev-13")
    # Listed met and ruled out at once: the rule-out must not be trusted to uphold.
    raw["criteria_met"].append({"criterion": "cgm-1-diabetes", "span": "Telehealth visit today to review diabetes control."})
    raw["criteria_not_met"].append({"criterion": "cgm-1-diabetes", "span": "no diabetes diagnosis"})

    verdict, _ = triage_note(case.claim, note, GRAPH, _stub(raw))

    assert verdict.outcome == "needs_human"
    assert [r.kind for r in verdict.reasons] == ["extraction_rejected"]
    assert "cgm-1-diabetes" in verdict.reasons[0].statement and "diagnosis code" in verdict.reasons[0].statement


@pytest.mark.parametrize("case_id", ["ev-19", "ev-20"])
def test_a_claim_whose_billed_codes_conflict_is_handed_off_without_calling_the_model(case_id):
    case = load_case(EVAL / f"{case_id}.json")

    def must_not_call(note, criteria):
        raise AssertionError("the model was called for a claim its billed codes already decide")

    verdict, extraction = triage_note(case.claim, case.note, GRAPH, must_not_call)

    assert extraction is None
    assert verdict.outcome == "needs_human"
    assert [r.kind for r in verdict.reasons] == ["coding_conflict"]
    assert verdict.findings


def test_a_span_that_shows_several_alternatives_fail_is_quoted_once():
    case = load_case(EVAL / "ev-10.json")
    raw, note = _gold_raw("ev-10")
    raw["criteria_not_met"] = [
        {"criterion": "cgm-4a-insulin", "span": "managed on metformin alone"},
        {"criterion": "cgm-4b1-level2", "span": "No hypoglycemia reported."},
        {"criterion": "cgm-4b2-level3", "span": "No hypoglycemia reported."},
    ]

    verdict, _ = triage_note(case.claim, note, GRAPH, _stub(raw))

    assert verdict.outcome == "uphold"
    (reason,) = verdict.reasons
    assert reason.statement.count("No hypoglycemia reported.") == 1


def test_the_extractor_is_offered_every_curated_criterion_with_its_lcd_quote():
    case = load_case(EVAL / "ev-01.json")
    seen = {}

    def spy(note, criteria):
        seen["criteria"] = criteria
        return Extraction(None, "refusal", {})

    triage_note(case.claim, case.note, GRAPH, spy)

    def ids(criteria):
        return {x for c in criteria for x in (c.id, *ids(c.any_of))}

    assert all(c.quote for c in seen["criteria"])
    ids = ids(seen["criteria"])
    assert ids == GRAPH.criterion_ids()


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


# Run records: the CLI with a stand-in for our `extract` that records the request it was asked to
# send, as JSON, the way the live test shows the SDK sends it.

def _sending(edit=lambda req: req, raw=None, stop_reason="refusal"):
    def stand_in(note, criteria, client):
        sent = edit(build_request(note, criteria))
        return Extraction(raw, stop_reason, {
            "request_id": "req_stand_in", "http_request_body": json.dumps(sent), "response": {"model": sent["model"]}})
    return stand_in


def _eval_extracted(tmp_path, monkeypatch, stand_in):
    monkeypatch.setattr(rcm_ai, "_client", lambda: None)
    monkeypatch.setattr(ex, "extract", stand_in)
    code = rcm_ai.main(["eval", "--arms", "extracted", "--data", str(DATA), "--out", str(tmp_path)])
    (record,) = tmp_path.glob("eval-*.json")
    return code, json.loads(record.read_text())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_run_record_describes_the_requests_sent_not_the_files_on_disk(tmp_path, monkeypatch):
    def edited(req):
        return {**req, "model": "claude-other", "system": "an edited prompt", "max_tokens": 1234,
                "output_config": {**req["output_config"], "effort": "low"}}

    code, record = _eval_extracted(tmp_path, monkeypatch, _sending(edited))

    arm = record["arms"]["extracted"]
    assert code == 0
    assert arm["prompt_sha256"] == _sha("an edited prompt")
    # Named apart from the file-bytes schema_sha256 of earlier records, which isn't comparable.
    assert arm["schema_canonical_sha256"] == _sha(
        json.dumps(json.loads(SCHEMA.read_text()), sort_keys=True, separators=(",", ":")))
    assert (arm["model"], arm["effort"], arm["max_tokens"], arm["temperature"]) == ("claude-other", "low", 1234, None)
    # ev-15, ev-16 and ev-17 are out of scope and ev-19, ev-20 bill conflicting codes: no model call.
    assert (arm["calls"], arm["responses"]) == (15, 15)


def test_a_prompt_that_changes_mid_run_is_recorded_as_every_hash_sent(tmp_path, monkeypatch):
    ev01 = load_case(EVAL / "ev-01.json").note

    def edited_for_ev01(req):
        return {**req, "system": "an edited prompt"} if ev01 in req["messages"][0]["content"] else req

    _, record = _eval_extracted(tmp_path, monkeypatch, _sending(edited_for_ev01))

    assert sorted(record["arms"]["extracted"]["prompt_sha256"]) == sorted([_sha("an edited prompt"), _sha(PROMPT.read_text())])


def test_each_model_call_is_logged_as_soon_as_it_returns(tmp_path, monkeypatch):
    def dies(*args):
        raise KeyboardInterrupt  # the whole run dies right after the first call returns
    monkeypatch.setattr(ex, "parse_extraction", dies)

    with pytest.raises(KeyboardInterrupt):
        _eval_extracted(tmp_path, monkeypatch, _sending(raw={}, stop_reason="end_turn"))

    (log,) = tmp_path.glob("eval-*-extractions/*.json")
    assert json.loads(log.read_text())["request_id"] == "req_stand_in"


def test_a_failed_call_is_counted_and_its_case_marked_as_calling_the_model(tmp_path, monkeypatch):
    def overloaded(note, criteria, client):
        raise RuntimeError("529 overloaded")

    code, record = _eval_extracted(tmp_path, monkeypatch, overloaded)

    arm = record["arms"]["extracted"]
    called = {r["case"] for r in record["results"] if r["arm"] == "extracted" and r["model_called"]}
    assert code == 3
    assert (arm["calls"], arm["responses"]) == (15, 0)
    assert {f"ev-{n:02d}" for n in range(1, 21)} - called == {"ev-15", "ev-16", "ev-17", "ev-19", "ev-20"}


def test_the_run_record_survives_a_request_without_effort_or_with_a_block_prompt(tmp_path, monkeypatch):
    blocks = [{"type": "text", "text": "a block prompt"}]

    def unusual(req):
        return {**{k: v for k, v in req.items() if k != "output_config"}, "system": blocks}

    code, record = _eval_extracted(tmp_path, monkeypatch, _sending(unusual))

    arm = record["arms"]["extracted"]
    assert code == 0
    assert arm["effort"] is None and arm["schema_canonical_sha256"] is None
    assert arm["prompt_sha256"] == _sha(json.dumps(blocks, sort_keys=True, separators=(",", ":")))


def test_triage_extract_prints_the_verdict_and_the_log_of_the_call(tmp_path, monkeypatch, capsys):
    raw, _ = _gold_raw("ev-01")
    monkeypatch.chdir(tmp_path)  # the log goes under the working directory
    monkeypatch.setattr(rcm_ai, "_client", lambda: None)
    monkeypatch.setattr(ex, "extract", _sending(raw=raw, stop_reason="end_turn"))

    code = rcm_ai.main(["triage", str(EVAL / "ev-01.json"), "--extract", "--data", str(DATA)])

    out = capsys.readouterr().out
    (log,) = tmp_path.glob("eval-results/extractions/ev-01-*.json")
    assert code == 0
    assert out.startswith("ev-01: overturnable")
    assert f"extraction log: {log.relative_to(tmp_path)}" in out
    assert json.loads(log.read_text())["request_id"] == "req_stand_in"


def test_triage_extract_keeps_the_log_when_triage_then_dies(tmp_path, monkeypatch):
    def dies(*args):
        raise KeyboardInterrupt
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rcm_ai, "_client", lambda: None)
    monkeypatch.setattr(ex, "extract", _sending(raw={}, stop_reason="end_turn"))
    monkeypatch.setattr(ex, "parse_extraction", dies)

    with pytest.raises(KeyboardInterrupt):
        rcm_ai.main(["triage", str(EVAL / "ev-01.json"), "--extract", "--data", str(DATA)])

    assert len(list(tmp_path.glob("eval-results/extractions/ev-01-*.json"))) == 1


def test_triage_extract_of_an_out_of_scope_claim_makes_no_model_call(monkeypatch, capsys):
    def must_not_call(note, criteria, client):
        raise AssertionError("the model was called for an out-of-scope claim")
    monkeypatch.setattr(rcm_ai, "_client", lambda: None)
    monkeypatch.setattr(ex, "extract", must_not_call)

    code = rcm_ai.main(["triage", str(EVAL / "ev-16.json"), "--extract", "--data", str(DATA)])

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("ev-16: needs_human")
    assert "denial_out_of_scope" in out and "no model call" in out


# Live: these call the model. They need credentials and are skipped, visibly, without them.
_has_credentials = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                        or (Path.home() / ".config" / "anthropic").exists())
live = pytest.mark.skipif(not _has_credentials, reason="needs Anthropic credentials (live model call)")


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
