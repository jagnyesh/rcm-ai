"""LLM evidence extraction: note text in, evidence the graph can adjudicate out.

The model only reads the note. Whatever it returns is checked against the note and the
graph before use; one ungrounded item rejects the whole extraction and the claim goes to
a human, because dropping just that item could flip the verdict.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rcm_ai.adjudicate import adjudicate
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


def parse_extraction(raw: dict, note: str, graph: CoverageGraph) -> Parsed:
    if not isinstance(raw, dict):
        return Parsed(None, ["response does not match the extraction schema: not an object"])
    fields = {name: _items(raw, name, key) for name, key in
              (("diagnoses", "code"), ("criteria_met", "criterion"), ("criteria_not_met", "criterion"))}
    bad = [name for name, items in fields.items() if items is None]
    if bad:
        return Parsed(None, [f"response does not match the extraction schema: {', '.join(bad)}"])

    problems, spans = [], []
    known = graph.criterion_ids()
    for name, items in fields.items():
        for key, span in items:
            if name == "diagnoses":
                if not is_icd10cm(key) or graph.title(key) is None:
                    problems.append(f"diagnosis {key!r} is not a code in the loaded ICD-10-CM terminology")
            elif key not in known:
                problems.append(f"criterion {key!r} is not a curated criterion")
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


def triage_note(claim: Claim, note: str, graph: CoverageGraph, extractor: Extractor) -> tuple[Verdict, Extraction]:
    """Extract evidence from the note, validate it, then adjudicate as with supplied evidence."""
    lcds = [lcd for lcd in graph.lcds_listing(claim.hcpcs) if graph.applicability(lcd, claim.hcpcs)]
    criteria = [c for lcd in lcds for c in graph.criteria(lcd)]
    extraction = extractor(note, criteria)
    if extraction.raw is None:
        return _rejected(f"Evidence extraction stopped with {extraction.stop_reason}; no evidence was used"), extraction
    parsed = parse_extraction(extraction.raw, note, graph)
    if parsed.problems:
        return _rejected("Extracted evidence was rejected: " + "; ".join(parsed.problems)), extraction
    return adjudicate(claim, parsed.evidence, graph), extraction
