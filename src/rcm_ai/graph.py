"""Terminology and coverage graph built from pinned public-source fixtures.

Every source row's triples live in a named graph whose IRI identifies that row, so a
SPARQL answer can return, for each fact it used, the file, version and line it came from.
"""

import csv
import html
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

from rdflib import Dataset, Literal, Namespace, URIRef
from rdflib.namespace import SKOS

from xml.sax.saxutils import escape

from rcm_ai.icd10cm import CodeRef, parse_tabular
from rcm_ai.provenance import verify_fixtures
from rcm_ai.schemas import Source

RCM = Namespace("urn:rcm:")
ICD = Namespace("urn:icd10cm:")
HCPCS = Namespace("urn:hcpcs:")
LCD = Namespace("urn:lcd:")
ARTICLE = Namespace("urn:article:")
CRITERION = Namespace("urn:criterion:")
EXCLUDES1 = Namespace("urn:excludes1:")

TABULAR = "cdc/icd10cm-tabular-2027-E08-E13.xml"
LCD_HCPCS = "cms/lcd_x_hcpc_code-L33822-v70.csv"
LCD_RELATED = "cms/lcd_related_documents-L33822-v70.csv"
LCD_TEXT = "cms/lcd-L33822-v70.csv"
ARTICLE_COVERED = "cms/article_x_icd10_covered-A52464-v65.csv"
CRITERIA = "L33822-cgm-criteria.json"


