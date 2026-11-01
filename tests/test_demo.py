import json
import subprocess
import sys
from pathlib import Path

import rcm_ai
from rcm_ai import appeal as ap
from rcm_ai import extract as ex
from rcm_ai.adjudicate import adjudicate
from rcm_ai.graph import build_graph
from rcm_ai.schemas import load_case

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
CLI = Path(sys.executable).parent / "rcm-ai"
DEMO = json.loads((DATA / "demo" / "demo.json").read_text())


def _run(*args):
    return subprocess.run([str(CLI), "demo", *args], cwd=REPO, capture_output=True, text=True, timeout=120)


def _verdicts():
    graph = build_graph(DATA)
    for case_id in DEMO["cases"]:
        case = load_case(DATA / "eval" / "cases" / f"{case_id}.json")
        yield case, adjudicate(case.claim, case.evidence, graph)


def test_the_demo_shows_each_cases_claim_note_evidence_verdict_and_where_each_step_comes_from():
    result = _run()

    assert result.returncode == 0, result.stderr
    for case, verdict in _verdicts():
        assert f"{case.id}: {verdict.outcome}" in result.stdout
        assert f"{case.claim.hcpcs} for {', '.join(case.claim.icd10)}, denied CARC {case.claim.carc}" in result.stdout
        assert case.note in result.stdout
        for e in (*case.evidence.diagnoses, *case.evidence.criteria, *case.evidence.criteria_not_met):
            assert e.span in result.stdout
        for step in (*verdict.path, *verdict.reasons, *verdict.findings):
            assert step.statement in result.stdout
            if step.source:
                assert f"data/fixtures/{step.source.path}:{step.source.line}" in result.stdout


def test_the_demo_covers_every_verdict():
    assert {verdict.outcome for _, verdict in _verdicts()} == {"overturnable", "uphold", "needs_human"}


def test_the_demo_shows_each_recorded_letter_with_its_origin_and_checks_it_against_the_verdict():
    result = _run()

    verdicts = {case.id: verdict for case, verdict in _verdicts()}
    for case_id, recorded in DEMO["letters"].items():
        check = ap.check_citations(recorded["letter"], verdicts[case_id].path, verdicts[case_id].findings)
        assert check.passed
        for line in recorded["letter"].splitlines():
            assert line.strip() in result.stdout
        for origin in (recorded["model"], recorded["recorded"], recorded["git_sha"]):
            assert origin in result.stdout
        assert f"citation check: passed ({', '.join(check.citations)})" in result.stdout


def test_the_demo_calls_no_model(monkeypatch, capsys):
    def no_model(*args, **kwargs):
        raise AssertionError("the demo called the model")
    monkeypatch.setattr(rcm_ai, "_client", no_model)
    monkeypatch.setattr(ex, "send", no_model)

    assert rcm_ai.main(["demo", "--data", str(DATA)]) == 0


def test_the_demo_exits_4_when_a_recorded_letter_fails_its_check(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    for part in ("fixtures", "curated", "eval"):
        (data / part).symlink_to(DATA / part)
    demo = json.loads(json.dumps(DEMO))
    demo["letters"]["ev-01"]["letter"] += "\nThe beneficiary also qualifies under LCD L39999."
    (data / "demo").mkdir()
    (data / "demo" / "demo.json").write_text(json.dumps(demo))

    assert rcm_ai.main(["demo", "--data", str(data)]) == 4
    assert "citation check: FAILED, unsupported: L39999" in capsys.readouterr().out
