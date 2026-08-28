"""Task 42 fixed-cohort discovery, approval, and production contracts."""

from __future__ import annotations

import json
import sys
import hashlib
import multiprocessing
import time
import zipfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from rdflib import Graph, Namespace, RDF, URIRef


ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
FIXTURES = ROOT / "tests" / "fixtures" / "first-party-pilot"
DISCOVERY = ROOT / "audits" / "task42-careers-discovery.json"
AUDIT = ROOT / "audits" / "task42-organization-source-audit.json"
NIGHTLY_PLAN = ROOT / "audits" / "task42-nightly-operational-plan.json"
APPROVAL = ROOT / "audits" / "task42-production-approval.json"
sys.path.insert(0, str(ROOT / "scripts"))

import first_party_sources as fps  # noqa: E402
import live_pipeline  # noqa: E402
import promote_jobs_snapshot  # noqa: E402
import source_schedule  # noqa: E402
import task42_nightly  # noqa: E402
from career_discovery_monitor import (  # noqa: E402
    MAX_RESPONSE_BYTES, monitored_pages, run_monitor,
)
from first_party_pilot import run_pilot  # noqa: E402
from task42_discovery import SUPPLEMENTAL_PROBES, validate as validate_discovery  # noqa: E402
from task42_source_audit import (  # noqa: E402
    AuditError, TASK41_FIXED_20, build_audit, fixed_cohort,
    task42_review_sources,
)
from task42_review_archive import build_archive  # noqa: E402


