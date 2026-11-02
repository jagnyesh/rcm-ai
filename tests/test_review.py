import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
STATUSES = {"claude-drafted", "confirmed"}
EXPERTISE = {"coding", "clinical", "payer-policy", "appeals", "legal"}


def _reviewable():
    """Each thing Claude wrote that a person has to review: every case, criteria file and recorded letter."""
    for folder in ("eval/cases", "synthetic", "curated"):
        for path in sorted((DATA / folder).glob("*.json")):
            yield str(path.relative_to(DATA)), json.loads(path.read_text()).get("review")
    for case_id, recorded in json.loads((DATA / "demo" / "demo.json").read_text())["letters"].items():
        yield f"demo letter for {case_id}", recorded.get("review")


def test_everything_claude_wrote_for_a_person_to_review_carries_a_review_tag():
    untagged = [where for where, review in _reviewable() if review is None]
    assert not untagged, f"no review tag: {untagged}"


def test_each_review_tag_names_its_status_expertise_scope_and_issue():
    for where, review in _reviewable():
        assert review["status"] in STATUSES, where
        assert review["expertise"] and set(review["expertise"]) <= EXPERTISE, where
        assert review["covers"].strip(), where
        assert re.fullmatch(r"#\d+", review["issue"]), where
