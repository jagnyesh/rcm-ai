from pathlib import Path

import pytest

from rcm_ai.icd10cm import CodeRef, parse_code_ref, parse_tabular
from rcm_ai.provenance import verify_fixtures

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "fixtures"
SLICE = "cdc/icd10cm-tabular-2027-E08-E13.xml"


def _slice():
    return parse_tabular(FIXTURES / SLICE)


def test_every_diag_in_the_slice_becomes_a_concept_with_title_and_parent():
    expected = next(r for r in verify_fixtures(FIXTURES) if r.path == SLICE).record_count
    tab = _slice()

    assert tab.version == "2027"
    assert len(tab.concepts) == expected
    assert tab.concepts["E11.21"].title == "Type 2 diabetes mellitus with diabetic nephropathy"
    assert tab.concepts["E11.21"].parent == "E11.2"
    assert tab.concepts["E11.2"].parent == "E11"
    assert tab.concepts["E11"].parent is None


def test_excludes1_notes_become_code_references_of_all_three_source_forms():
    rules = {(r.code, r.target): r.note for r in _slice().excludes1}

    # "(E10.-)": the category and every code under it
    assert rules[("E11", CodeRef("subtree", "E10"))] == "type 1 diabetes mellitus (E10.-)"
    # "(O24.4-)": a subcategory wildcard pointing outside the slice
    assert ("E11", CodeRef("subtree", "O24.4")) in rules
    # "(P70.2)": one exact code
    assert ("E11", CodeRef("exact", "P70.2")) in rules
    # "(E11.0-E11.8)": an inclusive range of sibling categories
    assert ("E11.A", CodeRef("range", "E11.0", "E11.8")) in rules


def test_every_excludes1_note_in_the_slice_is_parsed_none_dropped():
    assert len(_slice().excludes1) == 43


# Regression: ISSUE-001 — wildcard range ends and malformed refs parsed silently
# Found by /qa on 2026-10-29
# Report: qa-report-rcm-ai-functional-2026-10-29.md (session scratchpad)
@pytest.mark.parametrize(
    "note, expected",
    [
        ("cerebral infarction (I63.3-I63.5-)", CodeRef("range", "I63.3", "I63.5")),
        ("atherosclerosis of the extremities (I70.2--I70.7-)", CodeRef("range", "I70.2", "I70.7")),
        ("type 1 diabetes mellitus (E10.-)", CodeRef("subtree", "E10")),
        ("gestational diabetes (O24.4-)", CodeRef("subtree", "O24.4")),
    ],
)
def test_wildcard_dashes_on_either_end_of_a_reference_are_read_as_subtree_markers(note, expected):
    assert parse_code_ref(note) == expected


@pytest.mark.parametrize("note", ["eating disorders (F98.2.-F98.3)", "pain (NOS)", "stage (A-B-C)"])
def test_a_trailing_reference_that_is_not_code_shaped_raises_instead_of_parsing(note):
    with pytest.raises(ValueError):
        parse_code_ref(note)
