import csv
import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

MANIFEST = "MANIFEST.json"


class ProvenanceError(Exception):
    pass


@dataclass(frozen=True)
class FixtureSource:
    path: str
    table: str  # the source table the fixture's records come from, which decides how they load
    source_url: str
    version: str
    retrieved: str
    license: str
    record_kind: str
    record_count: int
    sha256: str


def _count_records(path: Path, kind: str) -> int:
    if kind == "diag":
        return sum(1 for _ in ET.parse(path).getroot().iter("diag"))
    if kind == "csv_row":
        with path.open(encoding="utf-8", newline="") as fh:
            return sum(1 for _ in csv.DictReader(fh))
    raise ProvenanceError(f"{path.name}: unknown record_kind {kind!r}")


def verify_fixtures(fixtures_dir: Path) -> list[FixtureSource]:
    entries = json.loads((fixtures_dir / MANIFEST).read_text())["fixtures"]
    records = [
        FixtureSource(**{k: e[k] for k in FixtureSource.__dataclass_fields__})
        for e in entries
    ]
    for r in records:
        actual = hashlib.sha256((fixtures_dir / r.path).read_bytes()).hexdigest()
        if actual != r.sha256:
            raise ProvenanceError(f"{r.path}: sha256 {actual} != manifest {r.sha256}")
        count = _count_records(fixtures_dir / r.path, r.record_kind)
        if count != r.record_count:
            raise ProvenanceError(f"{r.path}: {count} records != manifest {r.record_count}")
    on_disk = {
        p.relative_to(fixtures_dir).as_posix()
        for p in fixtures_dir.rglob("*")
        if p.is_file() and p.name != MANIFEST and not p.name.startswith(".")
    }
    unlisted = sorted(on_disk - {r.path for r in records})
    if unlisted:
        raise ProvenanceError(f"fixtures without a manifest entry: {', '.join(unlisted)}")
    return records
