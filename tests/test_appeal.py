import json
import os
import re
import shutil
import time
from pathlib import Path

import pytest

import rcm_ai
from rcm_ai import appeal as ap
from rcm_ai import baseline as bl
from rcm_ai import extract as ex
from rcm_ai.adjudicate import adjudicate
from rcm_ai.extract import Extraction
from rcm_ai.graph import build_graph
from rcm_ai.schemas import Hop, load_case

DATA = Path(__file__).resolve().parents[1] / "data"
EVAL = DATA / "eval" / "cases"
GRAPH = build_graph(DATA)


def _verdict(case_id):
    case = load_case(EVAL / f"{case_id}.json")
    return adjudicate(case.claim, case.evidence, GRAPH)


def _path(case_id):
    return _verdict(case_id).path


EV01 = _path("ev-01")  # E2103, LCD L33822, article A52464, E11.65


# The checker: deterministic, a pure function of the letter and the reference verdict's path and findings.

def test_a_letter_citing_only_codes_and_ids_in_the_path_passes():
    letter = ("We appeal the denial of E2103 under LCD L33822 and its policy article A52464. "
              "The beneficiary's diagnosis, E11.65, is covered.")

    check = ap.check_citations(letter, EV01)

    assert check.passed
    assert check.citations == ("E2103", "L33822", "A52464", "E11.65")
    assert check.unsupported == ()


@pytest.mark.parametrize("cited", ["L99999", "A12345"])
def test_a_fabricated_policy_id_fails(cited):
    check = ap.check_citations(f"Coverage is established by {cited} and LCD L33822.", EV01)

    assert not check.passed
    assert check.unsupported == (cited,)


def test_a_fabricated_code_fails():
    check = ap.check_citations("The note documents E11.99, which is covered.", EV01)

    assert check.unsupported == ("E11.99",)


@pytest.mark.parametrize("cited", ["E11.9", "Z79.4", "A4239"])
def test_a_real_code_outside_the_path_fails(cited):
    # A covered diabetes code, a status code and the CGM supply allowance: all real, none
    # established by this path.
    check = ap.check_citations(f"See also {cited}.", EV01)

    assert check.unsupported == (cited,)


@pytest.mark.parametrize("text, cited", [
    ("CPT 95250", "95250"), ("CPT code 95250", "95250"), ("CPT® 95251", "95251"), ("CPT 3044F", "3044F"),
])
def test_a_cpt_code_labeled_as_one_fails(text, cited):
    assert ap.check_citations(f"Billed as {text}.", EV01).unsupported == (cited,)


@pytest.mark.parametrize("cited", ["E119", "Z794", "E11649", "E113213", "O24111"])
def test_a_code_written_without_its_dot_is_still_a_citation(cited):
    # Claim forms and remittances carry codes undotted; a code outside the path fails either way.
    assert ap.check_citations(f"Diagnosis {cited} applies.", EV01).unsupported == (cited,)


@pytest.mark.parametrize("text", ["E1165", "E2103KX", "E2103-KX", "LCD 33822", "LCD #33822", "Article 52464",
                                  "policy article 52464"])
def test_a_path_code_or_policy_id_in_another_written_form_is_supported(text):
    check = ap.check_citations(f"Per {text}, coverage applies.", EV01)

    assert check.citations and check.passed


@pytest.mark.parametrize("text, cited", [
    ("A4239NU", "A4239NU"), ("LCD 12345", "L12345"), ("DL33822", "DL33822"), ("NCD 40.2", "NCD 40.2"),
    ("NCD 280.1", "NCD 280.1"), ("article 12345", "A12345"),
])
def test_another_item_or_policy_in_any_written_form_fails(text, cited):
    assert ap.check_citations(f"Per {text}, coverage applies.", EV01).unsupported == (cited,)


@pytest.mark.parametrize("cited", ["T38.3X5A", "C7A.00", "Z3A.20", "E11.3213"])
def test_long_codes_and_categories_with_a_letter_third_are_read_whole(cited):
    assert ap.citations(f"Code {cited} was documented.") == (cited,)


def test_clinical_abbreviations_and_other_numbers_are_not_citations():
    letter = ("The A1C is 8.2% (HbA1c). T2D, T1D, T2DM and G6PD on MDI. Glucose <54 mg/dL (3.0 mmol/L). "
              "CARC 50, criteria (1)-(5), version 70, [ZIP code], 42 CFR 405.940. Form CMS-20027, "
              "P.O. Box 20010, Nashville, TN 37202. 20160 minutes of CGM data; $12500 billed; 95250 alone.")

    assert ap.check_citations(letter, EV01).citations == ()