def normalize_lcd_text(raw: str) -> str:
    """LCD text as a reader sees it: tags removed before unescaping, so "&lt;54" survives."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _csv_records(path: Path):
    """Yield (start_line, row) per CSV record; records may span lines."""
    text = path.read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    next_start = 2  # header is line 1
    for row in reader:
        yield next_start, row
        next_start = reader.line_num + 1


def _local(iri) -> str:
    return str(iri).rsplit(":", 1)[-1]


@dataclass(frozen=True)
class CoveragePath:
    lcd: str
    article: str
    cited_article_version: str
    title: str
    concept_anchor: str  # text a reviewer finds on the concept's source line
    listing: Source
    relation: Source
    concept: Source
    covered: Source


@dataclass(frozen=True)
class Excludes1Rule:
    owner: str  # the code whose Excludes1 note this is (the billed code or one of its ancestors)
    target: CodeRef
    note: str
    source: Source


@dataclass(frozen=True)
class Criterion:
    id: str
    quote: str
    source: Source
    satisfied_by: str | None
    any_of: tuple["Criterion", ...]


class CoverageGraph:
    def __init__(self, dataset: Dataset):
        self._ds = dataset

    def _source(self, g) -> Source:
        meta = {p: o for p, o in self._ds.default_graph.predicate_objects(g)}
        return Source(str(meta[RCM.path]), str(meta[RCM.version]), int(meta[RCM.line]))

    def title(self, code: str) -> str | None:
        rows = list(self._ds.query(
            "SELECT ?t WHERE { GRAPH ?g { ?c skos:prefLabel ?t } }",
            initNs={"skos": SKOS}, initBindings={"c": ICD[code]},
        ))
        return str(rows[0].t) if rows else None

    def ancestors(self, code: str) -> list[str]:
        """The code itself, then each broader concept up to its category."""
        chain = [code]
        while True:
            rows = list(self._ds.query(
                "SELECT ?p WHERE { GRAPH ?g { ?c skos:broader ?p } }",
                initNs={"skos": SKOS}, initBindings={"c": ICD[chain[-1]]},
            ))
            if not rows:
                return chain
            chain.append(_local(rows[0].p))

    def excludes1_rules(self, code: str) -> list[Excludes1Rule]:
        """Excludes1 notes that apply to a code: its own and every ancestor's."""
        rules = []
        for owner in self.ancestors(code):
            rows = self._ds.query(
                """
                SELECT ?kind ?start ?end ?note ?g WHERE {
                  GRAPH ?g { ?owner rcm:excludes1 ?r . ?r rcm:refKind ?kind ; rcm:refStart ?start ;
                             rcm:note ?note OPTIONAL { ?r rcm:refEnd ?end } }
                } ORDER BY ?g""",
                initNs={"rcm": RCM}, initBindings={"owner": ICD[owner]},
            )
            rules += [
                Excludes1Rule(owner, CodeRef(str(r.kind), str(r.start), str(r.end) if r.end else None),
                              str(r.note), self._source(r.g))
                for r in rows
            ]
        return rules

    def coverage_paths(self, hcpcs: str, dx: str) -> list[CoveragePath]:
        rows = self._ds.query(
            """
            SELECT ?lcd ?article ?citedVersion ?title ?anchor ?gList ?gRel ?gConcept ?gCov WHERE {
              GRAPH ?gList { ?lcd rcm:listsHcpcs ?hcpcs }
              GRAPH ?gRel { ?lcd rcm:relatedArticle ?article . ?article rcm:citedVersion ?citedVersion }
              GRAPH ?gConcept { ?dx skos:prefLabel ?title ; rcm:sourceAnchor ?anchor }
              GRAPH ?gCov { ?article rcm:coversDiagnosis ?dx }
            }""",
            initNs={"rcm": RCM, "skos": SKOS},
            initBindings={"hcpcs": HCPCS[hcpcs], "dx": ICD[dx]},
        )
        return [
            CoveragePath(
                _local(r.lcd), _local(r.article), str(r.citedVersion), str(r.title), str(r.anchor),
                self._source(r.gList), self._source(r.gRel),
                self._source(r.gConcept), self._source(r.gCov),
            )
            for r in rows
        ]

    def applicability(self, lcd: str, hcpcs: str) -> tuple[str, Source] | None:
        rows = list(self._ds.query(
            """
            SELECT ?quote ?g WHERE {
              GRAPH ?g { ?lcd rcm:criteriaApplyTo ?hcpcs . ?lcd rcm:applicabilityQuote ?quote }
            }""",
            initNs={"rcm": RCM},
            initBindings={"lcd": LCD[lcd], "hcpcs": HCPCS[hcpcs]},
        ))
        return (str(rows[0].quote), self._source(rows[0].g)) if rows else None

    def _criterion(self, crit) -> Criterion:
        rows = list(self._ds.query(
            """
            SELECT ?quote ?g ?satisfiedBy WHERE {
              GRAPH ?g { ?crit rcm:quote ?quote OPTIONAL { ?crit rcm:satisfiedBy ?satisfiedBy } }
            }""",
            initNs={"rcm": RCM},
            initBindings={"crit": crit},
        ))
        subs = sorted(r.sub for r in self._ds.query(
            "SELECT ?sub WHERE { GRAPH ?g { ?crit rcm:anyOf ?sub } }",
            initNs={"rcm": RCM}, initBindings={"crit": crit},
        ))
        r = rows[0]
        return Criterion(
            _local(crit), str(r.quote), self._source(r.g),
            _local(r.satisfiedBy) if r.satisfiedBy else None,
            tuple(self._criterion(s) for s in subs),
        )

    def criterion_ids(self) -> set[str]:
        rows = self._ds.query("SELECT ?crit WHERE { GRAPH ?g { ?crit rcm:quote ?q } }", initNs={"rcm": RCM})
        return {_local(r.crit) for r in rows}

    def criteria(self, lcd: str) -> list[Criterion]:
        crits = [r.crit for r in self._ds.query(
            "SELECT ?crit WHERE { GRAPH ?g { ?lcd rcm:requires ?crit } }",
            initNs={"rcm": RCM}, initBindings={"lcd": LCD[lcd]},
        )]
        return sorted((self._criterion(c) for c in crits), key=lambda c: c.id)


