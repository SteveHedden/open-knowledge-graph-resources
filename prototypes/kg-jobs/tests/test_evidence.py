"""Every qualified/review record must expose field-level evidence;
every not_match record must lack positive (unnegated) evidence.
Network-free."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

EVIDENCE_FIELDS = {
    "concept_uri", "concept_label", "concept_scheme",
    "strength", "matched_phrase", "source_field", "negated",
}


@pytest.fixture(scope="module")
def generated_json():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate.py")], check=True, cwd=str(ROOT))
    with (ROOT / "data" / "jobs.json").open(encoding="utf-8") as f:
        return json.load(f)


def test_qualified_and_review_records_have_positive_evidence(generated_json):
    for record in generated_json:
        if record["classification"] in ("qualified", "review"):
            positive = [e for e in record["evidence"] if not e["negated"]]
            assert positive, f"{record['id']} is {record['classification']} but has no positive evidence"


def test_not_match_records_lack_positive_evidence(generated_json):
    for record in generated_json:
        if record["classification"] == "not_match":
            positive = [e for e in record["evidence"] if not e["negated"]]
            assert not positive, f"{record['id']} is not_match but has positive evidence: {positive}"


def test_evidence_entries_are_fully_explainable(generated_json):
    for record in generated_json:
        for ev in record["evidence"]:
            assert EVIDENCE_FIELDS <= ev.keys(), f"{record['id']} evidence entry missing fields: {ev}"
            assert ev["source_field"] in ("title", "description", "qualifications", "responsibilities")
            assert ev["strength"] in ("strong", "contextual")
            assert isinstance(ev["negated"], bool)
            assert ev["matched_phrase"], "matched_phrase must not be empty"


def test_no_hidden_score_literals(generated_json):
    """The prototype must not publish opaque numeric confidence scores."""
    for record in generated_json:
        assert "score" not in record
        for ev in record["evidence"]:
            assert "score" not in ev
