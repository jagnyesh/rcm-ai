import json
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
