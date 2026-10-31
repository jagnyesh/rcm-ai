"""LLM evidence extraction: note text in, evidence the graph can adjudicate out.

The model only reads the note. Whatever it returns is checked against the note and the
graph before use; one ungrounded item rejects the whole extraction and the claim goes to
a human, because dropping just that item could flip the verdict.
"""

import json
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


Extractor = Callable[[str, list[Criterion]], Extraction]


def _describe(criteria: list[Criterion], depth: int = 0) -> list[str]:
    lines = []
    for c in criteria:
        note = " (decided from the diagnosis code)" if c.satisfied_by == "covered_diagnosis" else ""
        alternatives = " Met by any one of these alternatives:" if c.any_of else ""
        lines.append(f"{'  ' * depth}- {c.id}{note}: \"{c.quote}\"{alternatives}")
        lines.extend(_describe(list(c.any_of), depth + 1))
    return lines


def build_request(note: str, criteria: list[Criterion]) -> dict:
    """Keyword arguments for messages.create. Criteria are top-level ones; alternatives nest."""
    content = (
        "Coverage criteria (id: policy text):\n" + "\n".join(_describe(criteria))
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
    """One live model call. The record keeps the exact request bytes sent, for the wire check."""
    raw = client.messages.with_raw_response.create(**build_request(note, criteria))
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


def _rejected(statement: str) -> Verdict:
    return Verdict("needs_human", (), reasons=(Reason("extraction_rejected", statement),))


def triage_note(
    claim: Claim, note: str, graph: CoverageGraph, extractor: Extractor,
) -> tuple[Verdict, Extraction | None]:
    """Extract evidence from the note, validate it, then adjudicate as with supplied evidence.
    A claim the claim alone decides is handed off without calling the model (no Extraction)."""
    for decided_by_claim in (out_of_scope, billed_coding_conflict):
        if (hand_off := decided_by_claim(claim, graph)) is not None:
            return hand_off, None
    criteria = [c for lcd in applicable_lcds(claim, graph) for c in graph.criteria(lcd)]
    extraction = extractor(note, criteria)
    if extraction.raw is None:
        return _rejected(f"Evidence extraction stopped with {extraction.stop_reason}; no evidence was used"), extraction
    parsed = parse_extraction(extraction.raw, note, graph, criteria)
    if parsed.problems:
        return _rejected("Extracted evidence was rejected: " + "; ".join(parsed.problems)), extraction
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