@pytest.mark.parametrize("cited", ["E21.03", "L33.822", "A52.464", "E1165-KX", "E1165XY", "E116512"])
def test_a_made_up_code_sharing_a_path_codes_characters_fails(cited):
    # Only an ICD-10-CM code is written with a dot, and only a HCPCS code carries modifiers.
    assert ap.check_citations(f"Diagnosis {cited} applies.", EV01).unsupported == (cited,)


@pytest.mark.parametrize("text, cited", [
    ("E2103.0", "E2103.0"), ("E11.65.1", "E11.65.1"), ("L33822.1", "L33822.1"), ("A52464.12", "A52464.12"),
    ("LCD 33822.1", "L33822.1"), ("LCD L33822.1", "L33822.1"), ("article 52464.12", "A52464.12"),
])
def test_a_path_code_with_a_dotted_suffix_is_another_code(text, cited):
    check = ap.check_citations(f"Per {text}, coverage applies.", EV01)

    assert check.citations == check.unsupported == (cited,)


def test_a_code_written_two_ways_is_counted_once():
    check = ap.check_citations("Insulin use Z79.4 (Z794). Item E2103, billed as E2103-KX.", EV01)

    assert check.citations == ("Z79.4", "E2103")
    assert check.unsupported == ("Z79.4",)


@pytest.mark.parametrize("letter", ["Item E2103, E21.03 and E2103-KX.", "Item E2103-KX, E21.03 and E2103."])
def test_one_code_in_several_forms_counts_once_in_any_order_and_names_its_unsupported_form(letter):
    check = ap.check_citations(letter, EV01)

    assert check.citations == check.unsupported == ("E21.03",)


def test_two_codes_written_without_their_dots_stay_two():
    # E113213 is E11.3213, not HCPCS E1132 with a modifier, so it isn't the category E11.32.
    check = ap.check_citations("Diagnosis E113213, from category E1132.", _path("ev-07"))

    assert check.citations == ("E113213", "E1132")
    assert check.unsupported == ("E1132",)


def test_a_long_whitespace_run_after_a_label_is_read_quickly():
    # The letter is model output; a label's optional parts between adjacent \s* backtracked
    # cubically on a whitespace run (2,000 spaces took about 12 s).
    text = " x. ".join(label + " " * 2000 for label in ("CPT", "LCD", "article", "NCD")) + " x."

    start = time.perf_counter()
    assert ap.citations(text) == ()
    assert time.perf_counter() - start < 1


def test_codes_are_found_next_to_punctuation_and_counted_once():
    check = ap.check_citations("Diagnosis (E11.65). Again: E11.65; item E2103, LCD L33822.", EV01)

    assert check.citations == ("E11.65", "E2103", "L33822")


def test_an_empty_path_supports_no_citation():
    check = ap.check_citations("E2103 under LCD L33822 for E11.65.", ())

    assert check.unsupported == ("E2103", "L33822", "E11.65")


def test_a_letter_citing_nothing_fails_against_a_reference_that_establishes_nothing():
    # An empty reference means no letter is warranted, so leaving every code out can't pass.
    letter = "We ask that the denial be reversed."

    assert not ap.check_citations(letter, ()).passed
    assert ap.check_citations(letter, EV01).passed


def test_the_checker_reads_the_path_statements_not_their_source_anchors():
    hop = Hop("E2103 is listed in LCD L33822", anchor="E11.9", source=EV01[0].source)

    assert ap.established((hop,)) == ("E2103", "L33822")


def test_a_code_named_only_in_a_statements_aside_is_not_established():
    # ev-07's concept hop notes its 7th character comes "from E11.32 sevenChrDef"; E11.32 itself
    # isn't a billable code, and the path doesn't establish it as the diagnosis.
    ev07 = _path("ev-07")

    assert "E11.32" in " ".join(h.statement for h in ev07)
    assert ap.check_citations("The diagnosis E11.32 is covered.", ev07).unsupported == ("E11.32",)


def test_an_unsourced_steps_aside_is_part_of_what_it_asserts():
    # The baseline's reasons are its own claims, so its "(Z79.4)" is a citation, not provenance.
    case = load_case(EVAL / "ev-01.json")
    reason = Hop("cgm-4a-insulin: the beneficiary is insulin-treated (Z79.4 supported).")

    content = ap.build_request(case.claim, (reason,), [])["messages"][0]["content"]

    assert "insulin-treated (Z79.4 supported)." in content
    assert ap.established((reason,)) == ("Z79.4",)