TASK42_SOURCE_KEYS = {
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


def _slow_nightly_worker(_source_key: str, _output: str) -> None:
    time.sleep(2)


def fixture(key: str):
    return json.loads((FIXTURES / f"{key}.json").read_text(encoding="utf-8"))


def fixture_runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "review"
    run_pilot(
        fixtures=FIXTURES,
        runtime_dir=runtime,
        retrieved_at="2026-08-27T23:30:00Z",
        selected_sources=sorted(TASK42_SOURCE_KEYS),
    )
    # Exercise the live-diagnostic audit contract with deterministic fixtures.
    run_path = runtime / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["mode"] = "live-local-review"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    return runtime


def test_fixed_cohort_is_exactly_107_and_preserves_the_85_22_baseline():
    cohort = fixed_cohort()
    identifiers = {row["identifier"] for row in cohort}
    assert len(cohort) == len(identifiers) == 107
    assert sum(bool(row.get("careersPage")) for row in cohort) == 85
    assert sum(not row.get("careersPage") for row in cohort) == 22
    assert not identifiers & TASK41_FIXED_20
    approved = {
        source.organization_iri for source in task42_review_sources().values()
    }
    assert {
        row["iri"] for row in cohort if row["jobsProductionEnabled"]
    } == approved


def test_discovery_capture_has_actual_bounded_evidence_for_all_85_pages():
    payload = json.loads(DISCOVERY.read_text(encoding="utf-8"))
    validate_discovery(payload)
    pages = payload["careersPages"]
    assert len(pages) == len({row["identifier"] for row in pages}) == 85
    assert all(row["careersPage"].startswith("https://") for row in pages)
    assert all(row["discoveryReason"] for row in pages)
    assert all(
        row["httpStatus"] is not None or row["retrievalError"]
        for row in pages
    )
    assert all(
        row["contentSha256"] or row["retrievalError"]
        for row in pages
    )
    assert all("secondaryProbes" in row for row in pages)
    assert sum(bool(row["secondaryProbes"]) for row in pages) >= 60
    assert payload["bounds"]["requestsPerCareersPageMaximum"] == 3
    assert {row["identifier"] for row in payload["supplementalChecks"]} == set(
        SUPPLEMENTAL_PROBES
    )


def test_known_misses_have_specific_retrieved_provider_decisions(tmp_path):
    audit = build_audit(fixture_runtime(tmp_path))
    by_id = {row["id"]: row for row in audit["organizations"]}
    expected = {
        "metropolitan-museum-of-art": ("workday", "pipeline-ready"),
        "national-library-of-norway": ("webcruiter", "pipeline-ready"),
        "stanford-university-school-of-medicine": ("taleo-selectminds", "pipeline-ready"),
        "helsinki-university-library": ("successfactors", "blocked"),
        "defense-logistics-agency": ("usajobs", "blocked"),
        "library-of-congress": ("loc-careers", "blocked"),
    }
    for identifier, (provider, disposition) in expected.items():
        row = by_id[identifier]
        assert row["provider"] == provider
        assert row["sourceDisposition"] == disposition
        assert row["exactEndpoint"].startswith("https://")
        assert row["sourceDispositionReason"]
        assert row["discoveryEvidence"]["discoveryReason"]


def test_task42_sources_are_complete_approved_24_hour_contracts():
    sources = fps.load_first_party_sources()
    assert set(task42_review_sources()) == TASK42_SOURCE_KEYS
    production = fps.load_production_first_party_sources()
    scheduled = {key for batch in source_schedule.bounded_batches() for key in batch}
    organizations = json.loads((REPO_ROOT / "data" / "organizations.json").read_text())
    by_iri = {row["iri"]: row for row in organizations["organizations"]}
    graph = Graph().parse(REPO_ROOT / "sources.ttl", format="turtle")
    dcterms = Namespace("http://purl.org/dc/terms/")
    dcat = Namespace("http://www.w3.org/ns/dcat#")
    okg = Namespace("https://openknowledgegraphs.com/ontology#")
    for key in TASK42_SOURCE_KEYS:
        source = sources[key]
        assert key in production
        assert key in scheduled
        assert source.republication_status == "production-approved"
        assert source.production_approved is True
        assert by_iri[source.organization_iri]["jobsProductionEnabled"] is True
        subject = URIRef(source.dataset_uri)
        assert (subject, RDF.type, okg.CareerSource) in graph
        assert len(list(graph.objects(subject, dcterms.publisher))) == 1
        assert len(list(graph.objects(subject, dcat.landingPage))) == 1
        assert len(list(graph.objects(subject, dcat.endpointURL))) == 1
        assert source.refresh_interval_seconds == 86400
        assert source.timeout_seconds <= 20
        assert source.max_requests_per_batch <= 64
        assert source.max_requests_per_batch <= source.max_requests_per_run
        assert source.max_requests_per_run <= 800
        assert source.max_response_bytes <= 5_000_000
    assert {
        key: (
            sources[key].max_requests_per_run,
            sources[key].max_requests_per_batch,
        )
        for key in (
            "first-party-microsoft-research",
            "first-party-stanford-university-school-of-medicine",
            "first-party-university-of-maryland",
            "first-party-university-of-north-carolina-at-chapel-hill",
        )
    } == {
        "first-party-microsoft-research": (150, 64),
        "first-party-stanford-university-school-of-medicine": (203, 64),
        "first-party-university-of-maryland": (315, 64),
        "first-party-university-of-north-carolina-at-chapel-hill": (800, 64),
    }


def test_plos_enforces_the_exact_greenhouse_tenant_and_posting_path():
    source = fps.load_first_party_sources()["first-party-public-library-of-science"]
    records = fps.records_from_payload(fixture(source.key), source)
    assert source.endpoint == "https://boards-api.greenhouse.io/v1/boards/plos/jobs"
    assert [row["sourceRecordId"] for row in records] == ["1234567"]
    assert records[0]["canonicalUrl"] == (
        "https://job-boards.eu.greenhouse.io/plos/jobs/1234567"
    )
    for bad_url in (
        "https://job-boards.eu.greenhouse.io/other/jobs/1234567",
        "https://job-boards.eu.greenhouse.io/plos/jobs/1234567?ref=x",
        "https://evil.example/plos/jobs/1234567",
    ):
        bad = deepcopy(fixture(source.key))
        bad["jobs"][0]["absolute_url"] = bad_url
        with pytest.raises(fps.FirstPartySourceError):
            fps.records_from_payload(bad, source)


@pytest.mark.parametrize(
    "key",
    ["first-party-embl-ebi", "first-party-metropolitan-museum-of-art"],
)
def test_workday_adapter_requires_exact_filtered_listing_and_complete_details(key):
    source = fps.load_first_party_sources()[key]
    payload = fixture(key)
    records = fps.records_from_payload(payload, source)
    assert source.adapter == fps.WORKDAY_ADAPTER
    assert len(records) == 1
    assert records[0]["sourceRecordId"].startswith("JR")
    partial = deepcopy(payload)
    partial["details"] = []
    with pytest.raises(fps.FirstPartySourceError, match="exactly match"):
        fps.records_from_payload(partial, source)
    over_cap = replace(source, max_records_per_run=0)
    with pytest.raises(fps.FirstPartySourceError, match="record cap"):
        fps.records_from_payload(payload, over_cap)
    escaped = deepcopy(payload)
    escaped["details"][0]["payload"]["jobPostingInfo"]["externalUrl"] = (
        "https://evil.example/site/job/location/job"
    )
    with pytest.raises(fps.FirstPartySourceError, match="unapproved host"):
        fps.records_from_payload(escaped, source)


def test_workday_fetch_uses_exact_facet_and_bounded_cxs_detail_paths(monkeypatch):
    source = fps.load_first_party_sources()["first-party-embl-ebi"]
    payload = fixture(source.key)
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

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs["json"]))
        return Response(payload["listingPages"][0])

    def fake_get(url, **kwargs):
        calls.append(("GET", url, None))
        return Response(payload["details"][0]["payload"])

    monkeypatch.setattr(fps.requests, "post", fake_post)
    monkeypatch.setattr(fps.requests, "get", fake_get)
    live_payload = fps.fetch_source(source)
    assert len(fps.records_from_payload(live_payload, source)) == 1
    assert calls[0] == (
        "POST",
        "https://embl.wd103.myworkdayjobs.com/wday/cxs/embl/EMBL/jobs",
        {
            "appliedFacets": {"locations": ["e80afa7e65f5100729542eddd1ff0000"]},
            "limit": 20,
            "offset": 0,
            "searchText": "",
        },
    )
    assert calls[1][0:2] == (
        "GET",
        "https://embl.wd103.myworkdayjobs.com/wday/cxs/embl/EMBL"
        "/job/Hinxton-Cambridgeshire/Scientific-Database-Curator_JR4174",
    )


