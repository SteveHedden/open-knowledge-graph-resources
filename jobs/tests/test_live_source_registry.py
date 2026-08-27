"""Network-free tests for the RDF source registry and HTTP bounds."""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))

from live_sources import (  # noqa: E402
    LivePipelineError,
    build_feed_url,
    fetch_json_http,
    load_source_registry,
)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers=None, chunks=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks or [body]
        self.closed = False

    def iter_content(self, chunk_size=65536):
        yield from self._chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


def test_remotive_source_is_fully_registry_driven():
    source = load_source_registry(REPO_ROOT / "sources.ttl")["remotive"]
    assert source.adapter == "remotive"
    assert source.endpoint == "https://remotive.com/api/remote-jobs"
    assert source.allowed_host == "remotive.com"
    assert source.source_query == "knowledge graph"
    assert source.min_refresh_interval_seconds == 21600
    assert source.timeout_seconds == 20
    assert source.max_response_bytes == 5_000_000
    assert source.max_records_per_run == 100
    assert source.max_requests_per_run == 1
    assert source.attribution_text == "Remotive"
    assert source.attribution_url == "https://remotive.com/"
    assert source.terms_url == "https://remotive.com/remote-jobs/api"


def test_himalayas_is_default_ready_with_four_bounded_rdf_queries():
    source = load_source_registry(REPO_ROOT / "sources.ttl")["himalayas"]
    assert source.adapter == "himalayas"
    assert source.endpoint == "https://himalayas.app/jobs/api/search"
    assert source.allowed_host == "himalayas.app"
    assert source.source_queries == (
        "knowledge graph",
        "ontology",
        "semantic web",
        "SPARQL",
    )
    assert [family.uri.rsplit("/", 1)[-1] for family in source.query_families] == [
        "himalayas-query-knowledge-graph",
        "himalayas-query-ontology",
        "himalayas-query-semantic-web",
        "himalayas-query-sparql",
    ]
    assert [family.order for family in source.query_families] == [1, 2, 3, 4]
    assert source.query_families[0].concept_uris == (
        "https://openknowledgegraphs.com/jobs/vocab/skill-knowledge-graph",
    )
    assert source.query_families[1].concept_uris == (
        "https://openknowledgegraphs.com/jobs/vocab/role-ontologist",
        "https://openknowledgegraphs.com/jobs/vocab/role-ontology-engineer",
    )
    assert source.min_refresh_interval_seconds == 86400
    assert source.max_records_per_run == 80
    assert source.max_requests_per_run == 4
    assert source.attribution_text == "Himalayas"
    assert source.attribution_url == "https://himalayas.app/"
    assert source.terms_url == "https://himalayas.app/docs/remote-jobs-api"

    graph = Graph()
    graph.parse(REPO_ROOT / "sources.ttl", format="turtle")
    kgjobs = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
    assert not any(
        isinstance(value, Literal)
        for value in graph.objects(URIRef(source.dataset_uri), kgjobs.sourceQuery)
    )
    assert {
        concept
        for family in source.query_families
        for concept in family.concept_uris
    } == {
        "https://openknowledgegraphs.com/jobs/vocab/role-ontologist",
        "https://openknowledgegraphs.com/jobs/vocab/role-ontology-engineer",
        "https://openknowledgegraphs.com/jobs/vocab/skill-knowledge-graph",
        "https://openknowledgegraphs.com/jobs/vocab/skill-semantic-web",
        "https://openknowledgegraphs.com/jobs/vocab/skill-sparql",
    }

    for request_number, query in enumerate(source.source_queries, start=1):
        parsed = urlparse(build_feed_url(source, request_number))
        assert parsed.scheme == "https"
        assert parsed.hostname == "himalayas.app"
        assert parse_qs(parsed.query) == {
            "q": [query],
            "limit": ["20"],
            "offset": ["0"],
        }
    with pytest.raises(LivePipelineError, match="request limit"):
        build_feed_url(source, 5)


def test_search_enabled_requires_an_actual_xsd_boolean(tmp_path):
    text = (REPO_ROOT / "sources.ttl").read_text(encoding="utf-8")
    invalid = text.replace(
        "kgjobs:searchEnabled true", 'kgjobs:searchEnabled "true"', 1
    )
    registry = tmp_path / "sources.ttl"
    registry.write_text(invalid, encoding="utf-8")
    with pytest.raises(LivePipelineError, match="xsd:boolean"):
        load_source_registry(registry)


