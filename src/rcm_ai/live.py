"""Live mode (ADR 0002, 0006, 0008): one bundled case, its note optionally edited, run through the
real pipeline, with each step sent to the page as it finishes.

Three settings gate it, all on the server:
- LIVE_MODE must be "on". It is the off switch: any other value, or none, turns live mode off
  without touching the key or the code. "stand-in" runs the same steps with canned answers built
  from the case's answer key and no model call, so the page can be tried without a key.
- DEMO_ACCESS_CODE: the code a visitor types; compared in constant time.
- ANTHROPIC_API_KEY: read by the SDK only, never sent to the page or logged.

The model and its settings are the eval's own (extract.MODEL, EFFORT, MAX_TOKENS and the prompt
files), so a live run is the system the README measures.
"""

import hmac
import json
import os
import queue
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass

from rcm_ai import appeal as ap
from rcm_ai import extract as ex
from rcm_ai.extract import Extraction
from rcm_ai.graph import CoverageGraph, Criterion
from rcm_ai.schemas import Claim, Evidence, Finding, Hop, Reason, Verdict

# The longest bundled note is 343 characters; an edited note gets room, not a novel.
NOTE_LIMIT = 4000
# One overall deadline for the run, under Vercel Hobby's 300-second limit (ADR 0006).
DEADLINE = 240.0

OFF = "Live mode is switched off on this deployment. The recorded cases are on the page."
NOT_SET_UP = "Live mode is not set up on this deployment. The recorded cases are on the page."


def mode(env: Mapping[str, str] = os.environ) -> str:
    """"on", "stand-in" or "off", from LIVE_MODE alone."""
    value = env.get("LIVE_MODE", "").strip().lower()
    return value if value in ("on", "stand-in") else "off"


def status(env: Mapping[str, str] = os.environ) -> tuple[bool, str | None]:
    """Whether live mode can run, and if not, what to tell the visitor. A stand-in needs no key."""
    m = mode(env)
    if m == "off":
        return False, OFF
    if not env.get("DEMO_ACCESS_CODE") or (m == "on" and not env.get("ANTHROPIC_API_KEY")):
        return False, NOT_SET_UP
    return True, None


def code_ok(given: str | None, env: Mapping[str, str] = os.environ) -> bool:
    expected = env.get("DEMO_ACCESS_CODE", "")
    return bool(given) and bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


@dataclass(frozen=True)
class Backend:
    """The three model calls. Tests stand in for these; the SDK is never mocked."""
    extract: Callable[[str, list[Criterion]], Extraction]
    check: Callable[[list[ex.Pair]], Extraction]
    draft: Callable[[Claim, tuple[Hop, ...], list[tuple[str, str]], tuple[Finding, ...]], Extraction]


def anthropic_backend(timeout: float = DEADLINE) -> Backend:
    import anthropic

    client = anthropic.Anthropic(timeout=timeout, max_retries=1)
    return Backend(
        extract=lambda note, criteria: ex.extract(note, criteria, client),
        check=lambda pairs: ex.check_support(pairs, client),
        draft=lambda claim, path, quotes, findings: ap.draft(claim, path, quotes, client, findings=findings),
    )


STAND_IN = "stand-in, no model"


def _walk(criteria) -> list[Criterion]:
    return [x for c in criteria for x in (c, *_walk(c.any_of))]


def stand_in_backend(evidence: Evidence, pause: float = 1.0) -> Backend:
    """Canned answers from the case's answer key, after a short pause so the steps arrive one by
    one. The extraction is the gold evidence, the span check passes every span, and the letter
    restates the verdict's own steps, so its citations are what the graph established. An edited
    note isn't read: a gold span it no longer holds shows as a problem, as it would for a model."""
    served = {"response": {"model": STAND_IN}}

    def extract(note, criteria):
        time.sleep(pause)
        # The answer key may fail a criterion with alternatives as a whole; the extraction schema
        # wants each alternative named, as a model is asked to, so each takes the same span.
        offered = {c.id: c for c in _walk(criteria)}
        not_met = []
        for c in evidence.criteria_not_met:
            parent = offered.get(c.criterion)
            ids = [a.id for a in parent.any_of] if parent and parent.any_of else [c.criterion]
            not_met += [{"criterion": i, "span": c.span} for i in ids]
        return Extraction({
            "diagnoses": [{"code": d.code, "span": d.span} for d in evidence.diagnoses],
            "criteria_met": [{"criterion": c.criterion, "span": c.span} for c in evidence.criteria],
            "criteria_not_met": not_met,
        }, "end_turn", served)

    def check(pairs):
        time.sleep(pause)
        return Extraction({"items": [{"item": n, "supported": True, "reason": "stand-in"}
                                     for n in range(1, len(pairs) + 1)]}, "end_turn", served)

    def draft(claim, path, quotes, findings):
        time.sleep(pause)
        steps = " ".join(ap.asserted(h).rstrip(".") + "." for h in path)
        cited = "".join(f'\n- "{span}"' for _, span in quotes)
        letter = (f"We ask that the denial of {claim.hcpcs} be overturned. {steps}"
                  + (f"\n\nThe record supports this:{cited}" if cited else "")
                  + "\n\n(Stand-in letter: written from the verdict's steps, not by a model.)")
        return Extraction({"letter": letter}, "end_turn", served)

    return Backend(extract, check, draft)


