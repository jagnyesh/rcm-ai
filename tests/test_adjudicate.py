import csv
import io
from dataclasses import replace
from pathlib import Path

from rcm_ai.adjudicate import adjudicate
from rcm_ai.graph import build_graph, normalize_lcd_text
from rcm_ai.schemas import load_case

DATA = Path(__file__).resolve().parents[1] / "data"
FIXTURES = DATA / "fixtures"


def _record_text_at(path: Path, line: int) -> str:
    """Text of the source record that starts at a 1-based line, as a reviewer would read it."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if path.suffix == ".xml":
        return lines[line - 1]
    record = next(csv.reader(io.StringIO("".join(lines[line - 1 :]), newline="")))
    return normalize_lcd_text(" ".join(record))


def test_documented_cgm_denial_is_overturnable_with_every_hop_traceable_to_a_source_line():
    graph = build_graph(DATA)
    case = load_case(DATA / "synthetic" / "case-001.json")

    verdict = adjudicate(case.claim, case.evidence, graph)

    assert verdict.outcome == "overturnable"
    assert [h.statement for h in verdict.path] == [
        "E2103 is listed in LCD L33822",
        "LCD L33822 criteria apply to E2103",
        "LCD L33822 is related to article A52464",
        "E11.65 is an ICD-10-CM code: Type 2 diabetes mellitus with hyperglycemia",
        "Article A52464 covers E11.65",
        "Criterion cgm-1-diabetes met by covered diagnosis E11.65",
        "Criterion cgm-2-training met by documented evidence",
        "Criterion cgm-3-fda-indication met by documented evidence",
        "Criterion cgm-4-glycemic-control met by cgm-4a-insulin",
        "Criterion cgm-5-visit met by documented evidence",
    ]
    for hop in verdict.path:
        assert hop.anchor in _record_text_at(FIXTURES / hop.source.path, hop.source.line), hop
        assert hop.source.version


def test_a_covered_diagnosis_without_insulin_or_hypoglycemia_evidence_is_not_overturnable():
    graph = build_graph(DATA)
    case = load_case(DATA / "synthetic" / "case-001.json")
    evidence = replace(
        case.evidence,
        criteria=tuple(c for c in case.evidence.criteria if c.criterion != "cgm-4a-insulin"),
    )

    verdict = adjudicate(case.claim, evidence, graph)

    assert verdict.outcome == "needs_human"
    assert verdict.path == ()