def test_arbeitnow_source_remains_an_optional_bounded_registry_feed():
    source = load_source_registry(REPO_ROOT / "sources.ttl")["arbeitnow"]
    assert source.adapter == "arbeitnow"
    assert source.endpoint == "https://www.arbeitnow.com/api/job-board-api"
    assert source.allowed_host == "www.arbeitnow.com"
    assert source.min_refresh_interval_seconds == 3600
    assert source.timeout_seconds == 20
    assert source.max_response_bytes == 5_000_000
    assert source.max_records_per_run == 300
    assert source.max_requests_per_run == 3
    assert source.attribution_text == "Arbeitnow"
    assert source.attribution_url == "https://www.arbeitnow.com/"
    assert source.terms_url == "https://www.arbeitnow.com/terms"
    assert parse_qs(urlparse(build_feed_url(source, 1)).query) == {"page": ["1"]}
    assert parse_qs(urlparse(build_feed_url(source, 3)).query) == {"page": ["3"]}
    with pytest.raises(LivePipelineError, match="request limit"):
        build_feed_url(source, 4)


def test_remotive_feed_url_is_one_bounded_registry_query():
    source = load_source_registry(REPO_ROOT / "sources.ttl")["remotive"]
    parsed = urlparse(build_feed_url(source))
    assert parsed.scheme == "https"
    assert parsed.hostname == source.allowed_host
    assert parse_qs(parsed.query) == {"search": ["knowledge graph"], "limit": ["100"]}


def test_bounded_http_sets_safety_controls(monkeypatch):
    source = load_source_registry(REPO_ROOT / "sources.ttl")["remotive"]
    payload = {"jobs": []}
    response = FakeResponse(json.dumps(payload).encode(), headers={"Content-Length": "12"})
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return response

    monkeypatch.setattr("live_sources.requests.get", fake_get)
    assert fetch_json_http(build_feed_url(source), source) == payload
    assert seen["timeout"] == 20
    assert seen["stream"] is True
    assert seen["allow_redirects"] is False
    assert seen["headers"]["Accept"] == "application/json"
    assert response.closed is True


def test_bounded_http_rejects_declared_or_streamed_oversize(monkeypatch):
    source = load_source_registry(REPO_ROOT / "sources.ttl")["remotive"]
    declared = FakeResponse(b"{}", headers={"Content-Length": str(source.max_response_bytes + 1)})
    monkeypatch.setattr("live_sources.requests.get", lambda *args, **kwargs: declared)
    with pytest.raises(LivePipelineError, match="exceeds"):
        fetch_json_http(build_feed_url(source), source)

    streamed = FakeResponse(
        b"", chunks=[b"x" * source.max_response_bytes, b"x"], headers={}
    )
    monkeypatch.setattr("live_sources.requests.get", lambda *args, **kwargs: streamed)
    with pytest.raises(LivePipelineError, match="exceeds"):
        fetch_json_http(build_feed_url(source), source)


def test_bounded_http_rejects_redirects(monkeypatch):
    source = load_source_registry(REPO_ROOT / "sources.ttl")["remotive"]
    redirect = FakeResponse(b"", status=302, headers={"Location": "https://example.com"})
    monkeypatch.setattr("live_sources.requests.get", lambda *args, **kwargs: redirect)
    with pytest.raises(LivePipelineError, match="redirect"):
        fetch_json_http(build_feed_url(source), source)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_bounded_http_reports_http_failures(monkeypatch, status):
    source = load_source_registry(REPO_ROOT / "sources.ttl")["arbeitnow"]
    response = FakeResponse(b"{}", status=status)
    monkeypatch.setattr("live_sources.requests.get", lambda *args, **kwargs: response)
    with pytest.raises(LivePipelineError, match="HTTP error"):
        fetch_json_http(build_feed_url(source), source)
    assert response.closed is True


def test_bounded_http_reports_timeout(monkeypatch):
    source = load_source_registry(REPO_ROOT / "sources.ttl")["arbeitnow"]

    def timeout(*args, **kwargs):
        raise requests.Timeout("mock timeout")

    monkeypatch.setattr("live_sources.requests.get", timeout)
    with pytest.raises(LivePipelineError, match="request to arbeitnow failed"):
        fetch_json_http(build_feed_url(source), source)


@pytest.mark.parametrize("body", [b"{not-json", b"[]"])
def test_bounded_http_rejects_malformed_json_shapes(monkeypatch, body):
    source = load_source_registry(REPO_ROOT / "sources.ttl")["arbeitnow"]
    response = FakeResponse(body)
    monkeypatch.setattr("live_sources.requests.get", lambda *args, **kwargs: response)
    with pytest.raises(LivePipelineError, match="invalid UTF-8 JSON|JSON object"):
        fetch_json_http(build_feed_url(source), source)
