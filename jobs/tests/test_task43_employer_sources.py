"""Issue 63 peer-employer evidence and review-only source contracts."""

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF, SKOS

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
FIXTURES = ROOT / "tests" / "fixtures" / "first-party-pilot"
sys.path.insert(0, str(ROOT / "scripts"))

import first_party_sources as fps  # noqa: E402
from classifier import load_match_terms  # noqa: E402
from first_party_classifier import (  # noqa: E402
    classify_first_party_records,
    load_first_party_policy,
)
from first_party_pilot import run_pilot  # noqa: E402
from live_pipeline import run_pipeline  # noqa: E402

OKG = Namespace("https://openknowledgegraphs.com/ontology#")
KGJOBS = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
ORG_BASE = "https://openknowledgegraphs.com/organization/"

PEER_ORGANIZATIONS = {
    "jpmorgan-chase", "siemens", "sap", "amazon", "accenture",
    "crowdstrike", "bloomberg", "capital-one",
}
REVIEW_SOURCES = {
    "first-party-jpmorgan-chase", "first-party-sap", "first-party-amazon",
    "first-party-accenture", "first-party-crowdstrike", "first-party-capital-one",
}
DEFERRED_ORGANIZATIONS = {"siemens", "bloomberg"}
APPROVAL = ROOT / "audits" / "task43-production-approval.json"


def _fixture(source_key):
    return json.loads((FIXTURES / f"{source_key}.json").read_text())


def test_peer_organizations_apply_only_the_six_reviewed_production_approvals():
    graph = Graph().parse(REPO_ROOT / "organizations.ttl", format="turtle")
    for identifier in PEER_ORGANIZATIONS:
        subject = URIRef(f"{ORG_BASE}{identifier}/")
        assert (subject, RDF.type, OKG.Organization) in graph
        assert list(graph.objects(subject, KGJOBS.inclusionEvidence))
        assert list(graph.objects(subject, KGJOBS.careersPage))
        assert graph.value(subject, OKG.jobsProductionEnabled).toPython() is (
            identifier not in DEFERRED_ORGANIZATIONS
        )
        assert str(graph.value(subject, KGJOBS.reviewStatus)) == "evidence-reviewed"
    jpmorgan = URIRef(f"{ORG_BASE}jpmorgan-chase/")
    assert {str(value) for value in graph.objects(jpmorgan, SKOS.altLabel)} >= {
        "JPMorgan Chase", "JPMorganChase", "J.P. Morgan",
    }


def test_six_bounded_sources_are_production_approved_with_exact_reviewed_adapters():
    sources = fps.load_first_party_sources()
    assert REVIEW_SOURCES <= set(sources)
    assert REVIEW_SOURCES <= set(fps.load_production_first_party_sources())
    expected = {
        "first-party-jpmorgan-chase": fps.ORACLE_RECRUITING_ADAPTER,
        "first-party-sap": fps.SUCCESSFACTORS_RMK_ADAPTER,
        "first-party-amazon": fps.AMAZON_JOBS_ADAPTER,
        "first-party-accenture": fps.WORKDAY_QUERY_ADAPTER,
        "first-party-crowdstrike": fps.WORKDAY_QUERY_ADAPTER,
        "first-party-capital-one": fps.WORKDAY_QUERY_ADAPTER,
    }
    for key, adapter in expected.items():
        source = sources[key]
        assert source.adapter == adapter
        assert source.republication_status == "production-approved"
        assert source.production_approved is True
        assert source.refresh_interval_seconds == 86400
        assert source.max_requests_per_batch <= 64


def test_all_new_adapter_fixtures_normalize_with_first_party_provenance():
    sources = fps.load_first_party_sources()
    for key in sorted(REVIEW_SOURCES):
        records = fps.records_from_payload(_fixture(key), sources[key])
        assert records
        for record in records:
            assert record["organizationIri"] == sources[key].organization_iri
            assert record["firstParty"] is True
            assert record["sourceOccurrences"] == [{
                "sourceDataset": sources[key].dataset_uri,
                "sourceRecordId": record["sourceRecordId"],
                "sourceUrl": record["canonicalUrl"],
                "provider": sources[key].provider,
                "tenant": sources[key].tenant,
                "firstParty": True,
            }]


def test_jpmorgan_context_plane_requisition_qualifies_without_classifier_changes():
    source = fps.load_first_party_sources()["first-party-jpmorgan-chase"]
    records = fps.records_from_payload(_fixture(source.key), source)
    classified = classify_first_party_records(
        records,
        load_match_terms(ROOT / "vocabularies" / "kg-jobs.ttl"),
        load_first_party_policy(ROOT / "vocabularies" / "kg-jobs.ttl"),
    )
    assert [(row["sourceRecordId"], row["classification"]) for row in classified] == [
        ("210773809", "qualified")
    ]
    assert classified[0]["canonicalUrl"].endswith("/CX_1001/job/210773809/")


def test_partial_or_cross_host_payloads_fail_closed():
    sources = fps.load_first_party_sources()
    oracle = deepcopy(_fixture("first-party-jpmorgan-chase"))
    oracle["details"] = []
    with pytest.raises(fps.FirstPartySourceError, match="details do not match"):
        fps.records_from_payload(oracle, sources["first-party-jpmorgan-chase"])

    amazon = deepcopy(_fixture("first-party-amazon"))
    amazon["listingPages"][0]["hits"] = 2
    with pytest.raises(fps.FirstPartySourceError, match="partial"):
        fps.records_from_payload(amazon, sources["first-party-amazon"])

    sap = deepcopy(_fixture("first-party-sap"))
    sap["details"][0]["url"] = sap["details"][0]["url"].replace(
        "jobs.sap.com", "evil.example"
    )
    with pytest.raises(fps.FirstPartySourceError, match="details do not match"):
        fps.records_from_payload(sap, sources["first-party-sap"])


