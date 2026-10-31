import json
import re
from dataclasses import dataclass
from pathlib import Path

from rcm_ai.codes import is_cpt, is_hcpcs_level2, is_icd10cm

# Claim adjustment reason codes are 1-3 digits, some with a one-letter prefix (e.g. "50", "B7").
_CARC = re.compile(r"[A-Z]?[0-9]{1,3}")
COVERAGE_PERIODS = ("initial", "continued")
OUTCOMES = ("overturnable", "uphold", "needs_human")


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Claim:
    hcpcs: str
    icd10: tuple[str, ...]
    carc: str
    coverage: str  # "initial" | "continued"

    def __post_init__(self):
        if is_cpt(self.hcpcs):
            raise SchemaError(f"claim item {self.hcpcs!r} is a CPT code; only HCPCS Level II is in scope")
        if not is_hcpcs_level2(self.hcpcs):
            raise SchemaError(f"claim item {self.hcpcs!r} is not a HCPCS Level II code")
        if not self.icd10:
            raise SchemaError("claim needs at least one ICD-10-CM code")
        for code in self.icd10:
            if not is_icd10cm(code):
                raise SchemaError(f"claim diagnosis {code!r} is not an ICD-10-CM code")
        if not _CARC.fullmatch(self.carc):
            raise SchemaError(f"denial reason {self.carc!r} is not a CARC code")
        if self.coverage not in COVERAGE_PERIODS:
            raise SchemaError(f"claim coverage {self.coverage!r} must be one of {COVERAGE_PERIODS}")


@dataclass(frozen=True)
class DocumentedCode:
    code: str
    span: str


@dataclass(frozen=True)
class CriterionEvidence:
    criterion: str
    span: str


@dataclass(frozen=True)
class Evidence:
    diagnoses: tuple[DocumentedCode, ...]
    criteria: tuple[CriterionEvidence, ...]
    criteria_not_met: tuple[CriterionEvidence, ...] = ()  # spans showing a criterion affirmatively fails


@dataclass(frozen=True)
class Source:
    path: str  # relative to the fixtures dir
    version: str
    line: int  # 1-based line where the source record starts


@dataclass(frozen=True)
class Hop:
    statement: str
    anchor: str  # text a reviewer will find in the source record
    source: Source


@dataclass(frozen=True)
class Finding:
    type: str  # "specificity" | "excludes1"
    codes: tuple[str, ...]
    statement: str
    anchor: str | None = None  # text to look for at source, when the finding rests on a source row
    source: Source | None = None


@dataclass(frozen=True)
class Reason:
    """Why a verdict is uphold or needs_human."""
    kind: str
    statement: str
    anchor: str | None = None  # text to look for at source
    source: Source | None = None
    span: str | None = None  # the note text the reason rests on


@dataclass(frozen=True)
class Verdict:
    outcome: str  # one of OUTCOMES
    path: tuple[Hop, ...]
    findings: tuple[Finding, ...] = ()
    reasons: tuple[Reason, ...] = ()

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown verdict outcome {self.outcome!r}")
        if self.outcome == "overturnable" and not self.path:
            raise ValueError("an overturnable verdict needs a justification path")


@dataclass(frozen=True)
class Case:
    id: str
    claim: Claim
    evidence: Evidence
    note: str


def _documented_code(item: dict, path: Path) -> str:
    code = item["code"]
    if not isinstance(code, str) or not is_icd10cm(code):
        raise SchemaError(f"{path}: documented diagnosis {code!r} is not an ICD-10-CM code")
    return code


def _span(item: dict, note: str, what: str, path: Path) -> str:
    span = item["span"]
    if not isinstance(span, str) or not span.strip():
        raise SchemaError(f"{path}: {what} needs a non-empty text span")
    if span not in note:
        raise SchemaError(f"{path}: {what} span {span!r} does not appear in the note")
    return span


def load_case(path: Path) -> Case:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SchemaError(f"{path}: cannot read case: {e}") from e
    if not isinstance(raw, dict):
        raise SchemaError(f"{path}: case must be a JSON object")
    if raw.get("synthetic") is not True:
        raise SchemaError(f"{path}: case is not marked synthetic; only synthetic cases are accepted")
    try:
        c, e, note = raw["claim"], raw["evidence"], raw["note"]
        if not isinstance(c["icd10"], list):
            raise SchemaError(f"{path}: claim icd10 must be a list of codes")
        return Case(
            id=raw["id"],
            claim=Claim(c["hcpcs"], tuple(c["icd10"]), c["carc"], c["coverage"]),
            evidence=Evidence(
                tuple(
                    DocumentedCode(_documented_code(d, path), _span(d, note, d["code"], path))
                    for d in e["diagnoses"]
                ),
                tuple(
                    CriterionEvidence(x["criterion"], _span(x, note, x["criterion"], path))
                    for x in e["criteria"]
                ),
                tuple(
                    CriterionEvidence(x["criterion"], _span(x, note, x["criterion"], path))
                    for x in e.get("criteria_not_met", [])
                ),
            ),
            note=note,
        )
    except (KeyError, TypeError) as e:
        raise SchemaError(f"{path}: case is missing or mistypes field {e}") from e
