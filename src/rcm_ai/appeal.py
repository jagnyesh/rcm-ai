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

# Every code or policy ID a letter can cite, each pattern giving (match, how to write it).
# Codes start like an ICD-10-CM category: a letter, a digit and a digit third, or one of the six
# categories with a letter third, so "A1C", "T2D" and "G6PD" aren't codes. That shape also covers
# HCPCS Level II codes, with any modifier. A bare number is only a CPT code or a policy ID when
# labeled as one, since letters carry ZIP codes, form numbers, minutes and money.
_CITATIONS = (
    (re.compile(r"\b(?:[A-Z]\d\d|C4A|C7A|C7B|D3A|M1A|Z3A)(?:\.[0-9A-Z]{1,4}|[0-9A-Z]{1,5})?(?:-[A-Z0-9]{2})*\b"),
     lambda m: m.group()),
    (re.compile(r"\bD?L\d{5}\b|\bA\d{5}\b"), lambda m: m.group()),
    (re.compile(r"\bLCD\s*(?:ID\s*)?#?\s*L?(\d{5})\b", re.IGNORECASE), lambda m: f"L{m.group(1)}"),
    (re.compile(r"\barticle\s*(?:ID\s*)?#?\s*A?(\d{5})\b", re.IGNORECASE), lambda m: f"A{m.group(1)}"),
    (re.compile(r"\bNCD\s*#?\s*(\d{1,3}(?:\.\d{1,2})?)\b", re.IGNORECASE), lambda m: f"NCD {m.group(1)}"),
    (re.compile(r"\bCPT\s*®?\s*(?:code\s*)?#?\s*(\d{4}[0-9FTU])\b", re.IGNORECASE), lambda m: m.group(1)),
)
# A HCPCS code with its modifiers, which cite the code itself.
_MODIFIED = re.compile(r"([A-V]\d{4})(?:-?[A-Z0-9]{2})+")
# An aside in parentheses that names a code, such as ev-07's "(7th character from E11.32
# sevenChrDef)", qualifies a statement; it doesn't establish that code.
_ASIDE = re.compile(r"\s*\([^)]*\)")


@dataclass(frozen=True)
class CitationCheck:
    citations: tuple[str, ...]  # each code or policy ID the letter cites, once, in order of first appearance
    unsupported: tuple[str, ...]  # those the reference path doesn't establish

    @property
    def passed(self) -> bool:
        return not self.unsupported


def citations(text: str) -> tuple[str, ...]:
    """Each code or policy ID in the text, once, in order of first appearance."""
    found = sorted((m.start(), write(m)) for pattern, write in _CITATIONS for m in pattern.finditer(text))
    return tuple(dict.fromkeys(c for _, c in found))


def _readings(citation: str) -> set[str]:
    """The forms one code takes: dotted or not, and a HCPCS code with or without modifiers."""
    forms = {citation.replace(".", "")}
    if m := _MODIFIED.fullmatch(citation):
        forms.add(m.group(1))
    return forms


def asserted(statement: str) -> str:
    """A hop statement without its asides that name a code."""
    return _ASIDE.sub(lambda m: "" if citations(m.group()) else m.group(), statement)


def established(path: tuple[Hop, ...]) -> tuple[str, ...]:
    """What a path lets a letter cite. A hop's statement is what it establishes; its anchor is
    only text to find at the source (the article hop's anchor is "52464"), so it doesn't count."""
    return citations(" ".join(asserted(h.statement) for h in path))


def check_citations(letter: str, path: tuple[Hop, ...]) -> CitationCheck:
    allowed = set().union(*(_readings(c) for c in established(path)))
    cited = citations(letter)
    return CitationCheck(cited, tuple(c for c in cited if not _readings(c) & allowed))


def quotes(evidence: Evidence | None) -> list[tuple[str, str]]:
    """The note's quotes behind a verdict, diagnoses first. Diagnoses are labeled without their
    codes, so the letter has no code to cite but the path's."""
    if evidence is None:
        return []
    return [("diagnosis", d.span) for d in evidence.diagnoses] + [(c.criterion, c.span) for c in evidence.criteria]


def build_request(claim: Claim, path: tuple[Hop, ...], note_quotes: list[tuple[str, str]]) -> dict:
    """Keyword arguments for messages.create. The steps are what the checker reads, with no
    sources: file names like icd10cm-tabular-2027-E08-E13.xml would put codes the checker rejects
    in front of a drafter of sourced paths only, and every arm's steps should read alike."""
    content = (
        ex.describe_claim(claim)
        + "\n\nJustification:\n" + "\n".join(f"{n}. {asserted(h.statement)}" for n, h in enumerate(path, start=1))
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
