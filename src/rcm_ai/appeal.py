"""Appeal drafting and citation checking.

The drafter (an LLM) writes a letter from a verdict's justification path, its findings and the
note's quotes. The checker (deterministic) pulls every code and policy ID from the letter and fails
it if any is not in a reference verdict's path or findings, so a letter never cites what that
verdict didn't establish. Against a reference that establishes nothing, every letter fails.
"""

import re
from dataclasses import dataclass

from rcm_ai import extract as ex
from rcm_ai.extract import Extraction
from rcm_ai.schemas import Claim, Evidence, Finding, Hop

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
# HCPCS Level II codes, with any modifier, and LCD and article IDs (L33822, A52464). A bare number
# is only a CPT code or a policy ID when labeled as one, since letters carry ZIP codes, form
# numbers, minutes and money. A code is read through any further dotted part, so "E2103.0" isn't
# read as E2103. Each optional part of a label carries its own leading whitespace: adjacent \s*
# backtrack cubically on a whitespace run in the letter.
# What may come between a label and its ID: "(LCD) 39999", "LCD ID", "LCD No.", "LCD number", "LCD #", "LCD:".
_LABEL_TAIL = r"(?:\s*\))?(?:\s*ID)?(?:\s*(?:No\b\.?|number\b|#))?(?:\s*:)?\s*"
_CITATIONS = (
    (re.compile(r"\b(?:[A-Z]\d\d|C4A|C7A|C7B|D3A|M1A|Z3A)(?:\.[0-9A-Z]{1,4}|[0-9A-Z]{1,5})?(?:\.[0-9A-Z]+)*"
                r"(?:-[A-Z0-9]{2})*\b"),
     lambda m: m.group()),
    (re.compile(r"\bDL\d{5}\b"), lambda m: m.group()),
    (re.compile(rf"\bLCD{_LABEL_TAIL}L?(\d{{5}}(?:\.[0-9A-Z]+)*)\b", re.IGNORECASE), lambda m: f"L{m.group(1)}"),
    (re.compile(rf"\barticle{_LABEL_TAIL}A?(\d{{5}}(?:\.[0-9A-Z]+)*)\b", re.IGNORECASE),
     lambda m: f"A{m.group(1)}"),
    (re.compile(rf"\bNCD{_LABEL_TAIL}(\d{{1,3}}(?:\.\d{{1,2}})?)\b", re.IGNORECASE), lambda m: f"NCD {m.group(1)}"),
    (re.compile(rf"\bCPT(?:\s*®)?(?:\s*code)?{_LABEL_TAIL}(\d{{4}}[0-9FTU])\b", re.IGNORECASE), lambda m: m.group(1)),
)
# A HCPCS code with its modifiers, which cite the code itself.
_MODIFIED = re.compile(r"([A-V]\d{4})(?:-?[A-Z0-9]{2})+")
# An aside in parentheses that names a code, such as ev-07's "(7th character from E11.32
# sevenChrDef)", is a sourced statement's provenance; it doesn't establish that code.
_ASIDE = re.compile(r"\s*\([^)]*\)")


@dataclass(frozen=True)
class CitationCheck:
    citations: tuple[str, ...]  # each code or policy ID the letter cites, once, in order: its first unsupported form, else its first
    unsupported: tuple[str, ...]  # those the reference doesn't establish, each in the form it doesn't
    warranted: bool = True  # False when the reference establishes nothing, so no letter passes

    @property
    def passed(self) -> bool:
        return self.warranted and not self.unsupported


def citations(text: str) -> tuple[str, ...]:
    """Each code or policy ID in the text, once, in order of first appearance."""
    found = sorted((m.start(), write(m)) for pattern, write in _CITATIONS for m in pattern.finditer(text))
    return tuple(dict.fromkeys(c for _, c in found))


def _forms(citation: str) -> set[tuple[str, str]]:
    """The codes a letter's citation can be, by kind. A dot marks an ICD-10-CM code (an NCD ID's
    dot reads the same way on both sides); written without one, a code may be either kind, and a
    HCPCS code may carry modifiers."""
    if "." in citation:
        return {("icd", citation.replace(".", ""))}
    forms = {("icd", citation), ("other", citation)}
    if m := _MODIFIED.fullmatch(citation):
        forms.add(("other", m.group(1)))
    return forms


