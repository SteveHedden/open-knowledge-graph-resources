"""Strict, input-order-independent cross-source reconciliation tests."""

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from reconcile import match_method, normalize_title, reconcile_records  # noqa: E402


def records():
    common = {
        "organizationIri": "https://openknowledgegraphs.com/organization/example/",
        "title": "Senior C++ / C# KG Engineer (RDF-2)",
        "description": "Build RDF systems.",
        "locationKeys": ["New York", "US"],
        "workplaceMode": "hybrid",
        "datePosted": "2026-08-25",
        "requisitionId": "REQ-42",
        "employmentType": "full-time",
        "validThrough": "2026-09-30",
        "firstSeenAt": "2026-08-25T12:00:00Z",
        "lastSeenAt": "2026-08-26T12:00:00Z",
    }
    first = dict(common, **{
        "id": "first", "firstParty": True, "provider": "greenhouse", "tenant": "example",
        "sourceRecordId": "42", "sourceDataset": "https://example.test/first",
        "canonicalUrl": "https://jobs.example.test/42", "sourceUrl": "https://jobs.example.test/42",
        "classification": "qualified", "evidence": [], "active": True,
    })
    aggregate = dict(common, **{
        "id": "aggregate", "firstParty": False, "provider": "aggregator", "tenant": None,
        "sourceRecordId": "copy-42", "sourceDataset": "https://example.test/aggregate",
        "canonicalUrl": "https://aggregate.test/42", "sourceUrl": "https://aggregate.test/42",
        "datePosted": "2026-08-24", "firstSeenAt": "2026-08-24T08:00:00Z",
        "lastSeenAt": "2026-08-27T08:00:00Z",
    })
    return first, aggregate


def test_title_normalization_preserves_seniority_parentheses_codes_plus_and_hash():
    assert normalize_title("Senior C++ / C# KG Engineer (RDF–2)") == (
        "senior c++ / c# kg engineer (rdf-2)"
    )
    assert normalize_title("C++ Engineer") != normalize_title("C Engineer")
    assert normalize_title("Senior Engineer") != normalize_title("Engineer")


def test_strict_reviewed_fields_merge_to_first_party_and_retain_sorted_provenance():
    first, aggregate = records()
    method, _ = match_method(first, aggregate)
    assert method == "exact-reviewed-fields-requisition"
    merged, audit = reconcile_records([aggregate, first])
    assert audit["mergedAggregatorOccurrences"] == 1
    assert len(merged) == 1
    result = merged[0]
    assert result["id"] == "first"
    assert result["canonicalUrl"] == first["canonicalUrl"]
    assert result["firstSeenAt"] == aggregate["firstSeenAt"]
    assert result["lastSeenAt"] == aggregate["lastSeenAt"]
    assert [row["firstParty"] for row in result["sourceOccurrences"]] == [True, False]


def test_organization_mismatch_missing_location_date_and_contradiction_each_veto():
    first, aggregate = records()
    cases = []
    mismatch = copy.deepcopy(aggregate)
    mismatch["organizationIri"] = "https://openknowledgegraphs.com/organization/other/"
    cases.append(mismatch)
    missing_location = copy.deepcopy(aggregate)
    missing_location["locationKeys"] = []
    cases.append(missing_location)
    stale = copy.deepcopy(aggregate)
    stale["datePosted"] = "2026-08-20"
    cases.append(stale)
    contradiction = copy.deepcopy(aggregate)
    contradiction["employmentType"] = "part-time"
    cases.append(contradiction)
    assert all(match_method(first, candidate)[0] is None for candidate in cases)


def test_full_description_digest_is_allowed_but_partial_or_missing_description_is_not():
    first, aggregate = records()
    first["requisitionId"] = None
    aggregate["requisitionId"] = None
    assert match_method(first, aggregate)[0] == "exact-reviewed-fields-description-digest"
    aggregate["description"] = "Build RDF systems and something else."
    assert match_method(first, aggregate)[0] is None


def test_multiple_first_party_candidates_merge_none_and_output_is_order_independent():
    first, aggregate = records()
    duplicate = copy.deepcopy(first)
    duplicate["id"] = "first-duplicate"
    duplicate["canonicalUrl"] = "https://jobs.example.test/duplicate"
    duplicate["sourceUrl"] = duplicate["canonicalUrl"]
    left, left_audit = reconcile_records([aggregate, duplicate, first])
    right, right_audit = reconcile_records([first, aggregate, duplicate])
    assert left == right
    assert left_audit == right_audit
    assert left_audit["mergedAggregatorOccurrences"] == 0
    assert left_audit["ambiguous"][0]["candidateIds"] == ["first", "first-duplicate"]


def test_reconciliation_is_idempotent_for_retained_occurrence_provenance():
    first, aggregate = records()
    once, first_audit = reconcile_records([first, aggregate])
    twice, second_audit = reconcile_records(once)
    assert twice == once
    assert len(twice[0]["sourceOccurrences"]) == 2
    assert first_audit["mergedAggregatorOccurrences"] == 1
    assert second_audit["outputRecords"] == 1
