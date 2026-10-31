import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from rcm_ai.adjudicate import adjudicate
from rcm_ai import baseline as bl
from rcm_ai.evaluate import EvalError, load_eval_set, run_eval
from rcm_ai import extract as ex
from rcm_ai.graph import IngestError, build_graph
from rcm_ai.provenance import ProvenanceError
from rcm_ai.schemas import SchemaError, load_case


class CredentialError(Exception):
    pass


def _client():
    """A client checked with one cheap call, so a missing key or model fails before any case runs."""
    import anthropic

    client = anthropic.Anthropic()  # resolves an API key, OAuth profile or federation from the environment
    try:
        client.models.retrieve(ex.MODEL)
    except TypeError as e:  # the SDK raises TypeError when it finds no credentials at all
        raise CredentialError(f"no Anthropic credentials found ({e}); set ANTHROPIC_API_KEY") from e
    except anthropic.AuthenticationError as e:
        raise CredentialError(f"Anthropic credentials were rejected: {e.message}") from e
    except anthropic.NotFoundError as e:
        raise CredentialError(f"model {ex.MODEL} is not available to these credentials: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise CredentialError(f"cannot reach the Anthropic API: {e}") from e
    return client


def _save_extraction(log_dir: Path, case_id: str, extraction) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{case_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(
        {"case": case_id, "stop_reason": extraction.stop_reason, "raw": extraction.raw, **extraction.record},
        indent=2) + "\n")
    return path


def _triage(case_path: Path, data_dir: Path, extract: bool = False, log_dir: Path = Path("eval-results/extractions")) -> int:
    try:
        case = load_case(case_path)
        graph = build_graph(data_dir)
        if extract:
            client = _client()
            logs = []

            def call(note, criteria):
                extraction = ex.extract(note, criteria, client)
                logs.append(_save_extraction(log_dir, case.id, extraction))  # before the case can fail
                return extraction

            verdict, extraction = ex.triage_note(case.claim, case.note, graph, call)
        elif case.evidence is None:
            raise SchemaError(f"{case_path}: case has no evidence; run with --extract to extract it from the note")
        else:
            verdict = adjudicate(case.claim, case.evidence, graph)
    except SchemaError as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 2
    except (IngestError, ProvenanceError) as e:
        print(f"ingest failed: {e}", file=sys.stderr)
        return 2
    except CredentialError as e:
        print(f"extraction unavailable: {e}", file=sys.stderr)
        return 2
    _warn_rejected(graph)
    print(f"{case.id}: {verdict.outcome}")
    if extract and extraction is None:
        print("  no model call: the claim is out of scope")
    elif extract:
        served = (extraction.record.get("response") or {}).get("model", "?")
        print(f"  evidence extracted by {served} (effort {ex.EFFORT}), stop {extraction.stop_reason}, "
              f"request {extraction.record.get('request_id')}")
        print(f"  extraction log: {logs[0]}")
    for r in verdict.reasons:
        print(f"  reason ({r.kind}): {r.statement}")
        if r.source:
            print(f"     {data_dir / 'fixtures' / r.source.path}:{r.source.line}  [{r.source.version}]")
            print(f"     look for: {r.anchor}")
    for n, hop in enumerate(verdict.path, start=1):
        print(f"  {n}. {hop.statement}")
        if hop.source:
            print(f"     {data_dir / 'fixtures' / hop.source.path}:{hop.source.line}  [{hop.source.version}]")
            print(f"     look for: {hop.anchor}")
    for f in verdict.findings:
        print(f"  finding ({f.type}): {f.statement}")
        if f.source:
            print(f"     {data_dir / 'fixtures' / f.source.path}:{f.source.line}  [{f.source.version}]")
            print(f"     look for: {f.anchor}")
    return 0


def _warn_rejected(graph) -> None:
    rejected = sum(len(s.rejected) for s in graph.ingest)
    if rejected:
        print(f"warning: ingest rejected {rejected} source row{'s' if rejected > 1 else ''}; "
              "run `rcm-ai ingest` to see them", file=sys.stderr)


def _ingest(data_dir: Path) -> int:
    try:
        graph = build_graph(data_dir)
    except (IngestError, ProvenanceError) as e:
        print(f"ingest failed: {e}", file=sys.stderr)
        return 2
    for s in graph.ingest:
        print(f"{s.path}  [{s.table}]  parsed {s.parsed}  rejected {len(s.rejected)}  "
              f"loaded {s.loaded}  in graph {s.in_graph}")
        for r in s.rejected:
            print(f"    line {r.line}: {r.reason}")
    return 0


# Model id per arm, recorded with every run; the graph arm calls no model.
ARM_MODELS = {"graph": None, "extracted": ex.MODEL, "llm-only": ex.MODEL}
# Each model-calling arm logs its calls to eval-<stamp>-<suffix>/ in the output directory.
LOG_SUFFIXES = {"extracted": "extractions", "llm-only": "llm-only"}


def _served(extraction) -> dict:
    if extraction is None:
        return {"served_model": None, "request_id": None, "stop_reason": None}
    return {"served_model": (extraction.record.get("response") or {}).get("model"),
            "request_id": extraction.record.get("request_id"), "stop_reason": extraction.stop_reason}


def _canonical_sha256(value) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _sent_config(extractions: list, calls: int) -> dict:
    """The configuration read from the request bodies sent, not from constants or files that
    could change mid-run; a setting that varied is recorded as every value sent. Only calls
    that returned have a body to read, so calls and responses are counted apart."""
    # Opus 5.5 rejects temperature, so runs are not deterministic; effort and the exact
    # prompt and schema are what pin the configuration. The schema is hashed as canonical
    # JSON because the request carries it re-serialized.
    seen = {}
    for extraction in extractions:
        body = json.loads(extraction.record["http_request_body"])
        output = body.get("output_config") or {}
        for key, value in {
            "model": body.get("model"), "effort": output.get("effort"),
            "max_tokens": body.get("max_tokens"), "temperature": body.get("temperature"),
            "prompt_sha256": _canonical_sha256(body.get("system")),
            "schema_canonical_sha256": _canonical_sha256((output.get("format") or {}).get("schema")),
        }.items():
            seen.setdefault(key, set()).add(value)
    config = {key: next(iter(v)) if len(v) == 1 else sorted(v, key=str) for key, v in seen.items()}
    return {**config, "calls": calls, "responses": len(extractions)}


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


def _eval(data_dir: Path, out_dir: Path, arm_names: list[str] = ("graph",)) -> int:
    started = datetime.now(timezone.utc)
    stamp = started.strftime('%Y%m%dT%H%M%SZ')
    log_dirs = {arm: out_dir / f"eval-{stamp}-{suffix}" for arm, suffix in LOG_SUFFIXES.items()}
    responses = {arm: {} for arm in LOG_SUFFIXES}  # arm -> case id -> the call's Extraction
    called = {arm: set() for arm in LOG_SUFFIXES}
    try:
        unknown = sorted(set(arm_names) - ARM_MODELS.keys())
        if unknown:
            raise EvalError(f"unknown arm {', '.join(unknown)}; choose from {', '.join(ARM_MODELS)}")
        eval_set = load_eval_set(data_dir / "eval")
        graph = build_graph(data_dir)
        _warn_rejected(graph)
        client = _client() if set(arm_names) & LOG_SUFFIXES.keys() else None

        def calling(arm, c, call):
            called[arm].add(c.case.id)
            response = responses[arm][c.case.id] = call()
            _save_extraction(log_dirs[arm], c.case.id, response)  # before the case can fail
            return response

        def extracted(c):
            verdict, _ = ex.triage_note(c.case.claim, c.case.note, graph, lambda note, criteria: calling(
                "extracted", c, lambda: ex.extract(note, criteria, client)))
            return verdict

        def llm_only(c):
            # The criteria of each LCD whose HCPCS table lists the item: the policy the denial
            # falls under, as an analyst would look it up. Nothing else comes from the graph.
            policy = {lcd: graph.criteria(lcd) for lcd in graph.lcds_listing(c.case.claim.hcpcs)}
            return bl.verdict_from(calling("llm-only", c, lambda: bl.ask(c.case.claim, c.case.note, policy, client)))

        available = {"graph": lambda c: adjudicate(c.case.claim, c.case.evidence, graph),
                     "extracted": extracted, "llm-only": llm_only}
        report = run_eval(eval_set.cases, {name: available[name] for name in arm_names})
    except (EvalError, IngestError, ProvenanceError, CredentialError) as e:
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
    if len(report.arms) > 1:
        print(f"  arms disagree on {len(report.disagreements)} of {len(eval_set.cases)} cases")
        for d in report.disagreements:
            print(f"    {d.case} gold {d.gold}: " + ", ".join(f"{name} {r.outcome}" for name, r in d.arms.items()))
            for name, r in d.arms.items():
                if not r.correct:
                    for line in r.why:
                        print(f"      {name}: {line}")
    record = {
        "date": started.isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "eval_set": {"data_dir": str(data_dir.resolve()), "expected_cases": eval_set.expected,
                     "sha256": eval_set.sha256},
        "arms": {
            name: {"model": ARM_MODELS[name],
                   **(_sent_config(list(responses[name].values()), len(called[name])) if name in LOG_SUFFIXES else {}),
                   **asdict(m), "accuracy": m.accuracy,
                   "abstention_rate": m.abstention_rate, "abstention_precision": m.abstention_precision}
            for name, m in report.arms.items()
        },
        "results": [{**asdict(r), "correct": r.correct, "known_system_gap": gaps[r.case],
                     **({"model_called": r.case in called[r.arm], **_served(responses[r.arm].get(r.case))}
                        if r.arm in LOG_SUFFIXES else {})}
                    for r in report.results],
        "disagreements": [
            {"case": d.case, "gold": d.gold,
             "arms": {name: {"outcome": r.outcome, "correct": r.correct, "why": list(r.why)}
                      for name, r in d.arms.items()}}
            for d in report.disagreements],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"eval-{stamp}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"results: {path}")
    for arm, logged in responses.items():
        if logged:
            print(f"{arm} logs: {log_dirs[arm]}")
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
    triage.add_argument("--extract", action="store_true",
                        help="extract evidence from the note with the LLM instead of using the case's evidence")
    ev = sub.add_parser("eval", help="score the arms against the locked eval set")
    ev.add_argument("--data", type=Path, default=Path("data"))
    ev.add_argument("--out", type=Path, default=Path("eval-results"))
    ev.add_argument("--arms", default="graph",
                    help="comma-separated arms: graph (gold evidence), extracted (LLM evidence; calls the model), "
                         "llm-only (the model decides with no graph; calls the model)")
    ing = sub.add_parser("ingest", help="load the sources and report per-source record counts")
    ing.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    if args.command == "ingest":
        return _ingest(args.data)
    if args.command == "eval":
        return _eval(args.data, args.out, args.arms.split(","))
    return _triage(args.case, args.data, args.extract)


if __name__ == "__main__":
    sys.exit(main())
