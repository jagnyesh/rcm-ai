import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from rcm_ai.adjudicate import adjudicate
from rcm_ai.evaluate import EvalError, load_eval_set, run_eval
from rcm_ai.graph import build_graph
from rcm_ai.schemas import SchemaError, load_case


def _triage(case_path: Path, data_dir: Path) -> int:
    try:
        case = load_case(case_path)
        verdict = adjudicate(case.claim, case.evidence, build_graph(data_dir))
    except SchemaError as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 2
    print(f"{case.id}: {verdict.outcome}")
    for r in verdict.reasons:
        print(f"  reason ({r.kind}): {r.statement}")
        if r.source:
            print(f"     {data_dir / 'fixtures' / r.source.path}:{r.source.line}  [{r.source.version}]")
            print(f"     look for: {r.anchor}")
    for n, hop in enumerate(verdict.path, start=1):
        print(f"  {n}. {hop.statement}")
        print(f"     {data_dir / 'fixtures' / hop.source.path}:{hop.source.line}  [{hop.source.version}]")
        print(f"     look for: {hop.anchor}")
    for f in verdict.findings:
        print(f"  finding ({f.type}): {f.statement}")
        if f.source:
            print(f"     {data_dir / 'fixtures' / f.source.path}:{f.source.line}  [{f.source.version}]")
            print(f"     look for: {f.anchor}")
    return 0


# Model id per arm, recorded with every run; the graph arm calls no model.
ARM_MODELS = {"graph": None}


def _git(*args: str) -> str | None:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _git_sha() -> str:
    sha = _git("rev-parse", "HEAD")
    if sha is None:
        return "unknown (not a git checkout)"
    return sha + ("-dirty" if _git("status", "--porcelain") else "")


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _eval(data_dir: Path, out_dir: Path) -> int:
    try:
        eval_set = load_eval_set(data_dir / "eval")
        graph = build_graph(data_dir)
        arms = {"graph": lambda c: adjudicate(c.case.claim, c.case.evidence, graph)}
        report = run_eval(eval_set.cases, arms)
    except EvalError as e:
        print(f"eval failed: {e}", file=sys.stderr)
        return 2
    print(f"eval: {len(eval_set.cases)} cases ran (expected {eval_set.expected})")
    for name, m in report.arms.items():
        print(f"  {name}: accuracy {m.correct}/{m.n} ({_pct(m.accuracy)}), errors {m.errors}")
        print(f"    false overturnable: {len(m.false_overturnable)} {m.false_overturnable}")
        print(f"    false uphold: {len(m.false_uphold)} {m.false_uphold}")
        print(f"    abstention rate: {m.abstentions}/{m.n} ({_pct(m.abstention_rate)}), "
              f"precision {m.correct_abstentions}/{m.abstentions} ({_pct(m.abstention_precision)})")
    gaps = {c.case.id: c.gold.get("known_system_gap") for c in eval_set.cases}
    for r in report.results:
        if not r.correct:
            note = "known gap" if gaps[r.case] else "UNEXPLAINED"
            print(f"    {r.arm} {r.case}: gold {r.gold}, got {r.outcome}  [{note}]")
    now = datetime.now(timezone.utc)
    record = {
        "date": now.isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "eval_set": {"data_dir": str(data_dir.resolve()), "expected_cases": eval_set.expected,
                     "sha256": eval_set.sha256},
        "arms": {
            name: {"model": ARM_MODELS[name], **asdict(m), "accuracy": m.accuracy,
                   "abstention_rate": m.abstention_rate, "abstention_precision": m.abstention_precision}
            for name, m in report.arms.items()
        },
        "results": [{**asdict(r), "correct": r.correct, "known_system_gap": gaps[r.case]}
                    for r in report.results],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"eval-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"results: {path}")
    errors = sum(m.errors for m in report.arms.values())
    if errors:
        print(f"eval incomplete: {errors} arm error{'s' if errors > 1 else ''}; metrics are not comparable",
              file=sys.stderr)
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rcm-ai")
    sub = parser.add_subparsers(dest="command", required=True)
    triage = sub.add_parser("triage", help="adjudicate one synthetic denied claim")
    triage.add_argument("case", type=Path)
    triage.add_argument("--data", type=Path, default=Path("data"))
    ev = sub.add_parser("eval", help="score the arms against the locked eval set")
    ev.add_argument("--data", type=Path, default=Path("data"))
    ev.add_argument("--out", type=Path, default=Path("eval-results"))
    args = parser.parse_args(argv)
    if args.command == "eval":
        return _eval(args.data, args.out)
    return _triage(args.case, args.data)


if __name__ == "__main__":
    sys.exit(main())
