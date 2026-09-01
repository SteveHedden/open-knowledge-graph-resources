"""Task 44 SAP, workplace, tags, RDF, audit, and immutable-baseline contracts."""

import hashlib
import json
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))

import first_party_sources as fps  # noqa: E402
from catalog_mentions import add_catalog_mentions, load_match_index  # noqa: E402
from job_normalization import add_job_tags, normalize_workplace  # noqa: E402
from live_records import build_graph, publish_snapshot  # noqa: E402
from live_pipeline import _prepare_for_reconciliation  # noqa: E402

SCHEMA = Namespace("https://schema.org/")
KGJOBS = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
KGJV = Namespace("https://openknowledgegraphs.com/jobs/vocab#")


def _payload():
    return json.loads((ROOT / "tests/fixtures/first-party-pilot/first-party-sap-task44.json").read_text())


def _sap_record(payload=None):
    source = fps.load_first_party_sources()["first-party-sap"]
    return fps.records_from_payload(payload or _payload(), source)[0]


def test_sap_pinned_page_identity_location_workplace_and_compensation():
    record = _sap_record()
    assert record["title"] == "Data and Applied Scientist, Finance and Spend Autonomous Suite, Palo Alto"
    assert record["canonicalUrl"] == (
        "https://jobs.sap.com/job/Palo-Alto-Data-and-Applied-Scientist%2C-"
        "Finance-and-Spend-Autonomous-Suite%2C-Palo-Alto-CA-94304/1431263233/"
    )
    assert record["sourceRecordId"] == "1431263233"
    assert record["requisitionId"] == "459718"
    assert record["employmentType"] == "Regular Full Time"
    assert record["location"] == "Palo Alto, CA, US, 94304"
    assert record["locationKeys"] == ["94304", "ca", "palo alto", "us"]
    assert record["workplaceMode"] == "hybrid" and record["remote"] is True
    assert record["combinedCompensation"] == {
        "minValue": 106900, "maxValue": 229400, "currency": "USD",
        "unitText": "annual", "basis": "base-plus-variable-target",
    }


def test_sap_preserves_legacy_street_address_and_does_not_infer_workplace():
    legacy = json.loads((ROOT / "tests/fixtures/first-party-pilot/first-party-sap.json").read_text())
    record = _sap_record(legacy)
    assert record["location"] == "Palo Alto, CA"
    assert record["workplaceMode"] == "unknown"
    assert "remote" not in record


def test_sap_generic_flexible_prose_and_unreviewed_markers_do_not_set_workplace():
    for replacement in (
        "Our flexible work policy combines office and remote time.",
        "Additional Locations: #LI-Flexible",
    ):
        payload = deepcopy(_payload())
        payload["details"][0]["html"] = payload["details"][0]["html"].replace(
            "Additional Locations: #LI-Hybrid", replacement
        )
        record = _sap_record(payload)
        assert record["workplaceMode"] == "unknown"
        assert "remote" not in record


def test_sap_compensation_accepts_reviewed_formatted_and_compact_usd_cad():
    prefix = "Compensation Range Transparency: The targeted annual combined range for this position is "
    for raw, expected in [
        ("$106,900 - $229,400 (USD)", (106900, 229400, "USD")),
        ("106900-229400USD", (106900, 229400, "USD")),
        ("$144,600-$322,500 (CAD)", (144600, 322500, "CAD")),
        ("144600-322500CAD", (144600, 322500, "CAD")),
    ]:
        value, reason = fps._sap_combined_compensation(prefix + raw + ".")
        assert reason is None
        assert (value["minValue"], value["maxValue"], value["currency"]) == expected


