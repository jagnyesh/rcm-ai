import argparse
import sys
from pathlib import Path

from rcm_ai.adjudicate import adjudicate
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
    if not verdict.path:
        print("  no complete, criteria-satisfied coverage path in the graph")
    for n, hop in enumerate(verdict.path, start=1):
        print(f"  {n}. {hop.statement}")
        print(f"     {data_dir / 'fixtures' / hop.source.path}:{hop.source.line}  [{hop.source.version}]")
        print(f"     look for: {hop.anchor}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rcm-ai")
    sub = parser.add_subparsers(dest="command", required=True)
    triage = sub.add_parser("triage", help="adjudicate one synthetic denied claim")
    triage.add_argument("case", type=Path)
    triage.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    return _triage(args.case, args.data)


if __name__ == "__main__":
    sys.exit(main())
