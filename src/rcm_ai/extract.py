"""LLM evidence extraction: note text in, evidence the graph can adjudicate out.

The model only reads the note. Whatever it returns is checked against the note and the
graph before use, then a second call checks that each span, read in its sentence, supports
the code or criterion it's attached to. One ungrounded or unsupported item sends the claim to
a human, because dropping just that item could flip the verdict.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rcm_ai.adjudicate import adjudicate, applicable_lcds, billed_coding_conflict, out_of_scope
from rcm_ai.codes import is_icd10cm
from rcm_ai.graph import CoverageGraph, Criterion
from rcm_ai.schemas import Claim, CriterionEvidence, DocumentedCode, Evidence, Reason, Verdict

PROMPTS = Path(__file__).parent / "prompts"
PROMPT = PROMPTS / "extract_evidence.md"
SCHEMA = PROMPTS / "extract_evidence.schema.json"
SUPPORT_PROMPT = PROMPTS / "check_support.md"
SUPPORT_SCHEMA = PROMPTS / "check_support.schema.json"
MODEL = "claude-opus-5-5"
# Opus 5.5 takes no temperature and can't turn thinking off; effort is the only control
# (its default is medium). Criteria judgments like "level 2 hypoglycemia" justify high.
EFFORT = "high"
MAX_TOKENS = 16000
# The shortest gold span is 11 characters ("Prediabetes"); shorter text like "CGM" or
# "insulin" matches too many places in a note to show anything.
MIN_SPAN = 10


@dataclass(frozen=True)
class Extraction:
    raw: dict | None  # the parsed JSON output, or None when the model stopped early or returned no JSON
    stop_reason: str
    record: dict  # what was sent and received, for the run log


@dataclass(frozen=True)
class Span:
    field: str
    key: str  # the code or criterion id
    span: str
    start: int
    end: int


@dataclass(frozen=True)
class Parsed:
    evidence: Evidence | None
    problems: list[str] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)


@dataclass(frozen=True)
class Pair:
    """An extracted span with what it's claimed to show, for the support check."""
    span: Span
    text: str  # the code's ICD-10-CM title or the criterion's policy text
    sentence: str  # the note sentence holding the span, or the sentences it crosses


Extractor = Callable[[str, list[Criterion]], Extraction]
SupportChecker = Callable[[list[Pair]], Extraction]


def describe_criteria(criteria: list[Criterion], depth: int = 0) -> list[str]:
    lines = []
    for c in criteria:
        note = " (decided from the diagnosis code)" if c.satisfied_by == "covered_diagnosis" else ""
        alternatives = " Met by any one of these alternatives:" if c.any_of else ""
        lines.append(f"{'  ' * depth}- {c.id}{note}: \"{c.quote}\"{alternatives}")
        lines.extend(describe_criteria(list(c.any_of), depth + 1))
    return lines


def describe_claim(claim: Claim) -> str:
    return (f"Claim:\n- billed item (HCPCS): {claim.hcpcs}\n"
            f"- billed diagnoses (ICD-10-CM): {', '.join(claim.icd10)}\n"
            f"- denial reason: CARC {claim.carc}\n"
            f"- coverage: {claim.coverage}")


def build_request(note: str, criteria: list[Criterion]) -> dict:
    """Keyword arguments for messages.create. Criteria are top-level ones; alternatives nest."""
    content = (
        "Coverage criteria (id: policy text):\n" + "\n".join(describe_criteria(criteria))
        + f"\n\nNote:\n<note>\n{note}\n</note>"
    )
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": PROMPT.read_text(),
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": EFFORT, "format": {"type": "json_schema", "schema": json.loads(SCHEMA.read_text())}},
    }


def extract(note: str, criteria: list[Criterion], client) -> Extraction:
    return send(build_request(note, criteria), client)


def send(request: dict, client) -> Extraction:
    """One live model call. The record keeps the exact request bytes sent, for the wire check."""
    raw = client.messages.with_raw_response.create(**request)
    message = raw.parse()
    record = {
        "request_id": raw.request_id,
        "http_request_body": raw.http_request.content.decode(),
        "response": message.to_dict(),
    }
    data = None
    if message.stop_reason == "end_turn":
        text = next((b.text for b in message.content if b.type == "text"), None)
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            data = None
    return Extraction(data, message.stop_reason, record)


