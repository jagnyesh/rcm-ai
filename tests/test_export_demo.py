import json
import subprocess
import sys
from pathlib import Path

from rcm_ai import demo_export

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
PUBLIC = REPO / "public" / "demo-cases.json"
CLI = Path(sys.executable).parent / "rcm-ai"


def test_the_committed_demo_data_is_current_with_the_code_and_data():
    # If this fails, run `uv run rcm-ai export-demo` and commit public/demo-cases.json.
    assert PUBLIC.read_text() == demo_export.render(DATA)


def test_the_check_flag_reports_a_stale_file(tmp_path):
    out = tmp_path / "demo-cases.json"
    out.write_text("{}\n")
    stale = subprocess.run([str(CLI), "export-demo", "--out", str(out), "--check"], cwd=REPO, capture_output=True, text=True)
    assert stale.returncode == 1 and "out of date" in stale.stderr
    wrote = subprocess.run([str(CLI), "export-demo", "--out", str(out)], cwd=REPO, capture_output=True, text=True)
    assert wrote.returncode == 0, wrote.stderr
    current = subprocess.run([str(CLI), "export-demo", "--out", str(out), "--check"], cwd=REPO, capture_output=True, text=True)
    assert current.returncode == 0, current.stderr


def test_every_eval_case_is_exported_with_its_verdict_label_and_each_arms_outcome():
    data = json.loads(PUBLIC.read_text())
    ids = sorted(p.stem for p in (DATA / "eval" / "cases").glob("*.json"))
    assert [c["id"] for c in data["cases"]] == ids
    for c in data["cases"]:
        assert c["verdict"]["outcome"] in {"overturnable", "uphold", "needs_human"}
        assert c["gold"]["verdict"] in {"overturnable", "uphold", "needs_human"}
        assert set(c["arms"]) == {"graph", "extracted", "llm-only"}
    featured = [c["id"] for c in data["cases"] if c["featured"]]
    assert featured == json.loads((DATA / "demo" / "demo.json").read_text())["cases"]


def test_each_overturnable_step_names_a_source_row():
    data = json.loads(PUBLIC.read_text())
    for c in data["cases"]:
        for hop in c["verdict"]["path"]:
            assert hop["source"] and hop["source"]["path"] and hop["source"]["line"] >= 1, (c["id"], hop["statement"])


def test_every_source_in_the_manifest_is_shown_with_its_load_counts():
    data = json.loads(PUBLIC.read_text())
    manifest = json.loads((DATA / "fixtures" / "MANIFEST.json").read_text())["fixtures"]
    assert [s["path"] for s in data["sources"]] == [f["path"] for f in manifest]
    for s in data["sources"]:
        assert s["in_graph"] == s["expected_records"] and s["rejected"] == 0, s["path"]


def test_the_recorded_letter_passes_its_citation_check():
    data = json.loads(PUBLIC.read_text())
    letters = [c for c in data["cases"] if "letter" in c]
    assert letters
    for c in letters:
        assert c["verdict"]["outcome"] == "overturnable"
        assert c["letter"]["check"]["passed"], c["id"]


def test_the_run_records_accuracy_matches_the_readme_results():
    run = json.loads((DATA / "demo" / "run-2026-11-01.json").read_text())
    gold = {p.stem: json.loads(p.read_text())["gold"]["verdict"] for p in (DATA / "eval" / "cases").glob("*.json")}
    right = {arm: sum(o[c] == g for c, g in gold.items()) for arm, o in run["outcomes"].items()}
    assert right == {"graph": 20, "extracted": 16, "llm-only": 19}
    misses = {arm: sorted(c for c, g in gold.items() if o[c] != g) for arm, o in run["outcomes"].items()}
    assert misses["extracted"] == ["ev-09", "ev-11", "ev-12", "ev-13"]
    assert misses["llm-only"] == ["ev-18"]
