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

from rcm_ai.codes import is_hcpcs_level2, is_icd10cm
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

TABULAR = "icd10cm_tabular"
# Each MCD export table a fixture can hold, with the columns that key one of its rows.
CSV_KEYS = {
    "lcd": ("lcd_id", "lcd_version"),
    "lcd_x_hcpc_code": ("lcd_id", "lcd_version", "hcpc_code_id", "hcpc_code_group"),
    "lcd_related_documents": ("lcd_id", "lcd_version", "related_num"),
    "article_x_icd10_covered": ("article_id", "article_version", "icd10_code_id", "icd10_covered_group"),
}


class IngestError(Exception):
    pass


@dataclass(frozen=True)
class Rejected:
    line: int
    reason: str


@dataclass(frozen=True)
class SourceLoad:
    """One fixture's ingest: records read, rows rejected (with why), rows loaded, and loaded
    records found in the graph afterwards, which must equal loaded."""
    path: str
    table: str
    parsed: int
    rejected: tuple[Rejected, ...]
    loaded: int
    in_graph: int


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
    seventh_from: str | None  # for an expanded code, the code whose sevenChrDef supplied the 7th character
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
    def __init__(self, dataset: Dataset, ingest: tuple[SourceLoad, ...] = ()):
        self._ds = dataset
        self.ingest = ingest

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
        """The code itself, then each broader concept: category, then section and chapter when loaded."""
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

    def lcds_listing(self, hcpcs: str) -> list[str]:
        rows = self._ds.query(
            "SELECT DISTINCT ?lcd WHERE { GRAPH ?g { ?lcd rcm:listsHcpcs ?hcpcs } }",
            initNs={"rcm": RCM}, initBindings={"hcpcs": HCPCS[hcpcs]},
        )
        return sorted(_local(r.lcd) for r in rows)

    def covered_list_incomplete(self) -> bool:
        """Whether any covered-diagnosis row was rejected at ingest, so a code missing from the
        covered list may only be missing from what loaded."""
        return any(s.rejected for s in self.ingest if s.table == "article_x_icd10_covered")

    def on_covered_list(self, dx: str) -> bool:
        """Whether any loaded article lists the code, whether or not the code is in the terminology slice."""
        return bool(self._ds.query(
            "ASK { GRAPH ?g { ?article rcm:coversDiagnosis ?dx } }",
            initNs={"rcm": RCM}, initBindings={"dx": ICD[dx]},
        ).askAnswer)

    def covered_codes_under(self, dx: str) -> list[str]:
        """Covered codes below dx (dx itself excluded). ICD-10-CM codes nest by prefix, which also
        reaches 7th-character codes (E11.37X1 under E11.37X) and codes outside the loaded slice."""
        rows = self._ds.query(
            """SELECT DISTINCT ?d WHERE { GRAPH ?g { ?article rcm:coversDiagnosis ?d }
                 FILTER(STRSTARTS(STR(?d), STR(?dx)) && ?d != ?dx) }""",
            initNs={"rcm": RCM}, initBindings={"dx": ICD[dx]},
        )
        return sorted(_local(r.d) for r in rows)

    def coverage_paths(self, hcpcs: str, dx: str) -> list[CoveragePath]:
        rows = self._ds.query(
            """
            SELECT ?lcd ?article ?citedVersion ?title ?anchor ?seventhFrom ?gList ?gRel ?gConcept ?gCov WHERE {
              GRAPH ?gList { ?lcd rcm:listsHcpcs ?hcpcs }
              GRAPH ?gRel { ?lcd rcm:relatedArticle ?article . ?article rcm:citedVersion ?citedVersion }
              GRAPH ?gConcept { ?dx skos:prefLabel ?title ; rcm:sourceAnchor ?anchor
                                OPTIONAL { ?dx rcm:seventhFrom ?seventhFrom } }
              GRAPH ?gCov { ?article rcm:coversDiagnosis ?dx }
            }""",
            initNs={"rcm": RCM, "skos": SKOS},
            initBindings={"hcpcs": HCPCS[hcpcs], "dx": ICD[dx]},
        )
        return [
            CoveragePath(
                _local(r.lcd), _local(r.article), str(r.citedVersion), str(r.title), str(r.anchor),
                _local(r.seventhFrom) if r.seventhFrom else None,
                self._source(r.gList), self._source(r.gRel),
                self._source(r.gConcept), self._source(r.gCov),
            )
            for r in rows
        ]

    def applicability(self, lcd: str, hcpcs: str) -> tuple[str, str, Source] | None:
        """(label, quote, source) of the curated criteria that apply to this item under this LCD."""
        rows = list(self._ds.query(
            """
            SELECT ?label ?quote ?g WHERE {
              GRAPH ?g { ?lcd rcm:criteriaApplyTo ?hcpcs . ?lcd rcm:applicabilityQuote ?quote ;
                         rcm:applicabilityLabel ?label }
            }""",
            initNs={"rcm": RCM},
            initBindings={"lcd": LCD[lcd], "hcpcs": HCPCS[hcpcs]},
        ))
        return (str(rows[0].label), str(rows[0].quote), self._source(rows[0].g)) if rows else None

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


