import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from rcm_ai.adjudicate import adjudicate
from rcm_ai import appeal as ap
from rcm_ai import baseline as bl
from rcm_ai.evaluate import EvalError, load_eval_set, run_eval
from rcm_ai import extract as ex
from rcm_ai.graph import IngestError, build_graph
from rcm_ai.provenance import ProvenanceError
from rcm_ai.schemas import SchemaError, load_case
from rcm_ai import stats


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


def _triage(case_path: Path, data_dir: Path, extract: bool = False, draft: bool = False,
            log_dir: Path = Path("eval-results/extractions"), draft_dir: Path = Path("eval-results/drafts"),
            check_dir: Path = Path("eval-results/support-checks")) -> int:
    appeal = draft_error = log_error = None
    checks = []
    try:
        case = load_case(case_path)
        graph = build_graph(data_dir)
        client = None
        if extract:
            client = _client()
            logs = []

            def call(note, criteria):
                extraction = ex.extract(note, criteria, client)
                logs.append(_save_extraction(log_dir, case.id, extraction))  # before the case can fail
                return extraction

            def check(pairs):
                response = ex.check_support(pairs, client)
                checks.append((response, _save_extraction(check_dir, case.id, response)))
                return response

            verdict, extraction = ex.triage_note(case.claim, case.note, graph, call, check)
            evidence = ex.extracted_evidence(case.claim, case.note, graph, extraction) if draft else None
        elif case.evidence is None:
            raise SchemaError(f"{case_path}: case has no evidence; run with --extract to extract it from the note")
        else:
            verdict, evidence = adjudicate(case.claim, case.evidence, graph), case.evidence
        if draft and verdict.outcome == "overturnable":
            try:
                if client is None:
                    client = _client()
                appeal = ap.draft(case.claim, verdict.path, ap.quotes(evidence), client, findings=verdict.findings)
            except Exception as e:  # the verdict needs no model, so it's still reported
                draft_error = f"{type(e).__name__}: {e}"
            else:
                try:
                    draft_log = _save_extraction(draft_dir, case.id, appeal)
                except OSError as e:  # the letter is paid for, so it's still printed
                    log_error = f"{type(e).__name__}: {e}"
    except SchemaError as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 2
    except (IngestError, ProvenanceError) as e:
        print(f"ingest failed: {e}", file=sys.stderr)
        return 2
    except CredentialError as e:
        print(f"model unavailable: {e}", file=sys.stderr)
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
        for response, log in checks:
            served = (response.record.get("response") or {}).get("model", "?")
            print(f"  spans checked by {served} (effort {ex.EFFORT}), stop {response.stop_reason}, "
                  f"request {response.record.get('request_id')}")
            print(f"  support check log: {log}")
    _print_steps(verdict, data_dir)
    if draft_error:
        print(f"appeal draft failed: {draft_error}", file=sys.stderr)
        return 3
    if draft and appeal is None:
        print("  no appeal drafted: the verdict is not overturnable")
    elif draft:
        letter = ap.letter_of(appeal)
        if letter is None:
            print(f"  no appeal letter: the drafter returned none (stop {appeal.stop_reason})")
        else:
            print("  appeal draft:")
            check = _print_letter(letter, verdict)
        if log_error:
            print(f"appeal draft log not written: {log_error}", file=sys.stderr)
        else:
            print(f"  draft log: {draft_log}")
        if letter is not None and not check.passed:
            return 4  # a failing letter must not be sent, whatever else went wrong
        if letter is None or log_error:
            return 3
    return 0


def _print_steps(verdict, data_dir: Path) -> None:
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
        else:
            print("     unsourced: no source row backs this step")
    for f in verdict.findings:
        print(f"  finding ({f.type}): {f.statement}")
        if f.source:
            print(f"     {data_dir / 'fixtures' / f.source.path}:{f.source.line}  [{f.source.version}]")
            print(f"     look for: {f.anchor}")


def _print_letter(letter: str, verdict) -> ap.CitationCheck:
    for line in letter.splitlines():
        print(f"    {line}".rstrip())
    check = ap.check_citations(letter, verdict.path, verdict.findings)
    cited = ", ".join(check.citations) or "nothing cited"
    if check.passed:
        print(f"  citation check: passed ({cited})")
    else:
        print(f"  citation check: FAILED, unsupported: {', '.join(check.unsupported)} (cited: {cited})")
    return check


