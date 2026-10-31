"""Appeal drafting and citation checking.

The drafter (an LLM) writes a letter from a verdict's justification path and the note's quotes.
The checker (deterministic) pulls every code and policy ID from the letter and fails it if any
is not in the reference path, so a letter never cites what the path didn't establish.
"""

import re
from dataclasses import dataclass

from rcm_ai import extract as ex
from rcm_ai.extract import Extraction
from rcm_ai.schemas import Claim, Evidence, Hop

PROMPT = ex.PROMPTS / "draft_appeal.md"
SCHEMA = {
    "type": "object",
    "properties": {"letter": {"type": "string"}},
    "required": ["letter"],
    "additionalProperties": False,
}

# Every code or policy ID a letter can cite. An undotted three-character ICD-10-CM token counts
# only with a digit third, or as one of the six categories with a letter third, so the
# abbreviations "A1C" and "T2D" aren't read as codes. Alternatives are tried in order.
_CITATION = re.compile(
    r"\b(?:"
    r"[LA]\d{5}"  # LCD or policy article ID
    r"|[A-V]\d{4}"  # HCPCS Level II
    r"|\d{5}|\d{4}[FTU]"  # CPT
    r"|[A-Z]\d[0-9A-Z]\.[0-9A-Z]{1,4}"  # ICD-10-CM code
    r"|[A-Z]\d\d|C4A|C7A|C7B|D3A|M1A|Z3A"  # ICD-10-CM category
    r")\b"
)


@dataclass(frozen=True)
class CitationCheck:
    citations: tuple[str, ...]  # each code or policy ID the letter cites, once, in order of first appearance
    unsupported: tuple[str, ...]  # those the reference path doesn't establish

    @property
    def passed(self) -> bool:
        return not self.unsupported


def citations(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_CITATION.findall(text)))


def established(path: tuple[Hop, ...]) -> tuple[str, ...]:
    """What a path lets a letter cite. A hop's statement is what it establishes; its anchor is
    only text to find at the source (the article hop's anchor is "52464"), so it doesn't count."""
    return citations(" ".join(h.statement for h in path))


def check_citations(letter: str, path: tuple[Hop, ...]) -> CitationCheck:
    allowed = set(established(path))
    cited = citations(letter)
    return CitationCheck(cited, tuple(c for c in cited if c not in allowed))


def quotes(evidence: Evidence | None) -> list[tuple[str, str]]:
    """The note's quotes behind a verdict, diagnoses first. Diagnoses are labeled without their
    codes, so the letter has no code to cite but the path's."""
    if evidence is None:
        return []
    return [("diagnosis", d.span) for d in evidence.diagnoses] + [(c.criterion, c.span) for c in evidence.criteria]


def _step(n: int, hop: Hop) -> str:
    where = f"source: {hop.source.path}, line {hop.source.line}, {hop.source.version}" if hop.source else "no source"
    return f"{n}. {hop.statement} ({where})"


def build_request(claim: Claim, path: tuple[Hop, ...], note_quotes: list[tuple[str, str]]) -> dict:
    """Keyword arguments for messages.create."""
    content = (
        ex.describe_claim(claim)
        + "\n\nJustification:\n" + "\n".join(_step(n, h) for n, h in enumerate(path, start=1))
        + "\n\nNote quotes:\n" + ("\n".join(f'- {label}: "{span}"' for label, span in note_quotes) or "none")
    )
    return {
        "model": ex.MODEL,
        "max_tokens": ex.MAX_TOKENS,
        "system": PROMPT.read_text(),
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": ex.EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
    }


def draft(claim: Claim, path: tuple[Hop, ...], note_quotes: list[tuple[str, str]], client) -> Extraction:
    return ex.send(build_request(claim, path, note_quotes), client)


def letter_of(response: Extraction) -> str | None:
    letter = response.raw.get("letter") if isinstance(response.raw, dict) else None
    return letter if isinstance(letter, str) and letter.strip() else None


@dataclass(frozen=True)
class Appeal:
    """What drafting gave for one overturnable verdict in the eval."""
    reference: tuple[str, ...]  # what the reference path lets the letter cite
    letter: str | None = None
    check: CitationCheck | None = None  # None without a letter
    error: str | None = None  # the drafting call raised


def tally(appeals: list[Appeal]) -> dict:
    checks = [a.check for a in appeals if a.check is not None]
    cited = sum(len(c.citations) for c in checks)
    unsupported = sum(len(c.unsupported) for c in checks)
    return {
        "drafted": len(appeals),
        "without_letter": sum(a.letter is None and a.error is None for a in appeals),
        "errors": sum(a.error is not None for a in appeals),
        "failed": sum(not c.passed for c in checks),
        "citations": cited,
        "unsupported": unsupported,
        "unsupported_rate": unsupported / cited if cited else None,
    }