def test_invalid_sap_compensation_is_diagnostic_only_end_to_end():
    original = "$106,900 - $229,400 (USD)"
    invalid = (
        "$1,06,900 - $229,400 (USD)",
        "$0 - $229,400 (USD)",
        "$229,400 - $106,900 (USD)",
        "$106,900 - $229,400 (USD). The targeted annual combined range for this "
        "position is $144,600 - $322,500 (CAD)",
    )
    for replacement in invalid:
        payload = deepcopy(_payload())
        payload["details"][0]["html"] = payload["details"][0]["html"].replace(
            original, replacement
        )
        private = _sap_record(payload)
        assert "combinedCompensation" not in private
        assert private["normalizationDiagnostics"][0]["code"] == "sap-invalid-combined-compensation"
        assert replacement in private["description"]
        public = _prepare_for_reconciliation([private], {})[0]
        assert "normalizationDiagnostics" not in public
        graph = build_graph(
            [{**public, "classification": "qualified", "firstSeenAt": "2026-08-31T00:00:00Z",
              "lastSeenAt": "2026-08-31T00:00:00Z", "retrievedAt": "2026-08-31T00:00:00Z",
              "active": True}],
            {"runId": "task44-invalid", "retrievedAt": "2026-08-31T00:00:00Z"},
            fps.load_first_party_sources()["first-party-sap"],
        )
        job = next(graph.subjects(RDF.type, SCHEMA.JobPosting))
        assert graph.value(job, KGJOBS.combinedCompensation) is None


def test_sap_compensation_ignores_unreviewed_markers_and_unrelated_dollar_values():
    assert fps._sap_combined_compensation(
        "Salary range: $106,900 - $229,400 (USD). Bonus $5,000."
    ) == (None, None)
    value, reason = fps._sap_combined_compensation(
        "Compensation Range Transparency: Bonus $5,000. The targeted annual combined "
        "range for this position is $106,900 - $229,400 (USD). Equity $25,000."
    )
    assert reason is None
    assert value["minValue"] == 106900 and value["maxValue"] == 229400
    assert fps._sap_combined_compensation(
        "COMPENSATION   RANGE TRANSPARENCY: Pay varies by location."
    ) == (None, "reviewed transparency wording lacks a combined-range value")


def test_workplace_modes_and_compatibility_are_authoritative():
    assert normalize_workplace({"workplaceMode": "remote"})["remote"] is True
    assert normalize_workplace({"workplaceMode": "hybrid"})["remote"] is True
    assert normalize_workplace({"workplaceMode": "onsite"})["remote"] is False
    assert "remote" not in normalize_workplace({"workplaceMode": "unknown"})
    assert normalize_workplace({})["workplaceMode"] == "unknown"
    assert normalize_workplace({"workplaceMode": "ON_SITE"}) == {
        "workplaceMode": "onsite", "remote": False,
    }
    assert fps._mode(raw="ON_SITE") == "onsite"


def test_description_only_case_sensitive_bounded_tags():
    records = add_job_tags([
        {"id": "01-cypher", "title": "Other", "description": "Use Cypher."},
        {"id": "02-gql", "title": "Other", "description": "Use GQL."},
    ])
    assert [record["id"] for record in records] == ["01-cypher", "02-gql"]
    assert records[0]["jobTags"] == [{
        "label": "Cypher", "matchedPhrase": "Cypher",
        "relatedCatalogPage": "https://openknowledgegraphs.com/software/neo4j/",
    }]
    assert records[1]["jobTags"] == [{"label": "GQL", "matchedPhrase": "GQL"}]


def test_job_tag_negative_boundaries_and_pipeline_field_stability():
    negatives = [
        {"id": "01-cypher-title", "title": "Cypher", "description": "No language in this body."},
        {"id": "02-gql-title", "title": "GQL", "description": "No language in this body."},
        {"id": "03-cypher-lower", "title": "Other", "description": "cypher is lowercase."},
        {"id": "04-gql-lower", "title": "Other", "description": "gql is lowercase."},
        {"id": "05-graphql", "title": "Other", "description": "GraphQL is a different token."},
        {"id": "06-cypher-prefix", "title": "Other", "description": "SuperCypher."},
        {"id": "07-cypher-suffix", "title": "Other", "description": "Cypher2."},
        {"id": "08-gql-prefix", "title": "Other", "description": "preGQL."},
        {"id": "09-gql-suffix", "title": "Other", "description": "GQL2."},
    ]
    tagged_negatives = add_job_tags(negatives)
    assert [row["id"] for row in tagged_negatives] == [row["id"] for row in negatives]
    assert all("jobTags" not in row for row in tagged_negatives)

    original = {
        "id": "fixture:stable", "title": "Role", "description": "Cypher and GQL.",
        "classification": "qualified", "evidence": [{"matchedPhrase": "ontology"}],
        "catalogMentions": [{"matchedPhrase": "OWL"}], "organizationIri": "https://example.test/org",
        "admission": {"decision": "accepted"}, "sourceRecordId": "42",
    }
    tagged = add_job_tags([original])[0]
    assert len(add_job_tags([original])) == 1
    for key in ("classification", "evidence", "catalogMentions", "organizationIri", "admission", "sourceRecordId"):
        assert tagged[key] == original[key]
    assert list(tagged)[:len(original)] == list(original)