def test_a_code_the_verdicts_findings_name_is_established():
    # ev-08 bills E11.9, and the graph finds the note documents the more specific E11.65.
    ev08 = _verdict("ev-08")
    letter = "The note supports the more specific E11.65 over the billed E11.9."

    assert ap.check_citations(letter, ev08.path).unsupported == ("E11.65",)
    assert ap.check_citations(letter, ev08.path, ev08.findings).passed


# The drafter: an LLM writes the letter from the claim, the path and the note's quotes.

QUOTES = [("diagnosis", "Type 2 diabetes mellitus with hyperglycemia"), ("cgm-4a-insulin", "On multiple daily insulin injections.")]


def test_the_draft_request_carries_the_claim_the_path_and_the_note_quotes():
    case = load_case(EVAL / "ev-01.json")

    req = ap.build_request(case.claim, EV01, QUOTES)

    content = req["messages"][0]["content"]
    for text in ("E2103", "E11.65", "CARC 50", "initial"):
        assert text in content
    for hop in EV01:
        assert hop.statement in content
    for label, span in QUOTES:
        assert f'{label}: "{span}"' in content
    assert (req["model"], req["max_tokens"], req["output_config"]["effort"]) == (ex.MODEL, ex.MAX_TOKENS, ex.EFFORT)
    assert "temperature" not in req
    assert req["system"] == ap.PROMPT.read_text() and ap.PROMPT.parent == ex.PROMPTS
    assert req["output_config"]["format"]["schema"]["required"] == ["letter"]


@pytest.mark.parametrize("case_id", ["ev-01", "ev-06", "ev-07"])
def test_the_drafter_sees_no_citable_code_the_path_doesnt_establish(case_id):
    # Source file names such as icd10cm-tabular-2027-E08-E13.xml would put E08 and E13 in front of
    # the drafter of a sourced path only, where the checker rejects them.
    case, path = load_case(EVAL / f"{case_id}.json"), _path(case_id)

    content = ap.build_request(case.claim, path, ap.quotes(case.evidence))["messages"][0]["content"]

    assert set(ap.citations(content)) <= set(ap.established(path))


def test_a_step_without_an_aside_reads_the_same_sourced_or_not():
    case = load_case(EVAL / "ev-01.json")
    sourced, unsourced = EV01[0], Hop(EV01[0].statement)

    assert ap.build_request(case.claim, (sourced,), []) == ap.build_request(case.claim, (unsourced,), [])


def test_the_drafter_sees_the_verdicts_findings():
    case, ev08 = load_case(EVAL / "ev-08.json"), _verdict("ev-08")

    content = ap.build_request(case.claim, ev08.path, [], ev08.findings)["messages"][0]["content"]

    assert f"Findings:\n- {ev08.findings[0].statement}\n" in content


def test_quotes_come_from_the_evidence_diagnoses_first_without_their_codes():
    case = load_case(EVAL / "ev-01.json")

    quotes = ap.quotes(case.evidence)

    assert quotes[0] == ("diagnosis", "Type 2 diabetes mellitus with hyperglycemia")
    assert ("cgm-4a-insulin", "On multiple daily insulin injections.") in quotes
    assert ap.quotes(None) == []


@pytest.mark.parametrize("raw, letter", [
    ({"letter": "Dear reviewer"}, "Dear reviewer"),
    ({"letter": "  "}, None),
    ({"text": "Dear reviewer"}, None),
    (None, None),
])
def test_a_response_gives_a_letter_only_when_it_holds_one(raw, letter):
    assert ap.letter_of(Extraction(raw, "end_turn", {})) == letter


def test_a_draft_that_raised_is_an_error_not_a_draft_without_a_letter():
    tallied = ap.tally([ap.Appeal(("E2103",), error="RuntimeError: 529"), ap.Appeal(("E2103",))])

    assert (tallied["drafted"], tallied["errors"], tallied["without_letter"]) == (2, 1, 1)


def test_draft_sends_the_draft_request(monkeypatch):
    case = load_case(EVAL / "ev-01.json")
    sent = []
    monkeypatch.setattr(ex, "send", lambda request, client: sent.append(request) or Extraction(None, "refusal", {}))

    ap.draft(case.claim, EV01, QUOTES, client=None)

    assert sent == [ap.build_request(case.claim, EV01, QUOTES)]


def test_the_baselines_evidence_is_read_without_checking_it():
    response = Extraction({"diagnoses": [{"code": "E11.65", "span": "not in any note"}, "junk"],
                           "criteria_met": [{"criterion": "made-up", "span": "x"}],
                           "criteria_not_met": [{"criterion": "cgm-4b-hypoglycemia", "span": "No hypoglycemic events"}],
                           "verdict": "overturnable", "reasons": ["r"]}, "end_turn", {})

    evidence = bl.evidence_from(response)

    assert ap.quotes(evidence) == [("diagnosis", "not in any note"), ("made-up", "x")]
    assert bl.evidence_from(Extraction(None, "refusal", {})) is None


