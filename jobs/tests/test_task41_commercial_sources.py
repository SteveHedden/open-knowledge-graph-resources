"""Task 41 fixed-cohort audit, adapter, and registry-derived workflow contracts."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from rdflib import Graph, Namespace, RDF, URIRef

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
FIXTURES = ROOT / "tests" / "fixtures" / "first-party-pilot"
AUDIT = ROOT / "audits" / "task41-commercial-source-audit.json"
sys.path.insert(0, str(ROOT / "scripts"))

import first_party_sources as fps  # noqa: E402
from first_party_pilot import run_pilot  # noqa: E402
import live_pipeline  # noqa: E402
import source_schedule  # noqa: E402
from live_sources import LivePipelineError  # noqa: E402


FIXED_20 = {
    "arcade-data-ltd", "artsy", "biblioteksentralen",
    "blackcat-informatics-inc", "cambridge-semantics", "databricks",
    "franz-inc", "marklogic", "memgraph", "metaweb", "ontopic",
    "ontotext", "openlink-software", "q107620349", "sage-publishing",
    "semantic-web-company", "terminusdb", "triply", "typedb", "zazuko",
}
VIABLE = {
    "artsy": "first-party-artsy",
    "databricks": "first-party-databricks",
    "sage-publishing": "first-party-sage-publishing",
    "triply": "first-party-triply",
}


def fixture(key: str):
    path = FIXTURES / f"{key}.json"
    return json.loads(path.read_text())


def test_audit_is_exactly_the_fixed_20_with_deterministic_dispositions():
    audit = json.loads(AUDIT.read_text())
    rows = audit["organizations"]
    assert len(rows) == 20
    assert {row["id"] for row in rows} == FIXED_20
    assert len({row["id"] for row in rows}) == 20
    assert all(
        row["sourceDisposition"] and row["evidence"] and "officialHomepage" in row
        for row in rows
    )
    assert {
        row["id"] for row in rows if row["sourceDisposition"] == "review-source-added"
    } == set(VIABLE)
    for row in rows:
        assert row["proposedProductionAction"] in {
            "manager-review", "remain-registry-only"
        }
    assert audit["productionCandidates"] == ["first-party-databricks"]
    by_id = {row["id"]: row for row in rows}
    assert "HTTP 401" in by_id["artsy"]["robotsEvidence"]
    assert "HTTP 404" in by_id["triply"]["robotsEvidence"]
    assert by_id["sage-publishing"]["contentContainer"].startswith("main#main-content")


def test_canonical_organization_names_and_homepages_are_reconciled():
    organizations = json.loads((REPO_ROOT / "data" / "organizations.json").read_text())
    by_id = {row["identifier"]: row for row in organizations["organizations"]}
    assert by_id["q107620349"]["name"] == "Bokbasen"
    assert by_id["marklogic"]["officialWebsite"] == "https://www.progress.com/marklogic"
    assert by_id["semantic-web-company"]["officialWebsite"] == "https://semantic-web.com/"


def test_viable_sources_have_complete_review_contracts_and_remain_inert():
    sources = fps.load_first_party_sources()
    production = fps.load_production_first_party_sources()
    organizations = json.loads((REPO_ROOT / "data" / "organizations.json").read_text())
    by_iri = {row["iri"]: row for row in organizations["organizations"]}
    graph = Graph().parse(REPO_ROOT / "sources.ttl", format="turtle")
    dcterms = Namespace("http://purl.org/dc/terms/")
    dcat = Namespace("http://www.w3.org/ns/dcat#")
    okg = Namespace("https://openknowledgegraphs.com/ontology#")
    for key in VIABLE.values():
        source = sources[key]
        assert key not in production
        assert source.republication_status == "local-review-only"
        assert source.production_approved is False
        assert by_iri[source.organization_iri]["jobsProductionEnabled"] is False
        subject = URIRef(source.dataset_uri)
        assert (subject, RDF.type, okg.CareerSource) in graph
        assert len(list(graph.objects(subject, dcterms.publisher))) == 1
        assert len(list(graph.objects(subject, dcat.landingPage))) == 1
        assert len(list(graph.objects(subject, dcat.endpointURL))) == 1
        assert source.refresh_interval_seconds >= 86400
        assert source.timeout_seconds <= 20


@pytest.mark.parametrize(
    ("key", "adapter", "expected_id"),
    [
        ("first-party-sage-publishing", fps.TEAMTAILOR_ADAPTER, "8124398"),
        (
            "first-party-triply", fps.SAME_SITE_DETAIL_ADAPTER,
            "software-engineer-database-systems",
        ),
    ],
)
def test_new_html_detail_adapters_normalize_network_free_fixtures(
    key, adapter, expected_id,
):
    source = fps.load_first_party_sources()[key]
    assert source.adapter == adapter
    records = fps.records_from_payload(fixture(key), source)
    assert [row["sourceRecordId"] for row in records] == [expected_id]
    assert records[0]["organizationIri"] == source.organization_iri
    assert records[0]["sourceUrl"] == records[0]["canonicalUrl"]
    assert records[0]["firstParty"] is True
    assert "company navigation" not in records[0]["description"].casefold()
    assert "boilerplate" not in records[0]["description"].casefold()
    if key == "first-party-triply":
        assert "semantic data services" in records[0]["description"].casefold()


@pytest.mark.parametrize("key", ["first-party-sage-publishing", "first-party-triply"])
def test_new_html_adapters_run_end_to_end_through_hypothetical_production_gate(
    tmp_path, monkeypatch, key,
):
    reviewed = fps.load_first_party_sources()[key]
    approved = replace(
        reviewed,
        production_approved=True,
        republication_status="production-approved",
    )
    monkeypatch.setattr(
        live_pipeline,
        "load_production_first_party_sources",
        lambda *_args, **_kwargs: {key: approved},
    )
    runtime = tmp_path / key
    run = live_pipeline.run_pipeline(
        source_key=key,
        root=ROOT,
        runtime_dir=runtime,
        retrieved_at="2026-08-27T12:00:00Z",
        first_party_fetcher=lambda _source: fixture(key),
    )
    records = json.loads((runtime / "jobs.json").read_text())
    diagnostics = json.loads((runtime / "sources" / f"{key}.json").read_text())
    assert run["sourceKey"] == key
    assert diagnostics
    if key == "first-party-triply":
        assert records
        assert all(row["classification"] == "qualified" for row in records)
    else:
        assert records == []
        assert run["sourceClassificationCounts"]["not_match"] == 1
    assert (runtime / "raw" / f"{key}.json").is_file()


@pytest.mark.parametrize("key", ["first-party-sage-publishing", "first-party-triply"])
def test_header_and_footer_boilerplate_cannot_qualify_unrelated_job(tmp_path, key):
    source = fps.load_first_party_sources()[key]
    payload = fixture(key)
    if key == "first-party-sage-publishing":
        payload["details"][0]["html"] = (
            "<header>Knowledge graph RDF ontology SPARQL</header>"
            "<main><h1>Software Engineer</h1>"
            '<div class="mx-auto max-w-750 prose font-company-body">'
            "Build reliable customer account software and internal web forms."
            "</div></main><footer>Semantic web linked data SHACL</footer>"
        )
    else:
        payload["details"][0]["html"] = (
            "<header>Knowledge graph RDF ontology SPARQL</header>"
            "<main><h1>Software Engineer</h1>"
            '<div id="content" class="Vacancies_vacancyContent__fixture">'
            "Build reliable customer account software and internal web forms."
            "</div></main><footer>Semantic web linked data SHACL</footer>"
        )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / f"{key}.json").write_text(json.dumps(payload))
    result = run_pilot(
        fixtures=fixtures,
        runtime_dir=tmp_path / "runtime",
        retrieved_at="2026-08-27T12:00:00Z",
        selected_sources=[key],
    )
    source_result = result["sourceResults"][0]
    assert source_result["records"] == 1
    assert source_result["qualified"] == 0


@pytest.mark.parametrize("key", ["first-party-sage-publishing", "first-party-triply"])
def test_new_adapters_distinguish_reviewed_zero_from_broken_or_partial_pages(key):
    source = fps.load_first_party_sources()[key]
    zero = {
        "listingPages": [{
            "url": source.endpoint,
            "html": "<main><h1>Open positions</h1><p>No current openings.</p></main>",
        }],
        "details": [],
    }
    assert fps.records_from_payload(zero, source) == []
    broken = {
        "listingPages": [{
            "url": source.endpoint,
            "html": '<main><input placeholder="Search jobs"><h1>Open positions</h1></main>',
        }],
        "details": [],
    }
    with pytest.raises(fps.FirstPartySourceError, match="zero exact job links"):
        fps.records_from_payload(broken, source)
    partial = fixture(key)
    partial["details"] = []
    with pytest.raises(fps.FirstPartySourceError, match="does not exactly match"):
        fps.records_from_payload(partial, source)


def test_new_adapter_host_path_and_record_bounds_fail_closed():
    sources = fps.load_first_party_sources()
    triply = fixture("first-party-triply")
    triply["details"][0]["url"] = "https://evil.example/vacancies/job"
    with pytest.raises(fps.FirstPartySourceError, match="exact host"):
        fps.same_site_detail_records(triply, sources["first-party-triply"])

    sage = fixture("first-party-sage-publishing")
    with pytest.raises(fps.FirstPartySourceError, match="record cap"):
        fps.teamtailor_records(
            sage,
            replace(sources["first-party-sage-publishing"], max_records_per_run=0),
        )


def test_teamtailor_fetch_follows_only_bounded_reviewed_list_and_detail_paths(monkeypatch):
    source = fps.load_first_party_sources()["first-party-sage-publishing"]
    first = (
        '<h1>Open positions</h1><a href="/jobs/8124398-metadata-product-manager">Job</a>'
        '<a href="/jobs/show_more?page=2">More</a>'
    )
    second = '<p>No more pages.</p>'
    detail = (
        '<main><h1>Metadata Product Manager</h1>'
        '<div class="max-w-750 prose font-company-body">'
        'Lead taxonomy and metadata systems for publishing.</div></main>'
    )
    bodies = [first, second, detail]
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
    payload = fps.fetch_source(source)
    assert fps.teamtailor_records(payload, source)[0]["sourceRecordId"] == "8124398"
    assert calls == [
        "https://careers.sagepub.com/jobs",
        "https://careers.sagepub.com/jobs/show_more?page=2",
        "https://careers.sagepub.com/jobs/8124398-metadata-product-manager",
    ]


def test_new_adapter_timeout_is_an_isolated_source_error(monkeypatch):
    source = fps.load_first_party_sources()["first-party-triply"]
    monkeypatch.setattr(
        fps.requests, "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(fps.requests.Timeout()),
    )
    with pytest.raises(fps.FirstPartySourceError, match="request failed: Timeout"):
        fps.fetch_source(source)


@pytest.mark.parametrize("key", ["first-party-sage-publishing", "first-party-triply"])
def test_new_adapter_redirects_fail_closed(monkeypatch, key):
    source = fps.load_first_party_sources()[key]

    class Redirect:
        is_redirect = True
        is_permanent_redirect = False
        headers = {"Location": "https://evil.example/jobs"}

    monkeypatch.setattr(fps.requests, "get", lambda *args, **kwargs: Redirect())
    with pytest.raises(fps.FirstPartySourceError, match="disallowed redirect"):
        fps.fetch_source(source)


@pytest.mark.parametrize("key", ["first-party-sage-publishing", "first-party-triply"])
def test_new_adapter_failure_retains_last_good_records_and_raw(tmp_path, key):
    runtime = tmp_path / "runtime"
    first = run_pilot(
        fixtures=FIXTURES, runtime_dir=runtime,
        retrieved_at="2026-08-27T12:00:00Z", selected_sources=[key],
    )
    assert first["sourceResults"][0]["status"] == "refreshed"
    prior_records = (runtime / "sources" / f"{key}.json").read_bytes()
    prior_raw = (runtime / "raw" / f"{key}.json").read_bytes()
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / f"{key}.json").write_text(json.dumps({
        "listingPages": [{"url": fps.load_first_party_sources()[key].endpoint, "html": "<h1>Open positions</h1>"}],
        "details": [],
    }))
    second = run_pilot(
        fixtures=bad, runtime_dir=runtime,
        retrieved_at="2026-08-28T12:00:00Z", selected_sources=[key],
    )
    assert second["sourceResults"][0]["status"] == "retained-last-good"
    assert (runtime / "sources" / f"{key}.json").read_bytes() == prior_records
    assert (runtime / "raw" / f"{key}.json").read_bytes() == prior_raw


def test_registry_driven_schedule_is_complete_bounded_and_excludes_review_sources():
    weights = source_schedule.production_source_weights()
    batches = source_schedule.bounded_batches()
    flattened = [key for batch in batches for key in batch]
    assert set(flattened) == set(weights)
    assert len(flattened) == len(set(flattened))
    assert all(
        sum(weights[key] for key in batch) <= source_schedule.DEFAULT_BATCH_REQUEST_CAP
        for batch in batches
    )
    assert not (set(VIABLE.values()) & set(flattened))
    assert "remotive" not in flattened
    workflow = (REPO_ROOT / ".github" / "workflows" / "update-jobs.yml").read_text()
    assert "scripts/task42_nightly.py" in workflow
    assert "sources=(" not in workflow
    assert "group: repository-publication" in workflow


def test_sage_hypothetical_approval_fits_the_workflow_request_cap(monkeypatch):
    monkeypatch.setattr(
        source_schedule,
        "production_source_weights",
        lambda *_args, **_kwargs: {
            "adzuna": 4,
            "first-party-sage-publishing": 64,
            "himalayas": 4,
        },
    )
    batches = source_schedule.bounded_batches()
    assert any("first-party-sage-publishing" in batch for batch in batches)
    assert all(
        sum(source_schedule.production_source_weights()[key] for key in batch)
        <= source_schedule.DEFAULT_BATCH_REQUEST_CAP
        for batch in batches
    )


def test_remotive_is_rejected_by_default_production_pipeline(tmp_path):
    with pytest.raises(LivePipelineError, match="unknown or disabled source 'remotive'"):
        live_pipeline.run_pipeline(
            source_key="remotive",
            root=ROOT,
            runtime_dir=tmp_path / "runtime",
            retrieved_at="2026-08-27T12:00:00Z",
            fetcher=lambda *_args: (_ for _ in ()).throw(
                AssertionError("production gate must fail before network")
            ),
        )
