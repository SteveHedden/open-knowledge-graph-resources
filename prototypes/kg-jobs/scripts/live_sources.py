"""Registry-driven source loading and bounded HTTP for local live jobs.

Nothing in this module enables network access by itself. The command-line
orchestrator requires an explicit ``--live`` flag before calling it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from rdflib import Graph, Literal, Namespace, RDF, URIRef
from rdflib.namespace import XSD

KGJOBS = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/ontology#")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCTERMS = Namespace("http://purl.org/dc/terms/")

USER_AGENT = "OKG-KG-Jobs/1.0 (+https://openknowledgegraphs.com/)"

# Read only at request time, from the environment -- never stored in
# sources.ttl, never logged, never included in any raised error message.
JOOBLE_API_KEY_ENV = "JOOBLE_API_KEY"
ADZUNA_APP_ID_ENV = "ADZUNA_APP_ID"
ADZUNA_APP_KEY_ENV = "ADZUNA_APP_KEY"

# Adzuna's results_per_page is capped at 50 by the API itself; build_feed_url
# requests exactly this many per query family, so adzuna_adapter.py also
# uses it as its only reliable per-page completeness signal.
ADZUNA_PAGE_SIZE = 50


class LivePipelineError(RuntimeError):
    """A safe, user-facing live-ingestion failure."""


class RefreshNotDueError(LivePipelineError):
    """A source's registry-declared refresh interval has not elapsed yet.

    Distinct from other LivePipelineError cases so callers (the scheduled
    workflow) can treat this one as "nothing to do yet" rather than a
    failure -- see live_pipeline.enforce_refresh_interval.
    """


@dataclass(frozen=True)
class QueryFamilyConfig:
    """One stable RDF query-family definition for candidate discovery."""

    uri: str
    text: str
    concept_uris: tuple[str, ...]
    order: int


@dataclass(frozen=True)
class SourceConfig:
    key: str
    dataset_uri: str
    name: str
    endpoint: str
    adapter: str
    query_families: tuple[QueryFamilyConfig, ...]
    allowed_host: str
    attribution_text: str
    attribution_url: str
    terms_url: str
    min_refresh_interval_seconds: float
    timeout_seconds: int
    max_response_bytes: int
    max_records_per_run: int
    max_requests_per_run: int
    max_posting_age_days: int | None = None

    @property
    def source_queries(self) -> tuple[str, ...]:
        """Ordered query text, derived from the RDF query-family resources."""
        return tuple(family.text for family in self.query_families)

    @property
    def source_query(self) -> str:
        """Compatibility accessor for adapters with one registry query."""
        return self.source_queries[0]


def _one(graph: Graph, subject, predicate, label: str):
    values = list(graph.objects(subject, predicate))
    if len(values) != 1:
        raise LivePipelineError(
            f"source registry requires exactly one {label} for {subject}; found {len(values)}"
        )
    return values[0]


def _positive_int(value, label: str) -> int:
    try:
        parsed = int(value.toPython())
    except (TypeError, ValueError, AttributeError) as exc:
        raise LivePipelineError(f"source registry {label} must be an integer") from exc
    if parsed <= 0:
        raise LivePipelineError(f"source registry {label} must be positive")
    return parsed


def _optional_positive_int(value, label: str) -> int | None:
    """Like _positive_int, but the registry property may simply be absent."""
    if value is None:
        return None
    return _positive_int(value, label)


def load_source_registry(path: Path) -> dict[str, SourceConfig]:
    graph = Graph()
    graph.parse(path, format="turtle")
    sources: dict[str, SourceConfig] = {}

    for dataset in set(graph.subjects(KGJOBS.searchEnabled, None)):
        enabled = _one(graph, dataset, KGJOBS.searchEnabled, "kgjobs:searchEnabled")
        if (
            not isinstance(enabled, Literal)
            or enabled.datatype != XSD.boolean
            or not isinstance(enabled.toPython(), bool)
        ):
            raise LivePipelineError("kgjobs:searchEnabled must be an xsd:boolean")
        if not enabled.toPython():
            continue
        if (dataset, RDF.type, DCAT.Dataset) not in graph:
            raise LivePipelineError(f"search-enabled source is not a dcat:Dataset: {dataset}")

        key = str(_one(graph, dataset, DCTERMS.identifier, "dcterms:identifier"))
        distribution = _one(graph, dataset, DCAT.distribution, "dcat:distribution")
        endpoint = str(_one(graph, distribution, DCAT.accessURL, "dcat:accessURL"))
        allowed_host = str(_one(graph, dataset, KGJOBS.allowedHost, "kgjobs:allowedHost"))
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or parsed.hostname != allowed_host or parsed.username or parsed.password:
            raise LivePipelineError(
                f"source {key} endpoint must be HTTPS on its exact allowed host {allowed_host!r}"
            )
        if parsed.query or parsed.fragment:
            raise LivePipelineError(f"source {key} endpoint must not contain a query or fragment")

        for predicate, label in (
            (KGJOBS.attributionURL, "attribution URL"),
            (KGJOBS.termsURL, "terms URL"),
        ):
            registry_url = urlparse(str(_one(graph, dataset, predicate, label)))
            if (
                registry_url.scheme != "https"
                or not registry_url.hostname
                or registry_url.username
                or registry_url.password
            ):
                raise LivePipelineError(f"source {key} {label} must be an absolute HTTPS URL")

        interval_value = _one(
            graph, dataset, KGJOBS.minRefreshIntervalSeconds,
            "kgjobs:minRefreshIntervalSeconds",
        )
        try:
            interval = float(Decimal(str(interval_value)))
        except (ValueError, TypeError) as exc:
            raise LivePipelineError("minimum request interval must be numeric") from exc
        if interval < 0:
            raise LivePipelineError("minimum request interval cannot be negative")

        query_nodes = set(graph.objects(dataset, KGJOBS.sourceQuery))
        if not query_nodes:
            raise LivePipelineError(f"source {key} requires at least one kgjobs:sourceQuery")
        query_families = []
        for query_node in query_nodes:
            if not isinstance(query_node, URIRef):
                raise LivePipelineError(
                    f"source {key} kgjobs:sourceQuery must identify an RDF query family"
                )
            if (query_node, RDF.type, KGJOBS.QueryFamily) not in graph:
                raise LivePipelineError(
                    f"source {key} query family is not a kgjobs:QueryFamily: {query_node}"
                )
            text_value = _one(graph, query_node, KGJOBS.queryText, "kgjobs:queryText")
            if not isinstance(text_value, Literal) or text_value.language:
                raise LivePipelineError(
                    f"source {key} query family {query_node} requires literal query text"
                )
            query_text = str(text_value).strip()
            if not query_text:
                raise LivePipelineError(
                    f"source {key} query family {query_node} has empty query text"
                )
            query_order = _positive_int(
                _one(graph, query_node, KGJOBS.queryOrder, "kgjobs:queryOrder"),
                "query order",
            )
            concept_uris = tuple(
                sorted(
                    str(value)
                    for value in graph.objects(query_node, DCTERMS.subject)
                    if isinstance(value, URIRef)
                )
            )
            if len(concept_uris) != len(set(graph.objects(query_node, DCTERMS.subject))):
                raise LivePipelineError(
                    f"source {key} query family {query_node} subjects must be IRIs"
                )
            query_families.append(
                QueryFamilyConfig(
                    uri=str(query_node),
                    text=query_text,
                    concept_uris=concept_uris,
                    order=query_order,
                )
            )
        query_families = tuple(
            sorted(query_families, key=lambda family: (family.order, family.uri))
        )
        orders = tuple(family.order for family in query_families)
        if orders != tuple(range(1, len(query_families) + 1)):
            raise LivePipelineError(
                f"source {key} query-family order must be unique and contiguous from 1"
            )

        config = SourceConfig(
            key=key,
            dataset_uri=str(dataset),
            name=str(_one(graph, dataset, DCTERMS.title, "dcterms:title")),
            endpoint=endpoint,
            adapter=str(_one(graph, dataset, KGJOBS.adapter, "kgjobs:adapter")),
            query_families=query_families,
            allowed_host=allowed_host,
            attribution_text=str(
                _one(graph, dataset, KGJOBS.attributionText, "kgjobs:attributionText")
            ),
            attribution_url=str(
                _one(graph, dataset, KGJOBS.attributionURL, "kgjobs:attributionURL")
            ),
            terms_url=str(_one(graph, dataset, KGJOBS.termsURL, "kgjobs:termsURL")),
            min_refresh_interval_seconds=interval,
            timeout_seconds=_positive_int(
                _one(graph, dataset, KGJOBS.requestTimeoutSeconds, "request timeout"),
                "request timeout",
            ),
            max_response_bytes=_positive_int(
                _one(graph, dataset, KGJOBS.maxResponseBytes, "maximum response bytes"),
                "maximum response bytes",
            ),
            max_records_per_run=_positive_int(
                _one(graph, dataset, KGJOBS.maxRecordsPerRun, "maximum records per run"),
                "maximum records per run",
            ),
            max_requests_per_run=_positive_int(
                _one(graph, dataset, KGJOBS.maxRequestsPerRun, "maximum requests per run"),
                "maximum requests per run",
            ),
            max_posting_age_days=_optional_positive_int(
                graph.value(dataset, KGJOBS.maxPostingAgeDays), "maximum posting age in days"
            ),
        )
        if config.adapter in {"himalayas", "jobicy", "jooble", "adzuna"}:
            if len(config.source_queries) != config.max_requests_per_run:
                raise LivePipelineError(
                    f"{config.adapter} requires exactly one bounded request per declared query family"
                )
        elif len(config.source_queries) != 1:
            raise LivePipelineError(
                f"source {key} adapter {config.adapter!r} requires exactly one source query"
            )
        if key in sources:
            raise LivePipelineError(f"duplicate source key in registry: {key}")
        sources[key] = config

    if not sources:
        raise LivePipelineError("source registry contains no enabled search sources")
    return sources


def build_feed_url(source: SourceConfig, request_number: int = 1) -> str:
    """Build one registry-bounded request URL for a reviewed adapter."""
    if request_number < 1 or request_number > source.max_requests_per_run:
        raise LivePipelineError(
            f"request {request_number} exceeds {source.key}'s "
            f"{source.max_requests_per_run}-request limit"
        )
    parsed = urlparse(source.endpoint)
    if parsed.scheme != "https" or parsed.hostname != source.allowed_host:
        raise LivePipelineError("source endpoint failed the HTTPS/host allowlist check")
    params = parse_qsl(parsed.query, keep_blank_values=True)
    if source.adapter == "remotive":
        if request_number != 1:
            raise LivePipelineError("Remotive adapter permits exactly one request per run")
        params.extend(
            (("search", source.source_query), ("limit", str(source.max_records_per_run)))
        )
    elif source.adapter == "arbeitnow":
        params.append(("page", str(request_number)))
    elif source.adapter == "jobicy":
        if request_number > len(source.source_queries):
            raise LivePipelineError(
                f"Jobicy request {request_number} has no declared query family"
            )
        params.extend(
            (
                ("tag", source.source_queries[request_number - 1]),
                ("count", str(min(20, source.max_records_per_run))),
            )
        )
    elif source.adapter == "jooble":
        if request_number > len(source.source_queries):
            raise LivePipelineError(
                f"Jooble request {request_number} has no declared query family"
            )
        # Jooble authenticates via a URL path segment holding the API key,
        # which is never present in the registered endpoint or here -- only
        # fetch_json_http injects it, from the environment, at request time.
        params.append(("keywords", source.source_queries[request_number - 1]))
    elif source.adapter == "himalayas":
        if request_number > len(source.source_queries):
            raise LivePipelineError(
                f"Himalayas request {request_number} has no declared query family"
            )
        params.extend(
            (
                ("q", source.source_queries[request_number - 1]),
                ("limit", str(min(20, source.max_records_per_run))),
                ("offset", "0"),
            )
        )
    elif source.adapter == "adzuna":
        if request_number > len(source.source_queries):
            raise LivePipelineError(
                f"Adzuna request {request_number} has no declared query family"
            )
        # app_id/app_key are never added here -- only fetch_json_http injects
        # them, from the environment, at request time (see JOOBLE_API_KEY_ENV
        # for the equivalent pattern).
        params.extend(
            (
                ("what", source.source_queries[request_number - 1]),
                ("results_per_page", str(ADZUNA_PAGE_SIZE)),
                ("content-type", "application/json"),
            )
        )
    else:
        raise LivePipelineError(f"unsupported reviewed source adapter: {source.adapter!r}")
    return urlunparse(parsed._replace(query=urlencode(params), fragment=""))


def fetch_json_http(url: str, source: SourceConfig) -> dict:
    """Fetch one JSON response without redirects and with a hard byte cap."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != source.allowed_host:
        raise LivePipelineError(f"refusing request outside allowed host {source.allowed_host!r}")

    if source.adapter == "jooble":
        api_key = os.environ.get(JOOBLE_API_KEY_ENV, "").strip()
        if not api_key:
            raise LivePipelineError(
                f"{JOOBLE_API_KEY_ENV} environment variable is not set; "
                "Jooble requires an approved API key (see https://jooble.org/api/about)"
            )
        # The key lives only in this local variable and the outgoing request
        # -- it must never appear in a raised error message.
        request_url = f"https://{source.allowed_host}/api/{api_key}"
        json_body = dict(parse_qsl(parsed.query, keep_blank_values=True))
        try:
            response = requests.post(
                request_url,
                json=json_body,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=source.timeout_seconds,
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise LivePipelineError(
                f"request to {source.key} failed: {type(exc).__name__}"
            ) from None
    elif source.adapter == "adzuna":
        app_id = os.environ.get(ADZUNA_APP_ID_ENV, "").strip()
        app_key = os.environ.get(ADZUNA_APP_KEY_ENV, "").strip()
        if not app_id or not app_key:
            raise LivePipelineError(
                f"{ADZUNA_APP_ID_ENV} and {ADZUNA_APP_KEY_ENV} environment variables must "
                "both be set; Adzuna requires an approved app_id/app_key pair "
                "(see https://developer.adzuna.com/)"
            )
        # app_id/app_key live only in this local request -- like Jooble's
        # key, they must never appear in a raised error message.
        parsed_request = urlparse(url)
        params = [("app_id", app_id), ("app_key", app_key), *parse_qsl(parsed_request.query, keep_blank_values=True)]
        request_url = urlunparse(parsed_request._replace(query=urlencode(params)))
        try:
            response = requests.get(
                request_url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=source.timeout_seconds,
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise LivePipelineError(
                f"request to {source.key} failed: {type(exc).__name__}"
            ) from None
    else:
        try:
            response = requests.get(
                url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=source.timeout_seconds,
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise LivePipelineError(f"request to {source.key} failed: {exc}") from exc

    try:
        if 300 <= response.status_code < 400:
            raise LivePipelineError(f"{source.key} returned a disallowed redirect")
        response.raise_for_status()
        length_header = response.headers.get("Content-Length")
        if length_header:
            try:
                declared_length = int(length_header)
            except ValueError as exc:
                raise LivePipelineError("source returned an invalid Content-Length") from exc
            if declared_length > source.max_response_bytes:
                raise LivePipelineError(
                    f"{source.key} response exceeds {source.max_response_bytes} bytes"
                )

        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > source.max_response_bytes:
                raise LivePipelineError(
                    f"{source.key} response exceeds {source.max_response_bytes} bytes"
                )
    except requests.RequestException as exc:
        raise LivePipelineError(f"{source.key} returned an HTTP error: {exc}") from exc
    finally:
        response.close()

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LivePipelineError(f"{source.key} returned invalid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise LivePipelineError(f"{source.key} response must be a JSON object")
    return payload


Fetcher = Callable[[str, SourceConfig], dict]