class Cancelled(Exception):
    """The deadline passed; no further model call is made."""


def _served(e: Extraction) -> dict:
    response = e.record.get("response") or {}
    return {"model": response.get("model"), "stop_reason": e.stop_reason}


def run(claim: Claim, note: str, graph: CoverageGraph, backend: Backend, emit: Callable[[dict], None],
        cancelled: threading.Event | None = None) -> None:
    """The pipeline the CLI's `triage --extract --draft` runs, emitting one event per step."""
    cancelled = cancelled or threading.Event()
    calls = 0

    def before_call():
        nonlocal calls
        if cancelled.is_set():
            raise Cancelled()
        calls += 1

    def extractor(text: str, criteria: list[Criterion]) -> Extraction:
        emit({"event": "extraction_started"})
        before_call()
        extraction = backend.extract(text, criteria)
        if extraction.raw is None:
            evidence, problems = None, [f"the model stopped with {extraction.stop_reason} and returned no evidence"]
        else:
            parsed = ex.parse_extraction(extraction.raw, text, graph, criteria)
            evidence = asdict(parsed.evidence) if parsed.evidence else None
            problems = parsed.problems
        emit({"event": "extraction_done", **_served(extraction), "evidence": evidence, "problems": problems})
        return extraction

    def checker(pairs: list[ex.Pair]) -> Extraction:
        emit({"event": "support_check_started", "items": len(pairs)})
        before_call()
        response = backend.check(pairs)
        emit({"event": "support_check_done", **_served(response), "unsupported": ex.unsupported(response, pairs)})
        return response

    verdict, extraction = ex.triage_note(claim, note, graph, extractor, checker)
    emit({"event": "verdict", "verdict": asdict(verdict)})
    if verdict.outcome == "overturnable":
        evidence = ex.extracted_evidence(claim, note, graph, extraction)
        emit({"event": "draft_started"})
        before_call()
        response = backend.draft(claim, verdict.path, ap.quotes(evidence), verdict.findings)
        letter = ap.letter_of(response)
        check = ap.check_citations(letter, verdict.path, verdict.findings) if letter else None
        emit({"event": "draft_done", **_served(response), "letter": letter,
              "check": None if check is None else {"passed": check.passed, "citations": list(check.citations),
                                                   "unsupported": list(check.unsupported)}})
    emit({"event": "done", "model_calls": calls})


def _line(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode()


def stream(claim: Claim, note: str, graph: CoverageGraph, backend: Backend,
           deadline: float = DEADLINE) -> Iterator[bytes]:
    """NDJSON events from a run in a worker thread. Past the deadline the run is cancelled before
    its next model call, and an undecided claim goes to a human, as a refused response does."""
    events: queue.Queue = queue.Queue()
    cancelled = threading.Event()

    def work():
        try:
            run(claim, note, graph, backend, events.put, cancelled)
        except Cancelled:
            pass
        except Exception as e:  # an API error, a timeout or a bug: the page shows it as the run's end
            events.put({"event": "error", "detail": f"{type(e).__name__}: {e}"[:500]})
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()
    end = time.monotonic() + deadline
    decided = False
    while True:
        try:
            event = events.get(timeout=max(end - time.monotonic(), 0.001))
        except queue.Empty:
            cancelled.set()
            if decided:
                yield _line({"event": "error", "detail": "The appeal draft didn't finish in time."})
            else:
                yield _line({"event": "verdict", "verdict": asdict(Verdict("needs_human", (), reasons=(Reason(
                    "timed_out", "The model didn't answer in time, so no evidence was used"),)))})
            return
        if event is None:
            return
        decided = decided or event["event"] == "verdict"
        yield _line(event)