def _load_demo(data_dir: Path):
    path = data_dir / "demo" / "demo.json"
    try:
        demo = json.loads(path.read_text())
        case_ids, letters = demo["cases"], demo["letters"]
        if not isinstance(case_ids, list) or not isinstance(letters, dict):
            raise TypeError("cases must be a list and letters a mapping")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SchemaError(f"{path} is not a demo file: {type(e).__name__}: {e}") from e
    cases = [load_case(data_dir / "eval" / "cases" / f"{case_id}.json") for case_id in case_ids]
    for case in cases:
        if case.evidence is None:
            raise SchemaError(f"{case.id}: a demo case runs on its own evidence, and it has no evidence block")
    return cases, letters


def _demo(data_dir: Path) -> int:
    try:
        cases, letters = _load_demo(data_dir)
        graph = build_graph(data_dir)
    except SchemaError as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 2
    except (IngestError, ProvenanceError) as e:
        print(f"ingest failed: {e}", file=sys.stderr)
        return 2
    _warn_rejected(graph)
    failed = False
    for case in cases:
        verdict = adjudicate(case.claim, case.evidence, graph)
        claim = case.claim
        print(f"{case.id}: {verdict.outcome}")
        print(f"  claim: {claim.hcpcs} for {', '.join(claim.icd10)}, denied CARC {claim.carc} ({claim.coverage} coverage)")
        print(f"  note: {case.note}")
        print("  evidence (the case's own, as a perfect extractor would return it):")
        for d in case.evidence.diagnoses:
            print(f'    {d.code}: "{d.span}"')
        for c in case.evidence.criteria:
            print(f'    {c.criterion} met: "{c.span}"')
        for c in case.evidence.criteria_not_met:
            print(f'    {c.criterion} not met: "{c.span}"')
        _print_steps(verdict, data_dir)
        recorded = letters.get(case.id)
        if verdict.outcome == "overturnable" and recorded:
            print(f"  appeal letter, recorded {recorded['recorded']} from {recorded['model']} at {recorded['git_sha']} "
                  f"({recorded['arm']} arm), not drafted now:")
            failed |= not _print_letter(recorded["letter"], verdict).passed
        elif verdict.outcome == "overturnable":
            print("  no recorded letter: `rcm-ai triage <case> --draft` drafts one (calls the model)")
        elif recorded:
            print("  recorded letter FAILED: the verdict is not overturnable, so no letter is warranted")
            failed = True
        print()
    return 4 if failed else 0  # as in triage, a failing letter must not be sent


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
# The arms that run this system; a case's known_system_gap explains their misses, not the baseline's.
SYSTEM_ARMS = {"graph", "extracted"}


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
    stops = Counter(e.stop_reason for e in extractions)
    return {**config, "calls": calls, "responses": len(extractions),
            "stop_reasons": dict(sorted(stops.items(), key=lambda item: str(item[0])))}


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