def test_large_workday_source_uses_explicit_bounded_request_batches(monkeypatch):
    source = fps.load_first_party_sources()["first-party-university-of-maryland"]
    total = 125
    paths = [f"/job/College-Park/Role-{index}_JR{100000 + index}" for index in range(total)]
    paths[0] = "/job/Electrical-Hardware-and-Systems-Validation_JR100000"
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

    def fake_post(_url, **kwargs):
        offset = kwargs["json"]["offset"]
        rows = [{
            "title": f"Role {index}",
            "externalPath": paths[index],
            "locationsText": "College Park, MD",
        } for index in range(offset, min(offset + 20, total))]
        calls.append("listing")
        return Response({"total": total, "jobPostings": rows})

    def fake_get(url, **_kwargs):
        external_path = url.split("/wday/cxs/umd/UMCP", 1)[1]
        index = paths.index(external_path)
        calls.append("detail")
        return Response({"jobPostingInfo": {
            "externalUrl": f"https://umd.wd1.myworkdayjobs.com/UMCP{external_path}",
            "jobDescription": "A complete non-semantic position description with sufficient reviewed detail for bounded ingestion.",
            "jobReqId": f"JR{100000 + index}",
            "location": "College Park, MD",
            "title": f"Role {index}",
        }})

    monkeypatch.setattr(fps.requests, "post", fake_post)
    monkeypatch.setattr(fps.requests, "get", fake_get)
    payload = fps.fetch_source(source)
    assert len(fps.records_from_payload(payload, source)) == total
    assert len(payload["requestBatches"]) == 3
    assert all(
        batch["listingRequests"] + batch["detailRequests"]
        <= source.max_requests_per_batch
        for batch in payload["requestBatches"]
    )
    assert len(calls) <= source.max_requests_per_run
    assert calls.count("listing") == 7
    assert calls.count("detail") == total
    no_batch_evasion = deepcopy(payload)
    no_batch_evasion.pop("requestBatches")
    with pytest.raises(fps.FirstPartySourceError, match="complete-run request cap"):
        fps.records_from_payload(
            no_batch_evasion,
            replace(source, max_requests_per_run=100),
        )


def test_webcruiter_adapter_requires_exact_companylock_and_complete_result():
    source = fps.load_first_party_sources()["first-party-national-library-of-norway"]
    payload = fixture(source.key)
    records = fps.records_from_payload(payload, source)
    assert source.adapter == fps.WEBCRUITER_ADAPTER
    assert [row["sourceRecordId"] for row in records] == ["5128624401"]
    assert records[0]["requisitionId"] is None
    assert "Kvalifikasjoner" in records[0]["description"]
    wrong_tenant = deepcopy(payload)
    wrong_tenant["listing"]["Data"][0]["TenantId"] = "999999"
    with pytest.raises(fps.FirstPartySourceError, match="companylock"):
        fps.records_from_payload(wrong_tenant, source)
    partial = deepcopy(payload)
    partial["listing"]["Total"] = 2
    with pytest.raises(fps.FirstPartySourceError, match="partial"):
        fps.records_from_payload(partial, source)
    listing_only = deepcopy(payload["listing"])
    with pytest.raises(fps.FirstPartySourceError, match="full hydrated details"):
        fps.records_from_payload(listing_only, source)


def test_webcruiter_fetch_uses_exact_company_endpoint_token_and_hydrates_details(monkeypatch):
    source = fps.load_first_party_sources()["first-party-national-library-of-norway"]
    payload = fixture(source.key)
    calls = []

    class Response:
        is_redirect = False
        is_permanent_redirect = False
        headers = {}

        def __init__(self, body):
            self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

        def raise_for_status(self):
            return None

        def iter_content(self, _size):
            yield self.body

    class Session:
        def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            return Response(b'<input name="__RequestVerificationToken" value="exact-token">')

        def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            return Response(payload["listing"])

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        return Response(payload["details"][0]["html"].encode())

    monkeypatch.setattr(fps.requests, "Session", Session)
    monkeypatch.setattr(fps.requests, "get", fake_get)
    assert len(fps.records_from_payload(fps.fetch_source(source), source)) == 1
    assert [call[0:2] for call in calls] == [
        ("GET", source.endpoint),
        ("POST", "https://candidate.webcruiter.com/api/odvert/companysearch/810013"),
        ("GET", payload["details"][0]["url"]),
    ]
    assert calls[1][2]["headers"]["X-RequestVerificationToken"] == "exact-token"
    assert calls[1][2]["data"]["pageSize"] == source.max_records_per_run


@pytest.mark.parametrize(
    ("key", "adapter", "records"),
    [
        ("first-party-danish-bibliographic-centre", fps.EMPLY_ADAPTER, 0),
        ("first-party-regenstrief-institute", fps.UKG_ADAPTER, 1),
        ("first-party-renaissance-computing-institute", fps.PEOPLEADMIN_ADAPTER, 1),
        ("first-party-sib-swiss-institute-of-bioinformatics", fps.REFLINE_ADAPTER, 0),
        ("first-party-the-open-university", fps.SUCCESSFACTORS_ADAPTER, 1),
        ("first-party-wikimedia-deutschland", fps.SOFTGARDEN_ADAPTER, 1),
    ],
)
def test_reusable_provider_fixtures_have_exact_bounded_contracts(key, adapter, records):
    source = fps.load_first_party_sources()[key]
    assert source.adapter == adapter
    assert len(fps.records_from_payload(fixture(key), source)) == records