def test_production_enrichment_and_temporary_publication_have_json_rdf_tag_parity():
    base = _sap_record()
    specs = [
        ("01-cypher", "OWL modeling with Cypher."),
        ("02-gql", "OWL modeling with GQL."),
        ("03-graphql", "OWL modeling with GraphQL."),
        ("04-lower-cypher", "OWL modeling with cypher."),
        ("05-lower-gql", "OWL modeling with gql."),
        ("06-boundaries", "OWL modeling with SuperCypher, Cypher2, preGQL, and GQL2."),
    ]
    records = []
    for record_id, description in reversed(specs):
        record = deepcopy(base)
        record.update({
            "id": record_id, "sourceRecordId": record_id, "requisitionId": record_id,
            "title": f"Fixture {record_id}", "description": description,
            "canonicalUrl": f"https://jobs.sap.com/job/{record_id}/", "sourceUrl": f"https://jobs.sap.com/job/{record_id}/",
            "classification": "qualified", "evidence": [], "admission": {"decision": "accepted"},
            "firstSeenAt": "2026-08-31T00:00:00Z", "lastSeenAt": "2026-08-31T00:00:00Z",
            "retrievedAt": "2026-08-31T00:00:00Z", "active": True,
        })
        record.pop("combinedCompensation", None)
        records.append(record)

    mention_index = load_match_index(REPO, ROOT / "catalog-mention-policy.json")
    mentioned = add_catalog_mentions(records, mention_index)
    mentions_before_tags = {record["id"]: deepcopy(record["catalogMentions"]) for record in mentioned}
    enriched = add_job_tags(mentioned)
    expected_ids = [record_id for record_id, _ in specs]
    assert [record["id"] for record in enriched] == expected_ids
    assert {record["id"]: record["catalogMentions"] for record in enriched} == mentions_before_tags
    assert [record["id"] for record in enriched if "jobTags" in record] == ["01-cypher", "02-gql"]

    source = fps.load_first_party_sources()["first-party-sap"]
    run = {"runId": "task44-publication", "retrievedAt": "2026-08-31T00:00:00Z", "queryResults": []}
    graph = build_graph(enriched, run, source)
    with tempfile.TemporaryDirectory() as directory:
        runtime = Path(directory) / "runtime"
        publish_snapshot(enriched, run, graph, ROOT, runtime, {}, source.key, {source.key: records})
        emitted = json.loads((runtime / "jobs.json").read_text(encoding="utf-8"))
        emitted_graph = Graph().parse(runtime / "jobs.ttl", format="turtle")

    assert [record["id"] for record in emitted] == expected_ids
    assert {record["id"]: record["catalogMentions"] for record in emitted} == mentions_before_tags
    assert emitted[0]["jobTags"] == [{
        "label": "Cypher", "matchedPhrase": "Cypher",
        "relatedCatalogPage": "https://openknowledgegraphs.com/software/neo4j/",
    }]
    assert emitted[1]["jobTags"] == [{"label": "GQL", "matchedPhrase": "GQL"}]
    cypher_job = next(emitted_graph.subjects(SCHEMA.title, Literal("Fixture 01-cypher")))
    gql_job = next(emitted_graph.subjects(SCHEMA.title, Literal("Fixture 02-gql")))
    assert {str(value) for value in emitted_graph.objects(cypher_job, SCHEMA.keywords)} == {"Cypher"}
    assert {str(value) for value in emitted_graph.objects(gql_job, SCHEMA.keywords)} == {"GQL"}
    assert (cypher_job, KGJOBS.relatedCatalogPage, URIRef("https://openknowledgegraphs.com/software/neo4j/")) in emitted_graph
    assert (cypher_job, SCHEMA.mentions, URIRef("https://openknowledgegraphs.com/software/neo4j/")) not in emitted_graph
    assert emitted_graph.value(gql_job, KGJOBS.relatedCatalogPage) is None
    for record_id in ("03-graphql", "04-lower-cypher", "05-lower-gql", "06-boundaries"):
        negative_job = next(emitted_graph.subjects(SCHEMA.title, Literal(f"Fixture {record_id}")))
        assert list(emitted_graph.objects(negative_job, SCHEMA.keywords)) == []
        assert emitted_graph.value(negative_job, KGJOBS.relatedCatalogPage) is None
    owl_page = URIRef("https://openknowledgegraphs.com/resource/web-ontology-language/")
    assert mentions_before_tags["01-cypher"] == [{
        "title": "Web Ontology Language", "dataset": "resource", "qid": "Q826165",
        "canonicalUrl": str(owl_page), "matchedPhrase": "OWL",
    }]
    assert (cypher_job, SCHEMA.mentions, owl_page) in emitted_graph


