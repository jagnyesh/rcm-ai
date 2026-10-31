"""Issue #6: ingest counts every source record, rejects bad rows out loud, refuses duplicate keys,
and loads a second LCD from data files alone."""

import csv
import hashlib
import io
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from rcm_ai.adjudicate import adjudicate
from rcm_ai.graph import IngestError, build_graph
from rcm_ai.schemas import Claim, DocumentedCode, Evidence

DATA = Path(__file__).resolve().parents[1] / "data"
COVERED = "cms/article_x_icd10_covered-A52464-v65.csv"


def _copy_data(tmp_path):
    data = tmp_path / "data"
    for d in ("fixtures", "curated"):
        shutil.copytree(DATA / d, data / d)
    return data


def _count(path: Path, kind: str) -> int:
    if kind == "diag":
        return sum(1 for _ in ET.parse(path).getroot().iter("diag"))
    with path.open(encoding="utf-8", newline="") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def _pin(data: Path, path: str, table: str | None = None):
    """Add or refresh a manifest entry so a test-made fixture passes provenance checks."""
    fixtures = data / "fixtures"
    manifest = json.loads((fixtures / "MANIFEST.json").read_text())
    entry = next((e for e in manifest["fixtures"] if e["path"] == path), None)
    if entry is None:
        entry = {
            "path": path, "table": table, "source_url": "synthetic (test)", "version": "test v1",
            "retrieved": "2026-10-30", "license": "synthetic", "record_kind": "csv_row",
        }
        manifest["fixtures"].append(entry)
    entry["record_count"] = _count(fixtures / path, entry["record_kind"])
    entry["sha256"] = hashlib.sha256((fixtures / path).read_bytes()).hexdigest()
    (fixtures / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))


def _append_row(data: Path, path: str, **changes):
    """Append a copy of the file's first row, with changes, as a new record."""
    target = data / "fixtures" / path
    text = target.read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    row = {**next(reader), **changes}
    out = io.StringIO(newline="")
    csv.DictWriter(out, reader.fieldnames, lineterminator="\n").writerow(row)
    target.write_text(text + out.getvalue(), encoding="utf-8")
    _pin(data, path)


def _source(graph, path):
    return next(s for s in graph.ingest if s.path == path)


def test_ingest_reports_parsed_rejected_and_loaded_counts_for_every_source():
    graph = build_graph(DATA)

    counts = {s.path: (s.table, s.parsed, len(s.rejected), s.loaded) for s in graph.ingest}
    assert counts == {
        "cdc/icd10cm-tabular-2027-E08-E13.xml": ("icd10cm_tabular", 322, 0, 322),
        "cdc/icd10cm-tabular-2027-O24.xml": ("icd10cm_tabular", 57, 0, 57),
        COVERED: ("article_x_icd10_covered", 461, 0, 461),
        "cms/lcd_x_hcpc_code-L33822-v70.csv": ("lcd_x_hcpc_code", 31, 0, 31),
        "cms/lcd-L33822-v70.csv": ("lcd", 1, 0, 1),
        "cms/lcd_related_documents-L33822-v70.csv": ("lcd_related_documents", 3, 0, 3),
    }


def test_every_loaded_record_is_in_the_graph():
    for s in build_graph(DATA).ingest:
        assert s.in_graph == s.loaded == s.parsed - len(s.rejected), s


def test_a_malformed_row_is_rejected_and_counted_with_its_line_and_the_rest_still_load(tmp_path):
    data = _copy_data(tmp_path)
    _append_row(data, COVERED, icd10_code_id="E11.9!", sort_order="9999")

    graph = build_graph(data)

    s = _source(graph, COVERED)
    assert (s.parsed, s.loaded, s.in_graph) == (462, 461, 461)
    (rejected,) = s.rejected
    assert rejected.line == 463  # header is line 1, then 461 rows
    assert "E11.9!" in rejected.reason
    assert graph.on_covered_list("E11.9")


def test_a_duplicate_key_fails_ingest_naming_the_file_and_key(tmp_path):
    data = _copy_data(tmp_path)
    _append_row(data, COVERED, sort_order="9999")  # same article, version and code as row 1

    with pytest.raises(IngestError, match=r"article_x_icd10_covered-A52464-v65\.csv.*52464"):
        build_graph(data)