@pytest.mark.parametrize("key", sorted(TASK42_SOURCE_KEYS))
def test_every_task42_source_runs_end_to_end_through_the_real_production_pipeline(
    key, tmp_path
):
    """Exercise the production path, not merely each payload normalizer."""

    source = fps.load_first_party_sources()[key]
    runtime = tmp_path / key
    run = live_pipeline.run_pipeline(
        source_key=key,
        root=ROOT,
        runtime_dir=runtime,
        retrieved_at="2026-08-28T00:00:00Z",
        first_party_fetcher=lambda selected: fixture(selected.key),
    )

    expected = len(fps.records_from_payload(fixture(key), source))
    assert run["sourceKey"] == key
    assert run["fetchedCount"] == expected
    assert run["publicationPolicy"] == "first-party-qualified-only"
    assert run["completeSourceSnapshot"] is True
    assert (runtime / "raw" / f"{key}.json").is_file()
    assert (runtime / "sources" / f"{key}.json").is_file()
    graph = Graph().parse(runtime / "jobs.ttl", format="turtle")
    schema = Namespace("https://schema.org/")
    published = json.loads((runtime / "jobs.json").read_text(encoding="utf-8"))
    assert len(set(graph.subjects(RDF.type, schema.JobPosting))) == len(published)
    assert all(row["classification"] == "qualified" for row in published)


def test_task42_live_review_archive_approves_only_two_current_jobs():
    archive = ROOT / "audits" / "task42-live-review-inputs.zip"
    records = []
    with zipfile.ZipFile(archive) as replay:
        for key in sorted(TASK42_SOURCE_KEYS):
            records.extend(json.loads(replay.read(f"sources/{key}.json")))
    assert {
        row["id"] for row in records if row["classification"] == "qualified"
    } == {
        "firstparty:first-party-embl-ebi:JR4207",
        "firstparty:first-party-university-of-maryland:JR104812",
    }
    assert {
        row["id"] for row in records if row["classification"] == "review"
    } == {"firstparty:first-party-embl-ebi:JR3997"}


def test_new_provider_contracts_reject_partial_or_escaped_payloads():
    sources = fps.load_first_party_sources()

    successfactors = deepcopy(fixture("first-party-the-open-university"))
    successfactors["listing"]["totalJobs"] = 2
    with pytest.raises(fps.FirstPartySourceError, match="partial"):
        fps.records_from_payload(
            successfactors, sources["first-party-the-open-university"]
        )

    ukg = deepcopy(fixture("first-party-regenstrief-institute"))
    ukg["details"] = []
    with pytest.raises(fps.FirstPartySourceError, match="exactly match"):
        fps.records_from_payload(ukg, sources["first-party-regenstrief-institute"])

    softgarden = deepcopy(fixture("first-party-wikimedia-deutschland"))
    softgarden["details"][0]["url"] = "https://evil.example/job/66319753/x"
    with pytest.raises(fps.FirstPartySourceError, match="exactly match"):
        fps.records_from_payload(
            softgarden, sources["first-party-wikimedia-deutschland"]
        )

    peopleadmin = deepcopy(fixture("first-party-renaissance-computing-institute"))
    peopleadmin["listingHtml"] += '<a href="/postings/search?page=2">Next</a>'
    with pytest.raises(fps.FirstPartySourceError, match="pagination is partial"):
        fps.records_from_payload(
            peopleadmin, sources["first-party-renaissance-computing-institute"]
        )


def test_peopleadmin_multi_page_board_is_complete_and_bounded():
    source = fps.load_first_party_sources()[
        "first-party-university-of-north-carolina-at-chapel-hill"
    ]
    payload = fixture(source.key)
    records = fps.records_from_payload(payload, source)
    assert len(records) == 2
    assert {row["sourceRecordId"] for row in records} == {"400001", "400002"}
    partial = deepcopy(payload)
    partial["listingPages"].pop()
    partial["requestBatches"][0]["listingRequests"] -= 1
    with pytest.raises(fps.FirstPartySourceError, match="pagination is partial"):
        fps.records_from_payload(partial, source)


def test_peopleadmin_listing_pagination_spans_bounded_request_batches(monkeypatch):
    source = replace(
        fps.load_first_party_sources()[
            "first-party-university-of-north-carolina-at-chapel-hill"
        ],
        max_records_per_run=70,
    )
    calls = []

    def fake_html(_source, url):
        calls.append(url)
        match = fps.re.search(r"(?:[?&])page=([1-9]\d*)", url)
        page = int(match.group(1)) if match else 1
        next_link = (
            f'<a href="{source.endpoint}&page={page + 1}">Next</a>'
            if page < 65 else ""
        )
        return f"<main><p>No results found</p>{next_link}</main>"

    monkeypatch.setattr(fps, "_fetch_html", fake_html)
    payload = fps.fetch_source(source)
    assert fps.records_from_payload(payload, source) == []
    assert len(calls) == 65
    assert payload["requestBatches"] == [
        {"batch": 1, "listingRequests": 64, "detailRequests": 0},
        {"batch": 2, "listingRequests": 1, "detailRequests": 0},
    ]


