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
    kind: str = "diag"  # "diag" (a tabular entry) | "seventh" (expanded from a sevenChrDef)
    seventh_defined_at: str | None = None  # the code whose sevenChrDef supplied the character


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


def _plain(code: str) -> str:
    return code.replace(".", "")


def ref_covers(ref: CodeRef, code: str) -> bool:
    """Whether an Excludes1 target reference names this code. Codes compare without dots,
    which is safe because every category is exactly three characters."""
    c, start = _plain(code), _plain(ref.start)
    if ref.kind == "exact":
        return c == start
    if ref.kind == "subtree":
        return c.startswith(start)
    end = _plain(ref.end)
    return start <= c[: len(start)] and c[: len(end)] <= end


def _with_seventh(code: str, char: str) -> str:
    # The 7th character always sits in position 7; shorter codes are padded with placeholder X.
    base = code if "." in code else code + "."
    while len(_plain(base)) < 6:
        base += "X"
    return base + char


def parse_tabular(path: Path) -> Tabular:
    root = ET.parse(path).getroot()
    concepts: dict[str, Concept] = {}
    excludes1: list[Excludes1] = []

    def walk(diag: ET.Element, parent: str | None, seventh: tuple[str, list] | None) -> None:
        code, title = diag.findtext("name"), diag.findtext("desc")
        concepts[code] = Concept(code, title, parent)
        for note in diag.findall("excludes1/note"):
            excludes1.append(Excludes1(code, parse_code_ref(note.text), note.text))
        definition = diag.findall("sevenChrDef/extension")
        if definition:
            seventh = (code, [(e.get("char"), e.text) for e in definition])
        children = diag.findall("diag")
        for child in children:
            walk(child, code, seventh)
        if not children and seventh:
            defined_at, extensions = seventh
            for char, meaning in extensions:
                full = _with_seventh(code, char)
                concepts[full] = Concept(full, f"{title}, {meaning}", code, "seventh", defined_at)

    for section in root.iter("section"):
        for diag in section.findall("diag"):
            walk(diag, None, None)
    return Tabular(root.findtext("version"), concepts, excludes1)
