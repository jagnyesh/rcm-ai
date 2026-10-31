import csv
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from rcm_ai.adjudicate import adjudicate
from rcm_ai.graph import build_graph, normalize_lcd_text
from rcm_ai.schemas import CriterionEvidence, DocumentedCode, SchemaError, load_case

DATA = Path(__file__).resolve().parents[1] / "data"
FIXTURES = DATA / "fixtures"


def _record_text_at(path: Path, line: int) -> str:
    """Text of the source record that starts at a 1-based line, as a reviewer would read it."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if path.suffix == ".xml":
        return lines[line - 1]
    record = next(csv.reader(io.StringIO("".join(lines[line - 1 :]), newline="")))
    return normalize_lcd_text(" ".join(record))


def test_documented_cgm_denial_is_overturnable_with_every_hop_traceable_to_a_source_line():
    graph = build_graph(DATA)
    case = load_case(DATA / "synthetic" / "case-001.json")

    verdict = adjudicate(case.claim, case.evidence, graph)

    assert verdict.outcome == "overturnable"
    assert [h.statement for h in verdict.path] == [
        "E2103 is listed in LCD L33822",
        "LCD L33822 initial CGM criteria (1)-(5) apply to E2103",
        "LCD L33822 is related to article A52464 (link cites v49)",
        "E11.65 is an ICD-10-CM code: Type 2 diabetes mellitus with hyperglycemia",
        "Article A52464 covers E11.65",
        "Criterion cgm-1-diabetes met by covered diagnosis E11.65",
        "Criterion cgm-2-training met by documented evidence",
        "Criterion cgm-3-fda-indication met by documented evidence",
        "Criterion cgm-4-glycemic-control met by cgm-4a-insulin",
        "Criterion cgm-5-visit met by documented evidence",
    ]
    for hop in verdict.path:
        assert hop.anchor in _record_text_at(FIXTURES / hop.source.path, hop.source.line), hop
        assert hop.source.version


def test_a_covered_diagnosis_without_insulin_or_hypoglycemia_evidence_is_not_overturnable():
    graph = build_graph(DATA)
    case = load_case(DATA / "synthetic" / "case-001.json")
    evidence = replace(
        case.evidence,
        criteria=tuple(c for c in case.evidence.criteria if c.criterion != "cgm-4a-insulin"),
    )

    verdict = adjudicate(case.claim, evidence, graph)

    assert verdict.outcome == "needs_human"
    assert verdict.path == ()


# Fresh-context review of #3 (2026-10-30): inputs that were wrongly overturnable.
def _case():
    return load_case(DATA / "synthetic" / "case-001.json")


def _with_criteria(evidence, drop=(), add=()):
    kept = tuple(c for c in evidence.criteria if c.criterion not in drop)
    return replace(evidence, criteria=kept + tuple(CriterionEvidence(i, s) for i, s in add))


NOTE_SPAN = "Telehealth follow-up for type 2 diabetes mellitus with hyperglycemia"


def test_problematic_hypoglycemia_without_a_documented_level_2_or_3_event_is_not_overturnable():
    case = _case()
    evidence = _with_criteria(case.evidence, drop={"cgm-4a-insulin"}, add=[("cgm-4b-hypoglycemia", NOTE_SPAN)])

    assert adjudicate(case.claim, evidence, build_graph(DATA)).outcome == "needs_human"


def test_a_documented_level_3_event_satisfies_the_glycemic_control_criterion():
    case = _case()
    evidence = _with_criteria(case.evidence, drop={"cgm-4a-insulin"}, add=[("cgm-4b2-level3", NOTE_SPAN)])

    verdict = adjudicate(case.claim, evidence, build_graph(DATA))

    assert verdict.outcome == "overturnable"
    hop = next(h for h in verdict.path if "cgm-4-glycemic-control" in h.statement)
    assert hop.statement == "Criterion cgm-4-glycemic-control met by cgm-4b2-level3"
    assert hop.anchor.startswith("A history of one level 3 hypoglycemic event")


def test_a_supply_allowance_claim_is_not_overturnable_until_continued_coverage_is_modeled():
    case = _case()
    claim = replace(case.claim, hcpcs="A4239")

    assert adjudicate(claim, case.evidence, build_graph(DATA)).outcome == "needs_human"


def test_a_continued_coverage_claim_is_not_overturnable_until_continued_coverage_is_modeled():
    case = _case()
    claim = replace(case.claim, coverage="continued")

    assert adjudicate(claim, case.evidence, build_graph(DATA)).outcome == "needs_human"


def test_a_denial_that_is_not_for_medical_necessity_is_not_overturnable():
    case = _case()
    claim = replace(case.claim, carc="29")  # timely filing

    assert adjudicate(claim, case.evidence, build_graph(DATA)).outcome == "needs_human"


def test_evidence_naming_an_unknown_criterion_is_rejected():
    case = _case()
    evidence = _with_criteria(case.evidence, add=[("bogus", NOTE_SPAN)])

    with pytest.raises(SchemaError, match="bogus"):
        adjudicate(case.claim, evidence, build_graph(DATA))


def test_the_applicability_hop_cites_the_initial_criteria_sentence():
    case = _case()
    verdict = adjudicate(case.claim, case.evidence, build_graph(DATA))

    assert verdict.path[1].statement == "LCD L33822 initial CGM criteria (1)-(5) apply to E2103"
    assert verdict.path[1].anchor.startswith("To be eligible for coverage of a CGM and related supplies")


def test_the_article_link_hop_states_which_article_version_the_lcd_cites():
    verdict = adjudicate(_case().claim, _case().evidence, build_graph(DATA))

    assert verdict.path[2].statement == "LCD L33822 is related to article A52464 (link cites v49)"


def test_every_curated_quote_is_verbatim_in_the_lcd_text():
    curated = json.loads((DATA / "curated" / "L33822-cgm-criteria.json").read_text())
    row = next(csv.DictReader(io.StringIO((FIXTURES / curated["source"]).read_text(encoding="utf-8"), newline="")))
    text = normalize_lcd_text(row[curated["column"]])

    def quotes(c):
        yield c["quote"]
        for sub in c.get("any_of", []):
            yield from quotes(sub)

    found = [curated["applies_to"]["quote"]] + [q for c in curated["criteria"] for q in quotes(c)]
    assert len(found) >= 9
    assert [q for q in found if q not in text] == []


def test_a_covered_diagnosis_on_the_claim_but_not_documented_in_the_note_is_not_overturnable():
    case = _case()
    claim = replace(case.claim, icd10=("Z79.4", "E11.65"))
    evidence = replace(case.evidence, diagnoses=(DocumentedCode("Z79.4", NOTE_SPAN),))

    assert adjudicate(claim, evidence, build_graph(DATA)).outcome == "needs_human"


# Issue #4: specificity, Excludes1 and seventh-character codes, using the locked eval cases.
EVAL = DATA / "eval" / "cases"


def _eval_case(case_id):
    c = load_case(EVAL / f"{case_id}.json")
    return adjudicate(c.claim, c.evidence, build_graph(DATA))


def test_a_seventh_character_code_on_the_covered_list_is_overturnable():
    verdict = _eval_case("ev-07")

    assert verdict.outcome == "overturnable"
    assert "E11.3213 is an ICD-10-CM code" in verdict.path[3].statement


def test_a_billed_unspecified_code_with_a_more_specific_documented_code_is_flagged():
    verdict = _eval_case("ev-08")  # billed E11.9, note documents E11.65

    assert verdict.outcome == "overturnable"
    assert "Article A52464 covers E11.9" in [h.statement for h in verdict.path]
    (finding,) = verdict.findings
    assert finding.type == "specificity"
    assert finding.codes == ("E11.9", "E11.65")


def test_a_documented_code_from_another_category_does_not_support_the_billed_code():
    case = _case()
    claim = replace(case.claim, icd10=("E11.9",))
    evidence = replace(case.evidence, diagnoses=(DocumentedCode("E10.65", NOTE_SPAN),))

    assert adjudicate(claim, evidence, build_graph(DATA)).outcome == "needs_human"


def test_billed_codes_in_an_excludes1_conflict_are_not_overturnable_and_the_rule_is_sourced():
    verdict = _eval_case("ev-19")  # E10.9 billed with E11.65

    assert verdict.outcome == "needs_human"
    (finding,) = verdict.findings
    assert finding.type == "excludes1"
    assert set(finding.codes) == {"E10.9", "E11.65"}
    # Each category excludes the other, so either note is a correct citation; the first billed code's wins.
    assert finding.anchor in {"type 1 diabetes mellitus (E10.-)", "type 2 diabetes mellitus (E11.-)"}
    assert finding.anchor in _record_text_at(FIXTURES / finding.source.path, finding.source.line)


def test_excludes1_is_inherited_from_an_ancestor_category():
    verdict = _eval_case("ev-20")  # E11.65 with E13.9; the rule lives on E11, not E11.65

    assert verdict.outcome == "needs_human"
    assert [f.type for f in verdict.findings] == ["excludes1"]
    assert "E13" in verdict.findings[0].anchor


def test_a_billed_specific_code_supported_only_by_a_less_specific_documented_code_is_not_overturnable():
    case = _case()  # billing E11.65 when the note supports only E11.9 would be upcoding
    evidence = replace(case.evidence, diagnoses=(DocumentedCode("E11.9", NOTE_SPAN),))

    assert adjudicate(case.claim, evidence, build_graph(DATA)).outcome == "needs_human"


def test_every_hop_of_a_seventh_character_path_is_found_at_its_source_line():
    verdict = _eval_case("ev-07")

    for hop in verdict.path:
        assert hop.anchor in _record_text_at(FIXTURES / hop.source.path, hop.source.line), hop


# Fresh-context review of #4 (2026-10-30): specificity inputs that were wrongly overturnable.
@pytest.mark.parametrize(
    "billed, documented",
    [
        ("E11.40", "E11.9"),    # neuropathy, unspecified: billed is more specific than documented
        ("E11.8", "E11.9"),     # with unspecified complications
        ("E11.319", "E11.65"),  # unspecified retinopathy: documented code is a different condition
        ("E11.3519", "E11.9"),  # 7th character "unspecified eye"
        ("E11.9", "E11.ZZZZ"),  # documented code does not exist
        ("E11.9", "E11"),       # documented code is a category, not a billable code
    ],
)
def test_specificity_only_lets_a_documented_covered_code_support_its_without_complications_code(billed, documented):
    case = _case()
    claim = replace(case.claim, icd10=(billed,))
    evidence = replace(case.evidence, diagnoses=(DocumentedCode(documented, NOTE_SPAN),))

    assert adjudicate(claim, evidence, build_graph(DATA)).outcome == "needs_human"


@pytest.mark.parametrize(
    "billed, rule",
    [
        (("E11.65", "E11.A"), "(E11.0-E11.8)"),  # range target on E11.A
        (("E11.9", "E11.A"), "(E11.9)"),         # exact target on E11.A, and E11.9 excludes E11.A
    ],
)
def test_excludes1_range_and_exact_targets_are_conflicts(billed, rule):
    case = _case()
    claim = replace(case.claim, icd10=billed)

    verdict = adjudicate(claim, case.evidence, build_graph(DATA))

    assert verdict.outcome == "needs_human"
    assert verdict.findings and {f.type for f in verdict.findings} == {"excludes1"}
    assert any(f.anchor.endswith(rule) or "(E11.A)" in f.anchor for f in verdict.findings)


def test_a_seventh_character_hop_names_the_code_that_defines_its_character():
    hop = _eval_case("ev-07").path[3]

    assert "7th character from E11.32" in hop.statement


def test_documented_codes_in_an_excludes1_conflict_are_not_overturnable():
    case = _case()  # the note documents type 1 and type 2 together; the claim bills only E11.9
    claim = replace(case.claim, icd10=("E11.9",))
    evidence = replace(case.evidence, diagnoses=(
        DocumentedCode("E11.65", NOTE_SPAN), DocumentedCode("E10.9", NOTE_SPAN),
    ))

    verdict = adjudicate(claim, evidence, build_graph(DATA))

    assert verdict.outcome == "needs_human"
    # E10.9 conflicts with both E11 codes; each conflicting pair is reported.
    assert {frozenset(f.codes) for f in verdict.findings} >= {frozenset({"E11.65", "E10.9"})}
    assert {f.type for f in verdict.findings} == {"excludes1"}


# Issue #5: every non-overturnable verdict says why; uphold rests on what the note says.
def _reasons(verdict):
    return [r.kind for r in verdict.reasons]


@pytest.mark.parametrize("case_id, unmet", [
    ("ev-10", "cgm-4-glycemic-control"),  # metformin alone, no hypoglycemia
    ("ev-11", "cgm-4-glycemic-control"),  # one level 2 reading, no assistance
    ("ev-14", "cgm-5-visit"),             # last visit 11 months ago
])
def test_a_criterion_the_note_shows_is_not_met_upholds_the_denial(case_id, unmet):
    verdict = _eval_case(case_id)

    assert verdict.outcome == "uphold"
    (reason,) = verdict.reasons
    assert reason.kind == "criterion_not_met"
    assert unmet in reason.statement
    assert reason.anchor in _record_text_at(FIXTURES / reason.source.path, reason.source.line)
    note = load_case(EVAL / f"{case_id}.json").note
    assert reason.span and reason.span in note


@pytest.mark.parametrize("case_id", ["ev-12", "ev-13"])  # R73.03 prediabetes; E16.2 hypoglycemia
def test_a_documented_diagnosis_off_the_covered_list_upholds_the_denial(case_id):
    verdict = _eval_case(case_id)

    assert verdict.outcome == "uphold"
    assert _reasons(verdict) == ["diagnosis_not_covered"]


def test_an_undocumented_criterion_is_a_hand_off_not_an_uphold():
    verdict = _eval_case("ev-18")  # insulin undecided, no hypoglycemia mentioned

    assert verdict.outcome == "needs_human"
    assert _reasons(verdict) == ["criterion_not_documented"]
    assert "cgm-4-glycemic-control" in verdict.reasons[0].statement


def test_a_criterion_both_met_and_not_met_is_a_hand_off():
    case = _case()
    evidence = replace(case.evidence, criteria_not_met=(CriterionEvidence("cgm-4a-insulin", NOTE_SPAN),))

    verdict = adjudicate(case.claim, evidence, build_graph(DATA))

    assert verdict.outcome == "needs_human"
    assert _reasons(verdict) == ["criterion_conflicting"]


def test_one_met_branch_satisfies_an_any_of_criterion_even_if_another_branch_is_not_met():
    case = _case()  # insulin-treated, and the note says there is no problematic hypoglycemia
    evidence = replace(case.evidence, criteria_not_met=(CriterionEvidence("cgm-4b-hypoglycemia", NOTE_SPAN),))

    assert adjudicate(case.claim, evidence, build_graph(DATA)).outcome == "overturnable"


def test_a_documented_covered_code_beside_an_uncovered_billed_code_is_a_hand_off_not_an_uphold():
    case = _case()  # billed I10, but the note documents covered E11.65: recode, don't write off
    claim = replace(case.claim, icd10=("I10",))

    verdict = adjudicate(claim, case.evidence, build_graph(DATA))

    assert verdict.outcome == "needs_human"
    assert _reasons(verdict) == ["diagnosis_not_documented"]


@pytest.mark.parametrize("change, kind", [
    ({"carc": "29"}, "denial_out_of_scope"),
    ({"coverage": "continued"}, "coverage_out_of_scope"),
    ({"hcpcs": "E0601"}, "no_lcd_for_item"),
    ({"hcpcs": "A4239"}, "item_not_in_criteria"),
])
def test_out_of_scope_claims_are_handed_off_with_a_distinct_reason(change, kind):
    case = _case()

    verdict = adjudicate(replace(case.claim, **change), case.evidence, build_graph(DATA))

    assert verdict.outcome == "needs_human"
    assert _reasons(verdict) == [kind]


def test_a_billed_diagnosis_without_documentation_is_handed_off():
    case = _case()

    verdict = adjudicate(case.claim, replace(case.evidence, diagnoses=()), build_graph(DATA))

    assert verdict.outcome == "needs_human"
    assert _reasons(verdict) == ["diagnosis_not_documented"]


def test_a_covered_o24_diagnosis_is_overturnable_with_its_concept_hop_sourced_to_the_o24_slice():
    verdict = _eval_case("ev-06")  # O24.111, pre-existing type 2 diabetes in pregnancy

    assert verdict.outcome == "overturnable"
    concept = verdict.path[3]
    assert concept.source.path == "cdc/icd10cm-tabular-2027-O24.xml"
    for hop in verdict.path:
        assert hop.anchor in _record_text_at(FIXTURES / hop.source.path, hop.source.line), hop


# #4 re-review: specificity recodes only within the billed code's own category.
def test_a_documented_covered_code_from_another_category_does_not_support_a_billed_without_complications_code():
    case = _case()  # O24.111 is real and covered, but it is not a more specific E11.9
    claim = replace(case.claim, icd10=("E11.9",))
    evidence = replace(case.evidence, diagnoses=(DocumentedCode("O24.111", NOTE_SPAN),))

    assert _outcome_and_reasons(claim, evidence) == ("needs_human", ["diagnosis_not_documented"])


def test_a_chapter_level_excludes1_note_makes_a_coding_conflict():
    case = _case()  # chapter 15 excludes supervision of normal pregnancy (Z34.-)
    claim = replace(case.claim, icd10=("O24.111",))
    evidence = replace(case.evidence, diagnoses=(
        DocumentedCode("O24.111", NOTE_SPAN), DocumentedCode("Z34.01", NOTE_SPAN)))

    verdict = adjudicate(claim, evidence, build_graph(DATA))

    assert _reasons(verdict) == ["coding_conflict"]
    (finding,) = verdict.findings
    assert finding.codes == ("O24.111", "Z34.01")
    assert finding.anchor == "supervision of normal pregnancy (Z34.-)"


def test_an_excludes1_conflict_gives_a_coding_conflict_reason():
    assert _reasons(_eval_case("ev-19")) == ["coding_conflict"]


def test_an_overturnable_verdict_has_no_reasons():
    assert _case() and adjudicate(_case().claim, _case().evidence, build_graph(DATA)).reasons == ()


# Fresh-context review of #5 (2026-10-30): wrong verdicts and untested guards.
def _with(evidence, met=(), not_met=(), drop=()):
    kept = tuple(c for c in evidence.criteria if c.criterion not in drop)
    return replace(
        evidence,
        criteria=kept + tuple(CriterionEvidence(i, NOTE_SPAN) for i in met),
        criteria_not_met=tuple(CriterionEvidence(i, NOTE_SPAN) for i in not_met),
    )


def _outcome_and_reasons(claim, evidence):
    v = adjudicate(claim, evidence, build_graph(DATA))
    return v.outcome, _reasons(v)


@pytest.mark.parametrize("met, not_met", [
    (["cgm-4b-hypoglycemia"], ["cgm-4b1-level2", "cgm-4b2-level3"]),           # parent met, every branch not
    (["cgm-4-glycemic-control"], ["cgm-4a-insulin", "cgm-4b-hypoglycemia"]),   # top-level met, every branch not
    (["cgm-4b1-level2"], ["cgm-4-glycemic-control"]),                           # branch met, parent not
])
def test_met_and_not_met_at_different_levels_of_an_any_of_criterion_is_a_conflict(met, not_met):
    case = _case()
    evidence = _with(case.evidence, met=met, not_met=not_met, drop={"cgm-4a-insulin"})

    assert _outcome_and_reasons(case.claim, evidence) == ("needs_human", ["criterion_conflicting"])


def test_a_note_saying_the_diagnosis_criterion_fails_conflicts_with_the_covered_diagnosis():
    case = _case()

    assert _outcome_and_reasons(case.claim, _with(case.evidence, not_met=["cgm-1-diabetes"])) == (
        "needs_human", ["criterion_conflicting"])


def test_one_any_of_branch_not_met_with_the_other_unmentioned_is_not_documented():
    case = _case()
    evidence = _with(case.evidence, not_met=["cgm-4a-insulin"], drop={"cgm-4a-insulin"})

    assert _outcome_and_reasons(case.claim, evidence) == ("needs_human", ["criterion_not_documented"])


def test_a_conflict_outranks_a_documented_failure():
    case = _case()  # 4a both met and not met; visit not met
    evidence = _with(case.evidence, not_met=["cgm-4a-insulin", "cgm-5-visit"], drop={"cgm-5-visit"})

    assert _outcome_and_reasons(case.claim, evidence) == ("needs_human", ["criterion_conflicting"])


def test_a_documented_failure_outranks_an_undocumented_criterion():
    case = _case()  # training not mentioned; visit not met
    evidence = _with(case.evidence, not_met=["cgm-5-visit"], drop={"cgm-2-training", "cgm-5-visit"})

    assert _outcome_and_reasons(case.claim, evidence) == ("uphold", ["criterion_not_met"])


def test_an_uncovered_billed_code_beside_a_documented_covered_code_is_a_recode_hand_off():
    case = _case()  # billed I10, documented I10 and covered E11.65
    claim = replace(case.claim, icd10=("I10",))
    evidence = replace(case.evidence, diagnoses=case.evidence.diagnoses + (DocumentedCode("I10", NOTE_SPAN),))

    assert _outcome_and_reasons(claim, evidence) == ("needs_human", ["covered_diagnosis_not_billed"])


# E11.37X's covered children (E11.37X1-9) skip it in the parent chain.
@pytest.mark.parametrize("header", ["E11.64", "E11.3", "E11.37X", "O24.01"])
def test_a_non_billable_header_with_covered_children_is_a_recode_hand_off_not_an_uphold(header):
    case = _case()
    claim = replace(case.claim, icd10=(header,))
    evidence = replace(case.evidence, diagnoses=(DocumentedCode(header, NOTE_SPAN),))

    assert _outcome_and_reasons(claim, evidence) == ("needs_human", ["diagnosis_not_billable"])


def test_an_unknown_code_inside_a_loaded_category_is_a_hand_off_not_an_uphold():
    case = _case()  # E11.99 is not an ICD-10-CM code, though E11 is loaded
    claim = replace(case.claim, icd10=("E11.99",))
    evidence = replace(case.evidence, diagnoses=(DocumentedCode("E11.99", NOTE_SPAN),))

    assert _outcome_and_reasons(claim, evidence) == ("needs_human", ["diagnosis_not_in_terminology"])


@pytest.mark.parametrize("documented_header", ["E11.64", "E11.37X", "O24.01"])
def test_a_documented_header_with_covered_codes_under_it_blocks_an_uphold(documented_header):
    case = _case()  # billed I10 (not covered); the note also documents a recodable diabetes header
    claim = replace(case.claim, icd10=("I10",))
    evidence = replace(case.evidence, diagnoses=(
        DocumentedCode("I10", NOTE_SPAN), DocumentedCode(documented_header, NOTE_SPAN)))

    assert _outcome_and_reasons(claim, evidence) == ("needs_human", ["covered_diagnosis_not_billed"])


# #9 loaded R73 (elevated blood glucose), whose Excludes1 names diabetes mellitus (E08-E13).
def test_prediabetes_billed_beside_documented_diabetes_is_a_coding_conflict():
    case = _case()  # billed R73.03; the note documents E11.65
    claim = replace(case.claim, icd10=("R73.03",))

    verdict = adjudicate(claim, case.evidence, build_graph(DATA))

    assert _reasons(verdict) == ["coding_conflict"]
    (finding,) = verdict.findings
    assert set(finding.codes) == {"R73.03", "E11.65"}
    assert finding.anchor == "diabetes mellitus (E08-E13)"