def test_selectminds_requires_exact_medicine_facet_complete_pages_and_details():
    source = fps.load_first_party_sources()[
        "first-party-stanford-university-school-of-medicine"
    ]
    payload = fixture(source.key)
    records = fps.records_from_payload(payload, source)
    assert source.adapter == fps.SELECTMINDS_ADAPTER
    assert [row["sourceRecordId"] for row in records] == ["30994"]
    assert records[0]["requisitionId"] == "109041"
    assert records[0]["location"].startswith("School of Medicine")

    wrong_facet = deepcopy(payload)
    wrong_facet["listingPages"][0]["html"] = wrong_facet["listingPages"][0][
        "html"
    ].replace('data-location-ids="79"', 'data-location-ids="80"')
    with pytest.raises(fps.FirstPartySourceError, match="exact filtered"):
        fps.records_from_payload(wrong_facet, source)

    partial = deepcopy(payload)
    partial["facetResponse"]["UserMessage"] = "2"
    with pytest.raises(fps.FirstPartySourceError, match="partial"):
        fps.records_from_payload(partial, source)

    wrong_detail = deepcopy(payload)
    wrong_detail["details"][0]["html"] = wrong_detail["details"][0]["html"].replace(
        'id: "79"', 'id: "80"'
    )
    with pytest.raises(fps.FirstPartySourceError, match="Medicine contract"):
        fps.records_from_payload(wrong_detail, source)


def test_selectminds_fetch_allows_only_one_exact_numeric_search_redirect(monkeypatch):
    source = fps.load_first_party_sources()[
        "first-party-stanford-university-school-of-medicine"
    ]
    expected = fixture(source.key)
    calls = []

    class Response:
        def __init__(self, body=b"", *, status=200, location=None):
            self.body = body if isinstance(body, bytes) else body.encode()
            self.status_code = status
            self.headers = {"Location": location} if location else {}
            self.is_redirect = status in {301, 302, 303, 307, 308}
            self.is_permanent_redirect = status in {301, 308}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise fps.requests.HTTPError(response=self)

        def iter_content(self, _size):
            yield self.body

        def close(self):
            return None

    class Session:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if len(calls) == 1:
                return Response(
                    status=307,
                    location="/jobs/search/22500001",
                )
            if len(calls) == 2:
                return Response(
                    '<input id="tsstoken" value="exact-token">'
                    + expected["landingHtml"]
                )
            if "/add/location/79" in url:
                return Response(json.dumps(expected["facetResponse"]))
            if "/ajax/content/job_results" in url:
                return Response(json.dumps({
                    "Status": "OK", "Result": expected["listingPages"][0]["html"],
                }))
            return Response(expected["details"][0]["html"])

    monkeypatch.setattr(fps.requests, "Session", Session)
    payload = fps.fetch_source(source)
    assert len(fps.records_from_payload(payload, source)) == 1
    assert calls[0][2]["headers"]["User-Agent"] == "curl/8.7.1"
    assert calls[0][2]["allow_redirects"] is False
    assert calls[1][1] == "https://stanford.referrals.selectminds.com/jobs/search/22500001"

    class EscapedSession(Session):
        def request(self, method, url, **kwargs):
            return Response(status=307, location="https://evil.example/jobs/search/1")

    monkeypatch.setattr(fps.requests, "Session", EscapedSession)
    with pytest.raises(fps.FirstPartySourceError, match="redirect escaped"):
        fps.fetch_source(source)


def test_icpsr_exact_department_feed_accepts_a_reproducible_zero():
    source = fps.load_first_party_sources()[
        "first-party-inter-university-consortium-for-political-and-social-research"
    ]
    assert source.adapter == fps.DRUPAL_RSS_ADAPTER
    assert fps.records_from_payload(fixture(source.key), source) == []
    wrong_feed = deepcopy(fixture(source.key))
    wrong_feed["feedXml"] = wrong_feed["feedXml"].replace(
        "New Career Opportunities at U-M", "Other feed"
    )
    with pytest.raises(fps.FirstPartySourceError, match="identity"):
        fps.records_from_payload(wrong_feed, source)


def test_cnrs_unit_listing_and_microsoft_research_api_are_complete_and_bounded():
    sources = fps.load_first_party_sources()
    cnrs = sources["first-party-institute-of-scientific-and-technical-information"]
    cnrs_records = fps.records_from_payload(fixture(cnrs.key), cnrs)
    assert cnrs.adapter == fps.CNRS_ADAPTER
    assert [row["sourceRecordId"] for row in cnrs_records] == ["UAR76-FLOLAM-054"]

    microsoft = sources["first-party-microsoft-research"]
    records = fps.records_from_payload(fixture(microsoft.key), microsoft)
    assert microsoft.adapter == fps.MICROSOFT_RESEARCH_ADAPTER
    assert [row["sourceRecordId"] for row in records] == ["1184875"]
    assert records[0]["requisitionId"] == "1970393556957953"
    partial = deepcopy(fixture(microsoft.key))
    partial["listingPages"][0]["foundPosts"] = 2
    with pytest.raises(fps.FirstPartySourceError, match="partial"):
        fps.records_from_payload(partial, microsoft)

    research_only = deepcopy(fixture(microsoft.key))
    item = research_only["listingPages"][0]["posts"][0]
    item["applyUrl"] = None
    item["recordUrl"] = item["permalink"]
    record = fps.records_from_payload(research_only, microsoft)[0]
    assert record["canonicalUrl"] == item["permalink"]
    assert record["requisitionId"] is None