def test_rdf_uses_exact_workplace_combined_compensation_and_tag_terms():
    record = _sap_record()
    record.update({
        "classification": "qualified", "firstSeenAt": "2026-08-31T00:00:00Z",
        "lastSeenAt": "2026-08-31T00:00:00Z", "retrievedAt": "2026-08-31T00:00:00Z",
        "active": True, "jobTags": add_job_tags([record])[0]["jobTags"],
    })
    source = fps.load_first_party_sources()["first-party-sap"]
    graph = build_graph([record], {"runId": "task44", "retrievedAt": "2026-08-31T00:00:00Z"}, source)
    job = next(graph.subjects(RDF.type, SCHEMA.JobPosting))
    assert str(graph.value(job, SCHEMA.jobLocationType)) == "HYBRID"
    amount = graph.value(job, KGJOBS.combinedCompensation)
    assert (amount, RDF.type, SCHEMA.MonetaryAmount) in graph
    value = graph.value(amount, SCHEMA.value)
    assert (value, RDF.type, SCHEMA.QuantitativeValue) in graph
    assert graph.value(amount, SCHEMA.currency) == Literal("USD")
    assert graph.value(value, SCHEMA.minValue) == Literal(106900)
    assert graph.value(value, SCHEMA.maxValue) == Literal(229400)
    assert graph.value(value, SCHEMA.unitText) == Literal("annual")
    assert graph.value(amount, KGJOBS.compensationBasis) == KGJV["compensation-basis-base-plus-variable-target"]
    assert graph.value(job, SCHEMA.baseSalary) is None
    assert graph.value(job, SCHEMA.estimatedSalary) is None
    assert {str(value) for value in graph.objects(job, SCHEMA.keywords)} == {"Cypher", "GQL"}
    assert (job, KGJOBS.relatedCatalogPage, URIRef("https://openknowledgegraphs.com/software/neo4j/")) in graph
    assert (job, SCHEMA.mentions, URIRef("https://openknowledgegraphs.com/software/neo4j/")) not in graph


def test_rdf_workplace_mapping_covers_all_modes_and_unknown_omission():
    base = _sap_record()
    source = fps.load_first_party_sources()["first-party-sap"]
    expected = {"remote": "TELECOMMUTE", "hybrid": "HYBRID", "onsite": "ON_SITE", "unknown": None}
    for index, (mode, rdf_value) in enumerate(expected.items()):
        record = {**base, "id": f"fixture:{index}", "workplaceMode": mode,
                  "classification": "qualified", "firstSeenAt": "2026-08-31T00:00:00Z",
                  "lastSeenAt": "2026-08-31T00:00:00Z", "retrievedAt": "2026-08-31T00:00:00Z",
                  "active": True}
        graph = build_graph([record], {"runId": f"task44-{mode}", "retrievedAt": "2026-08-31T00:00:00Z"}, source)
        job = next(graph.subjects(RDF.type, SCHEMA.JobPosting))
        actual = graph.value(job, SCHEMA.jobLocationType)
        assert (str(actual) if actual is not None else None) == rdf_value