# triage --draft

def _drafting(letter=None, seen=None):
    """A stand-in drafter that writes `letter`, or by default echoes the steps it was given."""
    def stand_in(claim, path, quotes, client, findings=()):
        if seen is not None:
            seen.append((claim, path, quotes))
        sent = ap.build_request(claim, path, quotes, findings)
        text = letter if letter is not None else " ".join(ap.asserted(h) for h in path)
        return Extraction({"letter": text}, "end_turn", {
            "request_id": "req_draft", "http_request_body": json.dumps(sent), "response": {"model": sent["model"]}})
    return stand_in


def _triage(tmp_path, monkeypatch, case_id, drafter, *flags, client=lambda: None):
    monkeypatch.chdir(tmp_path)  # the draft log goes under the working directory
    monkeypatch.setattr(rcm_ai, "_client", client)
    monkeypatch.setattr(ap, "draft", drafter)
    return rcm_ai.main(["triage", str(EVAL / f"{case_id}.json"), "--draft", *flags, "--data", str(DATA)])


def test_triage_draft_prints_the_letter_and_the_citation_check(tmp_path, monkeypatch, capsys):
    code = _triage(tmp_path, monkeypatch, "ev-01", _drafting("Dear reviewer,\nWe appeal E2103 under LCD L33822."))

    out = capsys.readouterr().out
    (log,) = tmp_path.glob("eval-results/drafts/ev-01-*.json")
    assert code == 0
    assert out.startswith("ev-01: overturnable")
    assert "  appeal draft:\n    Dear reviewer,\n    We appeal E2103 under LCD L33822.\n" in out
    assert "  citation check: passed (E2103, L33822)" in out
    assert f"draft log: {log.relative_to(tmp_path)}" in out


def test_triage_draft_names_each_unsupported_citation_and_exits_4(tmp_path, monkeypatch, capsys):
    # A script gating on the exit code must not send a failing letter; 4 can't be read as a crash's 1.
    code = _triage(tmp_path, monkeypatch, "ev-01", _drafting("Covered under L99999 for E11.65 and Z79.4."))

    assert code == 4
    assert "  citation check: FAILED, unsupported: L99999, Z79.4 (cited: L99999, E11.65, Z79.4)" in capsys.readouterr().out


def test_triage_draft_exits_4_for_a_failing_letter_whose_log_cannot_be_written(tmp_path, monkeypatch, capsys):
    (tmp_path / "eval-results").write_text("")  # a file where the log directory would go

    code = _triage(tmp_path, monkeypatch, "ev-01", _drafting("Covered under L99999."))

    captured = capsys.readouterr()
    assert code == 4
    assert "citation check: FAILED, unsupported: L99999" in captured.out
    assert "appeal draft log not written:" in captured.err


def test_triage_draft_checks_against_the_verdicts_own_path(tmp_path, monkeypatch, capsys):
    seen = []

    _triage(tmp_path, monkeypatch, "ev-01", _drafting(seen=seen))

    ((claim, path, quotes),) = seen
    case = load_case(EVAL / "ev-01.json")
    assert path == EV01 and quotes == ap.quotes(case.evidence)
    assert "citation check: passed" in capsys.readouterr().out


# A valid extraction for ev-01 whose spans differ from the case's gold evidence.
EV01_EXTRACTED = {
    "diagnoses": [{"code": "E11.65", "span": "Type 2 diabetes mellitus with hyperglycemia"}],
    "criteria_met": [
        {"criterion": "cgm-5-visit", "span": "Telehealth visit today to review diabetes control"},
        {"criterion": "cgm-2-training", "span": "Completed CGM device training"},
        {"criterion": "cgm-3-fda-indication", "span": "CGM prescribed per its FDA-labeled indications"},
        {"criterion": "cgm-4a-insulin", "span": "On multiple daily insulin injections"},
    ],
    "criteria_not_met": [],
}
EV01_EXTRACTED_QUOTES = [("diagnosis", "Type 2 diabetes mellitus with hyperglycemia")] + [
    (c["criterion"], c["span"]) for c in EV01_EXTRACTED["criteria_met"]]


def _extracting(note, criteria, client):
    raw = EV01_EXTRACTED if note == load_case(EVAL / "ev-01.json").note else None
    return Extraction(raw, "end_turn" if raw else "refusal", {
        "request_id": "req_ex", "http_request_body": json.dumps(ex.build_request(note, criteria)),
        "response": {"model": ex.MODEL}})