def test_linux_zero_requires_a_successful_opening_diagnostic():
    source = fps.load_first_party_sources()["first-party-linux-foundation"]
    legitimate_zero = {
        "listingPages": [{
            "url": source.endpoint,
            "html": "<main><h1>Open positions</h1><p>No current openings.</p></main>",
        }],
        "details": [],
    }
    assert fps.records_from_payload(legitimate_zero, source) == []
    broken = {
        "listingPages": [{
            "url": source.endpoint,
            "html": '<main><input placeholder="Search in jobs"><h1>Open positions</h1></main>',
        }],
        "details": [],
    }
    with pytest.raises(fps.FirstPartySourceError, match="zero exact job links"):
        fps.records_from_payload(broken, source)


def test_all_viable_sources_run_network_free_without_publishing(tmp_path):
    public_jobs = (REPO_ROOT / "data" / "jobs" / "jobs.json").read_bytes()
    runtime = fixture_runtime(tmp_path)
    run = json.loads((runtime / "run.json").read_text(encoding="utf-8"))
    assert {row["sourceKey"] for row in run["sourceResults"]} == TASK42_SOURCE_KEYS
    assert all(row["status"] == "refreshed" for row in run["sourceResults"])
    assert run["publicationPerformed"] is False
    assert (REPO_ROOT / "data" / "jobs" / "jobs.json").read_bytes() == public_jobs


def test_live_review_inputs_are_preserved_in_a_deterministic_replay_archive(tmp_path):
    runtime = fixture_runtime(tmp_path)
    archive = tmp_path / "task42-inputs.zip"
    manifest_path = tmp_path / "task42-inputs-manifest.json"
    manifest = build_archive(runtime, archive, manifest_path)
    assert manifest["sourceCount"] == len(TASK42_SOURCE_KEYS)
    assert manifest["publicationPerformed"] is False
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive"]["sha256"]
    with zipfile.ZipFile(archive) as replay:
        assert replay.testzip() is None
        assert json.loads(replay.read("manifest.json"))["sourceCount"] == len(TASK42_SOURCE_KEYS)
        contracts = json.loads(replay.read("source-contracts.json"))
        assert {row["key"] for row in contracts["sources"]} == TASK42_SOURCE_KEYS
        for key in TASK42_SOURCE_KEYS:
            normalized = json.loads(replay.read(f"sources/{key}.json"))
            assert isinstance(normalized, list)
            raw_name = next(
                name for name in replay.namelist() if name.startswith(f"raw/{key}.")
            )
            assert hashlib.sha256(replay.read(raw_name)).hexdigest() == (
                manifest["entries"][raw_name]["sha256"]
            )


def test_nightly_nonpublishing_monitor_revisits_every_unresolved_careers_page(tmp_path):
    pages = monitored_pages()
    assert len(pages) == 85 - len(TASK42_SOURCE_KEYS)
    calls = []

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        encoding = "utf-8"

        def __init__(self, url):
            self.url = url

        def iter_content(self, _size):
            yield b'<main><h1>Careers</h1><a href="https://jobs.lever.co/example">View jobs</a></main>'

    def fetcher(url, headers):
        calls.append((url, headers))
        return Response(url)

    output = tmp_path / "monitor.json"
    run = run_monitor(
        output=output,
        retrieved_at="2026-08-28T01:00:00Z",
        fetcher=fetcher,
        max_workers=4,
    )
    assert run["mode"] == "nonpublishing-careers-discovery-monitor"
    assert run["publicationPerformed"] is False
    assert run["scheduleActivated"] is True
    assert run["scheduleCadence"] == "nightly-via-update-jobs"
    assert run["counts"]["pages"] == len(pages) == len(calls)
    assert all(row["change"] == "changed" for row in run["pages"])
    assert all(row["openingMarkers"] for row in run["pages"])
    assert all(row["providerCandidates"] for row in run["pages"])
    assert all(
        call[1]["Range"] == f"bytes=0-{MAX_RESPONSE_BYTES - 1}"
        for call in calls
    )
    assert output.is_file()
    workflow_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    )
    assert "task42_nightly.py" in workflow_text
    assert "jobs/runtime/careers-discovery/run.json" in workflow_text


def test_nightly_operational_contract_fits_jobs_workflow_before_catalog_generation():
    sources = task42_nightly.production_sources()
    batches = task42_nightly.bounded_parallel_batches(sources)
    plan = json.loads(NIGHTLY_PLAN.read_text(encoding="utf-8"))
    jobs_workflow = (
        REPO_ROOT / ".github" / "workflows" / "update-jobs.yml"
    ).read_text(encoding="utf-8")
    catalog_workflow = (
        REPO_ROOT / ".github" / "workflows" / "update-data.yml"
    ).read_text(encoding="utf-8")

    assert len(sources) == plan["fullIngestion"]["productionSourceCount"] == 34
    assert TASK42_SOURCE_KEYS <= set(sources)
    assert len(batches) == plan["fullIngestion"]["batchCount"] == 10
    assert {key for batch in batches for key in batch} == set(sources)
    assert max(map(len, batches)) == plan["fullIngestion"]["maxParallelSources"] == 4
    assert plan["fullIngestion"]["refreshIntervalSeconds"] == 86400
    assert plan["fullIngestion"]["sourceTimeoutSeconds"] == 720
    assert plan["fullIngestion"]["batchRequestCap"] == 128
    assert plan["discoveryMonitor"]["pageCount"] == 68
    assert plan["uncoveredOrganizations"] == 22
    assert plan["targetCron"] == task42_nightly.TARGET_CRON == "0 3 * * *"
    assert 'cron: "0 3 * * *"' in jobs_workflow
    assert 'cron: "0 6 * * *"' in catalog_workflow
    assert "timeout-minutes: 150" in jobs_workflow
    assert plan["workflowOverheadBudgetSeconds"] == 720
    assert plan["worstCaseSeconds"] == task42_nightly.worst_case_seconds(
        source_batches=len(batches)
    ) == 7920
    assert plan["workflowTimeoutSeconds"] - plan["worstCaseSeconds"] == 1080
    assert plan["worstCaseSeconds"] < plan["workflowTimeoutSeconds"] == 9000
    assert plan["activationStatus"] == "production-wiring-complete-pending-final-review"
    assert plan["scheduleConfigured"] is True
    assert plan["productionFlagsChanged"] is True
    assert plan["deploymentTriggered"] is False


