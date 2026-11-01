"""LLM-only baseline: the model decides the verdict itself, with no graph.

It is the comparison arm for graph-grounded triage. It gets the claim, the note and the
LCD's curated criteria text, with the extractor's model settings and output schema, so
what differs between the arms is what decides: the model's knowledge here, the graph's
covered-code list, ICD-10-CM terminology, Excludes1 rules and scope logic there.
"""

import json

from rcm_ai import extract as ex
from rcm_ai.extract import Extraction
from rcm_ai.graph import Criterion
from rcm_ai.schemas import OUTCOMES, Claim, CriterionEvidence, DocumentedCode, Evidence, Hop, Reason, Verdict

PROMPT = ex.PROMPTS / "llm_only_verdict.md"


def _schema() -> dict:
    """The extractor's output schema plus a verdict and its reasons."""
    schema = json.loads(ex.SCHEMA.read_text())
    return {
        **schema,
        "properties": {
            **schema["properties"],
            "verdict": {"type": "string", "enum": list(OUTCOMES)},
            "reasons": {"type": "array", "items": {"type": "string"}},
        },
        "required": [*schema["required"], "verdict", "reasons"],
    }


def build_request(claim: Claim, note: str, policy: dict[str, list[Criterion]]) -> dict:
    """Keyword arguments for messages.create. policy maps each LCD that lists the billed item
    to its curated criteria."""
    criteria = "\n\n".join(
        f"LCD {lcd} coverage criteria (id: policy text):\n" + "\n".join(ex.describe_criteria(c))
        for lcd, c in policy.items()
    ) or "No LCD lists the billed item."
    content = f"{ex.describe_claim(claim)}\n\n{criteria}\n\nNote:\n<note>\n{note}\n</note>"
    return {
        "model": ex.MODEL,
        "max_tokens": ex.MAX_TOKENS,
        "system": PROMPT.read_text(),
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": ex.EFFORT, "format": {"type": "json_schema", "schema": _schema()}},
    }


def ask(claim: Claim, note: str, policy: dict[str, list[Criterion]], client) -> Extraction:
    return ex.send(build_request(claim, note, policy), client)


def _no_verdict(statement: str) -> Verdict:
    return Verdict("needs_human", (), reasons=(Reason("no_verdict", statement),))


def verdict_from(response: Extraction) -> Verdict:
    """The model's verdict as given. Only a response that can't be read as a verdict hands the
    claim off, as a refused or unreadable extraction does in the extracted arm."""
    raw = response.raw
    if raw is None:
        return _no_verdict(f"The model stopped with {response.stop_reason}; no verdict was returned")
    outcome = raw.get("verdict") if isinstance(raw, dict) else None
    reasons = raw.get("reasons") if isinstance(raw, dict) else None
    if outcome not in OUTCOMES or not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        return _no_verdict("The model's response is not a verdict with reasons")
    if outcome == "overturnable":
        if not reasons:
            return _no_verdict("The model said overturnable but gave no reasons to justify it")
        return Verdict(outcome, tuple(Hop(r) for r in reasons))
    return Verdict(outcome, (), reasons=tuple(Reason("model_reason", r) for r in reasons))


def evidence_from(response: Extraction) -> Evidence | None:
    """The baseline's evidence fields as given, unchecked: what it hands the appeal drafter."""
    if not isinstance(response.raw, dict):
        return None

    def items(name: str, key: str) -> list[tuple[str, str]]:
        found = response.raw.get(name)
        return [(i[key], i["span"]) for i in (found if isinstance(found, list) else [])
                if isinstance(i, dict) and isinstance(i.get(key), str) and isinstance(i.get("span"), str)]

    return Evidence(
        tuple(DocumentedCode(k, s) for k, s in items("diagnoses", "code")),
        tuple(CriterionEvidence(k, s) for k, s in items("criteria_met", "criterion")),
        tuple(CriterionEvidence(k, s) for k, s in items("criteria_not_met", "criterion")),
    )