def test_triage_extract_draft_drafts_from_the_extracted_evidence_with_one_client(tmp_path, monkeypatch, capsys):
    seen, clients = [], []
    monkeypatch.setattr(ex, "extract", _extracting)

    code = _triage(tmp_path, monkeypatch, "ev-01", _drafting(seen=seen), "--extract",
                   client=lambda: clients.append(object()) or clients[-1])

    ((claim, path, quotes),) = seen
    assert code == 0
    assert path == EV01 and quotes == EV01_EXTRACTED_QUOTES
    assert len(clients) == 1


def test_triage_draft_gives_the_drafter_the_findings_and_checks_against_them(tmp_path, monkeypatch, capsys):
    code = _triage(tmp_path, monkeypatch, "ev-08", _drafting("We appeal E2103; the note supports E11.65."))

    (log,) = tmp_path.glob("eval-results/drafts/ev-08-*.json")
    sent = json.loads(json.loads(log.read_text())["http_request_body"])
    assert code == 0
    assert "Findings:\n- Billed E11.9 is covered" in sent["messages"][0]["content"]
    assert "citation check: passed (E2103, E11.65)" in capsys.readouterr().out


def test_triage_prints_the_letter_when_its_log_cannot_be_written(tmp_path, monkeypatch, capsys):
    (tmp_path / "eval-results").write_text("")  # a file where the log directory would go

    code = _triage(tmp_path, monkeypatch, "ev-01", _drafting("We appeal E2103 under LCD L33822."))

    captured = capsys.readouterr()
    assert code == 3
    assert captured.out.startswith("ev-01: overturnable")
    assert "citation check: passed (E2103, L33822)" in captured.out
    assert "appeal draft log not written:" in captured.err


def test_triage_keeps_the_verdict_when_drafting_fails(tmp_path, monkeypatch, capsys):
    def overloaded(*args, **kwargs):
        raise RuntimeError("529 overloaded")

    code = _triage(tmp_path, monkeypatch, "ev-01", overloaded)

    captured = capsys.readouterr()
    assert code == 3
    assert captured.out.startswith("ev-01: overturnable")
    assert "appeal draft failed: RuntimeError: 529 overloaded" in captured.err


def test_triage_draft_without_a_letter_says_so_checks_nothing_and_exits_3(tmp_path, monkeypatch, capsys):
    # No letter leaves nothing to send, as when the drafting call fails.
    def refuses(claim, path, quotes, client, findings=()):
        return Extraction(None, "refusal", {"request_id": "req_draft",
                                            "http_request_body": json.dumps(ap.build_request(claim, path, quotes, findings)),
                                            "response": {"model": ex.MODEL}})

    code = _triage(tmp_path, monkeypatch, "ev-01", refuses)

    out = capsys.readouterr().out
    (log,) = tmp_path.glob("eval-results/drafts/ev-01-*.json")
    assert code == 3
    assert "  no appeal letter: the drafter returned none (stop refusal)" in out
    assert "appeal draft:" not in out and "citation check" not in out
    assert f"draft log: {log.relative_to(tmp_path)}" in out


def test_triage_draft_of_a_verdict_that_is_not_overturnable_makes_no_call(tmp_path, monkeypatch, capsys):
    def must_not_call(*args, **kwargs):
        raise AssertionError("the drafter was called for a claim that is not overturnable")

    code = _triage(tmp_path, monkeypatch, "ev-10", must_not_call)

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("ev-10: uphold")
    assert "no appeal drafted: the verdict is not overturnable" in out


def test_triage_without_draft_needs_no_credentials(monkeypatch, capsys):
    def no_client():
        raise AssertionError("triage asked for a client without --draft or --extract")
    monkeypatch.setattr(rcm_ai, "_client", no_client)

    assert rcm_ai.main(["triage", str(EVAL / "ev-01.json"), "--data", str(DATA)]) == 0
    assert "appeal" not in capsys.readouterr().out


# eval --draft

EVIDENCE = {"diagnoses": [], "criteria_met": [{"criterion": "cgm-2-training", "span": "a baseline span"}],
            "criteria_not_met": []}


def _baseline_overturns(claim, note, policy, client):
    return Extraction({**EVIDENCE, "verdict": "overturnable", "reasons": ["Covered under LCD L33822 as E11.65"]},
                      "end_turn", {"request_id": "req_bl", "http_request_body": json.dumps(bl.build_request(claim, note, policy)),
                                   "response": {"model": ex.MODEL}})


