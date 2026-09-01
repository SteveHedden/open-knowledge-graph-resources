"""Contracts for inert, reviewed, bounded first-party career sources."""

import json
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from rdflib import Graph, Namespace, URIRef

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
FIXTURES = ROOT / "tests" / "fixtures" / "first-party-pilot"
sys.path.insert(0, str(ROOT / "scripts"))

import first_party_sources as fps  # noqa: E402
import first_party_pilot as fpp  # noqa: E402
from classifier import load_match_terms  # noqa: E402
from first_party_pilot import _live_refresh_due, run_pilot  # noqa: E402
from live_records import classify_records  # noqa: E402

ORIGINAL_PRODUCTION_SOURCES = {
    "first-party-neo4j", "first-party-relationalai", "first-party-tigergraph",
    "first-party-wikimedia", "first-party-stardog", "first-party-weaviate",
    "first-party-graphwise", "first-party-enterprise-knowledge",
    "first-party-metaphacts", "first-party-topquadrant", "first-party-eccenca",
    "first-party-w3c",
}
TASK41_REVIEW_SOURCES = {
    "first-party-artsy", "first-party-databricks",
    "first-party-sage-publishing", "first-party-triply",
}
TASK42_REVIEW_SOURCES = {
    "first-party-danish-bibliographic-centre",
    "first-party-embl-ebi",
    "first-party-institute-of-scientific-and-technical-information",
    "first-party-inter-university-consortium-for-political-and-social-research",
    "first-party-linux-foundation",
    "first-party-metropolitan-museum-of-art",
    "first-party-microsoft-research",
    "first-party-national-library-of-norway",
    "first-party-public-library-of-science",
    "first-party-regenstrief-institute",
    "first-party-renaissance-computing-institute",
    "first-party-sib-swiss-institute-of-bioinformatics",
    "first-party-stanford-university-school-of-medicine",
    "first-party-the-open-university",
    "first-party-university-of-maryland",
    "first-party-university-of-north-carolina-at-chapel-hill",
    "first-party-wikimedia-deutschland",
}
TASK43_REVIEW_SOURCES = {
    "first-party-accenture", "first-party-amazon", "first-party-capital-one",
    "first-party-crowdstrike", "first-party-jpmorgan-chase", "first-party-sap",
}
TASK44_REVIEW_SOURCES = {"first-party-workday-employer-review"}


def test_registry_preserves_review_sources_and_all_approved_sources():
    sources = fps.load_first_party_sources()
    production = fps.load_production_first_party_sources()
    assert set(sources) == (
        ORIGINAL_PRODUCTION_SOURCES | TASK41_REVIEW_SOURCES
        | TASK42_REVIEW_SOURCES | TASK43_REVIEW_SOURCES | TASK44_REVIEW_SOURCES
    )
    approved = (
        ORIGINAL_PRODUCTION_SOURCES | TASK42_REVIEW_SOURCES | TASK43_REVIEW_SOURCES
    )
    assert set(production) == approved
    assert {source.provider for source in sources.values()} == {
        "greenhouse", "lever", "ashby", "teamtailor", "workday",
        "webcruiter", "rippling", "same-site", "successfactors", "ukg",
        "softgarden", "refline", "emply",
        "peopleadmin", "taleo-selectminds", "drupal-rss", "cnrs",
        "microsoft-research",
        "oracle-recruiting", "amazon-jobs",
    }
    graph = Graph().parse(REPO_ROOT / "sources.ttl", format="turtle")
    kgjobs = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
    assert all(
        not list(graph.objects(URIRef(source.dataset_uri), kgjobs.searchEnabled))
        for source in sources.values()
    )
    for source in sources.values():
        assert source.endpoint.startswith(f"https://{source.allowed_host}/")
        assert source.terms_url.startswith("https://")
        assert source.robots_url.startswith("https://")
        assert source.attribution_url.startswith("https://")
        assert source.review_status == "evidence-reviewed"
        assert source.republication_status in {"production-approved", "local-review-only"}
        assert source.production_approved is (source.key in approved)
        assert source.refresh_interval_seconds >= 86400
        assert source.timeout_seconds <= 20
        assert source.max_response_bytes <= 15_000_000
        assert source.max_requests_per_batch <= source.max_requests_per_run
        assert source.max_requests_per_batch <= 64
        assert source.max_requests_per_run <= 800
        assert source.max_records_per_run <= 1_000
    for key in ORIGINAL_PRODUCTION_SOURCES:
        assert sources[key].max_response_bytes <= 5_000_000
        assert sources[key].max_requests_per_run <= 3
        assert sources[key].max_records_per_run <= 250


