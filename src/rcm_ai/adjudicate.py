from rcm_ai.graph import CoverageGraph, Criterion
from rcm_ai.schemas import Claim, Evidence, Hop, SchemaError, Verdict

# Only medical-necessity denials can be overturned by showing coverage criteria are met;
# timely filing (29), duplicates (18) or missing information (16) need other remedies.
MEDICAL_NECESSITY_CARCS = {"50"}


def _met_leaf(c: Criterion, met: set[str]) -> Criterion | None:
    """The evidenced criterion that satisfies c: c itself, or one any-of branch, recursively."""
    if c.any_of:
        return next((leaf for s in c.any_of if (leaf := _met_leaf(s, met))), None)
    return c if c.id in met else None


def _criterion_hop(c: Criterion, dx: str, met: set[str]) -> Hop | None:
    if c.satisfied_by == "covered_diagnosis":
        return Hop(f"Criterion {c.id} met by covered diagnosis {dx}", c.quote, c.source)
    leaf = _met_leaf(c, met)
    if leaf is None:
        return None
    how = "documented evidence" if leaf is c else leaf.id
    return Hop(f"Criterion {c.id} met by {how}", leaf.quote, leaf.source)


def adjudicate(claim: Claim, evidence: Evidence, graph: CoverageGraph) -> Verdict:
    met = {c.criterion for c in evidence.criteria}
    unknown = sorted(met - graph.criterion_ids())
    if unknown:
        raise SchemaError(f"evidence names unknown criteria: {', '.join(unknown)}")
    if claim.carc not in MEDICAL_NECESSITY_CARCS or claim.coverage != "initial":
        return Verdict("needs_human", ())
    documented = {d.code for d in evidence.diagnoses}
    for dx in claim.icd10:
        if dx not in documented:
            continue
        for p in graph.coverage_paths(claim.hcpcs, dx):
            applies = graph.applicability(p.lcd, claim.hcpcs)
            if applies is None:
                continue
            criteria = [_criterion_hop(c, dx, met) for c in graph.criteria(p.lcd)]
            if None in criteria:
                continue
            quote, source = applies
            return Verdict("overturnable", (
                Hop(f"{claim.hcpcs} is listed in LCD {p.lcd}", claim.hcpcs, p.listing),
                Hop(f"LCD {p.lcd} initial CGM criteria (1)-(5) apply to {claim.hcpcs}", quote, source),
                Hop(
                    f"LCD {p.lcd} is related to article {p.article} (link cites v{p.cited_article_version})",
                    p.article[1:], p.relation,
                ),
                Hop(f"{dx} is an ICD-10-CM code: {p.title}", dx, p.concept),
                Hop(f"Article {p.article} covers {dx}", dx, p.covered),
                *criteria,
            ))
    return Verdict("needs_human", ())
