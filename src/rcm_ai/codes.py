import csv
import re
from dataclasses import dataclass
from pathlib import Path

# Category I is five digits; Category II/III and PLA are four digits plus F/T/U.
# CPT is AMA-licensed, so committed fixtures must never contain these.
_CPT = re.compile(r"\d{5}|\d{4}[FTU]")

# The column the CMS MCD exports use for procedure codes in both LCD and article tables.
_PROCEDURE_COLUMN = "hcpc_code_id"


def is_cpt(code: str) -> bool:
    return _CPT.fullmatch(code.strip().upper()) is not None


@dataclass(frozen=True)
class CptScan:
    checked: int
    cpt_codes: list[str]


def scan_fixtures_for_cpt(fixtures_dir: Path) -> CptScan:
    checked, found = 0, []
    for path in sorted(fixtures_dir.rglob("*.csv")):
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if _PROCEDURE_COLUMN not in (reader.fieldnames or []):
                continue
            for row in reader:
                checked += 1
                if is_cpt(row[_PROCEDURE_COLUMN]):
                    found.append(row[_PROCEDURE_COLUMN])
    return CptScan(checked, found)