@pytest.mark.parametrize(
    ("source_key", "fixture_name", "expected_adapter"),
    [
        ("first-party-neo4j", "greenhouse.json", "firstparty-greenhouse"),
        ("first-party-stardog", "lever.json", "firstparty-lever"),
        ("first-party-weaviate", "ashby.json", "firstparty-ashby"),
        ("first-party-w3c", "schema.html", "firstparty-schema"),
    ],
)
def test_reviewed_adapter_fixtures_normalize_canonical_first_party_records(
    source_key, fixture_name, expected_adapter,
):
    source = fps.load_first_party_sources()[source_key]
    assert source.adapter == expected_adapter
    fixture = FIXTURES / fixture_name
    payload = json.loads(fixture.read_text()) if fixture.suffix == ".json" else fixture.read_text()
    records = fps.records_from_payload(payload, source)
    assert len(records) == 1
    record = records[0]
    assert record["id"].startswith(f"firstparty:{source.key}:")
    assert record["canonicalUrl"].startswith("https://")
    assert record["organizationIri"] == source.organization_iri
    assert record["firstParty"] is True
    assert record["sourceOccurrences"] == [{
        "sourceDataset": source.dataset_uri,
        "sourceRecordId": record["sourceRecordId"],
        "sourceUrl": record["canonicalUrl"],
        "provider": source.provider,
        "tenant": source.tenant,
        "firstParty": True,
    }]


def test_malformed_payload_and_unapproved_redirect_are_rejected(monkeypatch):
    source = fps.load_first_party_sources()["first-party-neo4j"]
    with pytest.raises(fps.FirstPartySourceError, match="jobs array"):
        fps.records_from_payload([], source)

    class Redirect:
        is_redirect = True
        is_permanent_redirect = False
        headers = {"Location": "https://unapproved.example/jobs"}

    monkeypatch.setattr(fps.requests, "get", lambda *args, **kwargs: Redirect())
    with pytest.raises(fps.FirstPartySourceError, match="disallowed redirect"):
        fps.fetch_source(source)


def test_cnrs_accepts_empty_listing_with_exact_reviewed_unit_heading():
    source = fps.load_first_party_sources()[
        "first-party-institute-of-scientific-and-technical-information"
    ]
    payload = {
        "listingHtml": (
            "<html><body><h1>Les offres d'emploi de UAR76 (INIST)</h1>"
            "<ul id='CphMain_UlUnitOffers'></ul></body></html>"
        ),
        "details": [],
    }

    assert fps.records_from_payload(payload, source) == []


@pytest.mark.parametrize(
    "listing_html",
    [
        (
            "<html><body><h1>Les offres d'emploi de UAR75 (OTHER)</h1>"
            "<p>Aucune offre</p></body></html>"
        ),
        (
            "<html><body><p>Les offres d'emploi de UAR76 (INIST)</p>"
            "</body></html>"
        ),
    ],
)
def test_cnrs_rejects_empty_listing_without_exact_reviewed_unit_heading(listing_html):
    source = fps.load_first_party_sources()[
        "first-party-institute-of-scientific-and-technical-information"
    ]

    with pytest.raises(fps.FirstPartySourceError, match="reviewed UAR76/INIST identity"):
        fps.records_from_payload(
            {"listingHtml": listing_html, "details": []},
            source,
        )


