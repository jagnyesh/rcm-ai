"""Issue #22: each extracted span must support the code or criterion it's attached to, read in
its note sentence; a pair that doesn't sends the claim to a human.

Only the live tests call the model. The rest test our own routing, with a stand-in for the
checker call (never for the SDK)."""

import hashlib
import json
import os
from pathlib import Path

import pytest

import rcm_ai
from rcm_ai import extract as ex
from rcm_ai.extract import (
    EFFORT, MAX_TOKENS, MODEL, SUPPORT_PROMPT, SUPPORT_SCHEMA, Extraction, build_support_request, sentence_around,
    triage_note,
)
from rcm_ai.graph import build_graph
from rcm_ai.schemas import Claim, load_case

DATA = Path(__file__).resolve().parents[1] / "data"
EVAL = DATA / "eval" / "cases"
GRAPH = build_graph(DATA)


def _raw(evidence, **override) -> dict:
    raw = {"diagnoses": [{"code": d.code, "span": d.span} for d in evidence.diagnoses],
           "criteria_met": [{"criterion": c.criterion, "span": c.span} for c in evidence.criteria],
           "criteria_not_met": [{"criterion": c.criterion, "span": c.span} for c in evidence.criteria_not_met]}
    return {**raw, **override}


def _negation():
    """ev-10 (gold uphold) with "not on insulin," added, and insulin marked met on "on insulin"."""
    case = load_case(EVAL / "ev-10.json")
    note = case.note.replace("No hypoglycemia reported.", "Patient is not on insulin, no hypoglycemia reported.")
    met = [{"criterion": c.criterion, "span": c.span} for c in case.evidence.criteria]
    return case.claim, note, _raw(case.evidence, criteria_met=[*met, {"criterion": "cgm-4a-insulin", "span": "on insulin"}],
                                  criteria_not_met=[]), "cgm-4a-insulin"


def _reused_span():
    """ev-01 (gold overturnable) without its FDA-indication sentence; cgm-3 reuses the training span."""
    case = load_case(EVAL / "ev-01.json")
    note = case.note.replace(" CGM prescribed per its FDA-labeled indications.", "")
    training = next(c.span for c in case.evidence.criteria if c.criterion == "cgm-2-training")
    met = [{"criterion": c.criterion, "span": training if c.criterion == "cgm-3-fda-indication" else c.span}
           for c in case.evidence.criteria]
    return case.claim, note, _raw(case.evidence, criteria_met=met), "cgm-3-fda-indication"


def _code_mismatch():
    """ev-04's note documents diabetes "without complications"; the claim bills E11.65, extracted on that span."""
    case = load_case(EVAL / "ev-04.json")
    claim = Claim(case.claim.hcpcs, ("E11.65",), case.claim.carc, case.claim.coverage)
    dx = [{"code": "E11.65", "span": "Type 2 diabetes mellitus without complications"}]
    return claim, case.note, _raw(case.evidence, diagnoses=dx), "E11.65"


REPROS = {"negation": _negation, "reused span": _reused_span, "code/span mismatch": _code_mismatch}


def _extractor(raw):
    return lambda note, criteria: Extraction(raw, "end_turn", {"stub": True})


def _judging(unsupported_keys=(), seen=None):
    """A stand-in checker: judges a pair unsupported when its key is listed, else supported."""
    def check(pairs):
        if seen is not None:
            seen.extend(pairs)
        items = [{"item": i, "reason": f"stand-in says {'no' if p.span.key in unsupported_keys else 'yes'}",
                  "supported": p.span.key not in unsupported_keys} for i, p in enumerate(pairs, 1)]
        return Extraction({"items": items}, "end_turn", {"stub": True})
    return check


@pytest.mark.parametrize("repro", REPROS)
def test_the_three_repros_are_overturnable_when_every_pair_is_judged_supported(repro):
    claim, note, raw, _ = REPROS[repro]()

    verdict, _ = triage_note(claim, note, GRAPH, _extractor(raw), _judging())

    assert verdict.outcome == "overturnable"


@pytest.mark.parametrize("repro", REPROS)
def test_an_unsupported_pair_hands_the_claim_off_naming_the_pair(repro):
    claim, note, raw, bad = REPROS[repro]()
    seen = []

    verdict, _ = triage_note(claim, note, GRAPH, _extractor(raw), _judging({bad}, seen))

    (pair,) = [p for p in seen if p.span.key == bad]
    assert verdict.outcome == "needs_human"
    (reason,) = verdict.reasons
    assert reason.kind == "span_not_supported"
    assert bad in reason.statement and repr(pair.span.span) in reason.statement
    assert pair.text in reason.statement and "stand-in says no" in reason.statement


