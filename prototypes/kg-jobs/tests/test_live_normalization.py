"""Network-free normalization, HTML safety, dedupe, and classification tests."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from classifier import load_match_terms  # noqa: E402
from arbeitnow_adapter import records_from_payload as arbeitnow_records  # noqa: E402
from himalayas_adapter import records_from_payload as himalayas_records  # noqa: E402
from live_records import classify_records, deduplicate  # noqa: E402
from live_sources import LivePipelineError, load_source_registry  # noqa: E402
from remotive_adapter import records_from_payload  # noqa: E402
from jooble_adapter import (  # noqa: E402
    is_desc_link,
    job_age_days,
    records_from_payload as jooble_records,
)
from adzuna_adapter import records_from_payload as adzuna_records  # noqa: E402
from live_sources import ADZUNA_PAGE_SIZE  # noqa: E402

import pytest

FIXTURE = ROOT / "tests" / "fixtures" / "remotive.json"
NOW = "2026-08-17T18:00:00Z"


def _records():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    source = load_source_registry(ROOT / "sources.ttl")["remotive"]
    normalized, fetched, complete = records_from_payload(payload, source, NOW)
    assert complete is True
    return normalized, fetched


def test_remotive_html_is_plain_text_and_urls_are_canonicalized():
    records, fetched = _records()
    assert fetched == 4
    first = next(record for record in records if record["sourceRecordId"] == "7001")
    assert "<" not in first["description"]
    assert first["description"] == "Build a knowledge graph using RDF and SPARQL."
    assert "utm_source" not in first["canonicalUrl"]
    assert first["id"] == "remotive-7001"
    assert first["sourceName"] == "Remotive"
    assert first["sourceAttributionUrl"] == "https://remotive.com/"


def test_arbeitnow_normalizes_salary_dates_lists_and_https_urls():
    payload = json.loads(
        (ROOT / "tests" / "fixtures" / "arbeitnow-page-1.json").read_text()
    )
    source = load_source_registry(ROOT / "sources.ttl")["arbeitnow"]
    records, fetched, complete = arbeitnow_records(payload, source, NOW)
    assert fetched == 3
    assert complete is False
    first = records[0]
    assert first["id"] == "arbeitnow-mock-knowledge-graph-engineer-8001"
    assert first["description"] == "Build a knowledge graph using RDF and SPARQL."
    assert first["canonicalUrl"] == (
        "https://www.arbeitnow.com/view/mock-knowledge-graph-engineer-8001"
    )
    assert first["salary"] == "EUR 90,000–110,000"
    assert first["employmentType"] == "Full-time"
    assert first["remote"] is True
    invalid_date = next(record for record in records if record["sourceRecordId"].endswith("8003"))
    assert "datePosted" not in invalid_date

    hostile = json.loads(json.dumps(payload))
    hostile["data"][0]["url"] = "http://example.org/insecure-job"
    with pytest.raises(LivePipelineError, match="failed normalization"):
        arbeitnow_records(hostile, source, NOW)

    employer_hosted = json.loads(json.dumps(payload))
    employer_hosted["data"][0]["url"] = "https://example.org/jobs/kg-engineer"
    external_records, _, _ = arbeitnow_records(employer_hosted, source, NOW)
    assert external_records[0]["canonicalUrl"] == "https://example.org/jobs/kg-engineer"


def test_himalayas_normalizes_official_search_results_with_attribution():
    payload = json.loads(
        (ROOT / "tests" / "fixtures" / "himalayas.json").read_text()
    )
    source = load_source_registry(ROOT / "sources.ttl")["himalayas"]
    records, fetched, complete = himalayas_records(
        payload, source, NOW, "ontology"
    )
    assert fetched == 2
    assert complete is True
    first = records[0]
    assert first["id"].startswith("himalayas-")
    assert first["description"] == (
        "Design production ontologies and build knowledge graphs with RDF, OWL, and SPARQL."
    )
    assert first["canonicalUrl"] == (
        "https://himalayas.app/companies/northstar-research/jobs/ontology-engineer"
    )
    assert first["sourceUrl"] == first["canonicalUrl"]
    assert first["sourceName"] == "Himalayas"
    assert first["sourceAttributionUrl"] == "https://himalayas.app/"
    assert first["discoveredBy"] == ["ontology"]
    assert first["location"] == "Americas, Europe"
    assert first["applicantLocationRequirements"] == ["Americas", "Europe"]
    assert first["datePosted"] == "2026-08-17"
    assert first["validThrough"] == "2026-09-16"
    assert first["employmentType"] == "Full-time"
    assert first["seniority"] == "Senior, Staff"
    assert first["salary"] == "USD 120,000–150,000 / year"
    assert first["baseSalary"] == {
        "currency": "USD",
        "minValue": 120000,
        "maxValue": 150000,
        "unitText": "year",
    }
    assert first["remote"] is True

    hostile = json.loads(json.dumps(payload))
    hostile["jobs"][0]["applicationLink"] = "https://example.org/jobs/ontology-engineer"
    with pytest.raises(LivePipelineError, match="failed normalization"):
        himalayas_records(hostile, source, NOW, "ontology")


def test_himalayas_rejects_malformed_search_metadata():
    source = load_source_registry(ROOT / "sources.ttl")["himalayas"]
    payload = json.loads(
        (ROOT / "tests" / "fixtures" / "himalayas.json").read_text()
    )
    payload["limit"] = 21
    with pytest.raises(LivePipelineError, match="invalid limit"):
        himalayas_records(payload, source, NOW, "ontology")


def test_arbeitnow_rejects_malformed_payload_and_record():
    source = load_source_registry(ROOT / "sources.ttl")["arbeitnow"]
    with pytest.raises(LivePipelineError, match="data, meta, and links"):
        arbeitnow_records({"data": []}, source, NOW)
    with pytest.raises(LivePipelineError, match="metadata"):
        arbeitnow_records(
            {
                "data": [],
                "meta": {"current_page": 2, "last_page": 1},
                "links": {"next": None},
            },
            source,
            NOW,
        )
    with pytest.raises(LivePipelineError, match="failed normalization"):
        arbeitnow_records(
            {
                "data": [{"title": "Missing all required fields"}],
                "meta": {"current_page": 1, "last_page": 1, "total": 1},
                "links": {"next": None},
            },
            source,
            NOW,
        )


def test_arbeitnow_accepts_live_pagination_shape_without_total_or_last_page():
    source = load_source_registry(ROOT / "sources.ttl")["arbeitnow"]
    payload = json.loads(
        (ROOT / "tests" / "fixtures" / "arbeitnow-page-1.json").read_text()
    )
    payload["meta"].pop("last_page")
    payload["meta"].pop("total")
    records, fetched, complete = arbeitnow_records(payload, source, NOW)
    assert len(records) == fetched == 3
    assert complete is False


def test_dedup_is_deterministic_and_classification_uses_rdf_vocab():
    records, _ = _records()
    forward = deduplicate(records)
    reverse = deduplicate(list(reversed(records)))
    assert forward == reverse
    assert len(forward) == 3

    terms = load_match_terms(ROOT / "vocabularies" / "kg-jobs.ttl")
    classified = classify_records(forward, terms)
    by_source_id = {record["sourceRecordId"]: record for record in classified}
    assert by_source_id["7001"]["classification"] == "qualified"
    assert by_source_id["7002"]["classification"] == "review"
    assert by_source_id["7003"]["classification"] == "not_match"
    assert by_source_id["7001"]["evidence"]
    assert all("score" not in item for item in by_source_id["7001"]["evidence"])


def test_stable_arbeitnow_identities_prevent_visible_field_fallback_collisions():
    source = load_source_registry(ROOT / "sources.ttl")["arbeitnow"]
    page_one = json.loads(
        (ROOT / "tests" / "fixtures" / "arbeitnow-page-1.json").read_text()
    )
    page_two = json.loads(
        (ROOT / "tests" / "fixtures" / "arbeitnow-page-2.json").read_text()
    )
    first, _, _ = arbeitnow_records(page_one, source, NOW)
    second, _, _ = arbeitnow_records(page_two, source, NOW)
    forward = deduplicate(first + second)
    reverse = deduplicate(list(reversed(first + second)))
    assert forward == reverse
    assert len(forward) == 5


def test_distinct_stable_urls_are_not_collapsed_by_visible_field_fallback():
    records, _ = _records()
    original = next(record for record in records if record["sourceRecordId"] == "7001")
    distinct = dict(original)
    original = dict(original)
    original["sourceRecordId"] = ""
    original["canonicalFingerprint"] = ""
    distinct.update(
        {
            "id": "remotive-7999",
            "sourceRecordId": "",
            "canonicalUrl": "https://remotive.com/remote-jobs/data/distinct-vacancy-7999",
            "sourceUrl": "https://remotive.com/remote-jobs/data/distinct-vacancy-7999",
            "canonicalFingerprint": "",
        }
    )
    assert len(deduplicate([original, distinct])) == 2


def test_visible_field_fallback_only_collapses_records_without_stable_identity():
    records, _ = _records()
    original = next(record for record in records if record["sourceRecordId"] == "7001")
    first = dict(original)
    second = dict(original)
    for record, suffix, query in (
        (first, "a", "ontology"),
        (second, "b", "knowledge graph"),
    ):
        record.update(
            {
                "id": f"fallback-{suffix}",
                "sourceRecordId": "",
                "canonicalUrl": "",
                "sourceUrl": "",
                "canonicalFingerprint": "",
                "discoveredBy": [query],
            }
        )
    result = deduplicate([first, second])
    reversed_result = deduplicate([second, first])
    assert result == reversed_result
    assert len(result) == 1
    assert result[0]["discoveredBy"] == ["knowledge graph", "ontology"]


def test_job_age_days_parses_the_tracking_link_and_defaults_to_none():
    assert job_age_days("https://jooble.org/away/123?p=1&jobAge=206&rgn=-1") == 206
    assert job_age_days("https://jooble.org/away/123?jobAge=0") == 0
    assert job_age_days("https://jooble.org/away/123?p=1&rgn=-1") is None
    assert job_age_days("") is None
    assert job_age_days(None) is None


def test_jooble_filters_postings_older_than_the_registry_cutoff():
    source = load_source_registry(ROOT / "sources.ttl")["jooble"]
    assert source.max_posting_age_days == 45
    payload = {
        "totalCount": 2,
        "jobs": [
            {
                "id": 1,
                "title": "Fresh Ontology Engineer",
                "company": "Fresh Co",
                "snippet": "Recently posted role.",
                "link": "https://jooble.org/desc/1?p=1&jobAge=10&rgn=-1",
                "updated": "2026-08-17T00:00:00.0000000",
            },
            {
                "id": 2,
                "title": "Stale Ontology Engineer",
                "company": "Stale Co",
                # "updated" looks recent even though the true age (jobAge)
                # is 206 days -- this is the real, verified discrepancy
                # that motivated the filter, reproduced here directly.
                "snippet": "This looks recent but is not.",
                "link": "https://jooble.org/desc/2?p=1&jobAge=206&rgn=-1",
                "updated": "2026-08-10T00:00:00.0000000",
            },
        ],
    }
    records, fetched, complete = jooble_records(payload, source, NOW, "ontology")
    assert fetched == 2  # both counted as fetched -- filtering is not a fetch failure
    assert [record["title"] for record in records] == ["Fresh Ontology Engineer"]


def test_is_desc_link_recognizes_jooble_hosted_pages():
    assert is_desc_link("https://jooble.org/desc/123?p=1&jobAge=10") is True
    assert is_desc_link("https://jooble.org/away/123?p=1&jobAge=10") is False
    assert is_desc_link("") is False
    assert is_desc_link(None) is False


def test_jooble_drops_away_links_keeping_only_desc_links():
    source = load_source_registry(ROOT / "sources.ttl")["jooble"]
    payload = {
        "totalCount": 2,
        "jobs": [
            {
                "id": 1,
                "title": "Redirected Ontology Engineer",
                "company": "Redirect Co",
                "snippet": "Reached only via an outbound tracking redirect.",
                # "/away/" links redirect off Jooble to wherever it scraped
                # the listing from -- verified live to be frequently dead
                # even at a low jobAge, so these are dropped regardless of
                # freshness.
                "link": "https://jooble.org/away/1?p=1&jobAge=5&rgn=-1",
                "updated": "2026-08-17T00:00:00.0000000",
            },
            {
                "id": 2,
                "title": "Hosted Ontology Engineer",
                "company": "Hosted Co",
                "snippet": "Jooble hosts this description directly.",
                "link": "https://jooble.org/desc/2?p=1&jobAge=5&rgn=-1",
                "updated": "2026-08-17T00:00:00.0000000",
            },
        ],
    }
    records, fetched, complete = jooble_records(payload, source, NOW, "ontology")
    assert fetched == 2  # both counted as fetched -- filtering is not a fetch failure
    assert [record["title"] for record in records] == ["Hosted Ontology Engineer"]


def _adzuna_job(**overrides):
    job = {
        "id": "50001",
        "title": "Ontology Engineer",
        "company": {"display_name": "Northstar Research"},
        "description": "Design production ontologies and build knowledge graphs.",
        "redirect_url": "https://www.adzuna.com/land/ad/50001?se=abc123",
        "location": {"display_name": "Austin, TX"},
        "created": "2026-08-15T09:00:00Z",
        "salary_min": 120000,
        "salary_max": 150000,
        "contract_type": "permanent",
        "contract_time": "full_time",
        "category": {"label": "IT Jobs", "tag": "it-jobs"},
    }
    job.update(overrides)
    return job


def test_adzuna_normalizes_a_search_result_with_off_host_redirect_and_salary():
    source = load_source_registry(ROOT / "sources.ttl")["adzuna"]
    payload = {"count": 1, "results": [_adzuna_job()]}
    records, fetched, complete = adzuna_records(payload, source, NOW, "ontology")
    assert fetched == 1
    assert complete is True
    first = records[0]
    assert first["id"] == "adzuna-50001"
    assert first["sourceRecordId"] == "50001"
    # canonicalize_url strips the "se" tracking parameter (not a utm_/ref
    # param) -- confirms it is NOT restricted to source.allowed_host
    # (api.adzuna.com), since redirect_url is on a different adzuna.com
    # subdomain than the registered API endpoint.
    assert first["canonicalUrl"] == "https://www.adzuna.com/land/ad/50001?se=abc123"
    assert first["hiringOrganization"] == "Northstar Research"
    assert first["location"] == "Austin, TX"
    assert first["datePosted"] == "2026-08-15"
    assert first["employmentType"] == "Permanent, Full Time"
    assert first["tags"] == ["IT Jobs"]
    assert first["salary"] == "USD 120,000–150,000"
    assert first["baseSalary"] == {
        "currency": "USD",
        "minValue": 120000,
        "maxValue": 150000,
    }
    assert first["sourceName"] == "Adzuna"
    assert first["sourceAttributionUrl"] == "https://www.adzuna.co.uk/"


def test_adzuna_rejects_malformed_payload_and_record():
    source = load_source_registry(ROOT / "sources.ttl")["adzuna"]
    with pytest.raises(LivePipelineError, match="results array"):
        adzuna_records({"count": 0}, source, NOW, "ontology")
    with pytest.raises(LivePipelineError, match="count metadata"):
        adzuna_records({"results": []}, source, NOW, "ontology")
    with pytest.raises(LivePipelineError, match="failed normalization"):
        adzuna_records(
            {"count": 1, "results": [{"title": "Missing everything else"}]},
            source,
            NOW,
            "ontology",
        )


def test_adzuna_completeness_uses_the_fixed_page_size():
    source = load_source_registry(ROOT / "sources.ttl")["adzuna"]
    full_page = [_adzuna_job(id=str(index)) for index in range(ADZUNA_PAGE_SIZE)]
    records, fetched, complete = adzuna_records(
        {"count": 500, "results": full_page}, source, NOW, "ontology"
    )
    assert fetched == ADZUNA_PAGE_SIZE
    assert complete is False

    short_page = full_page[:5]
    records, fetched, complete = adzuna_records(
        {"count": 5, "results": short_page}, source, NOW, "ontology"
    )
    assert fetched == 5
    assert complete is True