def _graphwise_fixture():
    return json.loads((FIXTURES / "first-party-graphwise.json").read_text())


def test_graphwise_live_shape_discovers_and_hydrates_only_careers_members():
    source = fps.load_first_party_sources()["first-party-graphwise"]
    assert source.adapter == fps.GRAPHWISE_ADAPTER
    assert source.endpoint == fps.GRAPHWISE_CAREERS_URL
    assert source.max_requests_per_run == 2
    records = fps.graphwise_records(_graphwise_fixture(), source)
    assert [record["sourceRecordId"] for record in records] == [
        "250001", "249103",
    ]
    semantic = records[1]
    assert semantic["id"] == "firstparty:first-party-graphwise:249103"
    assert "requisitionId" not in semantic
    assert semantic["canonicalUrl"] == "https://graphwise.bamboohr.com/careers/111"
    assert semantic["sourceUrl"] == "https://graphwise.ai/jobs/semantic-ai-engineer/"
    assert semantic["location"] == "Sofia (Hybrid)"
    assert semantic["workplaceMode"] == "hybrid"
    assert semantic["datePosted"] == "2026-05-08"
    assert "RDF, OWL, SHACL, or SPARQL" in semantic["description"]


def test_graphwise_wordpress_identity_survives_a_slug_change():
    source = fps.load_first_party_sources()["first-party-graphwise"]
    original = _graphwise_fixture()
    changed = deepcopy(original)
    old_url = "https://graphwise.ai/jobs/semantic-ai-engineer/"
    new_url = "https://graphwise.ai/jobs/semantic-ai-data-engineer/"
    changed["careersHtml"] = changed["careersHtml"].replace(old_url, new_url)
    item = next(row for row in changed["details"] if row["id"] == 249103)
    item["slug"] = "semantic-ai-data-engineer"
    item["link"] = new_url
    before = next(
        row for row in fps.graphwise_records(original, source)
        if row["sourceRecordId"] == "249103"
    )
    after = next(
        row for row in fps.graphwise_records(changed, source)
        if row["sourceRecordId"] == "249103"
    )
    assert before["id"] == after["id"]
    assert before["sourceUrl"] == old_url
    assert after["sourceUrl"] == new_url


def test_graphwise_opening_markers_with_zero_links_are_a_source_failure():
    source = fps.load_first_party_sources()["first-party-graphwise"]
    payload = _graphwise_fixture()
    payload["careersHtml"] = "<html><body><h2>Open Positions</h2></body></html>"
    payload["details"] = []
    with pytest.raises(fps.FirstPartySourceError, match="opening markers.*zero job links"):
        fps.graphwise_records(payload, source)


def test_graphwise_attribute_only_opening_marker_cannot_become_a_successful_zero():
    source = fps.load_first_party_sources()["first-party-graphwise"]
    payload = {
        "careersHtml": '<html><body><input placeholder="Search in jobs"></body></html>',
        "details": [],
    }
    with pytest.raises(fps.FirstPartySourceError, match="opening markers.*zero job links"):
        fps.graphwise_records(payload, source)


@pytest.mark.parametrize(
    ("source_key", "message"),
    [
        (
            "first-party-metaphacts",
            "At the moment we do not have any job openings, but please check back.",
        ),
        ("first-party-w3c", "None at this time."),
    ],
)
def test_schema_page_explicitly_reporting_no_jobs_is_a_legitimate_zero(
    source_key, message,
):
    source = fps.load_first_party_sources()[source_key]
    payload = f"<main><h1>Open positions</h1><p>{message}</p></main>"
    assert fps.schema_records(payload, source) == []


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "<html><body><h1>Careers</h1><p>Learn about our company.</p></body></html>",
        "<html><body><h1>Just a moment...</h1><p>Checking your browser.</p></body></html>",
        "<html><body><h1>Open positions</h1></body></html>",
    ],
)
def test_schema_page_without_postings_or_explicit_no_openings_fails(payload):
    source = fps.load_first_party_sources()["first-party-w3c"]
    with pytest.raises(fps.FirstPartySourceError, match="zero JobPosting records"):
        fps.schema_records(payload, source)


