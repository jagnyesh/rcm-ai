"""Eval run records read as repeats of one run: each arm's accuracy across them (#30).

Repeats separate run-to-run variance from the arms' differences only when they measured
the same thing, so records that differ in eval set, code or any model arm's configuration
are refused, never pooled.
"""

import json
from pathlib import Path


class StatsError(Exception):
    pass


# What each model-calling arm records from the requests it sent; graph arms record none.
CONFIG_KEYS = ("model", "effort", "max_tokens", "prompt_sha256", "schema_canonical_sha256")


def _read(path: Path) -> dict:
    try:
        record = json.loads(path.read_text())
        record["eval_set"]["sha256"], record["git_sha"], record["arms"].items()
        for r in record["results"]:
            r["arm"], r["case"], r["gold"], r["outcome"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise StatsError(f"{path} is not an eval run record: {type(e).__name__}: {e}") from e
    return record


def _measured(record: dict) -> dict:
    """Everything that must match for two records to be repeats of one run."""
    fields = {"eval set": record["eval_set"]["sha256"], "git SHA": record["git_sha"],
              "arms": sorted(record["arms"])}
    for arm, config in sorted(record["arms"].items()):
        for key in CONFIG_KEYS:
            fields[f"{arm} {key}"] = config.get(key)
        for key in CONFIG_KEYS:
            fields[f"{arm} support check {key}"] = (config.get("support_check") or {}).get(key)
    return fields


def load(paths: list[Path], allow_dirty: bool = False) -> list[dict]:
    records = [(p, _read(p)) for p in paths]
    for path, record in records:
        if record["git_sha"].endswith("-dirty") and not allow_dirty:
            raise StatsError(f"{path} ran on uncommitted code ({record['git_sha']}), so its code can't be "
                             "matched to the others; pass --allow-dirty to use it anyway")
    first_path, first = records[0]
    expected = _measured(first)
    for path, record in records[1:]:
        for field, value in _measured(record).items():
            if value != expected[field]:
                raise StatsError(f"{path} and {first_path} differ in {field}: {value!r} against {expected[field]!r}")
    return [r for _, r in records]


def _outcomes(records: list[dict]) -> dict[str, list[dict[str, tuple[str, str]]]]:
    """arm -> one {case: (gold, outcome)} per repeat. Every repeat must hold the same cases."""
    by_arm = {arm: [{} for _ in records] for arm in records[0]["arms"]}
    for i, record in enumerate(records):
        for r in record["results"]:
            by_arm[r["arm"]][i][r["case"]] = (r["gold"], r["outcome"])
    for arm, repeats in by_arm.items():
        cases = set(repeats[0])
        for i, repeat in enumerate(repeats[1:], 1):
            if set(repeat) != cases:
                missing = sorted(cases ^ set(repeat))
                raise StatsError(f"{arm} cases differ between repeat 1 and repeat {i + 1}: {', '.join(missing)}")
    return by_arm


def _number(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def report(records: list[dict]) -> list[str]:
    k = len(records)
    lines = [f"stats: {k} repeat{'s' if k > 1 else ''} at {records[0]['git_sha']}, "
             f"eval set {records[0]['eval_set']['sha256'][:12]}"]
    for arm, repeats in _outcomes(records).items():
        n = len(repeats[0])
        right = [sum(gold == got for gold, got in repeat.values()) for repeat in repeats]
        line = f"{arm}: accuracy {_number(sum(right) / k)}/{n} in {k} repeat{'s' if k > 1 else ''}"
        if k > 1:
            line += f" (each: {', '.join(map(str, right))}; range {min(right)} to {max(right)})"
        lines.append(line)
        # The costly errors, apart: an appeal that shouldn't be sent, a claim wrongly written off.
        for label, wrong in (("false overturnable", "overturnable"), ("false uphold", "uphold")):
            per = [sorted(c for c, (gold, got) in repeat.items() if got == wrong and gold != wrong) for repeat in repeats]
            cases = sorted(set().union(*per))
            lines.append(f"  {label}: {', '.join(str(len(p)) for p in per)}" + (f" ({', '.join(cases)})" if cases else ""))
        times = {case: sum(repeat[case][0] == repeat[case][1] for repeat in repeats) for case in sorted(repeats[0])}
        if k == 1:
            wrong = [case for case, t in times.items() if not t]
            lines.append(f"  wrong: {', '.join(wrong)}" if wrong else "  every case right")
            continue
        missed = [f"{case} {t}/{k}" for case, t in times.items() if t < k]
        lines.append(f"  right in fewer than {k} of {k}: {', '.join(missed)}" if missed
                     else f"  every case right in all {k}")
    return lines