def test_the_checker_sees_each_span_in_its_note_sentence_with_the_code_title_or_policy_text():
    claim, note, raw, _ = _negation()
    seen = []

    triage_note(claim, note, GRAPH, _extractor(raw), _judging(seen=seen))

    by_key = {p.span.key: p for p in seen}
    assert len(seen) == sum(len(items) for items in raw.values())
    assert by_key["cgm-4a-insulin"].sentence == "Patient is not on insulin, no hypoglycemia reported."
    assert by_key["cgm-4a-insulin"].text == "The beneficiary is insulin-treated"
    assert by_key["E11.9"].sentence == "Type 2 diabetes mellitus without complications, managed on metformin alone."
    assert by_key["E11.9"].text == GRAPH.title("E11.9")


@pytest.mark.parametrize("note, span, sentence", [
    ("Seen today. Glucose 3.0 mmol/L at night. Plan below.", "Glucose", "Glucose 3.0 mmol/L at night."),
    ("Seen today. Glucose 3.0 mmol/L at night. Plan below.", "at night", "Glucose 3.0 mmol/L at night."),
    ("Seen today. On insulin. Plan below.", "On insulin.", "On insulin."),
    ("Seen today. Training done; prescription written. Next.", "prescription written", "Training done; prescription written."),
    ("Seen today.\nOn insulin glargine nightly\nPlan below.", "insulin glargine", "On insulin glargine nightly"),
    ("Seen today. Level 2 events. Two dose changes. Next.", "events. Two dose", "Level 2 events. Two dose changes."),
    ("on insulin", "on insulin", "on insulin"),
])
def test_a_span_is_read_in_the_sentences_that_hold_it(note, span, sentence):
    start = note.find(span)

    assert sentence_around(note, start, start + len(span)) == sentence


def _broken(edit):
    """A stand-in checker whose judgments of every pair as supported are then edited."""
    def check(pairs):
        response = _judging()(pairs)
        return Extraction(edit(response.raw["items"]), "end_turn", {"stub": True})
    return check


@pytest.mark.parametrize("checker, stop", [
    (lambda pairs: Extraction(None, "refusal", {}), "refusal"),
    (lambda pairs: Extraction(None, "max_tokens", {}), "max_tokens"),
    (_broken(lambda items: {"items": items[1:]}), "end_turn"),  # a pair left unjudged
    (_broken(lambda items: {"items": [*items, items[0]]}), "end_turn"),  # a pair judged twice
    (_broken(lambda items: {"items": [{**i, "supported": "yes"} for i in items]}), "end_turn"),
])
def test_a_check_that_fails_or_skips_a_pair_hands_the_claim_off(checker, stop):
    claim, note, raw, _ = _reused_span()

    verdict, _ = triage_note(claim, note, GRAPH, _extractor(raw), checker)

    assert verdict.outcome == "needs_human"
    (reason,) = verdict.reasons
    assert reason.kind == "support_check_failed" and stop in reason.statement


def test_the_check_request_pins_the_model_prompt_and_schema_and_sends_only_the_span_sentences():
    case = load_case(EVAL / "ev-01.json")
    seen = []
    triage_note(case.claim, case.note, GRAPH, _extractor(_raw(case.evidence)), _judging(seen=seen))

    req = build_support_request(seen)

    content = req["messages"][0]["content"]
    assert (req["model"], req["output_config"]["effort"], req["max_tokens"]) == (MODEL, EFFORT, MAX_TOKENS)
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": json.loads(SUPPORT_SCHEMA.read_text())}
    assert req["system"] == SUPPORT_PROMPT.read_text()
    assert "temperature" not in req
    for n, pair in enumerate(seen, 1):
        assert f"Item {n}: " in content
        assert all(json.dumps(x) in content for x in (pair.span.span, pair.sentence, pair.text))
    assert "SYNTHETIC NOTE" not in content  # the one sentence no span is in


# Run records: the CLI with stand-ins for our `extract` (gold evidence for every case) and our
# `check_support`, which record the requests they were asked to send, as the SDK sends them.

GOLD = {c.note: _raw(c.evidence) for c in map(load_case, sorted(EVAL.glob("*.json")))}


def _extracting(note, criteria, client):
    return Extraction(GOLD[note], "end_turn", {
        "request_id": "req_extract", "http_request_body": json.dumps(ex.build_request(note, criteria)),
        "response": {"model": MODEL}})


def _checking(edit=lambda req: req, unsupported_keys=()):
    def stand_in(pairs, client):
        sent = edit(ex.build_support_request(pairs))
        judged = _judging(unsupported_keys)(pairs)
        return Extraction(judged.raw, "end_turn", {
            "request_id": "req_check", "http_request_body": json.dumps(sent), "response": {"model": sent["model"]}})
    return stand_in


def _eval_extracted(tmp_path, monkeypatch, checking):
    monkeypatch.setattr(rcm_ai, "_client", lambda: None)
    monkeypatch.setattr(ex, "extract", _extracting)
    monkeypatch.setattr(ex, "check_support", checking)
    code = rcm_ai.main(["eval", "--arms", "extracted", "--data", str(DATA), "--out", str(tmp_path)])
    (record,) = tmp_path.glob("eval-*.json")
    return code, json.loads(record.read_text())