@pytest.mark.parametrize(
    ("source_key", "fixture_name"),
    [
        ("first-party-neo4j", "greenhouse.json"),
        ("first-party-stardog", "lever.json"),
        ("first-party-weaviate", "ashby.json"),
    ],
)
def test_json_adapters_fail_instead_of_slicing_over_record_cap(source_key, fixture_name):
    source = replace(fps.load_first_party_sources()[source_key], max_records_per_run=1)
    payload = json.loads((FIXTURES / fixture_name).read_text())
    if isinstance(payload, list):
        payload.append(deepcopy(payload[0]))
    else:
        payload["jobs"].append(deepcopy(payload["jobs"][0]))
    with pytest.raises(fps.FirstPartySourceError, match="record cap"):
        fps.records_from_payload(payload, source)


def test_schema_adapter_fails_instead_of_slicing_over_record_cap():
    source = replace(
        fps.load_first_party_sources()["first-party-w3c"], max_records_per_run=1,
    )
    payload = """<script type="application/ld+json">
    [{"@type":"JobPosting"},{"@type":"JobPosting"}]
    </script>"""
    with pytest.raises(fps.FirstPartySourceError, match="record cap"):
        fps.schema_records(payload, source)


def _rippling_fixture():
    return json.loads((FIXTURES / "rippling.json").read_text())


def _eccenca_fixture():
    return json.loads((FIXTURES / "first-party-eccenca.json").read_text())


def test_topquadrant_rippling_fixture_discovers_and_hydrates_exactly():
    source = fps.load_first_party_sources()["first-party-topquadrant"]
    assert source.adapter == fps.RIPPLING_ADAPTER
    assert source.max_requests_per_run == 3
    assert source.max_records_per_run == 2
    records = fps.rippling_records(_rippling_fixture(), source)
    assert len(records) == 1
    assert records[0]["sourceRecordId"] == "b52abfb2-4ca4-4da7-9c4a-d9723caf23a9"
    assert records[0]["canonicalUrl"].startswith(
        "https://ats.rippling.com/topquadrant/jobs/"
    )
    assert records[0]["location"] == "Remote (United States)"
    assert "TopBraid EDG" in records[0]["description"]


def test_topquadrant_fetch_is_bounded_to_exact_list_and_detail_paths(monkeypatch):
    source = fps.load_first_party_sources()["first-party-topquadrant"]
    fixture = _rippling_fixture()
    bodies = [fixture["listing"], *fixture["details"]]
    calls = []

    class Response:
        is_redirect = False
        is_permanent_redirect = False
        headers = {}

        def __init__(self, body):
            self.body = json.dumps(body).encode()

        def raise_for_status(self):
            return None

        def iter_content(self, _size):
            yield self.body

    def fake_get(url, **kwargs):
        calls.append(url)
        return Response(bodies[len(calls) - 1])

    monkeypatch.setattr(fps.requests, "get", fake_get)
    assert len(fps.fetch_source(source)["details"]) == 1
    assert calls == [
        fps.RIPPLING_LIST_ENDPOINT,
        fps.RIPPLING_DETAIL_PREFIX + fixture["listing"]["items"][0]["id"],
    ]


def test_topquadrant_rippling_over_cap_fails_before_partial_hydration():
    source = fps.load_first_party_sources()["first-party-topquadrant"]
    payload = _rippling_fixture()
    payload["listing"]["items"] *= 3
    payload["listing"]["totalItems"] = 3
    with pytest.raises(fps.FirstPartySourceError, match="record cap"):
        fps.rippling_records(payload, source)


