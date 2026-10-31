import json
import shutil
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = Path(sys.executable).parent / "rcm-ai"
CASE = "data/synthetic/case-001.json"


def _run(*args):
    return subprocess.run([str(CLI), *args], cwd=REPO, capture_output=True, text=True, timeout=60)


def test_triage_prints_the_verdict_and_every_hop_with_its_source_location():
    result = _run("triage", CASE)

    assert result.returncode == 0, result.stderr
    out = result.stdout.splitlines()
    assert out[0] == "case-001: overturnable"
    hops = [line for line in out if re.match(r"\s+\d+\. ", line)]
    assert len(hops) == 10
    assert "data/fixtures/cms/lcd_x_hcpc_code-L33822-v70.csv:31" in result.stdout
    assert "data/fixtures/cms/article_x_icd10_covered-A52464-v65.csv:343" in result.stdout
    assert "data/fixtures/cdc/icd10cm-tabular-2027-E08-E13.xml:1474" in result.stdout


def test_triage_reports_a_rejected_case_on_stderr_with_exit_code_2(tmp_path):
    raw = json.loads((REPO / CASE).read_text())
    raw["claim"]["hcpcs"] = "95250"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw))

    result = _run("triage", str(bad))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "CPT" in result.stderr


def test_triage_rejects_evidence_naming_an_unknown_criterion_with_exit_code_2(tmp_path):
    raw = json.loads((REPO / CASE).read_text())
    raw["evidence"]["criteria"][0]["criterion"] = "bogus"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(raw))

    result = _run("triage", str(bad))

    assert result.returncode == 2
    assert "bogus" in result.stderr


def _small_data(tmp_path):
    """The data dirs the eval needs, without the large gitignored raw downloads."""
    data = tmp_path / "data"
    for d in ("eval", "fixtures", "curated"):
        shutil.copytree(REPO / "data" / d, data / d)
    return data


def test_eval_runs_the_locked_set_and_writes_dated_per_case_results(tmp_path):
    result = _run("eval", "--out", str(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "20 cases ran (expected 20)" in result.stdout
    assert "graph" in result.stdout
    (written,) = tmp_path.glob("eval-*.json")
    record = json.loads(written.read_text())
    assert record["git_sha"] and record["date"]
    assert record["eval_set"]["expected_cases"] == 20
    assert len(record["eval_set"]["sha256"]) == 64
    assert record["eval_set"]["data_dir"].endswith("data")
    assert record["arms"]["graph"]["model"] is None
    assert len(record["results"]) == 20
    assert {r["case"] for r in record["results"]} == {f"ev-{i:02d}" for i in range(1, 21)}
    ev06 = next(r for r in record["results"] if r["case"] == "ev-06")
    assert ev06["outcome"] == ev06["gold"] == "overturnable"


def test_eval_fails_with_exit_code_2_on_a_partial_case_set(tmp_path):
    data = _small_data(tmp_path)
    (data / "eval" / "cases" / "ev-20.json").unlink()

    result = _run("eval", "--data", str(data), "--out", str(tmp_path))

    assert result.returncode == 2
    assert "expected 20" in result.stderr


def test_eval_fails_with_exit_code_3_when_an_arm_errors(tmp_path):
    data = _small_data(tmp_path)
    case = data / "eval" / "cases" / "ev-01.json"
    raw = json.loads(case.read_text())
    raw["evidence"]["criteria"][0]["criterion"] = "bogus"  # loads fine, adjudication raises
    case.write_text(json.dumps(raw))

    result = _run("eval", "--data", str(data), "--out", str(tmp_path))

    assert result.returncode == 3
    assert "1 arm error" in result.stderr
    assert "errors 1" in result.stdout


def test_triage_prints_the_reason_for_an_uphold():
    result = _run("triage", "data/eval/cases/ev-14.json")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "ev-14: uphold"
    assert "reason (criterion_not_met)" in result.stdout
    assert "last practitioner visit for diabetes was 11 months ago" in result.stdout


def test_eval_prints_false_uphold(tmp_path):
    result = _run("eval", "--out", str(tmp_path))

    assert "false uphold: 0 []" in result.stdout


def test_ingest_prints_counts_per_source_and_exits_0():
    result = _run("ingest")

    assert result.returncode == 0, result.stderr
    assert "cms/article_x_icd10_covered-A52464-v65.csv" in result.stdout
    line = next(l for l in result.stdout.splitlines() if "article_x_icd10_covered-A52464" in l)
    assert "parsed 461" in line and "rejected 0" in line and "loaded 461" in line and "in graph 461" in line


def test_ingest_fails_with_exit_code_2_on_a_duplicate_key(tmp_path):
    data = _small_data(tmp_path)
    path = data / "fixtures" / "cms" / "lcd_x_hcpc_code-L33822-v70.csv"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines + [lines[1]]), encoding="utf-8")
    manifest = json.loads((data / "fixtures" / "MANIFEST.json").read_text())
    entry = next(e for e in manifest["fixtures"] if e["path"] == "cms/lcd_x_hcpc_code-L33822-v70.csv")
    entry["record_count"] += 1
    entry["sha256"] = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    (data / "fixtures" / "MANIFEST.json").write_text(json.dumps(manifest))

    result = _run("ingest", "--data", str(data))

    assert result.returncode == 2
    assert "duplicate" in result.stderr
