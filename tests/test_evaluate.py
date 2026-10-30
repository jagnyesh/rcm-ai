import json
import shutil
from pathlib import Path

import pytest

from rcm_ai.evaluate import EvalError, load_eval_set, run_eval
from rcm_ai.schemas import Hop, Source, Verdict

DATA = Path(__file__).resolve().parents[1] / "data"


def _verdict(outcome):
    if outcome == "overturnable":
        return Verdict(outcome, (Hop("stub", "stub", Source("stub", "v", 1)),))
    return Verdict(outcome, ())


def _arm(outcomes):
    """A stand-in arm that returns a fixed outcome per case id."""
    return lambda c: _verdict(outcomes[c.case.id])


def _copy_set(tmp_path, keep=None):
    eval_dir = tmp_path / "eval"
    shutil.copytree(DATA / "eval", eval_dir)
    if keep is not None:
        for p in (eval_dir / "cases").glob("*.json"):
            if p.stem not in keep:
                p.unlink()
    return eval_dir


def _gold(eval_set):
    return {c.case.id: c.gold["verdict"] for c in eval_set.cases}


def test_the_committed_set_loads_all_cases_with_gold_labels():
    eval_set = load_eval_set(DATA / "eval")

    assert eval_set.expected == json.loads((DATA / "eval" / "set.json").read_text())["expected_cases"] == 20
    assert len(eval_set.cases) == 20
    assert {c.gold["verdict"] for c in eval_set.cases} == {"overturnable", "uphold", "needs_human"}
    assert len(eval_set.sha256) == 64


def test_metrics_are_computed_per_arm_against_gold():
    eval_set = load_eval_set(DATA / "eval")
    # Correct everywhere except: ev-10 (gold uphold) abstains, ev-11 (gold uphold) and
    # ev-19 (gold needs_human) are overturned.
    outcomes = {**_gold(eval_set), "ev-10": "needs_human", "ev-11": "overturnable", "ev-19": "overturnable"}

    m = run_eval(eval_set.cases, {"stub": _arm(outcomes)}).arms["stub"]

    assert m.n == 20
    assert m.correct == 17
    assert m.accuracy == pytest.approx(0.85)
    assert m.abstentions == 6  # 6 gold needs_human, minus ev-19, plus ev-10
    assert m.abstention_rate == pytest.approx(0.3)
    assert m.abstention_precision == pytest.approx(5 / 6)
    assert m.false_overturnable == ["ev-11", "ev-19"]
    assert m.errors == 0


def test_an_arm_that_raises_is_counted_as_errors_not_as_abstentions():
    eval_set = load_eval_set(DATA / "eval")

    def arm(c):
        raise TimeoutError("needs_human")  # an error message must never pass for a verdict

    report = run_eval(eval_set.cases, {"broken": arm})

    m = report.arms["broken"]
    assert (m.correct, m.abstentions, m.errors) == (0, 0, 20)
    assert report.results[0].outcome == "error: TimeoutError: needs_human"


def test_an_empty_case_set_fails(tmp_path):
    with pytest.raises(EvalError, match="0 cases"):
        load_eval_set(_copy_set(tmp_path, keep=set()))


def test_running_on_no_cases_fails():
    with pytest.raises(EvalError, match="0 cases"):
        run_eval([], {"stub": _arm({})})


def test_a_partial_case_set_fails_against_the_expected_count(tmp_path):
    with pytest.raises(EvalError, match="19 cases.*expected 20"):
        load_eval_set(_copy_set(tmp_path, keep={f"ev-{i:02d}" for i in range(1, 20)}))


def test_an_oversized_case_set_fails_against_the_expected_count(tmp_path):
    eval_dir = _copy_set(tmp_path)
    shutil.copy(eval_dir / "cases" / "ev-01.json", eval_dir / "cases" / "ev-21.json")

    with pytest.raises(EvalError, match="21 cases.*expected 20"):
        load_eval_set(eval_dir)


def test_a_case_whose_id_does_not_match_its_file_fails(tmp_path):
    eval_dir = _copy_set(tmp_path)
    shutil.copy(eval_dir / "cases" / "ev-01.json", eval_dir / "cases" / "ev-20.json")

    with pytest.raises(EvalError, match="ev-20.json"):
        load_eval_set(eval_dir)


@pytest.mark.parametrize("gold", [{"verdict": "overturned"}, {}, ["overturnable"]])
def test_a_case_without_a_valid_gold_verdict_fails(tmp_path, gold):
    eval_dir = _copy_set(tmp_path)
    path = eval_dir / "cases" / "ev-01.json"
    raw = json.loads(path.read_text())
    raw["gold"] = gold
    path.write_text(json.dumps(raw))

    with pytest.raises(EvalError, match="ev-01.json"):
        load_eval_set(eval_dir)


@pytest.mark.parametrize("setup", ["missing set.json", "set.json without count", "case not JSON"])
def test_a_malformed_eval_directory_fails_as_an_eval_error(tmp_path, setup):
    eval_dir = _copy_set(tmp_path)
    if setup == "missing set.json":
        (eval_dir / "set.json").unlink()
    elif setup == "set.json without count":
        (eval_dir / "set.json").write_text("{}")
    else:
        (eval_dir / "cases" / "ev-01.json").write_text("{not json")

    with pytest.raises(EvalError):
        load_eval_set(eval_dir)


def test_running_with_no_arms_fails():
    with pytest.raises(EvalError, match="arm"):
        run_eval(load_eval_set(DATA / "eval").cases, {})