def _items(raw: dict, name: str, key: str) -> list[tuple[str, str]] | None:
    items = raw.get(name)
    if not isinstance(items, list) or not all(
        isinstance(i, dict) and isinstance(i.get(key), str) and isinstance(i.get("span"), str) for i in items
    ):
        return None
    return [(i[key], i["span"]) for i in items]


def _walk(criteria) -> list[Criterion]:
    return [x for c in criteria for x in (c, *_walk(c.any_of))]


def _code_problem(code: str, graph: CoverageGraph) -> str | None:
    if not is_icd10cm(code):
        return f"diagnosis {code!r} is not an ICD-10-CM code"
    if graph.title(code) is not None:
        return None
    # Slices load whole categories, so a code missing from a loaded category doesn't exist.
    if graph.title(code[:3]) is not None:
        return f"diagnosis {code!r} is not a code in the loaded ICD-10-CM category {code[:3]}"
    return f"diagnosis {code!r} is outside the loaded ICD-10-CM slices, so the graph can't check it"


def parse_extraction(raw: dict, note: str, graph: CoverageGraph, criteria: list[Criterion]) -> Parsed:
    """Check an extraction against the note, the terminology and the criteria offered for the claim."""
    if not isinstance(raw, dict):
        return Parsed(None, ["response does not match the extraction schema: not an object"])
    fields = {name: _items(raw, name, key) for name, key in
              (("diagnoses", "code"), ("criteria_met", "criterion"), ("criteria_not_met", "criterion"))}
    bad = [name for name, items in fields.items() if items is None]
    if bad:
        return Parsed(None, [f"response does not match the extraction schema: {', '.join(bad)}"])

    problems, spans = [], []
    offered = {c.id: c for c in _walk(criteria)}
    for name, items in fields.items():
        for key, span in items:
            if name == "diagnoses":
                if problem := _code_problem(key, graph):
                    problems.append(problem)
            elif key not in offered:
                problems.append(f"criterion {key!r} was not offered for this claim")
            elif name == "criteria_met" and offered[key].satisfied_by == "covered_diagnosis":
                problems.append(f"criterion {key!r} is decided from the diagnosis code, so it can't be listed as met")
            elif name == "criteria_not_met" and offered[key].any_of:
                # One span can rule out one alternative only; naming each keeps the model from
                # failing the parent while the note documents another alternative.
                problems.append(f"criterion {key!r} has alternatives; record each alternative the note shows is not met")
            start = note.find(span)
            if len(span.strip()) < MIN_SPAN:
                problems.append(f"{key} span {span!r} is shorter than {MIN_SPAN} characters")
            elif start < 0:
                problems.append(f"{key} span {span!r} is not in the note")
            else:
                spans.append(Span(name, key, span, start, start + len(span)))
    if problems:
        return Parsed(None, problems, spans)
    return Parsed(Evidence(
        tuple(DocumentedCode(k, s) for k, s in fields["diagnoses"]),
        tuple(CriterionEvidence(k, s) for k, s in fields["criteria_met"]),
        tuple(CriterionEvidence(k, s) for k, s in fields["criteria_not_met"]),
    ), [], spans)


# A sentence ends at terminal punctuation followed by whitespace, so "3.0mmol/L" stays whole.
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")


def sentence_around(note: str, start: int, end: int) -> str:
    """The note text from the sentence break before the span to the one after it."""
    breaks = [m.end() for m in _SENTENCE_END.finditer(note, 0, start)]
    after = _SENTENCE_END.search(note, max(start, end - 1))
    return note[breaks[-1] if breaks else 0:after.end() if after else len(note)].strip()


def support_pairs(spans: list[Span], note: str, graph: CoverageGraph, criteria: list[Criterion]) -> list[Pair]:
    offered = {c.id: c for c in _walk(criteria)}
    return [Pair(s, graph.title(s.key) if s.field == "diagnoses" else offered[s.key].quote,
                 sentence_around(note, s.start, s.end)) for s in spans]


_CLAIMED = {"diagnoses": "diagnosis {}", "criteria_met": "criterion {} met", "criteria_not_met": "criterion {} not met"}


def build_support_request(pairs: list[Pair]) -> dict:
    """Keyword arguments for messages.create: each pair's span in its sentence, never the whole note,
    so the check judges that span and can't find support elsewhere."""
    items = [f"Item {n}: {_CLAIMED[p.span.field].format(p.span.key)}\n"
             f"  {'ICD-10-CM title' if p.span.field == 'diagnoses' else 'policy text'}: {json.dumps(p.text)}\n"
             f"  span: {json.dumps(p.span.span)}\n  sentence: {json.dumps(p.sentence)}"
             for n, p in enumerate(pairs, 1)]
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SUPPORT_PROMPT.read_text(),
        "messages": [{"role": "user", "content": "\n\n".join(items)}],
        "output_config": {"effort": EFFORT, "format": {"type": "json_schema", "schema": json.loads(SUPPORT_SCHEMA.read_text())}},
    }


