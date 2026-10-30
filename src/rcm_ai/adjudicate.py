from rcm_ai.graph import CoverageGraph, Criterion
from rcm_ai.schemas import Claim, Evidence, Hop, Verdict


def _criterion_hop(c: Criterion, dx: str, met: set[str]) -> Hop | None:
    if c.satisfied_by == "covered_diagnosis":
        return Hop(f"Criterion {c.id} met by covered diagnosis {dx}", c.quote, c.source)
    if c.any_of:
        sub = next((s for s in c.any_of if s.id in met), None)
        return Hop(f"Criterion {c.id} met by {sub.id}", sub.quote, sub.source) if sub else None
    if c.id in met:
        return Hop(f"Criterion {c.id} met by documented evidence", c.quote, c.source)
    return None


def adjudicate(claim: Claim, evidence: Evidence, graph: CoverageGraph) -> Verdict:
    documented = {d.code for d in evidence.diagnoses}
    met = {c.criterion for c in evidence.criteria}
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
                Hop(f"LCD {p.lcd} criteria apply to {claim.hcpcs}", quote, source),
                Hop(f"LCD {p.lcd} is related to article {p.article}", p.article[1:], p.relation),
                Hop(f"{dx} is an ICD-10-CM code: {p.title}", dx, p.concept),
                Hop(f"Article {p.article} covers {dx}", dx, p.covered),
                *criteria,
            ))
    return Verdict("needs_human", ())