def test_a_code_defined_in_two_tabular_fixtures_fails_ingest(tmp_path):
    data = _copy_data(tmp_path)
    src = data / "fixtures" / "cdc" / "icd10cm-tabular-2027-O24.xml"
    shutil.copy(src, src.with_name("icd10cm-tabular-2027-O24-copy.xml"))
    manifest = json.loads((data / "fixtures" / "MANIFEST.json").read_text())
    entry = next(e for e in manifest["fixtures"] if e["path"] == "cdc/icd10cm-tabular-2027-O24.xml")
    manifest["fixtures"].append({**entry, "path": "cdc/icd10cm-tabular-2027-O24-copy.xml"})
    (data / "fixtures" / "MANIFEST.json").write_text(json.dumps(manifest))

    with pytest.raises(IngestError, match="O24"):
        build_graph(data)


def test_a_curated_quote_missing_from_its_lcd_text_fails_ingest(tmp_path):
    data = _copy_data(tmp_path)
    path = data / "curated" / "L33822-cgm-criteria.json"
    curated = json.loads(path.read_text())
    curated["criteria"][1]["quote"] = "The beneficiary has completed a course of yoga"
    path.write_text(json.dumps(curated))

    with pytest.raises(IngestError, match="yoga"):
        build_graph(data)


