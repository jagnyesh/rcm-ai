"""Issue #30: `rcm-ai stats` reads eval run records as repeats of one run and reports each
arm's accuracy across them. It refuses records that measured different things.

Every record here is synthetic, written to a temporary directory; no model is called."""

import json

import pytest

import rcm_ai

SHA = "a" * 40
CONFIG = {"model": "claude-opus-5-5", "effort": "high", "max_tokens": 16000,
          "prompt_sha256": "p" * 64, "schema_canonical_sha256": "s" * 64}


def _record(tmp_path, name, outcomes, git_sha=SHA, eval_set="e" * 64, configs=None):
    """A run record in eval's format. outcomes maps arm -> case -> (gold, outcome)."""
    configs = configs or {}
    record = {
        "date": "2026-11-02T00:00:00+00:00",
        "git_sha": git_sha,
        "eval_set": {"data_dir": "data", "expected_cases": 3, "sha256": eval_set},
        "arms": {arm: {"model": None} if arm == "graph" else {**CONFIG, **configs.get(arm, {})} for arm in outcomes},
        "results": [{"arm": arm, "case": case, "gold": gold, "outcome": outcome, "correct": gold == outcome}
                    for arm, cases in outcomes.items() for case, (gold, outcome) in cases.items()],
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(record))
    return path


def _stats(capsys, *args):
    code = rcm_ai.main(["stats", *map(str, args)])
    out = capsys.readouterr()
    return code, out.out, out.err


GOLD = {"ev-01": "overturnable", "ev-10": "uphold", "ev-15": "needs_human"}


def _arm(**got):
    """Outcomes for the three cases: each case's gold unless given."""
    return {case: (gold, got.get(case.replace("-", ""), gold)) for case, gold in GOLD.items()}


def test_one_record_prints_each_arms_accuracy(tmp_path, capsys):
    path = _record(tmp_path, "r1", {"graph": _arm(), "extracted": _arm(ev10="needs_human")})

    code, out, _ = _stats(capsys, path)

    assert code == 0
    assert "graph: accuracy 3/3 in 1 repeat\n  false overturnable: 0\n  false uphold: 0\n  every case right\n" in out
    assert "extracted: accuracy 2/3 in 1 repeat" in out and "  wrong: ev-10\n" in out


def test_repeats_give_each_arms_mean_and_range(tmp_path, capsys):
    paths = [_record(tmp_path, "r1", {"extracted": _arm(ev10="needs_human")}),
             _record(tmp_path, "r2", {"extracted": _arm()}),
             _record(tmp_path, "r3", {"extracted": _arm()})]

    code, out, _ = _stats(capsys, *paths)

    assert code == 0
    assert out.startswith(f"stats: 3 repeats at {SHA}")
    assert "extracted: accuracy 2.67/3 in 3 repeats (each: 2, 3, 3; range 2 to 3)" in out


def test_the_costly_errors_are_counted_apart_in_each_repeat(tmp_path, capsys):
    paths = [_record(tmp_path, "r1", {"llm-only": _arm(ev10="overturnable")}),
             _record(tmp_path, "r2", {"llm-only": _arm(ev15="uphold", ev01="uphold")})]

    _, out, _ = _stats(capsys, *paths)

    assert "  false overturnable: 1, 0 (ev-10)" in out
    assert "  false uphold: 0, 2 (ev-01, ev-15)" in out


def test_each_case_shows_how_many_repeats_got_it_right(tmp_path, capsys):
    paths = [_record(tmp_path, "r1", {"graph": _arm(), "extracted": _arm(ev10="needs_human")}),
             _record(tmp_path, "r2", {"graph": _arm(), "extracted": _arm(ev10="needs_human", ev01="needs_human")})]

    _, out, _ = _stats(capsys, *paths)

    assert "graph: accuracy 3/3 in 2 repeats" in out and "  every case right in all 2" in out
    assert "  right in fewer than 2 of 2: ev-01 1/2, ev-10 0/2" in out


@pytest.mark.parametrize("second, field", [
    ({"git_sha": "b" * 40}, "git SHA"),
    ({"eval_set": "f" * 64}, "eval set"),
    ({"configs": {"extracted": {"prompt_sha256": "q" * 64}}}, "extracted prompt_sha256"),
    ({"configs": {"extracted": {"effort": "medium"}}}, "extracted effort"),
    ({"configs": {"extracted": {"model": "claude-other"}}}, "extracted model"),
    ({"configs": {"extracted": {"schema_canonical_sha256": "t" * 64}}}, "extracted schema_canonical_sha256"),
    ({"configs": {"extracted": {"support_check": {**CONFIG, "prompt_sha256": "u" * 64}}}},
     "extracted support check prompt_sha256"),
])
def test_records_that_measured_different_things_are_refused_naming_the_field(tmp_path, capsys, second, field):
    checked = {"configs": {"extracted": {"support_check": CONFIG}}} if "support check" in field else {}
    first = _record(tmp_path, "r1", {"graph": _arm(), "extracted": _arm()}, **checked)
    other = _record(tmp_path, "r2", {"graph": _arm(), "extracted": _arm()}, **second)

    code, out, err = _stats(capsys, first, other)

    assert (code, out) == (2, "")
    assert err.startswith("stats refused: ") and f"differ in {field}:" in err


def test_records_that_ran_different_arms_are_refused(tmp_path, capsys):
    paths = [_record(tmp_path, "r1", {"graph": _arm(), "extracted": _arm()}), _record(tmp_path, "r2", {"graph": _arm()})]

    code, _, err = _stats(capsys, *paths)

    assert code == 2 and "differ in arms:" in err


def test_a_record_from_uncommitted_code_is_refused_unless_allowed(tmp_path, capsys):
    path = _record(tmp_path, "r1", {"graph": _arm()}, git_sha=SHA + "-dirty")

    refused, _, err = _stats(capsys, path)
    allowed, out, _ = _stats(capsys, path, "--allow-dirty")

    assert refused == 2 and "uncommitted code" in err and "--allow-dirty" in err
    assert allowed == 0 and "graph: accuracy 3/3" in out


def test_a_case_missing_from_one_repeat_is_refused(tmp_path, capsys):
    short = _arm()
    del short["ev-15"]
    paths = [_record(tmp_path, "r1", {"graph": _arm()}), _record(tmp_path, "r2", {"graph": short})]

    code, _, err = _stats(capsys, *paths)

    assert code == 2 and "graph cases differ between repeat 1 and repeat 2: ev-15" in err


@pytest.mark.parametrize("content", ["not json", "{}", '{"git_sha": "x", "eval_set": {}, "arms": {}, "results": []}'])
def test_a_file_that_is_not_a_run_record_is_refused(tmp_path, capsys, content):
    path = tmp_path / "bad.json"
    path.write_text(content)

    code, out, err = _stats(capsys, path)

    assert (code, out) == (2, "") and "is not an eval run record" in err


def test_a_missing_file_is_refused(tmp_path, capsys):
    code, _, err = _stats(capsys, tmp_path / "absent.json")

    assert code == 2 and "is not an eval run record" in err