def _reject_reason(table: str, row: dict) -> str | None:
    missing = [k for k in CSV_KEYS[table] if not row.get(k)]
    if missing:
        return f"missing {', '.join(missing)}"
    if table == "article_x_icd10_covered" and not is_icd10cm(row["icd10_code_id"]):
        return f"icd10_code_id {row['icd10_code_id']!r} is not an ICD-10-CM code"
    if table == "lcd_x_hcpc_code" and not is_hcpcs_level2(row["hcpc_code_id"]):
        return f"hcpc_code_id {row['hcpc_code_id']!r} is not a HCPCS Level II code"
    if table == "lcd_related_documents" and not row["r_article_id"]:
        return "links no article (an LCD or NCD link, which the graph does not model)"
    return None


def _tabular_lines(text_lines: list[str]) -> dict[str, int]:
    """Line of each diag code, section ("section-E08-E13") and chapter ("chapter-15") in a tabular file."""
    lines = {}
    for n, text in enumerate(text_lines, start=1):
        if m := re.search(r'<section id="([^"]+)">', text):
            lines[f"section-{m.group(1)}"] = n
        elif m := re.search(r"<name>([^<]+)</name>", text):
            chapter = n > 1 and text_lines[n - 2].strip() == "<chapter>"
            lines[f"chapter-{m.group(1)}" if chapter else m.group(1)] = n
    return lines


