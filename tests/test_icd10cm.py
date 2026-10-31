from pathlib import Path

import pytest

from rcm_ai.icd10cm import CodeRef, parse_code_refs, parse_tabular
from rcm_ai.provenance import verify_fixtures

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "fixtures"
SLICE = "cdc/icd10cm-tabular-2027-E08-E13.xml"


def _slice():
    return parse_tabular(FIXTURES / SLICE)


def test_every_diag_in_the_slice_becomes_a_concept_with_title_and_parent():
    expected = next(r for r in verify_fixtures(FIXTURES) if r.path == SLICE).record_count
    tab = _slice()

    assert tab.version == "2027"
    assert len([c for c in tab.concepts.values() if c.kind == "diag"]) == expected
    assert tab.concepts["E11.21"].title == "Type 2 diabetes mellitus with diabetic nephropathy"
    assert tab.concepts["E11.21"].parent == "E11.2"
    assert tab.concepts["E11.2"].parent == "E11"
    assert tab.concepts["E11"].parent == "E08-E13"
    assert tab.concepts["E08-E13"].kind == "section"
    assert tab.concepts["E08-E13"].parent is None  # the slice has no chapter wrapper


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
@pytest.mark.parametrize(
    "note, expected",
    [
        ("cerebral infarction (I63.3-I63.5-)", CodeRef("range", "I63.3", "I63.5")),
        ("atherosclerosis of the extremities (I70.2--I70.7-)", CodeRef("range", "I70.2", "I70.7")),
        ("type 1 diabetes mellitus (E10.-)", CodeRef("subtree", "E10")),
        ("gestational diabetes (O24.4-)", CodeRef("subtree", "O24.4")),
    ],
)
def test_range_end_wildcards_are_dropped_and_subtree_wildcards_kept(note, expected):
    assert parse_code_refs(note) == (expected,)


@pytest.mark.parametrize("note", ["eating disorders (F98.2.-F98.3)", "pain (NOS)", "diabetes (E10.-E11.-)"])
def test_a_trailing_reference_that_is_not_code_shaped_raises_instead_of_parsing(note):
    with pytest.raises(ValueError):
        parse_code_refs(note)


def test_seventh_character_definitions_expand_into_concepts_under_their_leaf():
    tab = _slice()

    c = tab.concepts["E11.3213"]
    assert c.kind == "seventh"
    assert c.parent == "E11.321"
    assert c.title == "Type 2 diabetes mellitus with mild nonproliferative diabetic retinopathy with macular edema, bilateral"


def test_every_covered_e08_to_e13_code_in_the_policy_article_is_a_concept():
    import csv

    path = FIXTURES / "cms" / "article_x_icd10_covered-A52464-v65.csv"
    covered = {r["icd10_code_id"] for r in csv.DictReader(path.open(encoding="utf-8", newline=""))}
    in_slice = {c for c in covered if c[:3] in {"E08", "E09", "E10", "E11", "E13"}}
    concepts = _slice().concepts

    assert len(in_slice) == 431
    assert sorted(in_slice - concepts.keys()) == []


# Issue #6: O24, section- and chapter-level notes, multi-code notes.
O24 = "cdc/icd10cm-tabular-2027-O24.xml"


def test_every_covered_o24_code_in_the_policy_article_is_a_concept_under_its_section_and_chapter():
    import csv

    path = FIXTURES / "cms" / "article_x_icd10_covered-A52464-v65.csv"
    covered = {r["icd10_code_id"] for r in csv.DictReader(path.open(encoding="utf-8", newline=""))}
    tab = parse_tabular(FIXTURES / O24)

    o24 = {c for c in covered if c.startswith("O24")}
    assert len(o24) == 30
    assert sorted(o24 - tab.concepts.keys()) == []
    assert tab.concepts["O24"].parent == "O20-O29"
    assert tab.concepts["O20-O29"].parent == "chapter-15"
    assert tab.concepts["chapter-15"].kind == "chapter"
    assert tab.concepts["chapter-15"].title == "Pregnancy, childbirth and the puerperium (O00-O9A)"


def test_a_chapter_level_excludes1_note_is_kept_with_the_chapter_as_its_owner():
    rules = [(r.code, r.target, r.note) for r in parse_tabular(FIXTURES / O24).excludes1]

    assert rules == [("chapter-15", CodeRef("subtree", "Z34"), "supervision of normal pregnancy (Z34.-)")]


_SECTION_NOTE = """<?xml version="1.0" encoding="utf-8"?>
<ICD10CM.tabular>
  <chapter>
    <name>1</name>
    <desc>Certain infectious and parasitic diseases (A00-B99)</desc>
    <section id="A15-A19">
      <desc>Tuberculosis (A15-A19)</desc>
      <excludes1>
        <note>{note}</note>
      </excludes1>
      <diag>
        <name>A15</name>
        <desc>Respiratory tuberculosis</desc>
      </diag>
    </section>
  </chapter>
</ICD10CM.tabular>
"""


def test_a_section_level_excludes1_note_is_kept_with_the_section_as_its_owner(tmp_path):
    xml = tmp_path / "t.xml"
    xml.write_text(_SECTION_NOTE.format(note="congenital tuberculosis (P37.0)"))

    tab = parse_tabular(xml)

    assert [(r.code, r.target) for r in tab.excludes1] == [("A15-A19", CodeRef("exact", "P37.0"))]
    assert tab.concepts["A15"].parent == "A15-A19"
    assert tab.concepts["A15-A19"].parent == "chapter-1"


def test_an_unparseable_section_level_note_raises_instead_of_being_dropped(tmp_path):
    xml = tmp_path / "t.xml"
    xml.write_text(_SECTION_NOTE.format(note="tuberculosis (NOS)"))

    with pytest.raises(ValueError, match="NOS"):
        parse_tabular(xml)


@pytest.mark.parametrize(
    "note, expected",
    [
        ("influenza with involvement of gastrointestinal tract (J09.X3, J10.2, J11.2)",
         (CodeRef("exact", "J09.X3"), CodeRef("exact", "J10.2"), CodeRef("exact", "J11.2"))),
        ("malignant neoplasm of auricular canal (external) (C43.2-,C44.2-)",
         (CodeRef("subtree", "C43.2"), CodeRef("subtree", "C44.2"))),
        ("malignant neoplasm of skin of genital organs (C51-C52, C60.-, C63.2)",
         (CodeRef("range", "C51", "C52"), CodeRef("subtree", "C60"), CodeRef("exact", "C63.2"))),
    ],
)
def test_a_note_naming_several_codes_yields_one_reference_per_code(note, expected):
    assert parse_code_refs(note) == expected
