import json
import subprocess
import sys
from pathlib import Path

import pytest

from rcm_ai import ARM_MODELS
from rcm_ai import gate

REPO = Path(__file__).resolve().parents[1]
CLI = Path(sys.executable).parent / "rcm-ai"
THRESHOLDS = {
    "expected_cases": 20,
    "arms": {
        "graph": {"min_correct": 20, "max_false_overturnable": 0, "max_false_uphold": 0, "max_errors": 0},
        "extracted": {"min_correct": 15, "max_false_overturnable": 0, "max_letters_failing_check": 0},
    },
}


def _arm(correct=20, false_overturnable=(), false_uphold=(), errors=0, appeals=None):
    a = {"n": 20, "correct": correct, "errors": errors,
         "false_overturnable": list(false_overturnable), "false_uphold": list(false_uphold)}
    if appeals is not None:
        a["appeals"] = appeals
    return a


def _record(**arms):
    return {"git_sha": "abc1234", "eval_set": {"expected_cases": 20}, "arms": arms}


def _by(checks):
    return {(c.arm, c.name): c for c in checks}


def test_a_clean_graph_run_passes_and_a_missing_arm_is_skipped():
    checks, notes = gate.check(_record(graph=_arm()), THRESHOLDS)

    assert all(c.passed for c in checks)
    assert len(checks) == 4
    assert notes == ["  skip extracted: not in this record"]


def test_a_false_overturnable_fails_its_max_even_when_accuracy_meets_its_floor():
    checks, _ = gate.check(_record(graph=_arm(), extracted=_arm(correct=16, false_overturnable=["ev-03"])),
                           THRESHOLDS)

    by = _by(checks)
    assert by["extracted", "min_correct"].passed is True
    assert by["extracted", "max_false_overturnable"].passed is False
    assert by["extracted", "max_false_overturnable"].actual == 1


def test_accuracy_below_the_floor_fails():
    checks, _ = gate.check(_record(extracted=_arm(correct=14)), THRESHOLDS)

    assert _by(checks)["extracted", "min_correct"].passed is False


def test_a_letter_floor_is_skipped_without_drafting_and_checked_with_it():
    without, _ = gate.check(_record(extracted=_arm(correct=16)), THRESHOLDS)
    assert _by(without)["extracted", "max_letters_failing_check"].passed is None

    drafted, _ = gate.check(_record(extracted=_arm(correct=16, appeals={"failed": 1, "unsupported": 2})), THRESHOLDS)
    assert _by(drafted)["extracted", "max_letters_failing_check"].passed is False


def test_an_arm_without_thresholds_is_reported_not_gated():
    checks, notes = gate.check(_record(graph=_arm(), **{"llm-only": _arm(correct=3)}), THRESHOLDS)

    assert all(c.arm != "llm-only" for c in checks)
    assert "  report only llm-only: 3/20 correct, not gated" in notes


def test_a_record_on_a_different_case_count_is_refused():
    record = _record(graph=_arm())
    record["eval_set"]["expected_cases"] = 19
    with pytest.raises(gate.GateError, match="19 cases"):
        gate.check(record, THRESHOLDS)


def test_a_record_with_no_arms_is_refused():
    with pytest.raises(gate.GateError, match="no arms"):
        gate.check({"eval_set": {"expected_cases": 20}}, THRESHOLDS)


@pytest.mark.parametrize("floors, match", [
    ({"min_accuracy": 0.8}, "unknown threshold"),
    ({"min_correct": -1}, "whole number"),
    ({"min_correct": True}, "whole number"),
])
def test_bad_thresholds_are_refused(tmp_path, floors, match):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"expected_cases": 20, "arms": {"graph": floors}}))
    with pytest.raises(gate.GateError, match=match):
        gate.load_thresholds(path)


def test_the_committed_thresholds_load_and_name_only_real_arms():
    t = gate.load_thresholds(REPO / "data/eval/thresholds.json")
    set_json = json.loads((REPO / "data/eval/set.json").read_text())

    assert t["expected_cases"] == set_json["expected_cases"]
    assert set(t["arms"]) <= set(ARM_MODELS)
    assert t["arms"]["graph"]["min_correct"] == t["expected_cases"]


def _run(*args):
    return subprocess.run([str(CLI), *args], cwd=REPO, capture_output=True, text=True, timeout=120)


def test_a_graph_eval_passes_the_committed_gate_end_to_end(tmp_path):
    ran = _run("eval", "--arms", "graph", "--out", str(tmp_path))
    assert ran.returncode == 0, ran.stderr
    record = next(tmp_path.glob("eval-*.json"))

    gated = _run("gate", str(record))

    assert gated.returncode == 0, gated.stderr
    assert ": pass, 4 checks passed" in gated.stdout
    assert "skip extracted: not in this record" in gated.stdout


def test_the_gate_exits_1_on_a_breach_and_2_on_an_unreadable_record(tmp_path):
    bad = tmp_path / "eval-bad.json"
    bad.write_text(json.dumps(_record(graph=_arm(correct=19))))
    failed = _run("gate", str(bad))
    assert failed.returncode == 1
    assert "FAIL graph min_correct: 19 (limit 20)" in failed.stdout
    assert "gate failed" in failed.stderr

    missing = _run("gate", str(tmp_path / "nope.json"))
    assert missing.returncode == 2
    assert "gate refused" in missing.stderr