def test_eccenca_fixture_discovers_and_hydrates_two_exact_same_site_jobs():
    source = fps.load_first_party_sources()["first-party-eccenca"]
    assert source.adapter == fps.ECCENCA_ADAPTER
    assert source.max_requests_per_run == 3
    assert source.max_records_per_run == 2
    records = fps.eccenca_records(_eccenca_fixture(), source)
    assert [record["sourceRecordId"] for record in records] == [
        "junior-sales-manager-inside-sales",
        "senior-cloud-infrastructure-engineer",
    ]
    assert all(record["canonicalUrl"].startswith(
        "https://eccenca.com/about-us/jobs/"
    ) for record in records)
    assert "Corporate Memory" in records[1]["description"]


def test_eccenca_fetch_uses_only_index_plus_two_discovered_detail_paths(monkeypatch):
    source = fps.load_first_party_sources()["first-party-eccenca"]
    fixture = _eccenca_fixture()
    bodies = [fixture["careersHtml"], *(row["html"] for row in fixture["details"])]
    calls = []

    class Response:
        is_redirect = False
        is_permanent_redirect = False
        headers = {}

        def __init__(self, body):
            self.body = body.encode()

        def raise_for_status(self):
            return None

        def iter_content(self, _size):
            yield self.body

    def fake_get(url, **kwargs):
        calls.append(url)
        return Response(bodies[len(calls) - 1])

    monkeypatch.setattr(fps.requests, "get", fake_get)
    assert len(fps.fetch_source(source)["details"]) == 2
    assert calls == [fps.ECCENCA_CAREERS_URL, *fps.eccenca_discovery_links(
        fixture["careersHtml"], source,
    )]


def test_eccenca_blank_or_over_cap_discovery_fails_closed():
    source = fps.load_first_party_sources()["first-party-eccenca"]
    with pytest.raises(fps.FirstPartySourceError, match="zero exact job links"):
        fps.eccenca_discovery_links("<h1>Careers</h1>", source)
    payload = _eccenca_fixture()["careersHtml"].replace(
        "</main>",
        '<a href="/about-us/jobs/third-opening">Third opening</a></main>',
    )
    with pytest.raises(fps.FirstPartySourceError, match="record cap"):
        fps.eccenca_discovery_links(payload, source)


def test_graphwise_bounds_and_detail_completeness_are_enforced():
    source = fps.load_first_party_sources()["first-party-graphwise"]
    with pytest.raises(fps.FirstPartySourceError, match="record cap"):
        fps.graphwise_records(_graphwise_fixture(), replace(source, max_records_per_run=1))
    with pytest.raises(fps.FirstPartySourceError, match="requires two requests"):
        fps.fetch_source(replace(source, max_requests_per_run=1))
    payload = _graphwise_fixture()
    payload["details"] = payload["details"][:1]
    with pytest.raises(fps.FirstPartySourceError, match="missing 1 discovered"):
        fps.graphwise_records(payload, source)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("link", "https://evil.example/jobs/semantic-ai-engineer/", "detail URL"),
        ("bamboo-url", "https://evil.example/careers/111", "apply URL"),
        ("bamboo-url", "https://graphwise.bamboohr.com/jobs/111", "apply URL"),
    ],
)
def test_graphwise_detail_and_apply_hosts_and_paths_are_exact(field, value, message):
    source = fps.load_first_party_sources()["first-party-graphwise"]
    payload = _graphwise_fixture()
    item = payload["details"][0]
    if field == "link":
        item["link"] = value
    else:
        item["toolset-meta"]["job-form"][field]["raw"] = value
    with pytest.raises(fps.FirstPartySourceError, match=message):
        fps.graphwise_records(payload, source)


