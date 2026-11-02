import json
import subprocess
import sys
from pathlib import Path

import anthropic
import pytest

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


def _data_with(tmp_path, demo, cases=None):
    """A data dir sharing the real sources, with its own demo file and, if given, its own eval cases."""
    data = tmp_path / "data"
    data.mkdir()
    for part in ("fixtures", "curated"):
        (data / part).symlink_to(DATA / part)
    if cases is None:
        (data / "eval").symlink_to(DATA / "eval")
    else:
        (data / "eval" / "cases").mkdir(parents=True)
        for case_id, raw in cases.items():
            (data / "eval" / "cases" / f"{case_id}.json").write_text(json.dumps(raw))
    (data / "demo").mkdir()
    if demo is not None:
        (data / "demo" / "demo.json").write_text(demo if isinstance(demo, str) else json.dumps(demo))
    return data


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
        # each span is in the printed note, so only its evidence line shows the evidence was printed
        for d in case.evidence.diagnoses:
            assert f'{d.code}: "{d.span}"' in result.stdout
        for c in case.evidence.criteria:
            assert f'{c.criterion} met: "{c.span}"' in result.stdout
        for c in case.evidence.criteria_not_met:
            assert f'{c.criterion} not met: "{c.span}"' in result.stdout
        for step in (*verdict.path, *verdict.reasons, *verdict.findings):
            assert step.statement in result.stdout
            if step.source:
                assert f"data/fixtures/{step.source.path}:{step.source.line}" in result.stdout


def test_the_demo_cases_cover_every_verdict():
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
    monkeypatch.setattr(anthropic, "Anthropic", no_model)

    assert rcm_ai.main(["demo", "--data", str(DATA)]) == 0


def test_the_demo_exits_4_when_a_recorded_letter_fails_its_check(tmp_path, capsys):
    demo = json.loads(json.dumps(DEMO))
    demo["letters"]["ev-01"]["letter"] += "\nThe beneficiary also qualifies under LCD L39999."

    assert rcm_ai.main(["demo", "--data", str(_data_with(tmp_path, demo))]) == 4
    assert "citation check: FAILED, unsupported: L39999" in capsys.readouterr().out


def test_the_demo_fails_a_recorded_letter_for_a_claim_it_wont_overturn(tmp_path, capsys):
    demo = json.loads(json.dumps(DEMO))
    demo["letters"]["ev-10"] = demo["letters"]["ev-01"]

    assert rcm_ai.main(["demo", "--data", str(_data_with(tmp_path, demo))]) == 4
    out = capsys.readouterr().out
    assert "ev-10: uphold" in out
    assert "recorded letter FAILED: the verdict is not overturnable, so no letter is warranted" in out


@pytest.mark.parametrize("demo", [None, "{not json", {"cases": ["ev-01"]}, {"cases": ["no-such-case"], "letters": {}}],
                         ids=["missing", "not json", "no letters", "unknown case"])
def test_the_demo_rejects_missing_or_malformed_demo_data_before_printing_anything(tmp_path, capsys, demo):
    assert rcm_ai.main(["demo", "--data", str(_data_with(tmp_path, demo))]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("rejected: ")


def test_the_demo_rejects_a_case_without_evidence(tmp_path, capsys):
    raw = json.loads((DATA / "eval" / "cases" / "ev-01.json").read_text())
    del raw["evidence"]

    data = _data_with(tmp_path, {"cases": ["ev-01"], "letters": {}}, cases={"ev-01": raw})
    assert rcm_ai.main(["demo", "--data", str(data)]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "ev-01" in out.err and "no evidence" in out.err