def build_graph(data_dir: Path) -> CoverageGraph:
    """Load every manifest fixture by its table, then every curated criteria file.

    Adding an LCD means adding its fixture rows and a curated criteria file; no code changes.
    Before load: manifest hashes and record counts, and key uniqueness per table. After load:
    every loaded record has its facts in the graph.
    """
    fixtures = Path(data_dir) / "fixtures"
    sources = verify_fixtures(fixtures)
    versions = {r.path: r.version for r in sources}
    unknown = sorted({r.table for r in sources} - {TABULAR, *CSV_KEYS})
    if unknown:
        raise IngestError(f"manifest names tables the graph cannot load: {', '.join(unknown)}")
    ds = Dataset()
    loads: list[tuple[str, str, int, tuple[Rejected, ...]]] = []

    def graph_for(path: str, line: int):
        iri = URIRef(f"urn:src:{path}#L{line}")
        ds.default_graph.add((iri, RCM.path, Literal(path)))
        ds.default_graph.add((iri, RCM.version, Literal(versions[path])))
        ds.default_graph.add((iri, RCM.line, Literal(line)))
        return ds.graph(iri)

    defined_in: dict[str, str] = {}
    for src in (s for s in sources if s.table == TABULAR):
        text_lines = (fixtures / src.path).read_text(encoding="utf-8").splitlines()
        lines = _tabular_lines(text_lines)

        def line_after(code: str, *needles: str) -> int:
            """The line of the last needle, searching each from where the one before it was found,
            starting at the code's own line."""
            n = lines[code]
            for needle in needles:
                n = next(i for i in range(n, len(text_lines) + 1) if needle in text_lines[i - 1])
            return n

        try:
            tab = parse_tabular(fixtures / src.path)
        except ValueError as e:
            raise IngestError(f"{src.path}: {e}") from e
        for code in tab.concepts:
            if code in defined_in:
                raise IngestError(f"{src.path}: {code} is already defined in {defined_in[code]}")
            defined_in[code] = src.path
        for concept in tab.concepts.values():
            if concept.kind == "seventh":
                # Source the expanded code to the sevenChrDef extension that defines its 7th character;
                # the code itself never appears in the file, so the anchor is that extension.
                anchor = f'<extension char="{concept.code[-1]}">'
                line = line_after(concept.seventh_defined_at, anchor)
            else:
                anchor, line = concept.code, lines[concept.code]
            g = graph_for(src.path, line)
            g.add((ICD[concept.code], SKOS.prefLabel, Literal(concept.title)))
            g.add((ICD[concept.code], RCM.kind, Literal(concept.kind)))
            g.add((ICD[concept.code], RCM.sourceAnchor, Literal(anchor)))
            if concept.seventh_defined_at:
                g.add((ICD[concept.code], RCM.seventhFrom, ICD[concept.seventh_defined_at]))
            if concept.parent:
                g.add((ICD[concept.code], SKOS.broader, ICD[concept.parent]))
        for i, rule in enumerate(tab.excludes1):
            # Anchor on the owner's <excludes1> first, so an identical Excludes2 note can't claim it.
            g = graph_for(src.path, line_after(rule.code, "<excludes1>", f"<note>{escape(rule.note)}</note>"))
            node = EXCLUDES1[f"{rule.code}-{i}"]
            g.add((ICD[rule.code], RCM.excludes1, node))
            g.add((node, RCM.refKind, Literal(rule.target.kind)))
            g.add((node, RCM.refStart, Literal(rule.target.start)))
            if rule.target.end:
                g.add((node, RCM.refEnd, Literal(rule.target.end)))
            g.add((node, RCM.note, Literal(rule.note)))
        loads.append((src.path, TABULAR, sum(1 for c in tab.concepts.values() if c.kind == "diag"), ()))

    lcd_records: dict[str, tuple[str, int, dict]] = {}
    # The graph names LCDs and articles without their version, so one version of each may load.
    doc_versions: dict[str, set[str]] = {}
    covered_groups: dict[str, set[str]] = {}
    for table in CSV_KEYS:  # lcd rows first, so curated criteria can find their record
        seen: dict[tuple, str] = {}
        for src in (s for s in sources if s.table == table):
            parsed, rejected = 0, []
            for line, row in _csv_records(fixtures / src.path):
                parsed += 1
                key = tuple(row.get(k) for k in CSV_KEYS[table])
                if all(key):
                    if key in seen:
                        named = ", ".join(f"{k}={v}" for k, v in zip(CSV_KEYS[table], key))
                        raise IngestError(f"{src.path}:{line}: duplicate key {named} (first at {seen[key]})")
                    seen[key] = f"{src.path}:{line}"
                if table == "article_x_icd10_covered" and row.get("icd10_covered_group"):
                    # Counted before rejection, so a group whose rows all fail still shows up.
                    covered_groups.setdefault(f"A{row['article_id']}", set()).add(row["icd10_covered_group"])
                if reason := _reject_reason(table, row):
                    rejected.append(Rejected(line, reason))
                    continue
                doc = f"A{row['article_id']}" if table == "article_x_icd10_covered" else f"L{row['lcd_id']}"
                doc_versions.setdefault(doc, set()).add(row["article_version" if doc[0] == "A" else "lcd_version"])
                g = graph_for(src.path, line)
                if table == "lcd":
                    lcd = LCD["L" + row["lcd_id"]]
                    g.add((lcd, RCM.title, Literal(row.get("title", ""))))
                    lcd_records[f"L{row['lcd_id']}"] = (src.path, line, row)
                elif table == "lcd_x_hcpc_code":
                    g.add((LCD["L" + row["lcd_id"]], RCM.listsHcpcs, HCPCS[row["hcpc_code_id"]]))
                elif table == "lcd_related_documents":
                    article = ARTICLE["A" + row["r_article_id"]]
                    g.add((LCD["L" + row["lcd_id"]], RCM.relatedArticle, article))
                    # The link names the article version it was made against, which can lag the
                    # article version whose covered codes we load; keep it so the path can show it.
                    g.add((article, RCM.citedVersion, Literal(row["r_article_version"])))
                else:
                    g.add((ARTICLE["A" + row["article_id"]], RCM.coversDiagnosis, ICD[row["icd10_code_id"]]))
            if parsed and len(rejected) == parsed:
                raise IngestError(f"{src.path}: 0 of {parsed} rows loaded; first rejection: "
                                  f"line {rejected[0].line}: {rejected[0].reason}")
            loads.append((src.path, table, parsed, tuple(rejected)))
    for doc, found in sorted(doc_versions.items()):
        if len(found) > 1:
            raise IngestError(f"{doc}: versions {', '.join(sorted(found))} loaded together; load one version")
    for doc, groups in sorted(covered_groups.items()):
        # Groups can mean "one code from group 1 plus one from group 2"; a flat covered list
        # would then overturn a claim that has only half the pair.
        if len(groups) > 1:
            raise IngestError(f"{doc}: covered codes in groups {', '.join(sorted(groups))}; "
                              "multi-group articles are not modeled")

    criterion_files: dict[str, str] = {}
    for path in sorted((Path(data_dir) / "curated").glob("*.json")):
        _add_curated(path, lcd_records, criterion_files, graph_for)

    report = []
    for path, table, parsed, rejected in loads:
        graphs = list(ds.default_graph.subjects(RCM.path, Literal(path)))
        if table == TABULAR:
            in_graph = len({s for g in graphs for s in ds.graph(g).subjects(RCM.kind, Literal("diag"))})
        else:
            in_graph = sum(1 for g in graphs if len(ds.graph(g)))
        s = SourceLoad(path, table, parsed, rejected, parsed - len(rejected), in_graph)
        if s.in_graph != s.loaded:
            raise IngestError(f"{path}: {s.loaded} records loaded but {s.in_graph} found in the graph")
        report.append(s)
    return CoverageGraph(ds, tuple(report))