def test_nightly_process_batch_hard_stops_a_slow_source(tmp_path):
    outcome = task42_nightly.execute_process_batch(
        ["slow-source"],
        tmp_path,
        source_timeout_seconds=0.05,
        context=multiprocessing.get_context("fork"),
        worker=_slow_nightly_worker,
    )
    assert outcome["slow-source"]["status"] == "timed-out"
    assert outcome["slow-source"]["rawPayload"] is None


def test_nightly_bounds_cannot_be_relaxed_past_the_reviewed_budget(tmp_path):
    sources = task42_nightly.production_sources()
    with pytest.raises(task42_nightly.NightlyRunError, match="between 1 and 4"):
        task42_nightly.bounded_parallel_batches(sources, max_parallel=5)
    with pytest.raises(task42_nightly.NightlyRunError, match="between 1 and 720"):
        task42_nightly.execute_process_batch(
            [], tmp_path, source_timeout_seconds=721
        )


def test_nightly_run_batches_due_sources_and_preserves_failed_source_last_good(tmp_path):
    runtime = fixture_runtime(tmp_path)
    retained_key = "first-party-university-of-maryland"
    retained_source = (runtime / "sources" / f"{retained_key}.json").read_bytes()
    retained_raw = (runtime / "raw" / f"{retained_key}.json").read_bytes()
    executed = []

    def batch_executor(batch, _sources, timeout_seconds):
        executed.append(list(batch))
        assert timeout_seconds == 720
        return {
            key: {
                "error": "simulated isolated timeout" if key == retained_key else None,
                "rawPayload": None if key == retained_key else fixture(key),
                "status": "timed-out" if key == retained_key else "fetched",
            }
            for key in batch
        }

    def monitor_runner(*, output, retrieved_at):
        assert retrieved_at == "2026-08-29T00:00:00Z"
        return {
            "counts": {"pages": 68, "unreachable": 1},
            "publicationPerformed": False,
            "scheduleActivated": True,
        }

    public_jobs = (REPO_ROOT / "data" / "jobs" / "jobs.json").read_bytes()
    result = task42_nightly.run_nightly(
        runtime_dir=runtime,
        retrieved_at="2026-08-29T00:00:00Z",
        selected_source=retained_key,
        batch_executor=batch_executor,
        monitor_runner=monitor_runner,
    )

    assert executed == [[retained_key]]
    by_source = {row["sourceKey"]: row for row in result["sourceResults"]}
    assert result["sourceFailures"] == 1
    assert by_source[retained_key]["status"] == "retained-last-good"
    assert (runtime / "sources" / f"{retained_key}.json").read_bytes() == retained_source
    assert (runtime / "raw" / f"{retained_key}.json").read_bytes() == retained_raw
    assert (runtime / "raw" / "first-party-embl-ebi.json").is_file()
    assert result["runtimePublicationPerformed"] is True
    assert result["repositoryPublicationPerformed"] is False
    assert json.loads((runtime / "nightly-run.json").read_text())["sourceFailures"] == 1
    assert (REPO_ROOT / "data" / "jobs" / "jobs.json").read_bytes() == public_jobs


def test_atomic_public_directory_promotion_keeps_private_raw_out(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "raw").mkdir(parents=True)
    (runtime / "jobs.json").write_text("[]\n", encoding="utf-8")
    (runtime / "jobs.ttl").write_text("@prefix x: <https://example/> .\n", encoding="utf-8")
    (runtime / "run.json").write_text("{}\n", encoding="utf-8")
    (runtime / "raw" / "adzuna.json").write_text("{}\n", encoding="utf-8")
    (runtime / "raw" / "first-party-embl-ebi.json").write_text(
        "{}\n", encoding="utf-8"
    )
    destination = tmp_path / "data" / "jobs"
    destination.mkdir(parents=True)
    (destination / "manifest.json").write_text(
        '{"preserved": true}\n', encoding="utf-8"
    )

    promote_jobs_snapshot.promote(runtime, destination)

    assert (destination / "jobs.json").read_text(encoding="utf-8") == "[]\n"
    assert (destination / "raw" / "adzuna.json").is_file()
    assert not (destination / "raw" / "first-party-embl-ebi.json").exists()
    assert json.loads((destination / "manifest.json").read_text())["preserved"] is True


