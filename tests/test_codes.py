import shutil
from pathlib import Path

import pytest

from rcm_ai.codes import is_cpt, scan_fixtures_for_cpt

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "fixtures"


@pytest.mark.parametrize("code", ["99213", "00100", "0001F", "0042T", "0001U"])
def test_cpt_category_i_ii_iii_and_pla_codes_are_detected(code):
    assert is_cpt(code)


@pytest.mark.parametrize("code", ["E2103", "A4239", "J3420", "K0553", "E11.9", "9921", "992130"])
def test_hcpcs_level_ii_and_icd10_codes_are_not_cpt(code):
    assert not is_cpt(code)


def test_committed_procedure_code_fixtures_hold_no_cpt():
    scan = scan_fixtures_for_cpt(FIXTURES)

    assert scan.checked == 31
    assert scan.cpt_codes == []


def test_a_cpt_row_slipped_into_a_procedure_fixture_is_caught(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "fixtures")
    hcpcs = tmp_path / "fixtures" / "cms" / "lcd_x_hcpc_code-L33822-v70.csv"
    with hcpcs.open("a", encoding="utf-8", newline="") as fh:
        fh.write('33822,70,95250,1,1,"ILLUSTRATIVE ROW","X",2026-01-01,N\r\n')

    assert scan_fixtures_for_cpt(tmp_path / "fixtures").cpt_codes == ["95250"]