def build_graph(data_dir: Path) -> CoverageGraph:
    fixtures = Path(data_dir) / "fixtures"
    versions = {r.path: r.version for r in verify_fixtures(fixtures)}
    ds = Dataset()

    def graph_for(path: str, line: int):
        iri = URIRef(f"urn:src:{path}#L{line}")
        ds.default_graph.add((iri, RCM.path, Literal(path)))
        ds.default_graph.add((iri, RCM.version, Literal(versions[path])))
        ds.default_graph.add((iri, RCM.line, Literal(line)))
        return ds.graph(iri)

    tabular = fixtures / TABULAR
    text_lines = tabular.read_text(encoding="utf-8").splitlines()
    lines = {}
    for n, text in enumerate(text_lines, start=1):
        m = re.search(r"<name>([^<]+)</name>", text)
        if m:
            lines[m.group(1)] = n

    def line_after(code: str, needle: str) -> int:
        """First line at or below a code's <name> line that contains needle."""
        start = lines[code]
        return next(n for n in range(start, len(text_lines) + 1) if needle in text_lines[n - 1])

    tab = parse_tabular(tabular)
    for concept in tab.concepts.values():
        if concept.kind == "seventh":
            # Source the expanded code to the sevenChrDef extension that defines its 7th character;
            # the code itself never appears in the file, so the anchor is that extension.
            anchor = f'<extension char="{concept.code[-1]}">'
            line = line_after(concept.seventh_defined_at, anchor)
        else:
            anchor, line = concept.code, lines[concept.code]
        g = graph_for(TABULAR, line)
        g.add((ICD[concept.code], SKOS.prefLabel, Literal(concept.title)))
        g.add((ICD[concept.code], RCM.sourceAnchor, Literal(anchor)))
        if concept.parent:
            g.add((ICD[concept.code], SKOS.broader, ICD[concept.parent]))
    for i, rule in enumerate(tab.excludes1):
        g = graph_for(TABULAR, line_after(rule.code, f"<note>{escape(rule.note)}</note>"))
        node = EXCLUDES1[f"{rule.code}-{i}"]
        g.add((ICD[rule.code], RCM.excludes1, node))
        g.add((node, RCM.refKind, Literal(rule.target.kind)))
        g.add((node, RCM.refStart, Literal(rule.target.start)))
        if rule.target.end:
            g.add((node, RCM.refEnd, Literal(rule.target.end)))
        g.add((node, RCM.note, Literal(rule.note)))

    for line, row in _csv_records(fixtures / LCD_HCPCS):
        graph_for(LCD_HCPCS, line).add(
            (LCD["L" + row["lcd_id"]], RCM.listsHcpcs, HCPCS[row["hcpc_code_id"]])
        )
    for line, row in _csv_records(fixtures / LCD_RELATED):
        if row["r_article_id"]:
            g = graph_for(LCD_RELATED, line)
            article = ARTICLE["A" + row["r_article_id"]]
            g.add((LCD["L" + row["lcd_id"]], RCM.relatedArticle, article))
            # The link names the article version it was made against, which can lag the
            # article version whose covered codes we load; keep it so the path can show it.
            g.add((article, RCM.citedVersion, Literal(row["r_article_version"])))
    for line, row in _csv_records(fixtures / ARTICLE_COVERED):
        graph_for(ARTICLE_COVERED, line).add(
            (ARTICLE["A" + row["article_id"]], RCM.coversDiagnosis, ICD[row["icd10_code_id"]])
        )

    curated = json.loads((Path(data_dir) / "curated" / CRITERIA).read_text())
    (lcd_line, _), = _csv_records(fixtures / curated["source"])
    g = graph_for(curated["source"], lcd_line)
    lcd = LCD[curated["lcd"]]
    g.add((lcd, RCM.applicabilityQuote, Literal(curated["applies_to"]["quote"])))
    for code in curated["applies_to"]["hcpcs"]:
        g.add((lcd, RCM.criteriaApplyTo, HCPCS[code]))

    def add_criterion(c: dict):
        node = CRITERION[c["id"]]
        g.add((node, RCM.quote, Literal(c["quote"])))
        if "satisfied_by" in c:
            g.add((node, RCM.satisfiedBy, RCM[c["satisfied_by"]]))
        for sub in c.get("any_of", []):
            g.add((node, RCM.anyOf, CRITERION[sub["id"]]))
            add_criterion(sub)
        return node

    for c in curated["criteria"]:
        g.add((lcd, RCM.requires, add_criterion(c)))
    return CoverageGraph(ds)