def test_atomic_public_directory_promotion_rolls_back_a_failed_swap(
    tmp_path, monkeypatch,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "jobs.json").write_text("[]\n", encoding="utf-8")
    (runtime / "jobs.ttl").write_text("@prefix x: <https://example/> .\n", encoding="utf-8")
    (runtime / "run.json").write_text("{}\n", encoding="utf-8")
    destination = tmp_path / "data" / "jobs"
    destination.mkdir(parents=True)
    (destination / "jobs.json").write_text('[{"old": true}]\n', encoding="utf-8")
    old_bytes = (destination / "jobs.json").read_bytes()
    real_replace = promote_jobs_snapshot.os.replace

    def fail_candidate_swap(source, target):
        if Path(source).name.startswith(".jobs-promotion-") and Path(target) == destination:
            raise OSError("simulated candidate swap failure")
        return real_replace(source, target)

    monkeypatch.setattr(promote_jobs_snapshot.os, "replace", fail_candidate_swap)
    with pytest.raises(OSError, match="simulated candidate swap failure"):
        promote_jobs_snapshot.promote(runtime, destination)
    assert (destination / "jobs.json").read_bytes() == old_bytes
    assert not destination.with_name(".jobs-previous").exists()


def test_discovery_monitor_isolates_page_failures_and_records_them(tmp_path):
    class FailingFetcher:
        def __call__(self, _url, _headers):
            raise fps.requests.Timeout("temporary")

    run = run_monitor(
        output=tmp_path / "monitor.json",
        fetcher=FailingFetcher(),
        max_workers=4,
    )
    assert run["counts"]["unreachable"] == run["counts"]["pages"]
    assert all(row["retrievalError"] for row in run["pages"])


def test_audit_requires_successful_complete_runtime_diagnostics(tmp_path):
    runtime = fixture_runtime(tmp_path)
    audit = build_audit(runtime)
    assert all(row["classification"] == "qualified" for row in audit["proposedPublicJobs"])
    assert all(row["classification"] == "review" for row in audit["managerReviewJobs"])
    run_path = runtime / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["sourceResults"][0]["status"] = "retained-after-failure"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    with pytest.raises(AuditError, match="did not refresh successfully"):
        build_audit(runtime)


def test_audit_accepts_a_legitimate_refreshed_zero_but_not_a_silent_mismatch(tmp_path):
    runtime = fixture_runtime(tmp_path)
    run_path = runtime / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    result = next(
        row for row in run["sourceResults"]
        if row["sourceKey"] == "first-party-national-library-of-norway"
    )
    source_path = runtime / "sources" / "first-party-national-library-of-norway.json"
    source_path.write_text("[]\n", encoding="utf-8")
    result.update({"notMatch": 0, "qualified": 0, "records": 0, "review": 0})
    run_path.write_text(json.dumps(run), encoding="utf-8")
    build_audit(runtime)
    result["records"] = 1
    run_path.write_text(json.dumps(run), encoding="utf-8")
    with pytest.raises(AuditError, match="counts disagree"):
        build_audit(runtime)


def test_committed_audit_covers_every_page_and_separates_all_three_classes():
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    rows = audit["organizations"]
    assert len(rows) == 107
    assert {row["id"] for row in rows} == {
        row["identifier"] for row in fixed_cohort()
    }
    career_rows = [row for row in rows if row["careersPageStatus"] == "recorded-official"]
    assert len(career_rows) == 85
    assert all(row["discoveryEvidence"] for row in career_rows)
    assert all(row["exactEndpoint"] for row in career_rows)
    assert all(row["provider"] for row in career_rows)
    assert all(row["sourceDispositionReason"] for row in career_rows)
    assert {row["sourceDisposition"] for row in career_rows} == {"pipeline-ready", "blocked"}
    assert sum(row["sourceDisposition"] == "pipeline-ready" for row in career_rows) == 17
    assert sum(row["sourceDisposition"] == "blocked" for row in career_rows) == 68
    assert len(audit["fullJobIngestionPipelines"]) == 17
    assert len(audit["externallyBlockedSources"]) == 68
    assert {
        row["id"] for row in audit["fullJobIngestionPipelines"]
    } | {
        row["id"] for row in audit["externallyBlockedSources"]
    } == {row["id"] for row in career_rows}
    assert all(
        row["reason"]
        and "adapter not implemented" not in row["reason"].casefold()
        and "provider undetected" not in row["reason"].casefold()
        for row in audit["externallyBlockedSources"]
    )
    assert all(
        "adapter not implemented" not in row["sourceDispositionReason"].casefold()
        and "provider undetected" not in row["sourceDispositionReason"].casefold()
        for row in career_rows
    )
    assert all(row["classification"] == "qualified" for row in audit["proposedPublicJobs"])
    assert all(row["classification"] == "review" for row in audit["managerReviewJobs"])
    assert all(row["classification"] == "not_match" for row in audit["notMatchJobs"])
    assert audit["productionFlagsModified"] is True
    assert audit["publicSnapshotModified"] is False
    assert audit["scheduleModified"] is True
    approval = json.loads(APPROVAL.read_text(encoding="utf-8"))
    assert [row["id"] for row in approval["approvedQualifiedPostings"]] == [
        "firstparty:first-party-embl-ebi:JR4207",
        "firstparty:first-party-university-of-maryland:JR104812",
    ]
    assert [row["id"] for row in approval["heldReviewPostings"]] == [
        "firstparty:first-party-embl-ebi:JR3997"
    ]
    assert audit["operationalDesign"] == json.loads(
        NIGHTLY_PLAN.read_text(encoding="utf-8")
    )