def _eval(tmp_path, monkeypatch, drafter, arms="graph,llm-only", draft=True):
    monkeypatch.setattr(rcm_ai, "_client", lambda: None)
    monkeypatch.setattr(bl, "ask", _baseline_overturns)
    monkeypatch.setattr(ap, "draft", drafter)
    code = rcm_ai.main(["eval", "--arms", arms, "--data", str(DATA), "--out", str(tmp_path),
                        *(["--draft"] if draft else [])])
    (record,) = tmp_path.glob("eval-*.json")
    return code, json.loads(record.read_text())


def test_eval_draft_checks_each_overturnable_verdict_against_the_graphs_path_for_the_claim(tmp_path, monkeypatch, capsys):
    code, record = _eval(tmp_path, monkeypatch, _drafting())

    out = capsys.readouterr().out
    graph, baseline = record["arms"]["graph"]["appeals"], record["arms"]["llm-only"]["appeals"]
    assert code == 0
    # The graph arm drafts from the reference path itself, so every citation is supported.
    assert {k: graph[k] for k in ("drafted", "failed", "unsupported")} == {"drafted": 9, "failed": 0, "unsupported": 0}
    assert graph["unsupported_rate"] == 0.0
    # The baseline overturns all 20 and cites L33822 and E11.65 each time. Against the graph's
    # verdict, both are unsupported on the 11 claims the graph doesn't overturn, and E11.65 is on
    # 7 of the 9 it does: ev-01 bills E11.65, and on ev-08 the graph's specificity finding names it.
    assert {k: baseline[k] for k in ("drafted", "failed", "citations", "unsupported")} == {
        "drafted": 20, "failed": 18, "citations": 40, "unsupported": 29}
    assert baseline["unsupported_rate"] == pytest.approx(29 / 40)
    ev10 = next(r for r in record["results"] if r["arm"] == "llm-only" and r["case"] == "ev-10")
    assert ev10["appeal"]["unsupported"] == ["L33822", "E11.65"] and ev10["appeal"]["reference"] == []
    ev08 = next(r for r in record["results"] if r["arm"] == "llm-only" and r["case"] == "ev-08")
    assert ev08["appeal"]["unsupported"] == [] and "E11.65" in ev08["appeal"]["reference"]
    assert "appeals: 20 drafted, 18 failed the citation check, 0 without a letter, 0 errors; " \
           "unsupported citations 29/40 (72%)" in out
    assert len(list(tmp_path.glob("eval-*-drafts/*.json"))) == 29
    assert record["drafting"]["calls"] == 29 and record["drafting"]["prompt_sha256"]


def test_eval_draft_gives_each_arm_its_own_evidence(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ex, "extract", lambda note, criteria, client: Extraction(None, "refusal", {
        "request_id": "req_ex", "http_request_body": json.dumps(ex.build_request(note, criteria)),
        "response": {"model": ex.MODEL}}))

    code, record = _eval(tmp_path, monkeypatch, _drafting(seen=seen), arms="graph,extracted,llm-only")

    assert code == 0 and record["arms"]["extracted"]["errors"] == 0
    gold = ap.quotes(load_case(EVAL / "ev-01.json").evidence)
    ev01 = [quotes for claim, path, quotes in seen if claim.icd10 == ("E11.65",) and path == EV01]
    assert gold in ev01  # the graph arm, from the case's evidence
    assert [("cgm-2-training", "a baseline span")] in [q for _, _, q in seen]  # the baseline, its own fields
    assert len(seen) == 29  # the refused extractions overturn nothing


def test_eval_draft_gives_the_drafter_the_verdicts_findings(tmp_path, monkeypatch):
    code, record = _eval(tmp_path, monkeypatch, _drafting(), arms="graph")

    (log,) = tmp_path.glob("eval-*-drafts/graph-ev-08-*.json")
    sent = json.loads(json.loads(log.read_text())["http_request_body"])
    assert code == 0
    assert "Findings:\n- Billed E11.9 is covered" in sent["messages"][0]["content"]


@pytest.mark.parametrize("case_id", [f"ev-{n:02d}" for n in range(1, 10)])
def test_the_eval_reference_names_what_the_gold_label_does(case_id):
    # Every arm's letter is checked against the graph's verdict on the gold evidence, so that
    # verdict must agree with the label on its codes and findings, not just its outcome.
    gold, verdict = json.loads((EVAL / f"{case_id}.json").read_text())["gold"], _verdict(case_id)

    assert verdict.outcome == gold["verdict"] == "overturnable"
    assert [(f.type, f.codes) for f in verdict.findings] == [
        (f["type"], (f["billed"], f["documented"])) for f in gold["findings"]]
    assert {gold["path"][k] for k in ("hcpcs", "lcd", "article", "dx")} <= set(ap.established(verdict.path))


