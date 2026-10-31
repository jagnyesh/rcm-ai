"""Run named arms over the locked eval set and score them against gold labels.

An arm is any callable from an EvalCase to a Verdict. Metrics count the two
costly errors separately: a wrong "overturnable" (an appeal that shouldn't be sent) and
a wrong "uphold" (a claim written off that should have been appealed or reviewed).
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rcm_ai.schemas import OUTCOMES, Case, SchemaError, Verdict, load_case


class EvalError(Exception):
    pass


@dataclass(frozen=True)
class EvalCase:
    case: Case
    gold: dict


@dataclass(frozen=True)
class CaseResult:
    arm: str
    case: str
    gold: str
    outcome: str  # a verdict outcome, or "error: <type>: <message>"
    why: tuple[str, ...] = ()  # the verdict's reasons ("kind: statement"), or its path when it has none

    @property
    def correct(self) -> bool:
        return self.outcome == self.gold


@dataclass(frozen=True)
class Disagreement:
    case: str
    gold: str
    arms: dict[str, CaseResult]


@dataclass
class ArmMetrics:
    n: int = 0
    correct: int = 0
    abstentions: int = 0
    correct_abstentions: int = 0
    errors: int = 0
    false_overturnable: list[str] = field(default_factory=list)
    false_uphold: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.n

    @property
    def abstention_rate(self) -> float:
        return self.abstentions / self.n

    @property
    def abstention_precision(self) -> float | None:
        """Of the cases the arm handed to a human, the share where gold agrees it should."""
        return self.correct_abstentions / self.abstentions if self.abstentions else None


@dataclass(frozen=True)
class EvalSet:
    expected: int
    cases: list[EvalCase]
    sha256: str  # over set.json and every case file, so a run names the exact data it scored


@dataclass(frozen=True)
class EvalReport:
    results: list[CaseResult]
    arms: dict[str, ArmMetrics]

    @property
    def disagreements(self) -> list[Disagreement]:
        """Cases where the arms' outcomes differ, in case order. An error differs from any verdict."""
        by_case = {}
        for r in self.results:
            by_case.setdefault(r.case, {})[r.arm] = r
        return [Disagreement(case, next(iter(arms.values())).gold, arms)
                for case, arms in by_case.items() if len({r.outcome for r in arms.values()}) > 1]


Arm = Callable[[EvalCase], Verdict]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise EvalError(f"cannot read {path.name}: {e}") from e


def load_eval_set(eval_dir: Path) -> EvalSet:
    eval_dir = Path(eval_dir)
    meta = _read_json(eval_dir / "set.json")
    if not isinstance(meta, dict) or not isinstance(meta.get("expected_cases"), int):
        raise EvalError("set.json needs an integer expected_cases")
    expected = meta["expected_cases"]
    paths = sorted((eval_dir / "cases").glob("*.json"))
    if len(paths) != expected:
        raise EvalError(f"{len(paths)} cases in {eval_dir / 'cases'}, expected {expected}")
    digest = hashlib.sha256((eval_dir / "set.json").read_bytes())
    cases = []
    for p in paths:
        digest.update(p.name.encode() + b"\0" + p.read_bytes())
        raw = _read_json(p)
        gold = raw.get("gold") if isinstance(raw, dict) else None
        if not isinstance(gold, dict) or gold.get("verdict") not in OUTCOMES:
            raise EvalError(f"{p.name} has no valid gold verdict (one of {OUTCOMES})")
        if raw.get("id") != p.stem:
            raise EvalError(f"{p.name} holds case id {raw.get('id')!r}; the id must match the file name")
        try:
            cases.append(EvalCase(load_case(p), gold))
        except SchemaError as e:
            raise EvalError(f"{p.name} is not a valid case: {e}") from e
    return EvalSet(expected, cases, digest.hexdigest())


def run_eval(cases: list[EvalCase], arms: dict[str, Arm]) -> EvalReport:
    if not arms:
        raise EvalError("no arm to evaluate")
    if not cases:
        raise EvalError("0 cases to evaluate")
    results, metrics = [], {}
    for name, arm in arms.items():
        m = metrics[name] = ArmMetrics()
        for c in cases:
            try:
                verdict = arm(c)
                outcome = verdict.outcome
                why = tuple(f"{x.kind}: {x.statement}" for x in verdict.reasons) or tuple(h.statement for h in verdict.path)
            except Exception as e:  # a crashing arm is a scored error, not a crashed eval
                outcome, why = f"error: {type(e).__name__}: {e}", ()
                m.errors += 1
            r = CaseResult(name, c.case.id, c.gold["verdict"], outcome, why)
            results.append(r)
            m.n += 1
            m.correct += r.correct
            if outcome == "needs_human":
                m.abstentions += 1
                m.correct_abstentions += r.gold == "needs_human"
            if outcome == "overturnable" and r.gold != "overturnable":
                m.false_overturnable.append(r.case)
            if outcome == "uphold" and r.gold != "uphold":
                m.false_uphold.append(r.case)
    return EvalReport(results, metrics)
