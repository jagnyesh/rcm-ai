import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from rcm_ai.codes import ICD10CM_CODE


@dataclass(frozen=True)
class Concept:
    code: str
    title: str
    parent: str | None


@dataclass(frozen=True)
class CodeRef:
    kind: str  # "exact" | "subtree" | "range"
    start: str
    end: str | None = None


@dataclass(frozen=True)
class Excludes1:
    code: str
    target: CodeRef
    note: str


@dataclass(frozen=True)
class Tabular:
    version: str
    concepts: dict[str, Concept]
    excludes1: list[Excludes1]


_TRAILING_REF = re.compile(r"\(([A-Z0-9.\-]+)\)\s*$")
# "-" or ".-" after a code means "and everything under it"; a range end may carry one too
# ("I63.3-I63.5-"), and a range already spans its ends' subtrees, so it is dropped there.
_REF = re.compile(rf"(?P<start>{ICD10CM_CODE})(?P<wild>\.?-)?(?:-(?P<end>{ICD10CM_CODE})(?:\.?-)?)?")


def parse_code_ref(note: str) -> CodeRef:
    m = _TRAILING_REF.search(note)
    if m is None:
        raise ValueError(f"no code reference in note: {note!r}")
    ref = _REF.fullmatch(m.group(1))
    if ref is None:
        raise ValueError(f"unrecognized code reference {m.group(1)!r} in note: {note!r}")
    if ref["end"]:
        return CodeRef("range", ref["start"], ref["end"])
    if ref["wild"]:
        return CodeRef("subtree", ref["start"])
    return CodeRef("exact", ref["start"])


def parse_tabular(path: Path) -> Tabular:
    root = ET.parse(path).getroot()
    concepts: dict[str, Concept] = {}
    excludes1: list[Excludes1] = []

    def walk(diag: ET.Element, parent: str | None) -> None:
        code = diag.findtext("name")
        concepts[code] = Concept(code, diag.findtext("desc"), parent)
        for note in diag.findall("excludes1/note"):
            excludes1.append(Excludes1(code, parse_code_ref(note.text), note.text))
        for child in diag.findall("diag"):
            walk(child, code)

    for section in root.iter("section"):
        for diag in section.findall("diag"):
            walk(diag, None)
    return Tabular(root.findtext("version"), concepts, excludes1)
