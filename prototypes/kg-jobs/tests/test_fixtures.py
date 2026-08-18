"""Fixture regression suite: every generated decision must match its
reviewed expectation. Network-free."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from classifier import classify, find_evidence, load_match_terms  # noqa: E402

VOCAB_BASE = "https://openknowledgegraphs.com/prototypes/kg-jobs/vocab/"


@pytest.fixture(scope="module")
def fixtures():
    with (ROOT / "fixtures" / "jobs.json").open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def terms():
    return load_match_terms(ROOT / "vocabularies" / "kg-jobs.ttl")


def test_exactly_twenty_fixtures(fixtures):
    assert len(fixtures) == 20


def test_fixture_outcome_spread(fixtures):
    """Require an intentional spread across all three outcomes."""
    outcomes = [fx["expected_classification"] for fx in fixtures]
    assert outcomes.count("qualified") >= 3
    assert outcomes.count("review") >= 3
    assert outcomes.count("not_match") >= 3


def _classify_fixture(fx, terms):
    evidence = find_evidence(fx, terms)
    classification = classify(evidence)
    actual_concepts = sorted(
        {e.concept_uri[len(VOCAB_BASE):] for e in evidence if not e.negated}
    )
    return classification, actual_concepts, evidence


@pytest.mark.parametrize("fx_id", [f"f{i:03d}" for i in range(1, 21)])
def test_fixture_matches_reviewed_expectation(fx_id, fixtures, terms):
    fx = next(f for f in fixtures if f["id"] == fx_id)
    classification, actual_concepts, _ = _classify_fixture(fx, terms)
    assert classification == fx["expected_classification"], (
        f"{fx_id}: expected {fx['expected_classification']}, got {classification}"
    )
    assert actual_concepts == sorted(fx["expected_concepts"]), (
        f"{fx_id}: expected concepts {sorted(fx['expected_concepts'])}, got {actual_concepts}"
    )


def test_generic_graph_language_does_not_qualify(fixtures, terms):
    """f008: 'graph analytics dashboards' must not trigger a match --
    isolated/generic graph language is not in the controlled vocabulary."""
    fx = next(f for f in fixtures if f["id"] == "f008")
    classification, actual_concepts, _ = _classify_fixture(fx, terms)
    assert classification == "not_match"
    assert actual_concepts == []


def test_employer_identity_does_not_influence_score(fixtures, terms):
    """f016: employer name 'GraphPoint Hospitality Group' looks KG-related
    but must not be searched -- only posting content fields are searched."""
    fx = next(f for f in fixtures if f["id"] == "f016")
    classification, actual_concepts, _ = _classify_fixture(fx, terms)
    assert classification == "not_match"
    assert actual_concepts == []
    assert "GraphPoint" in fx["hiringOrganization"]


def test_substring_collision_does_not_qualify(fixtures, terms):
    """f010: lowercase 'owl' inside ordinary wildlife text must not match
    the case-sensitive OWL acronym term."""
    fx = next(f for f in fixtures if f["id"] == "f010")
    classification, actual_concepts, _ = _classify_fixture(fx, terms)
    assert classification == "not_match"
    assert actual_concepts == []
    assert "owl" in fx["description"].lower()


def test_negated_phrase_does_not_count_as_positive_evidence(fixtures, terms):
    """f013: 'No SPARQL experience is required' must not count SPARQL as
    positive evidence."""
    fx = next(f for f in fixtures if f["id"] == "f013")
    classification, actual_concepts, evidence = _classify_fixture(fx, terms)
    assert classification == "not_match"
    assert actual_concepts == []
    negated_matches = [e for e in evidence if e.negated]
    assert any(e.matched_phrase == "SPARQL" for e in negated_matches), (
        "expected SPARQL to be detected but marked negated"
    )