def check_support(pairs: list[Pair], client) -> Extraction:
    return send(build_support_request(pairs), client)


def unsupported(check: Extraction, pairs: list[Pair]) -> list[str] | None:
    """The pairs the check judged unsupported, each with its reason; None when the check
    didn't judge every pair exactly once."""
    items = check.raw.get("items") if isinstance(check.raw, dict) else None
    if not isinstance(items, list) or not all(
        isinstance(i, dict) and type(i.get("item")) is int and isinstance(i.get("supported"), bool)
        and isinstance(i.get("reason"), str) for i in items
    ) or sorted(i["item"] for i in items) != list(range(1, len(pairs) + 1)):
        return None
    return [f"Span {p.span.span!r} doesn't support {_CLAIMED[p.span.field].format(p.span.key)} ({p.text!r}): {i['reason']}"
            for i in items if not i["supported"] for p in [pairs[i["item"] - 1]]]


def _offered(claim: Claim, graph: CoverageGraph) -> list[Criterion]:
    return [c for lcd in applicable_lcds(claim, graph) for c in graph.criteria(lcd)]


def extracted_evidence(claim: Claim, note: str, graph: CoverageGraph, extraction: Extraction | None) -> Evidence | None:
    """The validated evidence triage_note adjudicated, or None when it used none."""
    if extraction is None or extraction.raw is None:
        return None
    return parse_extraction(extraction.raw, note, graph, _offered(claim, graph)).evidence


def _rejected(statement: str) -> Verdict:
    return Verdict("needs_human", (), reasons=(Reason("extraction_rejected", statement),))


def triage_note(
    claim: Claim, note: str, graph: CoverageGraph, extractor: Extractor, checker: SupportChecker,
) -> tuple[Verdict, Extraction | None]:
    """Extract evidence from the note, validate it, check each span supports what it's attached
    to, then adjudicate as with supplied evidence. A claim the claim alone decides is handed off
    without calling the model (no Extraction)."""
    for decided_by_claim in (out_of_scope, billed_coding_conflict):
        if (hand_off := decided_by_claim(claim, graph)) is not None:
            return hand_off, None
    criteria = _offered(claim, graph)
    extraction = extractor(note, criteria)
    if extraction.raw is None:
        return _rejected(f"Evidence extraction stopped with {extraction.stop_reason}; no evidence was used"), extraction
    parsed = parse_extraction(extraction.raw, note, graph, criteria)
    if parsed.problems:
        return _rejected("Extracted evidence was rejected: " + "; ".join(parsed.problems)), extraction
    # A span can be in the note and still not show its fact: negated ("not on insulin"), reused
    # for another criterion, or under a different code. Any such pair could flip the verdict.
    if pairs := support_pairs(parsed.spans, note, graph, criteria):
        check = checker(pairs)
        found = unsupported(check, pairs)
        if found is None:
            return Verdict("needs_human", (), reasons=(Reason(
                "support_check_failed",
                f"The span support check stopped with {check.stop_reason} or didn't judge every span; "
                "no evidence was used"),)), extraction
        if found:
            return Verdict("needs_human", (), reasons=tuple(
                Reason("span_not_supported", statement) for statement in found)), extraction
    verdict = adjudicate(claim, parsed.evidence, graph)
    # Supplied evidence is asserted complete; an extraction may have missed a covered diagnosis
    # the note documents, so "no documented diagnosis is covered" needs the note to rule it out.
    condition = sorted(c.id for c in _walk(criteria) if c.satisfied_by == "covered_diagnosis")
    ruled_out = bool(condition) and set(condition) <= {c.criterion for c in parsed.evidence.criteria_not_met}
    if verdict.outcome == "uphold" and not ruled_out and any(r.kind == "diagnosis_not_covered" for r in verdict.reasons):
        return Verdict("needs_human", (), reasons=(Reason(
            "covered_diagnosis_not_ruled_out",
            f"The billed diagnosis is not covered, but the note doesn't rule out the covered condition "
            f"({', '.join(condition)}) and extraction can miss a documented diagnosis"),)), extraction
    return verdict, extraction