def test_employer_audit_and_exact_baselines():
    audit = json.loads((ROOT / "audits/task44-employer-source-audit.json").read_text())
    expected_employers = {"Salesforce", "Workday", "ServiceNow", "Oracle"}
    assert {row["identity"]["employer"] for row in audit["organizations"]} == expected_employers
    assert len(audit["organizations"]) == len(expected_employers)
    allowed = {"viable-review-only", "deferred-no-bounded-public-feed", "deferred-auth-required", "deferred-cross-organization-scope", "rejected-insufficient-official-evidence"}
    assert all(row["disposition"] in allowed for row in audit["organizations"])
    assert audit["constraints"]["productionSourceCount"] == 40
    assert audit["constraints"]["jobsEnabledOrganizationCount"] == 35
    organization_graph = Graph().parse(REPO / "organizations.ttl", format="turtle")
    for row in audit["organizations"]:
        assert {item["claimType"] for item in row["evidence"]} == {"identity", "careers", "feed"}
        assert all(item["hashBasis"] == "retrieved-response-body" for item in row["evidence"])
        identity_evidence = next(item for item in row["evidence"] if item["claimType"] == "identity")
        assert identity_evidence["httpStatus"] == 200
        identity_bytes = (ROOT / identity_evidence["capturedPath"]).read_bytes().lower()
        assert row["identity"]["employer"].casefold().encode() in identity_bytes
        organization = URIRef(row["identity"]["organizationIri"])
        assert (organization, RDF.type, SCHEMA.Organization) in organization_graph
        assert organization_graph.value(organization, SCHEMA.name) == Literal(row["identity"]["employer"])
        assert str(organization_graph.value(organization, KGJOBS.careersPage)) == row["careersUrl"]
        for item in row["evidence"]:
            captured = ROOT / item["capturedPath"]
            assert captured.is_file()
            assert hashlib.sha256(captured.read_bytes()).hexdigest() == item["sha256"]
            if item.get("detailProbe"):
                detail = item["detailProbe"]
                detail_capture = ROOT / detail["capturedPath"]
                assert hashlib.sha256(detail_capture.read_bytes()).hexdigest() == detail["sha256"]
        if row["disposition"] != "viable-review-only":
            assert row["blocker"]
            evidence_by_type = {item["claimType"]: item for item in row["evidence"]}
            assert row["blockerEvidenceClaimTypes"]
            assert all(claim in evidence_by_type for claim in row["blockerEvidenceClaimTypes"])
            assert row["probeAttempts"]
            assert all({"authentication", "host", "method", "pagination", "path", "query", "result"} <= set(probe) for probe in row["probeAttempts"])
    sources = fps.load_first_party_sources()
    review = sources["first-party-workday-employer-review"]
    assert review.production_approved is False
    assert review.republication_status == "local-review-only"
    assert review.refresh_interval_seconds == 86400
    assert review.max_requests_per_batch == 64
    workday_audit = next(row for row in audit["organizations"] if row["identity"]["employer"] == "Workday")
    contract = workday_audit["reviewContract"]
    endpoint = urlparse(review.endpoint)
    assert workday_audit["careersUrl"] == review.careers_page
    assert contract["fixedHost"] == review.allowed_host == endpoint.hostname
    assert contract["tenant"] == review.tenant == "workday"
    assert contract["site"] == "Workday"
    assert endpoint.path.split("/")[3:5] == [contract["tenant"], contract["site"]]
    assert contract["publisher"] == review.organization_iri == workday_audit["identity"]["organizationIri"]
    assert contract["adapter"] == review.adapter
    assert contract["feedPath"] == endpoint.path
    assert urlparse(workday_audit["evidence"][2]["url"])._replace(query="") == endpoint._replace(query="")
    assert contract["endpointQuery"] == {"searchText": parse_qs(endpoint.query)["searchText"][0]}
    assert contract["authentication"] == "none"
    assert contract["requestBody"] == workday_audit["evidence"][2]["request"]["body"] == {
        "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "knowledge graph",
    }
    assert workday_audit["evidence"][2]["request"] == {
        "authentication": "none", "body": contract["requestBody"], "method": "POST",
    }
    assert contract["pagination"] == {
        "limit": 20, "method": "POST", "offsetStart": 0,
        "rule": "increment offset by returned jobPostings length until offset reaches total",
        "totalField": "total",
    }
    assert contract["requestCaps"] == {
        "maxBatchRequests": review.max_requests_per_batch,
        "maxRecordsPerRun": review.max_records_per_run,
        "maxRequestsPerRun": review.max_requests_per_run,
        "maxResponseBytes": review.max_response_bytes,
        "timeoutSeconds": review.timeout_seconds,
    }
    assert contract["refreshIntervalHours"] * 3600 == review.refresh_interval_seconds
    assert contract["detailPath"] == "/wday/cxs/workday/Workday{externalPath}"
    assert workday_audit["evidence"][1]["url"] != workday_audit["evidence"][2]["url"]
    assert "first-party-workday-employer-review" not in fps.load_production_first_party_sources()

    servicenow = next(row for row in audit["organizations"] if row["identity"]["employer"] == "ServiceNow")
    assert servicenow["careersUrl"] == "https://careers.servicenow.com/"
    assert servicenow["probeAttempts"][0]["path"] == "/jobs/"
    servicenow_careers = next(item for item in servicenow["evidence"] if item["claimType"] == "careers")
    servicenow_feed = next(item for item in servicenow["evidence"] if item["claimType"] == "feed")
    assert servicenow_careers["landingUrl"] == servicenow["careersUrl"]
    assert servicenow_feed["searchUrl"] == "https://careers.servicenow.com/jobs/"
    assert urlparse(servicenow_feed["url"]).path == "/jobs/"

    fixture = json.loads((ROOT / "tests/fixtures/first-party-pilot/first-party-workday-employer-review.json").read_text())
    retained_feed = json.loads((ROOT / "audits/task44-evidence/workday-feed.json").read_text())
    provenance = fixture["captureProvenance"]
    assert retained_feed["total"] == len(retained_feed["jobPostings"]) == provenance["capturedFeedTotal"] == 11
    assert provenance["capturedFeedJobCount"] == 11
    assert provenance["derivedFixtureJobCount"] == fixture["listingPages"][0]["total"] == len(fixture["details"]) == 1
    assert provenance["omittedCapturedJobs"] == 10
    assert fixture["details"][0]["externalPath"] == retained_feed["jobPostings"][0]["externalPath"]

    protected = {
        "data/jobs/jobs.json": "142245ee2e4fd00019f51e320a592f713f7e5f5bea5464e3278dca7548ef1586",
        "data/jobs/jobs.ttl": "0a42e17e969e9cccac073442715e0ba992e64e5d28941bc8a00f19758c4660e1",
        "data/jobs/manifest.json": "ea45b687982291f9468fa2864bd5f2a372bd131152381bdf5ea9fdf42c7a03d0",
        "data/manifest.json": "37c1fed52b7c73664f04905dce44ae9e8fa9c9c7de1345fce85562f0f9e39791",
    }
    for path, expected in protected.items():
        assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == expected


def test_workday_employer_review_source_normalizes_without_production_gate():
    payload = json.loads((ROOT / "tests/fixtures/first-party-pilot/first-party-workday-employer-review.json").read_text())
    source = fps.load_first_party_sources()["first-party-workday-employer-review"]
    records = fps.records_from_payload(payload, source)
    assert len(records) == 1
    assert records[0]["organizationIri"] == "https://openknowledgegraphs.com/organization/workday/"
    assert records[0]["title"] == "Director, Product Management - Context Engine, Data Cloud"
    assert records[0]["requisitionId"] == "JR-0109108"
    assert records[0]["canonicalUrl"].startswith("https://workday.wd5.myworkdayjobs.com/Workday/job/")
    assert source.production_approved is False