def _add_curated(path: Path, lcd_records: dict, criterion_files: dict[str, str], graph_for) -> None:
    curated = json.loads(path.read_text())
    if curated["lcd"] not in lcd_records:
        raise IngestError(f"{path.name}: no loaded LCD record for {curated['lcd']}")
    source, line, row = lcd_records[curated["lcd"]]
    if source != curated["source"]:
        raise IngestError(f"{path.name}: {curated['lcd']} is loaded from {source}, not {curated['source']}")
    text = normalize_lcd_text(row[curated["column"]])

    def quotes(c):
        yield c["quote"]
        for sub in c.get("any_of", []):
            yield from quotes(sub)

    missing = [q for q in [curated["applies_to"]["quote"], *(q for c in curated["criteria"] for q in quotes(c))]
               if q not in text]
    if missing:
        raise IngestError(f"{path.name}: quotes not in the {curated['column']} text of {source}: {missing}")

    g = graph_for(source, line)
    lcd = LCD[curated["lcd"]]
    g.add((lcd, RCM.applicabilityLabel, Literal(curated["applies_to"]["label"])))
    g.add((lcd, RCM.applicabilityQuote, Literal(curated["applies_to"]["quote"])))
    for code in curated["applies_to"]["hcpcs"]:
        g.add((lcd, RCM.criteriaApplyTo, HCPCS[code]))

    def add_criterion(c: dict):
        # Criterion IRIs carry no LCD, so a reused id would merge two LCDs' criteria.
        if c["id"] in criterion_files:
            raise IngestError(f"{path.name}: criterion id {c['id']} is already defined in {criterion_files[c['id']]}")
        criterion_files[c["id"]] = path.name
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