def test_eval_draft_gives_the_extracted_arm_its_own_validated_evidence(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ex, "extract", _extracting)

    _eval(tmp_path, monkeypatch, _drafting(seen=seen), arms="graph,extracted")

    ev01 = [quotes for claim, path, quotes in seen if path == EV01]
    assert sorted(ev01) == sorted([ap.quotes(load_case(EVAL / "ev-01.json").evidence), EV01_EXTRACTED_QUOTES])


def _eval_edited(tmp_path, monkeypatch, case_id, edit, drafter=None):
    """eval --arms llm-only --draft on a copy of the data with one eval case edited."""
    data = tmp_path / "data"
    for d in ("eval", "fixtures", "curated"):
        shutil.copytree(DATA / d, data / d)
    case = data / "eval" / "cases" / f"{case_id}.json"
    raw = json.loads(case.read_text())
    edit(raw)
    case.write_text(json.dumps(raw))
    monkeypatch.setattr(rcm_ai, "_client", lambda: None)
    monkeypatch.setattr(bl, "ask", _baseline_overturns)
    monkeypatch.setattr(ap, "draft", drafter or _drafting())

    code = rcm_ai.main(["eval", "--arms", "llm-only", "--data", str(data), "--out", str(tmp_path / "out"), "--draft"])

    (record,) = (tmp_path / "out").glob("eval-*.json")
    return code, json.loads(record.read_text())


def test_a_gold_overturnable_case_the_graph_wont_overturn_gets_no_appeal(tmp_path, monkeypatch, capsys):
    # The graph upholds ev-10 on its gold evidence, so there's no path to check a letter against.
    def relabel(raw):
        raw["gold"]["verdict"] = "overturnable"

    code, record = _eval_edited(tmp_path, monkeypatch, "ev-10", relabel)

    captured = capsys.readouterr()
    ev10 = next(r for r in record["results"] if r["case"] == "ev-10")
    assert code == 0
    assert "appeal" not in ev10
    assert record["drafting"]["calls"] == 19 and record["drafting"]["no_reference"] == ["ev-10"]
    assert record["arms"]["llm-only"]["appeals"]["no_reference"] == 1
    assert "appeals: 19 drafted" in captured.out and "1 without a reference" in captured.out
    assert "no appeal drafted for ev-10" in captured.err


def test_a_letter_for_a_claim_gold_doesnt_overturn_is_checked_against_nothing(tmp_path, monkeypatch):
    # Whatever the graph says, the gold label says no letter is warranted, so every citation fails.
    def relabel(raw):
        raw["gold"]["verdict"] = "needs_human"

    code, record = _eval_edited(tmp_path, monkeypatch, "ev-02", relabel)

    appeal = next(r for r in record["results"] if r["case"] == "ev-02")["appeal"]
    assert code == 0
    assert appeal["reference"] == [] and appeal["unsupported"] == appeal["citations"] != []
    assert record["drafting"]["no_reference"] == []


def test_eval_fails_a_letter_citing_nothing_for_a_claim_gold_doesnt_overturn(tmp_path, monkeypatch, capsys):
    # The baseline overturns all 20; gold overturns 9, so its 11 other letters fail though they cite nothing.
    code, record = _eval(tmp_path, monkeypatch, _drafting("We ask that the denial be reversed."))

    graph, baseline = record["arms"]["graph"]["appeals"], record["arms"]["llm-only"]["appeals"]
    assert code == 0
    assert (graph["failed"], baseline["failed"], baseline["citations"]) == (0, 11, 0)
    results = {(r["arm"], r["case"]): r["appeal"] for r in record["results"] if "appeal" in r}
    assert results["llm-only", "ev-10"]["passed"] is False and results["llm-only", "ev-01"]["passed"] is True
    assert "11 failed the citation check" in capsys.readouterr().out


def test_the_no_letter_warning_counts_only_calls_made(tmp_path, monkeypatch, capsys):
    def break_evidence(raw):
        raw["evidence"]["criteria"][0]["criterion"] = "bogus"

    def refuses(claim, path, quotes, client, findings=()):
        return Extraction(None, "refusal", {"request_id": "req_draft",
                                            "http_request_body": json.dumps(ap.build_request(claim, path, quotes, findings)),
                                            "response": {"model": ex.MODEL}})

    _eval_edited(tmp_path, monkeypatch, "ev-01", break_evidence, refuses)

    assert "warning: 19 of 19 appeal drafts had no letter" in capsys.readouterr().err


