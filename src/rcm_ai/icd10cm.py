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
    # "diag" (a tabular entry) | "seventh" (expanded from a sevenChrDef) | "section" | "chapter"
    kind: str = "diag"
    seventh_defined_at: str | None = None  # the code whose sevenChrDef supplied the character


@dataclass(frozen=True)
class CodeRef:
    kind: str  # "exact" | "subtree" | "range"
    start: str
    end: str | None = None


@dataclass(frozen=True)
class Excludes1:
    code: str  # the owning diag code, section id ("E08-E13") or chapter ("chapter-15")
    target: CodeRef
    note: str


@dataclass(frozen=True)
class Tabular:
    version: str
    concepts: dict[str, Concept]
    excludes1: list[Excludes1]


_TRAILING_REFS = re.compile(r"\(([A-Z0-9.\-, ]+)\)\s*$")
# "-" or ".-" after a code means "and everything under it"; a range end may carry one too
# ("I63.3-I63.5-"), and a range already spans its ends' subtrees, so it is dropped there.
_REF = re.compile(rf"(?P<start>{ICD10CM_CODE})(?P<wild>\.?-)?(?:-(?P<end>{ICD10CM_CODE})(?:\.?-)?)?")


def _parse_ref(text: str, note: str) -> CodeRef:
    ref = _REF.fullmatch(text)
    if ref is None:
        raise ValueError(f"unrecognized code reference {text!r} in note: {note!r}")
    if ref["end"]:
        return CodeRef("range", ref["start"], ref["end"])
    if ref["wild"]:
        return CodeRef("subtree", ref["start"])
    return CodeRef("exact", ref["start"])


def parse_code_refs(note: str) -> tuple[CodeRef, ...]:
    """The code references in a note's trailing parentheses: "(J09.X3, J10.2, J11.2)" names three."""
    m = _TRAILING_REFS.search(note)
    if m is None:
        raise ValueError(f"no code reference in note: {note!r}")
    return tuple(_parse_ref(part.strip(), note) for part in m.group(1).split(","))


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

    def define(concept: Concept) -> None:
        if concept.code in concepts:
            raise ValueError(f"{concept.code} is defined more than once")
        concepts[concept.code] = concept

    def add(el: ET.Element, code: str, parent: str | None, kind: str) -> None:
        define(Concept(code, el.findtext("desc"), parent, kind))
        for note in el.findall("excludes1/note"):
            excludes1.extend(Excludes1(code, ref, note.text) for ref in parse_code_refs(note.text))

    def walk(diag: ET.Element, parent: str | None, seventh: tuple[str, list] | None) -> None:
        code, title = diag.findtext("name"), diag.findtext("desc")
        add(diag, code, parent, "diag")
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
                define(Concept(full, f"{title}, {meaning}", code, "seventh", defined_at))

    def walk_section(section: ET.Element, chapter: str | None) -> None:
        add(section, section.get("id"), chapter, "section")
        for diag in section.findall("diag"):
            walk(diag, section.get("id"), None)

    # Sections and chapters become ancestors of their categories, so their Excludes1 notes
    # reach every code below them. A slice may hold a bare section without its chapter.
    for chapter in root.findall("chapter"):
        code = f"chapter-{chapter.findtext('name')}"
        add(chapter, code, None, "chapter")
        for section in chapter.findall("section"):
            walk_section(section, code)
    for section in root.findall("section"):
        walk_section(section, None)
    return Tabular(root.findtext("version"), concepts, excludes1)