def _sha(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_run_record_describes_the_support_check_requests_sent(tmp_path, monkeypatch):
    def edited(req):
        return {**req, "model": "claude-other", "system": "an edited prompt",
                "output_config": {**req["output_config"], "effort": "low"}}

    code, record = _eval_extracted(tmp_path, monkeypatch, _checking(edited))

    check = record["arms"]["extracted"]["support_check"]
    assert code == 0
    assert (check["model"], check["effort"], check["max_tokens"]) == ("claude-other", "low", MAX_TOKENS)
    assert check["prompt_sha256"] == _sha("an edited prompt")
    assert check["schema_canonical_sha256"] == _sha(json.loads(SUPPORT_SCHEMA.read_text()))
    # 15 cases reach extraction; ev-10's gold evidence is rejected before the check, since it
    # lists the parent cgm-4b-hypoglycemia as not met.
    assert (check["calls"], check["responses"], check["without_output"]) == (14, 14, 0)
    called = {r["case"]: r["support_check"] for r in record["results"] if r["arm"] == "extracted"}
    assert called["ev-01"] == {"called": True, "served_model": "claude-other", "request_id": "req_check",
                               "stop_reason": "end_turn"}
    assert called["ev-16"]["called"] is False  # out of scope: no extraction, nothing to check


def test_an_unsupported_pair_is_scored_as_a_hand_off_with_its_reason(tmp_path, monkeypatch):
    _, record = _eval_extracted(tmp_path, monkeypatch, _checking(unsupported_keys={"cgm-4a-insulin"}))

    (ev01,) = [r for r in record["results"] if r["arm"] == "extracted" and r["case"] == "ev-01"]
    assert ev01["outcome"] == "needs_human"
    assert ev01["why"] == ["span_not_supported: Span 'On multiple daily insulin injections.' doesn't support "
                           "criterion cgm-4a-insulin met ('The beneficiary is insulin-treated'): stand-in says no"]


def test_each_support_check_is_logged_as_soon_as_it_returns(tmp_path, monkeypatch):
    def dies(*args):
        raise KeyboardInterrupt  # the whole run dies right after the first check returns
    monkeypatch.setattr(ex, "adjudicate", dies)

    with pytest.raises(KeyboardInterrupt):
        _eval_extracted(tmp_path, monkeypatch, _checking())

    (log,) = tmp_path.glob("eval-*-support/*.json")
    assert json.loads(log.read_text())["request_id"] == "req_check"


def test_triage_extract_prints_the_support_check_and_its_log(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # the logs go under the working directory
    monkeypatch.setattr(rcm_ai, "_client", lambda: None)
    monkeypatch.setattr(ex, "extract", _extracting)
    monkeypatch.setattr(ex, "check_support", _checking())

    code = rcm_ai.main(["triage", str(EVAL / "ev-01.json"), "--extract", "--data", str(DATA)])

    out = capsys.readouterr().out
    (log,) = tmp_path.glob("eval-results/support-checks/ev-01-*.json")
    assert code == 0
    assert out.startswith("ev-01: overturnable")
    assert f"spans checked by {MODEL} (effort {EFFORT}), stop end_turn, request req_check" in out
    assert f"support check log: {log.relative_to(tmp_path)}" in out


# Live: these call the model. They need credentials and are skipped, visibly, without them.
_has_credentials = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                        or (Path.home() / ".config" / "anthropic").exists())
live = pytest.mark.skipif(not _has_credentials, reason="needs Anthropic credentials (live model call)")


@live
@pytest.mark.parametrize("repro", REPROS)
def test_live_the_check_hands_off_each_repro_naming_its_unsupported_pair(repro):
    import anthropic

    claim, note, raw, bad = REPROS[repro]()
    client = anthropic.Anthropic()

    verdict, _ = triage_note(claim, note, GRAPH, _extractor(raw), lambda pairs: ex.check_support(pairs, client))

    assert verdict.outcome == "needs_human", verdict
    assert {r.kind for r in verdict.reasons} == {"span_not_supported"}, verdict.reasons
    assert any(bad in r.statement for r in verdict.reasons), verdict.reasons


@live
@pytest.mark.parametrize("case_path", sorted(EVAL.glob("*.json")), ids=lambda p: p.stem)
def test_live_every_gold_span_passes_the_check(case_path):
    """Every eval case's gold evidence, whether or not triage would reach the check for it."""
    import anthropic

    case = load_case(case_path)
    criteria = GRAPH.criteria("L33822")
    spans = ex.parse_extraction(_raw(case.evidence), case.note, GRAPH, criteria).spans
    pairs = ex.support_pairs(spans, case.note, GRAPH, criteria)

    check = ex.check_support(pairs, anthropic.Anthropic())

    assert ex.unsupported(check, pairs) == [], check.raw
