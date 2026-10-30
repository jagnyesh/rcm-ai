"""Run named arms over the locked eval set and score them against gold labels.

An arm is any callable from an EvalCase to a Verdict. Metrics treat a wrong
"overturnable" as the costly error, so it is counted separately.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rcm_ai.schemas import Case, SchemaError, Verdict, load_case


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

    @property
    def correct(self) -> bool:
        return self.outcome == self.gold


@dataclass
class ArmMetrics:
    n: int = 0
    correct: int = 0
    abstentions: int = 0
    correct_abstentions: int = 0
    false_overturnable: list[str] = field(default_factory=list)

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
class EvalReport:
    expected: int
    results: list[CaseResult]
    arms: dict[str, ArmMetrics]


Arm = Callable[[EvalCase], Verdict]


def load_eval_set(eval_dir: Path) -> list[EvalCase]:
    eval_dir = Path(eval_dir)
    expected = json.loads((eval_dir / "set.json").read_text())["expected_cases"]
    paths = sorted((eval_dir / "cases").glob("*.json"))
    if len(paths) != expected:
        raise EvalError(f"{len(paths)} cases in {eval_dir / 'cases'}, expected {expected}")
    cases = []
    for p in paths:
        gold = json.loads(p.read_text()).get("gold")
        if not gold or "verdict" not in gold:
            raise EvalError(f"{p.name} has no gold verdict")
        try:
            cases.append(EvalCase(load_case(p), gold))
        except SchemaError as e:
            raise EvalError(f"{p.name} is not a valid case: {e}") from e
    return cases


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
                outcome = arm(c).outcome
            except Exception as e:  # a crashing arm is a wrong answer, not a crashed eval
                outcome = f"error: {type(e).__name__}: {e}"
            r = CaseResult(name, c.case.id, c.gold["verdict"], outcome)
            results.append(r)
            m.n += 1
            m.correct += r.correct
            if outcome == "needs_human":
                m.abstentions += 1
                m.correct_abstentions += r.gold == "needs_human"
            if outcome == "overturnable" and r.gold != "overturnable":
                m.false_overturnable.append(r.case)
    return EvalReport(len(cases), results, metrics)