def test_a_case_whose_reference_path_cannot_be_built_is_a_draft_error(tmp_path, monkeypatch):
    def break_evidence(raw):
        raw["evidence"]["criteria"][0]["criterion"] = "bogus"

    code, record = _eval_edited(tmp_path, monkeypatch, "ev-01", break_evidence)

    results = {r["case"]: r for r in record["results"]}
    assert code == 3
    assert results["ev-01"]["appeal"]["error"].startswith("SchemaError")
    assert results["ev-01"]["appeal"]["reference"] is None
    assert results["ev-02"]["appeal"]["error"] is None
    assert record["drafting"]["calls"] == record["drafting"]["responses"] == 19  # ev-01 never reached the drafter


def test_eval_without_draft_drafts_nothing(tmp_path, monkeypatch):
    def must_not_call(*args, **kwargs):
        raise AssertionError("eval drafted without --draft")

    code, record = _eval(tmp_path, monkeypatch, must_not_call, draft=False)

    assert code == 0
    assert "appeals" not in record["arms"]["graph"] and "drafting" not in record


def test_a_draft_without_a_letter_is_counted_and_never_passes(tmp_path, monkeypatch, capsys):
    def refuses(claim, path, quotes, client, findings=()):
        return Extraction(None, "refusal", {"request_id": "req_draft",
                                            "http_request_body": json.dumps(ap.build_request(claim, path, quotes, findings)),
                                            "response": {"model": ex.MODEL}})

    code, record = _eval(tmp_path, monkeypatch, refuses, arms="graph")

    graph = record["arms"]["graph"]["appeals"]
    assert code == 0
    assert (graph["drafted"], graph["without_letter"], graph["failed"], graph["citations"]) == (9, 9, 0, 0)
    assert graph["unsupported_rate"] is None
    assert "warning: 9 of 9 appeal drafts had no letter" in capsys.readouterr().err


def test_a_drafting_error_keeps_the_verdict_and_marks_the_run_incomplete(tmp_path, monkeypatch, capsys):
    def overloaded(claim, path, quotes, client, findings=()):
        raise RuntimeError("529 overloaded")

    code, record = _eval(tmp_path, monkeypatch, overloaded, arms="graph")

    arm = record["arms"]["graph"]
    assert code == 3
    assert (arm["correct"], arm["errors"]) == (20, 0)  # verdicts are scored as without drafting
    assert (arm["appeals"]["drafted"], arm["appeals"]["errors"]) == (9, 9)
    assert record["drafting"]["calls"] == 9 and record["drafting"]["responses"] == 0
    ev01 = next(r for r in record["results"] if r["case"] == "ev-01")
    assert ev01["appeal"]["reference"] == ["E2103", "L33822", "A52464", "E11.65"]  # built before the call failed
    assert "9 appeal draft errors" in capsys.readouterr().err


# triage's verdict needs no model, so it's still printed; eval checks credentials before any case.
@pytest.mark.parametrize("argv, code, message, out", [
    (["triage", str(EVAL / "ev-01.json"), "--draft"], 3,
     "appeal draft failed: CredentialError: no Anthropic credentials", "ev-01: overturnable"),
    (["eval", "--arms", "graph", "--draft", "--out", "out"], 2, "eval failed: no Anthropic credentials", ""),
])
def test_draft_without_credentials_drafts_nothing(tmp_path, monkeypatch, capsys, argv, code, message, out):
    def no_credentials():
        raise rcm_ai.CredentialError("no Anthropic credentials found; set ANTHROPIC_API_KEY")

    def must_not_call(*args, **kwargs):
        raise AssertionError("drafted without a client")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rcm_ai, "_client", no_credentials)
    monkeypatch.setattr(ap, "draft", must_not_call)

    assert rcm_ai.main([*argv, "--data", str(DATA)]) == code
    captured = capsys.readouterr()
    assert message in captured.err
    assert captured.out.startswith(out)
    assert list(tmp_path.iterdir()) == []


# Live: calls the model. Needs credentials and is skipped, visibly, without them.
_has_credentials = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                        or (Path.home() / ".config" / "anthropic").exists())


@pytest.mark.skipif(not _has_credentials, reason="needs Anthropic credentials (live model call)")
def test_live_draft_returns_a_letter_and_logs_the_wire_request():
    import anthropic

    case = load_case(EVAL / "ev-01.json")

    response = ap.draft(case.claim, EV01, ap.quotes(case.evidence), anthropic.Anthropic())

    sent = json.loads(response.record["http_request_body"])
    assert sent["model"] == ex.MODEL and sent["output_config"]["effort"] == ex.EFFORT
    assert "temperature" not in sent
    assert sent["system"] == ap.PROMPT.read_text()
    assert response.record["request_id"]
    letter = ap.letter_of(response)
    assert letter and re.search(r"\bE2103\b", letter)
    check = ap.check_citations(letter, EV01)
    assert check.passed, check.unsupported
