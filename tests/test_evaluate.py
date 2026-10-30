import json
import shutil
from pathlib import Path

import pytest

from rcm_ai.evaluate import EvalError, load_eval_set, run_eval
from rcm_ai.schemas import Verdict

DATA = Path(__file__).resolve().parents[1] / "data"


def _arm(outcomes):
    """A stand-in arm that returns a fixed outcome per case id."""
    def arm(case):
        outcome = outcomes[case.case.id]
        return Verdict(outcome, ()) if outcome != "overturnable" else _overturnable()
    return arm


def _overturnable():
    from rcm_ai.schemas import Hop, Source
    return Verdict("overturnable", (Hop("stub", "stub", Source("stub", "v", 1)),))


def _copy_set(tmp_path, keep=None):
    eval_dir = tmp_path / "eval"
    shutil.copytree(DATA / "eval", eval_dir)
    if keep is not None:
        for p in (eval_dir / "cases").glob("*.json"):
            if p.stem not in keep:
                p.unlink()
    return eval_dir


def test_the_committed_set_loads_all_cases_with_gold_labels():
    cases = load_eval_set(DATA / "eval")

    assert len(cases) == json.loads((DATA / "eval" / "set.json").read_text())["expected_cases"] == 20
    assert {c.gold["verdict"] for c in cases} == {"overturnable", "uphold", "needs_human"}


def test_metrics_are_computed_per_arm_against_gold(tmp_path):
    cases = load_eval_set(DATA / "eval")
    gold = {c.case.id: c.gold["verdict"] for c in cases}
    # Correct everywhere except: ev-10 (gold uphold) abstains, ev-19 (gold needs_human) is overturned.
    outcomes = {**gold, "ev-10": "needs_human", "ev-19": "overturnable"}

    report = run_eval(cases, {"stub": _arm(outcomes)})

    m = report.arms["stub"]
    assert m.n == 20
    assert m.correct == 18
    assert m.accuracy == pytest.approx(0.9)
    assert m.abstentions == 6  # 6 gold needs_human, minus ev-19, plus ev-10
    assert m.abstention_rate == pytest.approx(0.3)
    assert m.abstention_precision == pytest.approx(5 / 6)
    assert m.false_overturnable == ["ev-19"]


def test_a_case_whose_arm_raises_counts_as_wrong_and_is_recorded():
    cases = load_eval_set(DATA / "eval")

    def arm(case):
        raise ValueError("boom")

    report = run_eval(cases, {"broken": arm})

    assert report.arms["broken"].correct == 0
    assert report.results[0].outcome == "error: ValueError: boom"


def test_an_empty_case_set_fails(tmp_path):
    with pytest.raises(EvalError, match="0 cases"):
        load_eval_set(_copy_set(tmp_path, keep=set()))


def test_a_partial_case_set_fails_against_the_expected_count(tmp_path):
    with pytest.raises(EvalError, match="19 cases.*expected 20"):
        load_eval_set(_copy_set(tmp_path, keep={f"ev-{i:02d}" for i in range(1, 20)}))


def test_running_with_no_arms_fails():
    with pytest.raises(EvalError, match="arm"):
        run_eval(load_eval_set(DATA / "eval"), {})