def test_graphwise_fetch_uses_only_the_exact_two_bounded_endpoints(monkeypatch):
    source = fps.load_first_party_sources()["first-party-graphwise"]
    fixture = _graphwise_fixture()
    responses = [
        fixture["careersHtml"].encode(),
        b"\xef\xbb\xbf" + json.dumps(fixture["details"]).encode(),
    ]
    calls = []

    class Response:
        is_redirect = False
        is_permanent_redirect = False
        headers = {}

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def iter_content(self, _size):
            yield self.body

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(responses[len(calls) - 1])

    monkeypatch.setattr(fps.requests, "get", fake_get)
    payload = fps.fetch_source(source)
    assert len(payload["details"]) == 2
    assert calls[0][0] == "https://graphwise.ai/careers/"
    detail_url = calls[1][0]
    parsed = fps.urlparse(detail_url)
    assert parsed.scheme == "https"
    assert parsed.hostname == "graphwise.ai"
    assert parsed.path == "/wp-json/wp/v2/job"
    assert dict(fps.parse_qsl(parsed.query)) == fps.GRAPHWISE_DETAIL_QUERY
    assert all(call[1]["timeout"] == source.timeout_seconds for call in calls)
    assert all(call[1]["allow_redirects"] is False for call in calls)


def test_graphwise_byte_cap_applies_before_detail_hydration(monkeypatch):
    source = replace(
        fps.load_first_party_sources()["first-party-graphwise"],
        max_response_bytes=10,
    )

    class Response:
        is_redirect = False
        is_permanent_redirect = False
        headers = {}

        def raise_for_status(self):
            return None

        def iter_content(self, _size):
            yield b"<h2>Open Positions</h2>"

    monkeypatch.setattr(fps.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(fps.FirstPartySourceError, match="byte cap"):
        fps.fetch_source(source)


def test_malformed_content_length_is_an_isolated_source_error(monkeypatch):
    source = fps.load_first_party_sources()["first-party-graphwise"]

    class Response:
        is_redirect = False
        is_permanent_redirect = False
        headers = {"Content-Length": "not-a-number"}

        def raise_for_status(self):
            return None

        def iter_content(self, _size):
            raise AssertionError("malformed Content-Length must fail before reading the body")

    monkeypatch.setattr(fps.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(fps.FirstPartySourceError, match="malformed Content-Length"):
        fps.fetch_source(source)


def test_organization_membership_cannot_change_classification_or_evidence():
    terms = load_match_terms(ROOT / "vocabularies" / "kg-jobs.ttl")
    base = {
        "id": "same", "title": "Data Engineer", "description": "Build RDF and SPARQL services.",
        "qualifications": None, "responsibilities": None,
    }
    outputs = []
    for index, organization in enumerate((
        "https://openknowledgegraphs.com/organization/neo4j-inc/",
        "https://example.test/non-member/", None,
    )):
        record = dict(base, id=f"same-{index}", organizationIri=organization)
        classified = classify_records([record], terms)[0]
        outputs.append((classified["classification"], classified["evidence"]))
    assert outputs[0] == outputs[1] == outputs[2]


def test_graphwise_classification_is_byte_equivalent_across_membership_values():
    source = fps.load_first_party_sources()["first-party-graphwise"]
    record = fps.graphwise_records(_graphwise_fixture(), source)[0]
    terms = load_match_terms(ROOT / "vocabularies" / "kg-jobs.ttl")
    outputs = []
    for organization in (
        source.organization_iri, "https://example.test/non-member/", None,
    ):
        candidate = deepcopy(record)
        candidate["organizationIri"] = organization
        classified = classify_records([candidate], terms)[0]
        outputs.append(json.dumps({
            "classification": classified["classification"],
            "evidence": classified["evidence"],
        }, sort_keys=True))
    assert outputs[0] == outputs[1] == outputs[2]


def test_fixture_pilot_is_bounded_isolates_source_failure_and_retains_last_good(tmp_path):
    runtime = tmp_path / "first-party"
    first = run_pilot(
        fixtures=FIXTURES, runtime_dir=runtime, retrieved_at="2026-08-26T12:00:00Z",
        selected_sources=["first-party-neo4j"],
    )
    assert first["firstPartyRecordCount"] == 1
    prior = json.loads((runtime / "sources" / "first-party-neo4j.json").read_text())
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "first-party-neo4j.json").write_text("{}\n")
    second = run_pilot(
        fixtures=bad, runtime_dir=runtime, retrieved_at="2026-08-27T12:00:00Z",
        selected_sources=["first-party-neo4j"],
    )
    assert second["sourceResults"][0]["status"] == "retained-last-good"
    assert json.loads((runtime / "sources" / "first-party-neo4j.json").read_text()) == prior
    assert second["publicationPerformed"] is False


def test_bounded_topquadrant_and_eccenca_fixtures_run_through_pilot(tmp_path):
    result = run_pilot(
        fixtures=FIXTURES,
        runtime_dir=tmp_path / "first-party",
        retrieved_at="2026-08-26T12:00:00Z",
        selected_sources=["first-party-topquadrant", "first-party-eccenca"],
    )
    assert result["firstPartyRecordCount"] == 3
    assert {
        row["sourceKey"]: row["status"] for row in result["sourceResults"]
    } == {
        "first-party-eccenca": "refreshed",
        "first-party-topquadrant": "refreshed",
    }


def test_exact_allowed_host_is_validated_before_network():
    source = fps.load_first_party_sources()["first-party-neo4j"]
    bad = replace(source, endpoint="https://evil.example/jobs")
    with pytest.raises(fps.FirstPartySourceError, match="exact host"):
        fps.fetch_source(bad)


def test_live_refresh_guard_enforces_the_registry_interval(tmp_path):
    source = fps.load_first_party_sources()["first-party-neo4j"]
    runtime = tmp_path / "first-party"
    runtime.mkdir()
    (runtime / "run.json").write_text(json.dumps({
        "mode": "live-local-review",
        "retrievedAt": "2026-08-26T12:00:00Z",
        "sources": [{
            "sourceKey": source.key, "endpoint": source.endpoint,
            "adapter": source.adapter, "extractionMode": source.extraction_mode,
        }],
        "sourceResults": [{"sourceKey": source.key, "status": "refreshed"}],
    }))
    assert _live_refresh_due(runtime, source, "2026-08-27T11:59:59Z")[0] is False
    assert _live_refresh_due(runtime, source, "2026-08-27T12:00:00Z")[0] is True


def test_live_refresh_guard_carries_success_across_retained_results(tmp_path):
    source = fps.load_first_party_sources()["first-party-neo4j"]
    runtime = tmp_path / "first-party"
    runtime.mkdir()
    (runtime / "run.json").write_text(json.dumps({
        "mode": "live-local-review", "retrievedAt": "2026-08-26T13:00:00Z",
        "sources": [{
            "sourceKey": source.key, "endpoint": source.endpoint,
            "adapter": source.adapter, "extractionMode": source.extraction_mode,
        }],
        "sourceResults": [{
            "sourceKey": source.key, "status": "refresh-interval-retained",
            "lastSuccessfulAt": "2026-08-26T12:00:00Z",
        }],
    }))
    due, last_success = _live_refresh_due(runtime, source, "2026-08-27T11:59:59Z")
    assert due is False
    assert last_success == "2026-08-26T12:00:00Z"


def test_live_refresh_guard_invalidates_a_cached_source_when_adapter_changes(tmp_path):
    source = fps.load_first_party_sources()["first-party-graphwise"]
    runtime = tmp_path / "first-party"
    runtime.mkdir()
    (runtime / "run.json").write_text(json.dumps({
        "mode": "live-local-review", "retrievedAt": "2026-08-26T12:00:00Z",
        "sources": [{
            "sourceKey": source.key, "endpoint": source.endpoint,
            "adapter": "firstparty-schema",
            "extractionMode": "bounded-schema.org-jobposting",
        }],
        "sourceResults": [{
            "sourceKey": source.key, "status": "refreshed",
            "lastSuccessfulAt": "2026-08-26T12:00:00Z",
        }],
    }))
    assert _live_refresh_due(runtime, source, "2026-08-26T12:01:00Z")[0] is True


def test_graphwise_zero_result_retains_last_good_snapshot(tmp_path):
    runtime = tmp_path / "first-party"
    first = run_pilot(
        fixtures=FIXTURES, runtime_dir=runtime, retrieved_at="2026-08-26T12:00:00Z",
        selected_sources=["first-party-graphwise"],
    )
    assert first["firstPartyRecordCount"] == 2
    prior = json.loads((runtime / "sources" / "first-party-graphwise.json").read_text())
    prior_raw = (runtime / "raw" / "first-party-graphwise.json").read_bytes()
    bad = tmp_path / "bad"
    bad.mkdir()
    bad_payload = _graphwise_fixture()
    bad_payload["careersHtml"] = "<h2>Open Positions</h2>"
    bad_payload["details"] = []
    (bad / "first-party-graphwise.json").write_text(json.dumps(bad_payload))
    second = run_pilot(
        fixtures=bad, runtime_dir=runtime, retrieved_at="2026-08-27T12:00:00Z",
        selected_sources=["first-party-graphwise"],
    )
    result = second["sourceResults"][0]
    assert result["status"] == "retained-last-good"
    assert "opening markers" in result["error"]
    assert json.loads((runtime / "sources" / "first-party-graphwise.json").read_text()) == prior
    assert (runtime / "raw" / "first-party-graphwise.json").read_bytes() == prior_raw


def test_partial_source_run_carries_forward_omitted_raw_evidence(tmp_path):
    runtime = tmp_path / "first-party"
    run_pilot(
        fixtures=FIXTURES, runtime_dir=runtime, retrieved_at="2026-08-26T12:00:00Z",
        selected_sources=["first-party-graphwise", "first-party-neo4j"],
    )
    graphwise_raw = (runtime / "raw" / "first-party-graphwise.json").read_bytes()
    run_pilot(
        fixtures=FIXTURES, runtime_dir=runtime, retrieved_at="2026-08-27T12:00:00Z",
        selected_sources=["first-party-neo4j"],
    )
    assert (runtime / "raw" / "first-party-graphwise.json").read_bytes() == graphwise_raw


def test_refresh_interval_skip_carries_forward_raw_evidence(tmp_path, monkeypatch):
    runtime = tmp_path / "first-party"
    run_pilot(
        fixtures=FIXTURES, runtime_dir=runtime, retrieved_at="2026-08-26T12:00:00Z",
        selected_sources=["first-party-graphwise"],
    )
    run_path = runtime / "run.json"
    prior_run = json.loads(run_path.read_text())
    prior_run["mode"] = "live-local-review"
    run_path.write_text(json.dumps(prior_run))
    prior_raw = (runtime / "raw" / "first-party-graphwise.json").read_bytes()
    monkeypatch.setattr(
        fpp, "fetch_source",
        lambda _source: (_ for _ in ()).throw(AssertionError("refresh should be skipped")),
    )
    result = run_pilot(
        live=True, runtime_dir=runtime, retrieved_at="2026-08-26T13:00:00Z",
        selected_sources=["first-party-graphwise"],
    )
    assert result["sourceResults"][0]["status"] == "refresh-interval-retained"
    assert (runtime / "raw" / "first-party-graphwise.json").read_bytes() == prior_raw


def test_fixture_pilot_json_and_rdf_are_byte_deterministic(tmp_path):
    runtimes = [tmp_path / "one", tmp_path / "two"]
    for runtime in runtimes:
        run_pilot(
            fixtures=FIXTURES, runtime_dir=runtime,
            retrieved_at="2026-08-26T12:00:00Z",
            selected_sources=["first-party-neo4j"],
        )
    assert (runtimes[0] / "jobs.json").read_bytes() == (runtimes[1] / "jobs.json").read_bytes()
    assert (runtimes[0] / "jobs.ttl").read_bytes() == (runtimes[1] / "jobs.ttl").read_bytes()