def _write_csv(data: Path, path: str, like: str, rows: list[dict]):
    """A synthetic fixture with the same columns as a real one."""
    with (data / "fixtures" / like).open(encoding="utf-8", newline="") as fh:
        fields = csv.DictReader(fh).fieldnames
    out = io.StringIO(newline="")
    w = csv.DictWriter(out, fields, restval="", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    (data / "fixtures" / path).write_text(out.getvalue(), encoding="utf-8")
    table = like.split("/")[1].rsplit("-", 2)[0] if "lcd-" not in like else "lcd"
    _pin(data, path, table)


def test_a_second_lcd_loads_from_data_files_alone(tmp_path):
    # A synthetic LCD L99999 for item K9999, related to a synthetic article A99999 that covers E11.9.
    data = _copy_data(tmp_path)
    _write_csv(data, "cms/lcd-L99999-v1.csv", "cms/lcd-L33822-v70.csv", [{
        "lcd_id": "99999", "lcd_version": "1", "title": "Synthetic Test Policy",
        "indication": "<p>Synthetic test policy.</p><p>(1) The beneficiary has a documented test condition.</p>",
    }])
    _write_csv(data, "cms/lcd_x_hcpc_code-L99999-v1.csv", "cms/lcd_x_hcpc_code-L33822-v70.csv", [
        {"lcd_id": "99999", "lcd_version": "1", "hcpc_code_id": "K9999", "hcpc_code_group": "1"},
    ])
    _write_csv(data, "cms/lcd_related_documents-L99999-v1.csv", "cms/lcd_related_documents-L33822-v70.csv", [
        {"lcd_id": "99999", "lcd_version": "1", "related_num": "1", "r_article_id": "99999", "r_article_version": "1"},
    ])
    _write_csv(data, "cms/article_x_icd10_covered-A99999-v1.csv", COVERED, [
        {"article_id": "99999", "article_version": "1", "icd10_code_id": "E11.9", "icd10_covered_group": "1"},
    ])
    (data / "curated" / "L99999-test-criteria.json").write_text(json.dumps({
        "lcd": "L99999", "source": "cms/lcd-L99999-v1.csv", "column": "indication",
        "applies_to": {"hcpcs": ["K9999"], "label": "test criteria (1)", "quote": "Synthetic test policy."},
        "criteria": [{"id": "test-1-condition", "quote": "(1) The beneficiary has a documented test condition.",
                      "satisfied_by": "covered_diagnosis"}],
    }))

    graph = build_graph(data)
    claim = Claim("K9999", ("E11.9",), "50", "initial")
    verdict = adjudicate(claim, Evidence((DocumentedCode("E11.9", "type 2 diabetes"),), ()), graph)

    assert graph.lcds_listing("K9999") == ["L99999"]
    assert verdict.outcome == "overturnable"
    assert [h.statement for h in verdict.path[:3]] == [
        "K9999 is listed in LCD L99999",
        "LCD L99999 test criteria (1) apply to K9999",
        "LCD L99999 is related to article A99999 (link cites v1)",
    ]
    # The real LCD is untouched.
    assert graph.lcds_listing("E2103") == ["L33822"]
    assert {c.id for c in graph.criteria("L99999")} == {"test-1-condition"}


def test_chapter_level_excludes1_applies_to_codes_in_the_chapter_with_its_source_line():
    graph = build_graph(DATA)

    rules = [r for r in graph.excludes1_rules("O24.111") if r.owner == "chapter-15"]

    assert [r.note for r in rules] == ["supervision of normal pregnancy (Z34.-)"]
    assert rules[0].source.path == "cdc/icd10cm-tabular-2027-O24.xml"
    line = (DATA / "fixtures" / rules[0].source.path).read_text().splitlines()[rules[0].source.line - 1]
    assert "supervision of normal pregnancy (Z34.-)" in line


def test_a_covered_code_whose_tabular_slice_is_not_loaded_is_handed_off(tmp_path):
    from rcm_ai.schemas import load_case

    data = _copy_data(tmp_path)
    (data / "fixtures" / "cdc" / "icd10cm-tabular-2027-O24.xml").unlink()
    manifest = json.loads((data / "fixtures" / "MANIFEST.json").read_text())
    manifest["fixtures"] = [e for e in manifest["fixtures"] if not e["path"].endswith("O24.xml")]
    (data / "fixtures" / "MANIFEST.json").write_text(json.dumps(manifest))
    case = load_case(DATA / "eval" / "cases" / "ev-06.json")  # bills O24.111

    verdict = adjudicate(case.claim, case.evidence, build_graph(data))

    assert verdict.outcome == "needs_human"
    assert [r.kind for r in verdict.reasons] == ["diagnosis_not_in_terminology"]


# Fresh-context review of #6 (2026-10-30).
def test_the_same_code_in_two_hcpcs_groups_of_one_lcd_loads(tmp_path):
    data = _copy_data(tmp_path)  # real MCD data lists codes in more than one group (L33794 J7799)
    _append_row(data, "cms/lcd_x_hcpc_code-L33822-v70.csv", hcpc_code_group="9")

    s = _source(build_graph(data), "cms/lcd_x_hcpc_code-L33822-v70.csv")

    assert (s.parsed, s.loaded) == (32, 32)


def test_an_article_with_more_than_one_covered_group_fails_ingest(tmp_path):
    # Groups can mean "a code from group 1 plus one from group 2"; flattening them into one
    # covered list could overturn a claim that has only half the pair.
    data = _copy_data(tmp_path)
    _append_row(data, COVERED, icd10_covered_group="2")

    with pytest.raises(IngestError, match="group"):
        build_graph(data)


def test_a_criterion_id_reused_by_a_second_lcd_fails_ingest(tmp_path):
    data = _copy_data(tmp_path)
    _write_csv(data, "cms/lcd-L99999-v1.csv", "cms/lcd-L33822-v70.csv", [{
        "lcd_id": "99999", "lcd_version": "1", "indication": "<p>Synthetic test policy. (1) A test condition.</p>",
    }])
    (data / "curated" / "L99999-test-criteria.json").write_text(json.dumps({
        "lcd": "L99999", "source": "cms/lcd-L99999-v1.csv", "column": "indication",
        "applies_to": {"hcpcs": ["K9999"], "label": "test criteria", "quote": "Synthetic test policy."},
        "criteria": [{"id": "cgm-1-diabetes", "quote": "(1) A test condition."}],
    }))

    with pytest.raises(IngestError, match="cgm-1-diabetes"):
        build_graph(data)


def test_two_versions_of_one_lcd_fail_ingest(tmp_path):
    # The graph names an LCD without its version, so a second version would merge into the first.
    data = _copy_data(tmp_path)
    _write_csv(data, "cms/lcd_x_hcpc_code-L33822-v71.csv", "cms/lcd_x_hcpc_code-L33822-v70.csv", [
        {"lcd_id": "33822", "lcd_version": "71", "hcpc_code_id": "E2102", "hcpc_code_group": "1"},
    ])

    with pytest.raises(IngestError, match="L33822.*70.*71|L33822.*71.*70"):
        build_graph(data)


def test_a_code_defined_twice_in_one_tabular_file_fails_ingest(tmp_path):
    data = _copy_data(tmp_path)
    path = data / "fixtures" / "cdc" / "icd10cm-tabular-2027-O24.xml"
    text = path.read_text(encoding="utf-8")
    dup = "      <diag>\n        <name>O24.02</name>\n        <desc>Bogus duplicate</desc>\n      </diag>\n"
    path.write_text(text.replace("    </section>", dup + "    </section>"), encoding="utf-8")
    manifest = json.loads((data / "fixtures" / "MANIFEST.json").read_text())
    entry = next(e for e in manifest["fixtures"] if e["path"] == "cdc/icd10cm-tabular-2027-O24.xml")
    entry["record_count"] = 58
    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (data / "fixtures" / "MANIFEST.json").write_text(json.dumps(manifest))

    with pytest.raises(IngestError, match="O24.02"):
        build_graph(data)


def test_a_section_excludes1_note_is_sourced_to_its_excludes1_line_not_an_identical_excludes2_note(tmp_path):
    data = _copy_data(tmp_path)
    path = data / "fixtures" / "cdc" / "icd10cm-tabular-2027-O24.xml"
    text = path.read_text(encoding="utf-8")
    note = "maternal care related to the fetus and amniotic cavity and possible delivery problems (O30-O48)"
    # The real section has this note under excludes2; give it an excludes1 copy after that.
    marker = "      </excludes2>\n      <diag>"
    assert marker in text
    path.write_text(text.replace(
        marker, f"      </excludes2>\n      <excludes1>\n        <note>{note}</note>\n      </excludes1>\n      <diag>", 1),
        encoding="utf-8")
    manifest = json.loads((data / "fixtures" / "MANIFEST.json").read_text())
    entry = next(e for e in manifest["fixtures"] if e["path"] == "cdc/icd10cm-tabular-2027-O24.xml")
    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (data / "fixtures" / "MANIFEST.json").write_text(json.dumps(manifest))

    (rule,) = [r for r in build_graph(data).excludes1_rules("O24.111") if r.owner == "section-O20-O29"]

    lines = path.read_text(encoding="utf-8").splitlines()
    assert note in lines[rule.source.line - 1]
    assert lines[rule.source.line - 2].strip() == "<excludes1>"


# Re-review of #6 fixes (2026-10-30).
def _blank_column(data: Path, path: str, column: str):
    target = data / "fixtures" / path
    with target.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields, rows = reader.fieldnames, [{**r, column: ""} for r in reader]
    out = io.StringIO(newline="")
    w = csv.DictWriter(out, fields, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    target.write_text(out.getvalue(), encoding="utf-8")
    _pin(data, path)


def test_a_source_whose_every_row_is_rejected_fails_ingest(tmp_path):
    data = _copy_data(tmp_path)  # an export without group values would otherwise load an empty covered list
    _blank_column(data, COVERED, "icd10_covered_group")

    with pytest.raises(IngestError, match=r"article_x_icd10_covered-A52464-v65\.csv.*0 of 461"):
        build_graph(data)


@pytest.mark.parametrize("case_id", ["ev-12", "ev-13"])
def test_not_covered_is_not_upheld_when_the_covered_list_lost_rows_at_ingest(tmp_path, case_id):
    from rcm_ai.schemas import load_case

    data = _copy_data(tmp_path)
    _append_row(data, COVERED, icd10_code_id="E11.9!", sort_order="9999")
    case = load_case(DATA / "eval" / "cases" / f"{case_id}.json")  # gold uphold: diagnosis not covered

    verdict = adjudicate(case.claim, case.evidence, build_graph(data))

    assert verdict.outcome == "needs_human"
    assert [r.kind for r in verdict.reasons] == ["covered_list_incomplete"]


def test_a_rejected_row_in_a_second_group_still_counts_toward_the_group_check(tmp_path):
    data = _copy_data(tmp_path)
    _append_row(data, COVERED, icd10_code_id="E11.9!", icd10_covered_group="2", sort_order="9999")

    with pytest.raises(IngestError, match="group"):
        build_graph(data)


def test_two_versions_of_one_article_fail_ingest(tmp_path):
    data = _copy_data(tmp_path)
    _append_row(data, COVERED, article_version="66", sort_order="9999")

    with pytest.raises(IngestError, match="A52464.*65.*66"):
        build_graph(data)