def _eval(data_dir: Path, out_dir: Path, arm_names: list[str] = ("graph",), draft: bool = False) -> int:
    started = datetime.now(timezone.utc)
    stamp = started.strftime('%Y%m%dT%H%M%SZ')
    log_dirs = {arm: out_dir / f"eval-{stamp}-{suffix}" for arm, suffix in LOG_SUFFIXES.items()}
    responses = {arm: {} for arm in LOG_SUFFIXES}  # arm -> case id -> the call's Extraction
    called = {arm: set() for arm in LOG_SUFFIXES}
    # Cases whose response the arm couldn't use (refused, truncated, not JSON, or for the
    # baseline not a verdict). They score as hand-offs, so they're counted and warned about.
    unusable = {arm: set() for arm in LOG_SUFFIXES}
    check_dir = out_dir / f"eval-{stamp}-support"
    checks = {}  # case id -> the extracted arm's support check
    check_calls = set()
    draft_dir = out_dir / f"eval-{stamp}-drafts"
    appeals = {arm: {} for arm in arm_names} if draft else {}  # arm -> case id -> Appeal
    drafts = []  # every drafting call's response
    draft_calls = []  # the case of every drafting call made
    no_reference = {arm: set() for arm in arm_names}  # cases gold overturns but the graph won't, on gold evidence
    try:
        unknown = sorted(set(arm_names) - ARM_MODELS.keys())
        if unknown:
            raise EvalError(f"unknown arm {', '.join(unknown)}; choose from {', '.join(ARM_MODELS)}")
        eval_set = load_eval_set(data_dir / "eval")
        graph = build_graph(data_dir)
        _warn_rejected(graph)
        client = _client() if draft or set(arm_names) & LOG_SUFFIXES.keys() else None
        def appeal_for(arm, c, verdict, evidence):
            reference = None
            try:
                # What a letter for this claim may cite: nothing when the gold label doesn't overturn
                # it, else what the graph's verdict on the case's gold evidence asserts. Every arm's
                # letter is checked against it, so a claim gold overturns but the graph won't gets none.
                reference_path, reference_findings = (), ()
                if c.gold["verdict"] == "overturnable":
                    gold = adjudicate(c.case.claim, c.case.evidence, graph)
                    if gold.outcome != "overturnable":
                        return None
                    reference_path, reference_findings = gold.path, gold.findings
                reference = ap.established(reference_path, reference_findings)
                draft_calls.append(c.case.id)
                response = ap.draft(c.case.claim, verdict.path, ap.quotes(evidence), client, findings=verdict.findings)
                drafts.append(response)
            except Exception as e:  # a failed draft is counted; it must not change the verdict's score
                return ap.Appeal(reference, error=f"{type(e).__name__}: {e}")
            log_error = None
            try:
                _save_extraction(draft_dir, f"{arm}-{c.case.id}", response)
            except Exception as e:  # the letter is paid for, so it's still checked and recorded
                log_error = f"{type(e).__name__}: {e}"
            letter = ap.letter_of(response)
            check = ap.check_citations(letter, reference_path, reference_findings) if letter else None
            return ap.Appeal(reference, letter, check, log_error=log_error)

        def drafting(arm, decide):
            def run(c):
                verdict, evidence = decide(c)
                if draft and verdict.outcome == "overturnable":
                    if (appeal := appeal_for(arm, c, verdict, evidence)) is None:
                        no_reference[arm].add(c.case.id)
                    else:
                        appeals[arm][c.case.id] = appeal
                return verdict
            return run

        def calling(arm, c, call):
            called[arm].add(c.case.id)
            response = responses[arm][c.case.id] = call()
            _save_extraction(log_dirs[arm], c.case.id, response)  # before the case can fail
            if response.raw is None:
                unusable[arm].add(c.case.id)
            return response

        def checking(c, pairs):
            check_calls.add(c.case.id)
            response = checks[c.case.id] = ex.check_support(pairs, client)
            _save_extraction(check_dir, c.case.id, response)  # before the case can fail
            return response

        def extracted(c):
            verdict, extraction = ex.triage_note(c.case.claim, c.case.note, graph, lambda note, criteria: calling(
                "extracted", c, lambda: ex.extract(note, criteria, client)), lambda pairs: checking(c, pairs))
            return verdict, ex.extracted_evidence(c.case.claim, c.case.note, graph, extraction) if draft else None

        def llm_only(c):
            # The criteria of each LCD whose HCPCS table lists the item: the policy the denial
            # falls under, as an analyst would look it up. Nothing else comes from the graph.
            policy = {lcd: graph.criteria(lcd) for lcd in graph.lcds_listing(c.case.claim.hcpcs)}
            response = calling("llm-only", c, lambda: bl.ask(c.case.claim, c.case.note, policy, client))
            verdict = bl.verdict_from(response)
            if any(r.kind == "no_verdict" for r in verdict.reasons):
                unusable["llm-only"].add(c.case.id)
            return verdict, bl.evidence_from(response)

        available = {"graph": lambda c: (adjudicate(c.case.claim, c.case.evidence, graph), c.case.evidence),
                     "extracted": extracted, "llm-only": llm_only}
        report = run_eval(eval_set.cases, {name: drafting(name, available[name]) for name in arm_names})
    except (EvalError, IngestError, ProvenanceError, CredentialError) as e:
        print(f"eval failed: {e}", file=sys.stderr)
        return 2
    print(f"eval: {len(eval_set.cases)} cases ran (expected {eval_set.expected})")
    tallies = {name: {**ap.tally(list(appeals[name].values())), "no_reference": len(no_reference[name])}
               for name in appeals}
    unreferenced = sorted(set().union(*no_reference.values())) if draft else []
    for name, m in report.arms.items():
        print(f"  {name}: accuracy {m.correct}/{m.n} ({_pct(m.accuracy)}), errors {m.errors}")
        print(f"    false overturnable: {len(m.false_overturnable)} {m.false_overturnable}")
        print(f"    false uphold: {len(m.false_uphold)} {m.false_uphold}")
        print(f"    abstention rate: {m.abstentions}/{m.n} ({_pct(m.abstention_rate)}), "
              f"precision {m.correct_abstentions}/{m.abstentions} ({_pct(m.abstention_precision)})")
        if draft:
            a = tallies[name]
            print(f"    appeals: {a['attempted']} attempted: {a['letters']} letters ({a['failed']} failed the citation "
                  f"check), {a['without_letter']} without a letter, {a['errors']} errors; "
                  f"unsupported citations {a['unsupported']}/{a['citations']} ({_pct(a['unsupported_rate'])})"
                  + (f"; {a['no_reference']} without a reference" if a["no_reference"] else "")
                  + (f"; {a['log_errors']} logs not written" if a["log_errors"] else ""))
    gaps = {c.case.id: c.gold.get("known_system_gap") for c in eval_set.cases}
    for r in report.results:
        if not r.correct:
            note = f"  [{'known gap' if gaps[r.case] else 'UNEXPLAINED'}]" if r.arm in SYSTEM_ARMS else ""
            print(f"    {r.arm} {r.case}: gold {r.gold}, got {r.outcome}{note}")
    if len(report.arms) > 1:
        print(f"  arms disagree on {len(report.disagreements)} of {len(eval_set.cases)} cases")
        for d in report.disagreements:
            print(f"    {d.case} gold {d.gold}: " + ", ".join(f"{name} {r.outcome}" for name, r in d.arms.items()))
            for name, r in d.arms.items():
                if not r.correct:
                    for line in r.why:
                        print(f"      {name}: {line}")
    sent = {name: {**_sent_config(list(responses[name].values()), len(called[name])),
                   "without_output": len(unusable[name])}
            for name in report.arms if name in LOG_SUFFIXES}
    if "extracted" in sent:
        sent["extracted"]["support_check"] = {**_sent_config(list(checks.values()), len(check_calls)),
                                              "without_output": sum(r.raw is None for r in checks.values())}
    record = {
        "date": started.isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "eval_set": {"data_dir": str(data_dir.resolve()), "expected_cases": eval_set.expected,
                     "sha256": eval_set.sha256},
        "arms": {
            name: {"model": ARM_MODELS[name],
                   **sent.get(name, {}),
                   **({"appeals": tallies[name]} if draft else {}),
                   **asdict(m), "accuracy": m.accuracy,
                   "abstention_rate": m.abstention_rate, "abstention_precision": m.abstention_precision}
            for name, m in report.arms.items()
        },
        "results": [{**asdict(r), "correct": r.correct,
                     "known_system_gap": gaps[r.case] if r.arm in SYSTEM_ARMS else None,
                     **({"model_called": r.case in called[r.arm], **_served(responses[r.arm].get(r.case))}
                        if r.arm in LOG_SUFFIXES else {}),
                     **({"support_check": {"called": r.case in check_calls, **_served(checks.get(r.case))}}
                        if r.arm == "extracted" else {}),
                     **({"appeal": _appeal_record(appeals[r.arm][r.case])} if r.case in appeals.get(r.arm, {}) else {})}
                    for r in report.results],
        "disagreements": [
            {"case": d.case, "gold": d.gold,
             "arms": {name: {"outcome": r.outcome, "correct": r.correct, "why": list(r.why)}
                      for name, r in d.arms.items()}}
            for d in report.disagreements],
        **({"drafting": {**_sent_config(drafts, len(draft_calls)), "no_reference": unreferenced}}
           if draft else {}),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"eval-{stamp}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"results: {path}")
    for arm, s in sent.items():
        stops = ", ".join(f"{k} {n}" for k, n in s["stop_reasons"].items())
        print(f"{arm}: {s['calls']} model calls, {s['responses']} responses, {s['without_output']} without output"
              + (f" (stop reasons: {stops})" if stops else ""))
        if s["responses"]:
            print(f"  logs: {log_dirs[arm]}")
        if s["without_output"]:
            print(f"warning: {arm}: {s['without_output']} of {s['responses']} responses had no usable output",
                  file=sys.stderr)
        if check := s.get("support_check"):
            print(f"{arm} support check: {check['calls']} model calls, {check['responses']} responses, "
                  f"{check['without_output']} without output")
            if check["responses"]:
                print(f"  logs: {check_dir}")
    if draft:
        print(f"drafting: {len(draft_calls)} model calls, {len(drafts)} responses")
        if drafts:
            print(f"  logs: {draft_dir}")
        no_letter = sum(a["without_letter"] for a in tallies.values())
        if no_letter:
            print(f"warning: {no_letter} of {len(draft_calls)} appeal drafts had no letter", file=sys.stderr)
        if unreferenced:
            print(f"warning: no appeal drafted for {', '.join(unreferenced)}: gold overturns it but the graph's "
                  "verdict on the gold evidence doesn't, so there's no reference to check against", file=sys.stderr)
    errors = sum(m.errors for m in report.arms.values())
    draft_errors = sum(a["errors"] for a in tallies.values())
    log_errors = sum(a["log_errors"] for a in tallies.values())
    if errors:
        print(f"eval incomplete: {errors} arm error{'s' if errors > 1 else ''}; metrics are not comparable",
              file=sys.stderr)
    if draft_errors:
        print(f"eval incomplete: {draft_errors} appeal draft error{'s' if draft_errors > 1 else ''}; "
              "citation metrics are not comparable", file=sys.stderr)
    if log_errors:
        print(f"eval incomplete: {log_errors} appeal draft log{'s' if log_errors > 1 else ''} not written; "
              "the letters and their checks are in the record", file=sys.stderr)
    return 3 if errors or draft_errors or log_errors else 0


def _appeal_record(a) -> dict:
    return {"letter": a.letter, "passed": a.check.passed if a.check else None,
            "citations": list(a.check.citations) if a.check else [],
            "unsupported": list(a.check.unsupported) if a.check else [],
            "reference": list(a.reference) if a.reference is not None else None,
            "error": a.error, "log_error": a.log_error}


def _stats(paths: list[Path], allow_dirty: bool) -> int:
    try:
        records = stats.load(paths, allow_dirty)
        lines = stats.report(records)
    except stats.StatsError as e:
        print(f"stats refused: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rcm-ai")
    sub = parser.add_subparsers(dest="command", required=True)
    triage = sub.add_parser("triage", help="adjudicate one synthetic denied claim")
    triage.add_argument("case", type=Path)
    triage.add_argument("--data", type=Path, default=Path("data"))
    triage.add_argument("--extract", action="store_true",
                        help="extract evidence from the note with the LLM instead of using the case's evidence")
    triage.add_argument("--draft", action="store_true",
                        help="draft an appeal for an overturnable verdict and check its citations (calls the model)")
    ev = sub.add_parser("eval", help="score the arms against the locked eval set")
    ev.add_argument("--data", type=Path, default=Path("data"))
    ev.add_argument("--out", type=Path, default=Path("eval-results"))
    ev.add_argument("--arms", default="graph",
                    help="comma-separated arms: graph (gold evidence), extracted (LLM evidence; calls the model), "
                         "llm-only (the model decides with no graph; calls the model)")
    ev.add_argument("--draft", action="store_true",
                    help="draft an appeal for each overturnable verdict and score its citations (calls the model)")
    ing = sub.add_parser("ingest", help="load the sources and report per-source record counts")
    ing.add_argument("--data", type=Path, default=Path("data"))
    demo = sub.add_parser("demo", help="walk three bundled cases end to end, one per verdict (no model call)")
    demo.add_argument("--data", type=Path, default=Path("data"))
    st = sub.add_parser("stats", help="report each arm's accuracy across eval run records of one run (no model call)")
    st.add_argument("records", type=Path, nargs="+", help="eval run records (eval-<stamp>.json), each a repeat")
    st.add_argument("--allow-dirty", action="store_true", help="accept records that ran on uncommitted code")
    args = parser.parse_args(argv)
    if args.command == "stats":
        return _stats(args.records, args.allow_dirty)
    if args.command == "ingest":
        return _ingest(args.data)
    if args.command == "demo":
        return _demo(args.data)
    if args.command == "eval":
        return _eval(args.data, args.out, args.arms.split(","), args.draft)
    return _triage(args.case, args.data, args.extract, args.draft)


if __name__ == "__main__":
    sys.exit(main())
