"""Everything the web demo's recorded mode shows, as one JSON file the static page reads.

It runs no model: each verdict is recomputed from the graph, the appeal letter is the one recorded
in data/demo/demo.json, and the other arms' outcomes come from data/demo/run-*.json. The file
names the inputs it was built from, so a reviewer can tell whether it is current.
"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from rcm_ai import appeal as ap
from rcm_ai.adjudicate import adjudicate
from rcm_ai.evaluate import load_eval_set
from rcm_ai.graph import build_graph

DEFAULT_OUT = Path("public") / "demo-cases.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_record(demo_dir: Path) -> tuple[str, dict]:
    runs = sorted(demo_dir.glob("run-*.json"))
    if not runs:
        raise FileNotFoundError(f"no run-*.json in {demo_dir}")
    return runs[-1].name, json.loads(runs[-1].read_text())


def _sources(data_dir: Path, graph) -> list[dict]:
    manifest = json.loads((data_dir / "fixtures" / "MANIFEST.json").read_text())["fixtures"]
    loads = {s.path: s for s in graph.ingest}
    sources = []
    for f in manifest:
        s = loads.get(f["path"])
        sources.append({
            "path": f["path"],
            "description": f.get("description"),
            "version": f.get("version"),
            "retrieved": f.get("retrieved"),
            "source_url": f.get("source_url"),
            "sha256": f.get("sha256"),
            "expected_records": f.get("record_count"),
            "parsed": s.parsed if s else None,
            "rejected": len(s.rejected) if s else None,
            "in_graph": s.in_graph if s else None,
        })
    return sources


def build(data_dir: Path) -> dict:
    data_dir = Path(data_dir)
    eval_set = load_eval_set(data_dir / "eval")
    graph = build_graph(data_dir)
    demo = json.loads((data_dir / "demo" / "demo.json").read_text())
    run_name, run = _run_record(data_dir / "demo")
    cases = []
    for ec in eval_set.cases:
        case = ec.case
        verdict = adjudicate(case.claim, case.evidence, graph)
        entry = {
            "id": case.id,
            "featured": case.id in demo["cases"],
            "claim": asdict(case.claim),
            "note": case.note,
            "evidence": asdict(case.evidence) if case.evidence else None,
            "gold": {"verdict": ec.gold["verdict"], "reason": ec.gold.get("reason")},
            "verdict": asdict(verdict),
            "arms": {arm: outcomes.get(case.id) for arm, outcomes in run["outcomes"].items()},
        }
        recorded = demo["letters"].get(case.id)
        if recorded:
            check = ap.check_citations(recorded["letter"], verdict.path, verdict.findings)
            entry["letter"] = {
                "text": recorded["letter"],
                "recorded": recorded.get("recorded"),
                "model": recorded.get("model"),
                "arm": recorded.get("arm"),
                "git_sha": recorded.get("git_sha"),
                "check": {"passed": check.passed, "citations": list(check.citations),
                          "unsupported": list(check.unsupported)},
            }
        cases.append(entry)
    return {
        "generated_by": "rcm-ai export-demo",
        "inputs": {
            "eval_set_sha256": eval_set.sha256,
            "fixtures_manifest_sha256": _sha256(data_dir / "fixtures" / "MANIFEST.json"),
            "demo_sha256": _sha256(data_dir / "demo" / "demo.json"),
            "run_record": run_name,
            "run_record_sha256": _sha256(data_dir / "demo" / run_name),
        },
        "run": {**run["run"], "source": run["source"]},
        "sources": _sources(data_dir, graph),
        "cases": cases,
    }


def render(data_dir: Path) -> str:
    return json.dumps(build(data_dir), indent=1, ensure_ascii=False) + "\n"