def test_query_scoped_workday_accepts_provider_zero_total_only_after_first_page():
    source = fps.load_first_party_sources()["first-party-accenture"]
    payload = deepcopy(_fixture(source.key))
    first = payload["listingPages"][0]["jobPostings"][0]
    second = deepcopy(first)
    second["externalPath"] = second["externalPath"].replace("R001", "R004")
    second["title"] = "Ontology Consultant"
    payload["listingPages"][0]["total"] = 2
    payload["listingPages"].append({"jobPostings": [second], "total": 0})
    detail = deepcopy(payload["details"][0])
    detail["externalPath"] = detail["externalPath"].replace("R001", "R004")
    detail["payload"]["jobPostingInfo"]["externalUrl"] = (
        detail["payload"]["jobPostingInfo"]["externalUrl"].replace("R001", "R004")
    )
    detail["payload"]["jobPostingInfo"]["jobReqId"] = "R004"
    payload["details"].append(detail)
    payload["requestBatches"] = [{
        "batch": 1, "listingRequests": 2, "detailRequests": 2,
    }]
    assert len(fps.records_from_payload(payload, source)) == 2

    payload["listingPages"][1]["total"] = 3
    with pytest.raises(fps.FirstPartySourceError, match="total changed"):
        fps.records_from_payload(payload, source)


def test_review_fixture_run_is_isolated_and_never_publishes(tmp_path):
    runtime = tmp_path / "task43-review"
    run = run_pilot(
        fixtures=FIXTURES, runtime_dir=runtime,
        selected_sources=sorted(REVIEW_SOURCES),
        retrieved_at="2026-08-31T12:00:00Z",
    )
    assert run["publicationPerformed"] is False
    assert run["mode"] == "network-free-fixtures"
    assert {row["sourceKey"] for row in run["sourceResults"]} == REVIEW_SOURCES
    assert all(row["status"] == "refreshed" for row in run["sourceResults"])
    assert json.loads((runtime / "run.json").read_text())["publicationPerformed"] is False


@pytest.mark.parametrize("source_key", sorted(REVIEW_SOURCES))
def test_each_approved_source_runs_through_the_real_production_pipeline(
    tmp_path, source_key,
):
    payload = _fixture(source_key)
    source = fps.load_production_first_party_sources()[source_key]
    normalized = fps.records_from_payload(payload, source)
    classified = classify_first_party_records(
        normalized,
        load_match_terms(ROOT / "vocabularies" / "kg-jobs.ttl"),
        load_first_party_policy(ROOT / "vocabularies" / "kg-jobs.ttl"),
    )
    expected = {
        row["sourceRecordId"] for row in classified
        if row["classification"] == "qualified"
    }
    runtime = tmp_path / source_key
    run_pipeline(
        source_key=source_key,
        runtime_dir=runtime,
        retrieved_at="2026-08-31T16:00:00Z",
        first_party_fetcher=lambda _source: payload,
    )
    public = json.loads((runtime / "jobs.json").read_text())
    diagnostics = json.loads(
        (runtime / "sources" / f"{source_key}.json").read_text()
    )
    assert {row["sourceRecordId"] for row in public} == expected
    assert len(diagnostics) == len(classified)
    assert all(row["classification"] == "qualified" for row in public)


def test_tracked_manager_review_audit_is_complete_and_nonpublishing():
    audit = json.loads(
        (ROOT / "audits" / "task43-peer-employer-review.json").read_text()
    )
    assert audit["issue"] == 63
    assert audit["reviewStatus"] == "review-complete"
    assert audit["counts"] == {
        "deferredSources": 2,
        "evidenceQualifiedOrganizations": 8,
        "notMatch": 137,
        "pipelineReadyReviewedSources": 6,
        "qualified": 330,
        "records": 485,
        "review": 18,
    }
    assert not any(audit["constraints"].values())
    decisions = audit["organizationAndSourceDecisions"]
    assert {row["organization"] for row in decisions if row["sourceOutcome"] == "deferred"} == {
        "Bloomberg L.P.", "Siemens",
    }
    jobs = [job for source in audit["sourceReviews"] for job in source["jobs"]]
    context_plane = next(job for job in jobs if job["sourceRecordId"] == "210773809")
    assert context_plane["classification"] == "qualified"
    assert context_plane["title"] == "Context Plane Python Engineer"


def test_manager_approval_is_exactly_six_sources_and_qualified_only():
    approval = json.loads(APPROVAL.read_text())
    assert approval == {
        "approvedFirstPartyPublicationPolicy": "qualified-only",
        "approvedSourceCount": 6,
        "approvedSourceKeys": sorted(REVIEW_SOURCES),
        "deferredOrganizations": ["Bloomberg L.P.", "Siemens"],
        "deploymentAuthorized": True,
        "managerApprovedAt": "2026-08-31",
        "publicJobsSnapshotModifiedAtApproval": False,
        "schemaVersion": 1,
        "status": "approved-for-production",
    }
