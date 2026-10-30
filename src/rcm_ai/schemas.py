import json
import re
from dataclasses import dataclass
from pathlib import Path

from rcm_ai.codes import is_cpt, is_hcpcs_level2, is_icd10cm

# Claim adjustment reason codes are 1-3 digits, some with a one-letter prefix (e.g. "50", "B7").
_CARC = re.compile(r"[A-Z]?[0-9]{1,3}")


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Claim:
    hcpcs: str
    icd10: tuple[str, ...]
    carc: str

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
class Verdict:
    outcome: str  # "overturnable" | "needs_human"
    path: tuple[Hop, ...]


@dataclass(frozen=True)
class Case:
    id: str
    claim: Claim
    evidence: Evidence
    note: str


def load_case(path: Path) -> Case:
    raw = json.loads(Path(path).read_text())
    if raw.get("synthetic") is not True:
        raise SchemaError(f"{path}: case is not marked synthetic; only synthetic cases are accepted")
    c, e = raw["claim"], raw["evidence"]
    return Case(
        id=raw["id"],
        claim=Claim(c["hcpcs"], tuple(c["icd10"]), c["carc"]),
        evidence=Evidence(
            tuple(DocumentedCode(d["code"], d["span"]) for d in e["diagnoses"]),
            tuple(CriterionEvidence(x["criterion"], x["span"]) for x in e["criteria"]),
        ),
        note=raw["note"],
    )
