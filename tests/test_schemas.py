import json
from pathlib import Path

import pytest

from rcm_ai.schemas import SchemaError, load_case

CASE = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "case-001.json"


def _write_case(tmp_path, **changes):
    raw = json.loads(CASE.read_text())
    for dotted, value in changes.items():
        target = raw
        *parents, leaf = dotted.split("__")
        for key in parents:
            target = target[key]
        target[leaf] = value
    path = tmp_path / "case.json"
    path.write_text(json.dumps(raw))
    return path


@pytest.mark.parametrize(
    "change, message",
    [
        ({"claim__hcpcs": "95250"}, "CPT"),
        ({"claim__hcpcs": "e2103"}, "HCPCS"),
        ({"claim__icd10": ["E11.6.5"]}, "ICD-10-CM"),
        ({"claim__icd10": []}, "ICD-10-CM"),
        ({"claim__carc": "CO-50"}, "CARC"),
        ({"synthetic": False}, "synthetic"),
    ],
)
def test_a_case_that_breaks_one_boundary_rule_is_rejected_on_load(tmp_path, change, message):
    with pytest.raises(SchemaError, match=message):
        load_case(_write_case(tmp_path, **change))


def test_the_committed_synthetic_case_loads():
    assert load_case(CASE).claim.hcpcs == "E2103"


# Regression: ISSUE-A — malformed case files crashed the CLI with a traceback
# Found by /qa on 2026-10-30
@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps({"id": "x", "synthetic": True}), json.dumps(["not", "an", "object"])],
)
def test_a_case_file_that_is_not_a_complete_case_is_rejected_as_a_schema_error(tmp_path, content):
    path = tmp_path / "case.json"
    path.write_text(content)
    with pytest.raises(SchemaError):
        load_case(path)


def test_a_missing_case_file_is_rejected_as_a_schema_error(tmp_path):
    with pytest.raises(SchemaError, match="missing.json"):
        load_case(tmp_path / "missing.json")
