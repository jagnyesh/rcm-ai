from rcm_ai.graph import CoverageGraph, Criterion
from rcm_ai.icd10cm import ref_covers
from rcm_ai.schemas import Claim, Evidence, Finding, Hop, Reason, SchemaError, Verdict

# Only medical-necessity denials can be overturned by showing coverage criteria are met;
# timely filing (29), duplicates (18) or missing information (16) need other remedies.
MEDICAL_NECESSITY_CARCS = {"50"}


def _met_leaf(c: Criterion, met: set[str]) -> Criterion | None:
    """The evidenced criterion that satisfies c: c itself, or one any-of branch, recursively.
    Only leaves can satisfy: "history of problematic hypoglycemia" stated at the 4b level does
    not meet 4b, which needs a documented level 2 or level 3 event."""
    if c.any_of:
        return next((leaf for s in c.any_of if (leaf := _met_leaf(s, met))), None)
    return c if c.id in met else None


def _not_met_spans(c: Criterion, not_met: dict[str, str]) -> list[str]:
    """Note spans showing c fails: its own, or (any-of) every branch's, only when every branch fails."""
    if c.id in not_met:
        return [not_met[c.id]]
    if c.any_of:
        spans = [_not_met_spans(s, not_met) for s in c.any_of]
        if all(spans):
            return [x for branch in spans for x in branch]
    return []


def _claimed_met(c: Criterion, met: set[str]) -> bool:
    """Whether the evidence claims c is met at any level, satisfying or not."""
    return c.id in met or any(_claimed_met(s, met) for s in c.any_of)


def _conflicting(c: Criterion, met: set[str], not_met: dict[str, str]) -> bool:
    """Claimed met (at any level) and shown not met (its own id or every branch) at once."""
    if _claimed_met(c, met) and _not_met_spans(c, not_met):
        return True
    return any(_conflicting(s, met, not_met) for s in c.any_of)


def _assess(c: Criterion, dx: str, met: set[str], not_met: dict[str, str]) -> tuple[str, Hop | Reason]:
    """("met", Hop) or ("conflicting" | "not_met" | "missing", Reason) for one required criterion."""
    if c.satisfied_by == "covered_diagnosis":
        if c.id in not_met:  # the note contradicts the covered diagnosis it documents
            return "conflicting", Reason(
                "criterion_conflicting",
                f"Criterion {c.id} is met by covered diagnosis {dx} but the note says it is not",
                c.quote, c.source, not_met[c.id])
        return "met", Hop(f"Criterion {c.id} met by covered diagnosis {dx}", c.quote, c.source)
    if _conflicting(c, met, not_met):
        return "conflicting", Reason(
            "criterion_conflicting", f"Criterion {c.id} is documented both as met and as not met",
            c.quote, c.source)
    leaf = _met_leaf(c, met)
    if leaf is not None:
        how = "documented evidence" if leaf is c else leaf.id
        return "met", Hop(f"Criterion {c.id} met by {how}", leaf.quote, leaf.source)
    spans = _not_met_spans(c, not_met)
    if spans:
        said = "; ".join(f'"{x}"' for x in spans)
        return "not_met", Reason(
            "criterion_not_met", f"Criterion {c.id} is not met: the note says {said}", c.quote, c.source, spans[0])
    return "missing", Reason(
        "criterion_not_documented", f"Criterion {c.id} is not documented either way: request records",
        c.quote, c.source)


def _hand_off(*reasons: Reason) -> Verdict:
    return Verdict("needs_human", (), reasons=reasons)


def _combine(outcomes: list[Verdict]) -> Verdict:
    """Uphold only when every billed diagnosis leads to an uphold; otherwise hand off."""
    if outcomes and all(v.outcome == "uphold" for v in outcomes):
        return Verdict("uphold", (), reasons=tuple(dict.fromkeys(r for v in outcomes for r in v.reasons)))
    return _hand_off(*dict.fromkeys(r for v in outcomes if v.outcome == "needs_human" for r in v.reasons))


def _excludes1_conflicts(codes: tuple[str, ...], graph: CoverageGraph) -> list[Finding]:
    """One finding per conflicting pair of billed codes, citing the first rule that names it."""
    findings, seen = [], set()
    for a in codes:
        for rule in graph.excludes1_rules(a):
            for b in codes:
                pair = frozenset((a, b))
                if b != a and pair not in seen and ref_covers(rule.target, b):
                    seen.add(pair)
                    findings.append(Finding(
                        "excludes1", (a, b),
                        f"{a} and {b} cannot be billed together: {rule.owner} Excludes1 {rule.note}",
                        rule.note, rule.source,
                    ))
    return findings


def _is_without_complications(code: str, graph: CoverageGraph) -> bool:
    """The category's catch-all code (E08.9, E09.9, E10.9, E11.9, E13.9). Other titles that say
    "unspecified" (an unspecified eye, neuropathy, complication) are specific in what matters."""
    title = graph.title(code) or ""
    return code == f"{code[:3]}.9" and title.endswith("without complications")


