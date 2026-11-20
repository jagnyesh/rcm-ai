"""Pass or fail an eval run record against the floors in data/eval/thresholds.json.

The eval itself only reports. The gate turns a run into a yes or no that CI can act on:
each arm the thresholds name and the record holds must meet every floor set for it. An arm
the thresholds name but the record lacks is skipped (a graph-only run in CI has no model
arms), and a floor that needs `eval --draft` is skipped on a run without drafting.
"""

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_THRESHOLDS = Path("data/eval/thresholds.json")


class GateError(Exception):
    pass


@dataclass(frozen=True)
class Check:
    arm: str
    name: str
    limit: int
    actual: int | None  # None when the record can't answer it, so the check is skipped
    passed: bool | None  # None when skipped

    def line(self) -> str:
        if self.passed is None:
            return f"  skip {self.arm} {self.name}: not in this record (needs eval --draft)"
        word = "pass" if self.passed else "FAIL"
        return f"  {word} {self.arm} {self.name}: {self.actual} (limit {self.limit})"


# Each floor: what it reads from the arm's record, and whether it's a minimum or a maximum.
FLOORS = {
    "min_correct": (lambda a: a.get("correct"), min),
    "max_false_overturnable": (lambda a: _count(a.get("false_overturnable")), max),
    "max_false_uphold": (lambda a: _count(a.get("false_uphold")), max),
    "max_errors": (lambda a: a.get("errors"), max),
    "max_letters_failing_check": (lambda a: (a.get("appeals") or {}).get("failed"), max),
    "max_unsupported_citations": (lambda a: (a.get("appeals") or {}).get("unsupported"), max),
}


def _count(cases) -> int | None:
    return len(cases) if isinstance(cases, list) else None


def _read(path: Path, what: str) -> dict:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise GateError(f"cannot read {what} {path}: {e}") from e
    if not isinstance(value, dict):
        raise GateError(f"{what} {path} is not a JSON object")
    return value


def load_thresholds(path: Path = DEFAULT_THRESHOLDS) -> dict:
    t = _read(path, "thresholds")
    if not isinstance(t.get("expected_cases"), int) or not isinstance(t.get("arms"), dict):
        raise GateError(f"thresholds {path} needs an integer expected_cases and an arms object")
    for arm, floors in t["arms"].items():
        if not isinstance(floors, dict):
            raise GateError(f"thresholds for {arm} must be an object")
        for name, limit in floors.items():
            if name not in FLOORS:
                raise GateError(f"unknown threshold {name!r} for {arm}; choose from {', '.join(FLOORS)}")
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                raise GateError(f"threshold {arm}.{name} must be a whole number, not {limit!r}")
    return t


def check(record: dict, thresholds: dict) -> tuple[list[Check], list[str]]:
    """Every check the thresholds ask for, and notes on arms that were skipped or not gated.

    Raises GateError when the record ran on a different number of cases than the thresholds
    were set for, since then no floor means what it says.
    """
    arms = record.get("arms")
    if not isinstance(arms, dict) or not arms:
        raise GateError("the record has no arms; is it an eval run record (eval-<stamp>.json)?")
    expected = thresholds["expected_cases"]
    ran = (record.get("eval_set") or {}).get("expected_cases")
    if ran != expected:
        raise GateError(f"the record ran on {ran} cases; the thresholds are set for {expected}")
    checks, notes = [], []
    for arm, floors in thresholds["arms"].items():
        if arm not in arms:
            notes.append(f"  skip {arm}: not in this record")
            continue
        a = arms[arm]
        if a.get("n") != expected:
            raise GateError(f"{arm} scored {a.get('n')} cases; the thresholds are set for {expected}")
        for name, limit in floors.items():
            read, kind = FLOORS[name]
            actual = read(a)
            passed = None if actual is None else (actual >= limit if kind is min else actual <= limit)
            checks.append(Check(arm, name, limit, actual, passed))
    for arm in arms:
        if arm not in thresholds["arms"]:
            notes.append(f"  report only {arm}: {arms[arm].get('correct')}/{arms[arm].get('n')} correct, not gated")
    return checks, notes


def run(record_paths: list[Path], thresholds_path: Path = DEFAULT_THRESHOLDS) -> tuple[bool, list[str]]:
    """Gate each record; passes only if every record passes every check it can answer."""
    thresholds = load_thresholds(thresholds_path)
    lines, ok = [], True
    for path in record_paths:
        record = _read(path, "run record")
        checks, notes = check(record, thresholds)
        failed = [c for c in checks if c.passed is False]
        ok &= not failed
        lines.append(f"{Path(path).name} (git {record.get('git_sha')}): "
                     + ("FAIL" if failed else "pass") + f", {sum(c.passed is True for c in checks)} checks passed"
                     + (f", {len(failed)} failed" if failed else ""))
        lines += [c.line() for c in checks] + notes
    return ok, lines
