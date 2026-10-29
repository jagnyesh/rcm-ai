import json
import shutil
from pathlib import Path

import pytest

from rcm_ai.provenance import ProvenanceError, verify_fixtures

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "fixtures"


def test_every_committed_fixture_matches_its_manifest_entry():
    records = verify_fixtures(FIXTURES)

    assert [r.path for r in records] == [
        "cdc/icd10cm-tabular-2027-E08-E13.xml",
        "cms/article_x_icd10_covered-A52464-v65.csv",
        "cms/lcd_x_hcpc_code-L33822-v70.csv",
    ]
    assert records[0].version.startswith("ICD-10-CM FY2027")


def _copy_fixtures(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "fixtures")
    return tmp_path / "fixtures"


def test_a_fixture_edited_after_capture_is_rejected(tmp_path):
    fixtures = _copy_fixtures(tmp_path)
    xml = fixtures / "cdc" / "icd10cm-tabular-2027-E08-E13.xml"
    xml.write_text(xml.read_text().replace("Type 2 diabetes mellitus", "Type 2 DM", 1))

    with pytest.raises(ProvenanceError, match="icd10cm-tabular-2027-E08-E13.xml"):
        verify_fixtures(fixtures)


def test_a_fixture_file_missing_from_the_manifest_is_rejected(tmp_path):
    fixtures = _copy_fixtures(tmp_path)
    (fixtures / "cdc" / "hand_made.xml").write_text("<x/>")

    with pytest.raises(ProvenanceError, match="hand_made.xml"):
        verify_fixtures(fixtures)


def test_macos_finder_metadata_in_the_fixtures_dir_is_ignored(tmp_path):
    fixtures = _copy_fixtures(tmp_path)
    (fixtures / "cdc" / ".DS_Store").write_bytes(b"\x00")

    assert len(verify_fixtures(fixtures)) == 3


# Regression: ISSUE-002 — manifest record_count was never checked
# Found by /qa on 2026-10-29
# Report: qa-report-rcm-ai-functional-2026-10-29.md (session scratchpad)
@pytest.mark.parametrize(
    "fixture", ["cdc/icd10cm-tabular-2027-E08-E13.xml", "cms/lcd_x_hcpc_code-L33822-v70.csv"]
)
def test_a_manifest_record_count_that_disagrees_with_the_fixture_is_rejected(tmp_path, fixture):
    fixtures = _copy_fixtures(tmp_path)
    manifest = json.loads((fixtures / "MANIFEST.json").read_text())
    next(e for e in manifest["fixtures"] if e["path"] == fixture)["record_count"] = 999
    (fixtures / "MANIFEST.json").write_text(json.dumps(manifest))

    with pytest.raises(ProvenanceError, match="999"):
        verify_fixtures(fixtures)