def _form(citation: str) -> tuple[str, str]:
    """The code a statement names: statements write ICD-10-CM codes dotted and other codes plain."""
    return ("icd", citation.replace(".", "")) if "." in citation else ("other", citation)


def asserted(step: Hop | Finding) -> str:
    """What a step or finding asserts. A sourced one's aside naming a code is provenance; an
    unsourced one, such as the baseline's own reason, asserts all of its text."""
    if step.source is None:
        return step.statement
    return _ASIDE.sub(lambda m: "" if citations(m.group()) else m.group(), step.statement)


def established(path: tuple[Hop, ...], findings: tuple[Finding, ...] = ()) -> tuple[str, ...]:
    """What a verdict lets a letter cite: what its steps and findings assert. A hop's anchor is
    only text to find at the source (the article hop's anchor is "52464"), so it doesn't count."""
    return citations(" ".join(asserted(s) for s in (*path, *findings)))


def check_citations(letter: str, path: tuple[Hop, ...], findings: tuple[Finding, ...] = ()) -> CitationCheck:
    allowed = {_form(c) for c in established(path, findings)}
    # One code's written forms differ only by its dot or a hyphenated modifier. A code counts once,
    # named by the first of its forms the reference doesn't establish, if any.
    codes = {}
    for c in citations(letter):
        codes.setdefault(c.replace(".", "").split("-")[0], []).append(c)
    cited, unsupported = [], []
    for forms in codes.values():
        bad = [c for c in forms if not _forms(c) & allowed]
        cited.append((bad or forms)[0])
        unsupported += bad[:1]
    return CitationCheck(tuple(cited), tuple(unsupported), warranted=bool(allowed))


def quotes(evidence: Evidence | None) -> list[tuple[str, str]]:
    """The note's quotes behind a verdict, diagnoses first. Diagnoses are labeled without their
    codes, so the letter has no code to cite but the verdict's."""
    if evidence is None:
        return []
    return [("diagnosis", d.span) for d in evidence.diagnoses] + [(c.criterion, c.span) for c in evidence.criteria]


def build_request(claim: Claim, path: tuple[Hop, ...], note_quotes: list[tuple[str, str]],
                  findings: tuple[Finding, ...] = ()) -> dict:
    """Keyword arguments for messages.create. The steps and findings are what the checker reads,
    with no sources: file names like icd10cm-tabular-2027-E08-E13.xml would put codes the checker
    rejects in front of a drafter of sourced paths only, and every arm's steps should read alike."""
    steps = "\n".join(f"{n}. {asserted(h)}" for n, h in enumerate(path, start=1))
    noted = "".join(f"\n- {asserted(f)}" for f in findings)
    content = (
        ex.describe_claim(claim)
        + f"\n\nJustification:\n{steps}"
        + (f"\n\nFindings:{noted}" if findings else "")
        + "\n\nNote quotes:\n" + ("\n".join(f'- {label}: "{span}"' for label, span in note_quotes) or "none")
    )
    return {
        "model": ex.MODEL,
        "max_tokens": ex.MAX_TOKENS,
        "system": PROMPT.read_text(),
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": ex.EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
    }


def draft(claim: Claim, path: tuple[Hop, ...], note_quotes: list[tuple[str, str]], client,
          findings: tuple[Finding, ...] = ()) -> Extraction:
    return ex.send(build_request(claim, path, note_quotes, findings), client)


def letter_of(response: Extraction) -> str | None:
    letter = response.raw.get("letter") if isinstance(response.raw, dict) else None
    return letter if isinstance(letter, str) and letter.strip() else None


@dataclass(frozen=True)
class Appeal:
    """What drafting gave for one overturnable verdict in the eval."""
    reference: tuple[str, ...] | None  # what the reference verdict lets the letter cite; None if it wasn't built
    letter: str | None = None
    check: CitationCheck | None = None  # None without a letter
    error: str | None = None  # building the reference, the drafting call or writing its log raised


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