def _support(dx: str, documented: set[str], hcpcs: str, graph: CoverageGraph) -> tuple[bool, Finding | None]:
    """Whether the note supports a billed diagnosis, and any specificity finding.

    A billed "without complications" code is supported by a documented code of the same category
    that is itself a real, covered code for this item (recode finding). Nothing else is: a billed
    code more specific than the note would be upcoding.
    """
    if dx in documented:
        return True, None
    if _is_without_complications(dx, graph):
        candidates = sorted(
            d for d in documented
            if d != dx and d[:3] == dx[:3] and graph.title(d) and graph.coverage_paths(hcpcs, d)
        )
        if candidates:
            more = candidates[0]
            return True, Finding(
                "specificity", (dx, more),
                f"Billed {dx} is covered, but the note documents the more specific {more}: correct and resubmit",
            )
    return False, None


def adjudicate(claim: Claim, evidence: Evidence, graph: CoverageGraph) -> Verdict:
    met = {c.criterion for c in evidence.criteria}
    not_met = {c.criterion: c.span for c in evidence.criteria_not_met}
    unknown = sorted((met | not_met.keys()) - graph.criterion_ids())
    if unknown:
        raise SchemaError(f"evidence names unknown criteria: {', '.join(unknown)}")
    if claim.carc not in MEDICAL_NECESSITY_CARCS:
        return _hand_off(Reason("denial_out_of_scope",
                                f"CARC {claim.carc} is not a medical-necessity denial; coverage documentation cannot overturn it"))
    if claim.coverage != "initial":
        return _hand_off(Reason("coverage_out_of_scope",
                                "Continued coverage needs dates of service and the six-month adherence visit, which are not modeled"))
    lcds = graph.lcds_listing(claim.hcpcs)
    if not lcds:
        return _hand_off(Reason("no_lcd_for_item", f"No LCD in the graph lists {claim.hcpcs}"))
    applicable = [lcd for lcd in lcds if graph.applicability(lcd, claim.hcpcs)]
    if not applicable:
        return _hand_off(Reason("item_not_in_criteria",
                                f"{claim.hcpcs} is listed in {', '.join(lcds)}, but none of the modeled coverage criteria apply to it"))
    documented = {d.code for d in evidence.diagnoses}
    # Conflicts among documented codes count too: a note that documents type 1 and type 2
    # together cannot support either one.
    codes = tuple(dict.fromkeys((*claim.icd10, *sorted(documented))))
    conflicts = _excludes1_conflicts(codes, graph)
    if conflicts:
        return Verdict("needs_human", (), tuple(conflicts), (Reason(
            "coding_conflict", "Diagnoses on the claim or in the note conflict under Excludes1; fix coding first"),))

    outcomes = []
    for dx in claim.icd10:
        supported, finding = _support(dx, documented, claim.hcpcs, graph)
        if not supported:
            outcomes.append(_hand_off(Reason(
                "diagnosis_not_documented", f"Billed {dx} is not supported by a diagnosis documented in the note")))
            continue
        paths = [p for p in graph.coverage_paths(claim.hcpcs, dx) if p.lcd in applicable]
        if not paths:
            if graph.on_covered_list(dx):
                outcomes.append(_hand_off(Reason(
                    "diagnosis_not_in_terminology", f"{dx} is on the covered list but not in the loaded ICD-10-CM slice")))
            elif graph.covered_codes_under(dx):
                outcomes.append(_hand_off(Reason(
                    "diagnosis_not_billable",
                    f"{dx} is a header code; covered codes under it ({', '.join(graph.covered_codes_under(dx)[:3])}...) "
                    "need the specific code: recode")))
            elif any(graph.on_covered_list(d) for d in documented):
                outcomes.append(_hand_off(Reason(
                    "covered_diagnosis_not_billed", f"Billed {dx} is not covered, but the note documents a covered diagnosis: recode")))
            else:
                outcomes.append(Verdict("uphold", (), reasons=(Reason(
                    "diagnosis_not_covered",
                    f"{dx} is not on the covered diagnosis list for {claim.hcpcs}, and no documented diagnosis is"),)))
            continue
        p = paths[0]
        assessed = [_assess(c, dx, met, not_met) for c in graph.criteria(p.lcd)]
        states = {state for state, _ in assessed}
        if states == {"met"}:
            quote, source = graph.applicability(p.lcd, claim.hcpcs)
            return Verdict("overturnable", (
                Hop(f"{claim.hcpcs} is listed in LCD {p.lcd}", claim.hcpcs, p.listing),
                Hop(f"LCD {p.lcd} initial CGM criteria (1)-(5) apply to {claim.hcpcs}", quote, source),
                Hop(
                    f"LCD {p.lcd} is related to article {p.article} (link cites v{p.cited_article_version})",
                    p.article[1:], p.relation,
                ),
                Hop(
                    f"{dx} is an ICD-10-CM code: {p.title}"
                    + (f" (7th character from {p.seventh_from} sevenChrDef)" if p.seventh_from else ""),
                    p.concept_anchor, p.concept,
                ),
                Hop(f"Article {p.article} covers {dx}", dx, p.covered),
                *(hop for _, hop in assessed),
            ), (finding,) if finding else ())
        # A documented failure decides the claim; a contradiction or a gap needs a person.
        for state, outcome in (("conflicting", "needs_human"), ("not_met", "uphold"), ("missing", "needs_human")):
            if state in states:
                outcomes.append(Verdict(outcome, (), reasons=tuple(r for s, r in assessed if s == state)))
                break
    return _combine(outcomes)
