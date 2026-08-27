"""Validated first-party career sources and network-free fixture adapters."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import DCTERMS, RDF, XSD

ROOT = Path(__file__).resolve().parent.parent
KGJOBS = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/ontology#")
GRAPHWISE_ADAPTER = "firstparty-graphwise"
GRAPHWISE_CAREERS_URL = "https://graphwise.ai/careers/"
GRAPHWISE_DETAIL_ENDPOINT = "https://graphwise.ai/wp-json/wp/v2/job"
GRAPHWISE_DETAIL_QUERY = {
    "per_page": "100",
    "_fields": "id,slug,link,date,title,content,toolset-meta",
}
GRAPHWISE_ADAPTER_REVISION = "wordpress-numeric-id-v2"
RIPPLING_ADAPTER = "firstparty-rippling"
RIPPLING_LIST_ENDPOINT = (
    "https://ats.rippling.com/api/v2/board/topquadrant/jobs"
    "?groupJobsByLocation=true&page=0&pageSize=2"
)
RIPPLING_DETAIL_PREFIX = "https://ats.rippling.com/api/v2/board/topquadrant/jobs/"
ECCENCA_ADAPTER = "firstparty-eccenca"
ECCENCA_CAREERS_URL = "https://eccenca.com/about-us/jobs"


class FirstPartySourceError(RuntimeError):
    """A source or payload violates the reviewed local-pilot contract."""


@dataclass(frozen=True)
class FirstPartySource:
    key: str
    dataset_uri: str
    title: str
    organization_iri: str
    provider: str
    adapter: str
    extraction_mode: str
    tenant: str
    allowed_host: str
    endpoint: str
    careers_page: str
    terms_url: str
    robots_url: str
    attribution_text: str
    attribution_url: str
    republication_status: str
    refresh_interval_seconds: int
    timeout_seconds: int
    max_response_bytes: int
    max_requests_per_run: int
    max_records_per_run: int
    review_status: str
    production_approved: bool

    @property
    def name(self) -> str:
        return self.title

    @property
    def min_refresh_interval_seconds(self) -> int:
        return self.refresh_interval_seconds


def _one(graph: Graph, subject, predicate, label: str):
    values = list(graph.objects(subject, predicate))
    if len(values) != 1:
        raise FirstPartySourceError(
            f"first-party source {subject} requires exactly one {label}; found {len(values)}"
        )
    return values[0]


def _positive_int(value, label: str, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FirstPartySourceError(f"{label} must be an integer") from exc
    if parsed <= 0 or (maximum is not None and parsed > maximum):
        suffix = f" no greater than {maximum}" if maximum else ""
        raise FirstPartySourceError(f"{label} must be positive{suffix}")
    return parsed


def _boolean(value, label: str) -> bool:
    if (
        not isinstance(value, Literal)
        or value.datatype != XSD.boolean
        or not isinstance(value.toPython(), bool)
    ):
        raise FirstPartySourceError(f"{label} must be an xsd:boolean")
    return value.toPython()


def _https_exact_host(url: str, host: str, label: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != host:
        raise FirstPartySourceError(f"{label} must use HTTPS on exact host {host!r}")
    if parsed.username or parsed.password or parsed.fragment:
        raise FirstPartySourceError(f"{label} contains disallowed URL components")


def load_first_party_sources(
    path: Path = ROOT / "sources.ttl",
    organizations_path: Path = ROOT / "data" / "organizations.json",
) -> dict[str, FirstPartySource]:
    graph = Graph().parse(path, format="turtle")
    try:
        organization_payload = json.loads(organizations_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FirstPartySourceError("reviewed organization projection is missing or invalid") from exc
    organizations = {
        row.get("iri"): row for row in organization_payload.get("organizations", [])
        if row.get("iri")
    }
    output = {}
    for subject in sorted(set(graph.subjects(RDF.type, KGJOBS.FirstPartyCareerSource)), key=str):
        key = str(_one(graph, subject, DCTERMS.identifier, "identifier"))
        organization_iri = str(_one(graph, subject, KGJOBS.organization, "organization"))
        organization = organizations.get(organization_iri)
        if not organization or organization.get("reviewStatus") != "evidence-reviewed":
            raise FirstPartySourceError(f"source {key} organization is not evidence-reviewed")
        if not organization.get("active") or not organization.get("pilotSelected"):
            raise FirstPartySourceError(f"source {key} organization is not an active pilot member")
        host = str(_one(graph, subject, KGJOBS.allowedHost, "allowed host"))
        endpoint = str(_one(graph, subject, KGJOBS.sourceEndpoint, "endpoint"))
        _https_exact_host(endpoint, host, f"source {key} endpoint")
        terms = str(_one(graph, subject, KGJOBS.termsURL, "terms URL"))
        robots = str(_one(graph, subject, KGJOBS.robotsURL, "robots URL"))
        attribution_url = str(_one(graph, subject, KGJOBS.attributionURL, "attribution URL"))
        careers_page = str(_one(graph, subject, DCTERMS.source, "official careers page"))
        for value, label in (
            (terms, "terms URL"), (robots, "robots URL"),
            (attribution_url, "attribution URL"), (careers_page, "official careers page"),
        ):
            if urlparse(value).scheme != "https" or not urlparse(value).hostname:
                raise FirstPartySourceError(f"source {key} {label} must be absolute HTTPS")
        adapter = str(_one(graph, subject, KGJOBS.adapter, "adapter"))
        provider = str(_one(graph, subject, KGJOBS.careerProvider, "provider"))
        allowed_adapters = {
            "greenhouse": {"firstparty-greenhouse"},
            "lever": {"firstparty-lever"},
            "ashby": {"firstparty-ashby"},
            "rippling": {RIPPLING_ADAPTER},
            "same-site": {"firstparty-schema", GRAPHWISE_ADAPTER, ECCENCA_ADAPTER},
        }
        if adapter not in allowed_adapters.get(provider, set()):
            raise FirstPartySourceError(f"source {key} has an unreviewed provider/adapter pair")
        if adapter == GRAPHWISE_ADAPTER and (
            key != "first-party-graphwise"
            or host != "graphwise.ai"
            or endpoint != GRAPHWISE_CAREERS_URL
            or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
            != "bounded-graphwise-html-wordpress"
        ):
            raise FirstPartySourceError("Graphwise adapter requires its exact reviewed source contract")
        if adapter == RIPPLING_ADAPTER and (
            key != "first-party-topquadrant"
            or host != "ats.rippling.com"
            or endpoint != RIPPLING_LIST_ENDPOINT
            or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
            != "bounded-rippling-public-api"
        ):
            raise FirstPartySourceError("TopQuadrant adapter requires its exact reviewed source contract")
        if adapter == ECCENCA_ADAPTER and (
            key != "first-party-eccenca"
            or host != "eccenca.com"
            or endpoint != ECCENCA_CAREERS_URL
            or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
            != "bounded-eccenca-html-detail"
        ):
            raise FirstPartySourceError("eccenca adapter requires its exact reviewed source contract")
        review_status = str(_one(graph, subject, KGJOBS.sourceReviewStatus, "review status"))
        if review_status != "evidence-reviewed":
            raise FirstPartySourceError(f"source {key} is not evidence-reviewed")
        production_approved = _boolean(
            _one(graph, subject, KGJOBS.productionApproved, "production approval"),
            f"source {key} production approval",
        )
        refresh_interval = _positive_int(
            _one(graph, subject, KGJOBS.minRefreshIntervalSeconds, "refresh interval"),
            "refresh interval",
        )
        if refresh_interval < 86400:
            raise FirstPartySourceError(f"source {key} refresh interval is below 24 hours")
        config = FirstPartySource(
            key=key,
            dataset_uri=str(subject),
            title=str(_one(graph, subject, DCTERMS.title, "title")),
            organization_iri=organization_iri,
            provider=provider,
            adapter=adapter,
            extraction_mode=str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode")),
            tenant=str(_one(graph, subject, KGJOBS.tenantIdentifier, "tenant")),
            allowed_host=host,
            endpoint=endpoint,
            careers_page=careers_page,
            terms_url=terms,
            robots_url=robots,
            attribution_text=str(_one(graph, subject, KGJOBS.attributionText, "attribution text")),
            attribution_url=attribution_url,
            republication_status=str(_one(graph, subject, KGJOBS.republicationStatus, "republication status")),
            refresh_interval_seconds=refresh_interval,
            timeout_seconds=_positive_int(
                _one(graph, subject, KGJOBS.requestTimeoutSeconds, "request timeout"),
                "request timeout", 20,
            ),
            max_response_bytes=_positive_int(
                _one(graph, subject, KGJOBS.maxResponseBytes, "response cap"),
                "response cap", 5_000_000,
            ),
            max_requests_per_run=_positive_int(
                _one(graph, subject, KGJOBS.maxRequestsPerRun, "request cap"),
                "request cap", 3,
            ),
            max_records_per_run=_positive_int(
                _one(graph, subject, KGJOBS.maxRecordsPerRun, "record cap"),
                "record cap", 250,
            ),
            review_status=review_status,
            production_approved=production_approved,
        )
        expected_republication = (
            "production-approved" if production_approved else "local-review-only"
        )
        if config.republication_status != expected_republication:
            raise FirstPartySourceError(
                f"source {key} production and republication approvals do not agree"
            )
        if production_approved != (organization.get("productionApproved") is True):
            raise FirstPartySourceError(
                f"source {key} production approval does not match its reviewed organization"
            )
        if key in output:
            raise FirstPartySourceError(f"duplicate first-party source identifier {key}")
        output[key] = config
    if not 10 <= len(output) <= 20:
        raise FirstPartySourceError("first-party pilot must declare 10 to 20 sources")
    if sum(source.max_requests_per_run for source in output.values()) > 60:
        raise FirstPartySourceError("first-party pilot exceeds the 60-request global cap")
    return output


def load_production_first_party_sources(
    path: Path = ROOT / "sources.ttl",
    organizations_path: Path = ROOT / "data" / "organizations.json",
) -> dict[str, FirstPartySource]:
    """Return only explicitly reviewed and production-approved career sources."""
    sources = load_first_party_sources(path, organizations_path)
    return {
        key: source for key, source in sources.items()
        if source.production_approved
        and source.review_status == "evidence-reviewed"
        and source.republication_status == "production-approved"
    }


class _JsonLdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._capturing = False
        self._parts: list[str] = []
        self.blocks: list[str] = []
        self._text: list[str] = []
        self._marker_attributes: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {key.casefold(): value for key, value in attrs}
        for key in ("placeholder", "aria-label", "title", "id", "class"):
            if values.get(key):
                self._marker_attributes.append(values[key])
        if tag.casefold() == "script" and (values.get("type") or "").casefold() == "application/ld+json":
            self._capturing = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)
        else:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._capturing:
            self.blocks.append("".join(self._parts))
            self._capturing = False
            self._parts = []

    @property
    def normalized_text(self) -> str:
        return re.sub(
            r"[-_\s]+", " ", " ".join([*self._text, *self._marker_attributes])
        ).casefold()

    @property
    def explicitly_reports_no_openings(self) -> bool:
        return any(marker in self.normalized_text for marker in (
            "no open positions", "no current openings", "no open roles",
            "no job openings", "currently no openings", "no positions available",
            "do not have any job openings", "none at this time",
        ))

    @property
    def contains_opening_marker(self) -> bool:
        return any(marker in self.normalized_text for marker in (
            "open positions", "current openings", "open roles", "job openings",
            "search in jobs", "search jobs", "view jobs",
        ))


def _strip_html(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _iso_date(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).date().isoformat()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value))
    return match.group(1) if match else None


def _fingerprint(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def _mode(remote=False, raw: str | None = None) -> str:
    text = str(raw or "").casefold()
    if "hybrid" in text:
        return "hybrid"
    if remote or "remote" in text or "telecommute" in text:
        return "remote"
    if text:
        return "onsite"
    return "unknown"


def _location_keys(value) -> list[str]:
    if isinstance(value, dict):
        address = value.get("address", value)
        values = [
            address.get("addressLocality"), address.get("addressRegion"),
            address.get("addressCountry"), address.get("postalCode"),
        ]
    elif isinstance(value, list):
        return sorted({key for item in value for key in _location_keys(item)})
    else:
        values = re.split(r"[,;/|]", str(value or ""))
    return sorted({
        re.sub(r"\s+", " ", unicodedata_nfkc(part).strip()).casefold()
        for part in values if part and str(part).strip()
    })


def unicodedata_nfkc(value: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKC", str(value))


def _base_record(
    source: FirstPartySource, *, source_id: str, url: str, title: str,
    description: str, location: str | None, date_posted: str | None,
    valid_through: str | None = None, remote: bool = False,
    workplace_mode: str | None = None, employment_type: str | None = None,
    requisition_id: str | None = None, source_url: str | None = None,
) -> dict:
    if not source_id or not url or not title:
        raise FirstPartySourceError(f"{source.key} posting lacks stable ID, URL, or title")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FirstPartySourceError(f"{source.key} posting URL must be absolute HTTPS")
    allowed_posting_hosts = {
        "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io"},
        "lever": {"jobs.lever.co"},
        "ashby": {"jobs.ashbyhq.com"},
        "rippling": {source.allowed_host},
        "same-site": (
            {"graphwise.bamboohr.com"}
            if source.adapter == GRAPHWISE_ADAPTER
            else {source.allowed_host}
        ),
    }.get(source.provider, set())
    if parsed.hostname not in allowed_posting_hosts:
        raise FirstPartySourceError(
            f"{source.key} posting URL uses an unapproved host {parsed.hostname!r}"
        )
    normalized_description = _strip_html(description)
    record_id = f"firstparty:{source.key}:{source_id}"
    source_url = source_url or url
    occurrence = {
        "sourceDataset": source.dataset_uri,
        "sourceRecordId": str(source_id),
        "sourceUrl": source_url,
        "provider": source.provider,
        "tenant": source.tenant,
        "firstParty": True,
    }
    return {
        "id": record_id,
        "sourceDataset": source.dataset_uri,
        "sourceRecordId": str(source_id),
        "sourceUrl": source_url,
        "canonicalUrl": url,
        "canonicalFingerprint": _fingerprint(url),
        "title": _strip_html(title),
        "description": normalized_description,
        "qualifications": None,
        "responsibilities": None,
        "hiringOrganization": source.attribution_text.removeprefix("Jobs at ").removeprefix("Careers at "),
        "organizationIri": source.organization_iri,
        "location": _strip_html(location) or None,
        "locationKeys": _location_keys(location),
        "remote": remote,
        "workplaceMode": _mode(remote, workplace_mode),
        "datePosted": date_posted,
        "validThrough": valid_through,
        "employmentType": employment_type,
        "requisitionId": requisition_id,
        "firstParty": True,
        "provider": source.provider,
        "tenant": source.tenant,
        "sourceOccurrences": [occurrence],
        "discoveredBy": [source.key],
        "attributionText": source.attribution_text,
        "attributionUrl": source.attribution_url,
        "sourceName": source.attribution_text,
        "sourceAttributionUrl": source.attribution_url,
    }


def _enforce_record_cap(items: list, source: FirstPartySource, label: str) -> None:
    if len(items) > source.max_records_per_run:
        raise FirstPartySourceError(
            f"{source.key} {label} exceeds its record cap "
            f"({len(items)} > {source.max_records_per_run})"
        )


def greenhouse_records(payload, source: FirstPartySource) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise FirstPartySourceError("Greenhouse payload requires a jobs array")
    _enforce_record_cap(payload["jobs"], source, "Greenhouse payload")
    records = []
    for item in payload["jobs"]:
        if not isinstance(item, dict):
            raise FirstPartySourceError("Greenhouse jobs must be objects")
        location = item.get("location", {}).get("name") if isinstance(item.get("location"), dict) else None
        records.append(_base_record(
            source,
            source_id=str(item.get("id") or ""),
            url=item.get("absolute_url") or "",
            title=item.get("title") or "",
            description=item.get("content") or "",
            location=location,
            # updated_at is deliberately not treated as a genuine posting date.
            date_posted=_iso_date(item.get("first_published")),
            remote="remote" in str(location or "").casefold(),
            workplace_mode=location,
            requisition_id=str(item.get("requisition_id") or "") or None,
        ))
    return records


def lever_records(payload, source: FirstPartySource) -> list[dict]:
    if not isinstance(payload, list):
        raise FirstPartySourceError("Lever payload requires an array")
    _enforce_record_cap(payload, source, "Lever payload")
    records = []
    for item in payload:
        if not isinstance(item, dict):
            raise FirstPartySourceError("Lever postings must be objects")
        categories = item.get("categories") if isinstance(item.get("categories"), dict) else {}
        location = categories.get("location")
        description = " ".join(
            str(item.get(key) or "") for key in ("descriptionPlain", "additionalPlain")
        )
        records.append(_base_record(
            source,
            source_id=str(item.get("id") or ""),
            url=item.get("applyUrl") or item.get("hostedUrl") or "",
            title=item.get("text") or "",
            description=description,
            location=location,
            date_posted=_iso_date(item.get("createdAt")),
            remote=str(item.get("workplaceType") or "").casefold() == "remote",
            workplace_mode=item.get("workplaceType"),
            employment_type=categories.get("commitment"),
            requisition_id=str(item.get("id") or "") or None,
        ))
    return records


def ashby_records(payload, source: FirstPartySource) -> list[dict]:
    """Normalize Ashby's documented public Job Postings API response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise FirstPartySourceError("Ashby payload requires a jobs array")
    _enforce_record_cap(payload["jobs"], source, "Ashby payload")
    records = []
    for item in payload["jobs"]:
        if not isinstance(item, dict):
            raise FirstPartySourceError("Ashby jobs must be objects")
        if item.get("isListed") is False:
            continue
        job_url = str(item.get("jobUrl") or "")
        source_id = job_url.rstrip("/").rsplit("/", 1)[-1]
        raw_address = item.get("address")
        address = raw_address.get("postalAddress", {}) if isinstance(raw_address, dict) else {}
        location = item.get("location")
        if not location and isinstance(address, dict):
            location = ", ".join(
                str(address.get(key)) for key in (
                    "addressLocality", "addressRegion", "addressCountry"
                ) if address.get(key)
            )
        records.append(_base_record(
            source,
            source_id=source_id,
            url=item.get("applyUrl") or job_url,
            title=item.get("title") or "",
            description=item.get("descriptionPlain") or item.get("descriptionHtml") or "",
            location=location,
            date_posted=_iso_date(item.get("publishedAt")),
            remote=bool(item.get("isRemote")),
            workplace_mode=item.get("workplaceType"),
            employment_type=str(item.get("employmentType") or "") or None,
            requisition_id=source_id or None,
        ))
    return records


def _walk_jsonld(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk_jsonld(item)
    elif isinstance(value, dict):
        if "@graph" in value:
            yield from _walk_jsonld(value["@graph"])
        types = value.get("@type")
        type_values = types if isinstance(types, list) else [types]
        if "JobPosting" in type_values or "https://schema.org/JobPosting" in type_values:
            yield value


def schema_records(payload: str, source: FirstPartySource) -> list[dict]:
    if not isinstance(payload, str):
        raise FirstPartySourceError("Schema.org source payload must be HTML text")
    parser = _JsonLdParser()
    parser.feed(payload)
    postings = []
    for block in parser.blocks:
        try:
            decoded = json.loads(block)
        except json.JSONDecodeError as exc:
            raise FirstPartySourceError("source contains malformed JSON-LD") from exc
        postings.extend(_walk_jsonld(decoded))
    if not postings:
        if parser.explicitly_reports_no_openings:
            return []
        detail = " contains opening markers but" if parser.contains_opening_marker else ""
        raise FirstPartySourceError(
            f"{source.key} careers page{detail} yielded zero JobPosting records "
            "without an explicit reviewed no-openings statement"
        )
    _enforce_record_cap(postings, source, "Schema.org payload")
    records = []
    for index, item in enumerate(postings, start=1):
        identifier = item.get("identifier")
        if isinstance(identifier, dict):
            identifier = identifier.get("value") or identifier.get("name")
        url = urljoin(source.endpoint, str(item.get("url") or source.endpoint))
        job_location = item.get("jobLocation")
        location = None
        if isinstance(job_location, dict):
            address = job_location.get("address", job_location)
            if isinstance(address, dict):
                location = ", ".join(
                    str(address.get(key)) for key in (
                        "addressLocality", "addressRegion", "addressCountry"
                    ) if address.get(key)
                )
            else:
                location = str(address)
        elif job_location:
            location = str(job_location)
        remote = str(item.get("jobLocationType") or "").upper() == "TELECOMMUTE"
        records.append(_base_record(
            source,
            source_id=str(identifier or item.get("@id") or index),
            url=str(url),
            title=str(item.get("title") or item.get("name") or ""),
            description=str(item.get("description") or ""),
            location=location,
            date_posted=_iso_date(item.get("datePosted")),
            valid_through=_iso_date(item.get("validThrough")),
            remote=remote,
            workplace_mode=item.get("jobLocationType"),
            employment_type=str(item.get("employmentType") or "") or None,
            requisition_id=str(identifier or "") or None,
        ))
    return records


_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _rippling_job_url(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    match = re.fullmatch(rf"/topquadrant/jobs/({_UUID.pattern})", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "ats.rippling.com"
        or not match
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise FirstPartySourceError("TopQuadrant job URL violates the exact Rippling host/path contract")
    return urlunparse(parsed), match.group(1)


def rippling_discovery(payload, source: FirstPartySource) -> list[dict]:
    if (
        source.adapter != RIPPLING_ADAPTER
        or source.key != "first-party-topquadrant"
        or source.endpoint != RIPPLING_LIST_ENDPOINT
        or source.allowed_host != "ats.rippling.com"
    ):
        raise FirstPartySourceError("TopQuadrant discovery source contract changed")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise FirstPartySourceError("TopQuadrant Rippling payload requires an items array")
    items = payload["items"]
    total = payload.get("totalItems")
    total_pages = payload.get("totalPages")
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or total != len(items)
        or total_pages != (1 if total else 0)
    ):
        raise FirstPartySourceError("TopQuadrant Rippling payload is partial or inconsistent")
    _enforce_record_cap(items, source, "Rippling discovery")
    output = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise FirstPartySourceError("TopQuadrant Rippling jobs must be objects")
        url, job_id = _rippling_job_url(str(item.get("url") or ""))
        if str(item.get("id") or "") != job_id or job_id in seen:
            raise FirstPartySourceError("TopQuadrant Rippling job has an invalid or duplicate ID")
        if not _strip_html(item.get("name")):
            raise FirstPartySourceError("TopQuadrant Rippling job lacks a title")
        seen.add(job_id)
        output.append({**item, "id": job_id, "url": url})
    return sorted(output, key=lambda row: row["id"])


def rippling_records(payload, source: FirstPartySource) -> list[dict]:
    if not isinstance(payload, dict):
        raise FirstPartySourceError("TopQuadrant payload requires discovery and detail records")
    discovered = rippling_discovery(payload.get("listing"), source)
    details = payload.get("details")
    if not isinstance(details, list):
        raise FirstPartySourceError("TopQuadrant Rippling detail payload requires an array")
    _enforce_record_cap(details, source, "Rippling detail payload")
    by_id = {}
    for detail in details:
        if not isinstance(detail, dict):
            raise FirstPartySourceError("TopQuadrant Rippling details must be objects")
        url, url_id = _rippling_job_url(str(detail.get("url") or ""))
        job_id = str(detail.get("uuid") or "")
        if job_id != url_id or job_id in by_id:
            raise FirstPartySourceError("TopQuadrant Rippling detail has an invalid or duplicate ID")
        if detail.get("unlistedFromSearch") is True:
            raise FirstPartySourceError("TopQuadrant Rippling detail is not publicly listed")
        by_id[job_id] = {**detail, "url": url}
    expected = {item["id"] for item in discovered}
    if set(by_id) != expected:
        raise FirstPartySourceError("TopQuadrant Rippling details do not exactly match discovery")
    records = []
    for item in discovered:
        detail = by_id[item["id"]]
        if _strip_html(detail.get("name")) != _strip_html(item.get("name")):
            raise FirstPartySourceError("TopQuadrant Rippling detail title changed after discovery")
        description = detail.get("description")
        if not isinstance(description, dict):
            raise FirstPartySourceError("TopQuadrant Rippling detail lacks a description")
        description_html = " ".join(
            str(description.get(key) or "") for key in ("company", "role")
        )
        if not _strip_html(description_html):
            raise FirstPartySourceError("TopQuadrant Rippling detail has an empty description")
        locations = detail.get("workLocations")
        if not isinstance(locations, list):
            raise FirstPartySourceError("TopQuadrant Rippling detail lacks work locations")
        location = ", ".join(str(value) for value in locations if value)
        employment = detail.get("employmentType")
        employment_type = (
            str(employment.get("id") or employment.get("label") or "") or None
            if isinstance(employment, dict) else None
        )
        records.append(_base_record(
            source,
            source_id=item["id"],
            url=detail["url"],
            title=detail.get("name") or "",
            description=description_html,
            location=location,
            date_posted=_iso_date(detail.get("createdOn")),
            remote="remote" in location.casefold(),
            workplace_mode=location,
            employment_type=employment_type,
            requisition_id=item["id"],
        ))
    return records


class _EccencaCareersParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: set[str] = set()
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "a":
            return
        href = next((value for key, value in attrs if key.casefold() == "href"), None)
        if not href:
            return
        parsed = urlparse(urljoin(ECCENCA_CAREERS_URL, html.unescape(href)))
        if (
            parsed.scheme == "https"
            and parsed.hostname == "eccenca.com"
            and re.fullmatch(r"/about-us/jobs/[a-z0-9][a-z0-9-]*", parsed.path)
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        ):
            self.links.add(urlunparse(parsed))

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    @property
    def explicitly_reports_no_openings(self) -> bool:
        text = re.sub(r"[-_\s]+", " ", " ".join(self.text)).casefold()
        return any(marker in text for marker in (
            "no open positions", "no current openings", "no open roles",
            "no job openings", "currently no openings", "no positions available",
        ))


def eccenca_discovery_links(payload: str, source: FirstPartySource) -> list[str]:
    if (
        source.adapter != ECCENCA_ADAPTER
        or source.key != "first-party-eccenca"
        or source.endpoint != ECCENCA_CAREERS_URL
        or source.allowed_host != "eccenca.com"
    ):
        raise FirstPartySourceError("eccenca discovery source contract changed")
    if not isinstance(payload, str):
        raise FirstPartySourceError("eccenca careers payload must be HTML text")
    parser = _EccencaCareersParser()
    parser.feed(payload)
    links = sorted(parser.links)
    if not links and not parser.explicitly_reports_no_openings:
        raise FirstPartySourceError(
            "eccenca careers page yielded zero exact job links without an explicit no-openings statement"
        )
    _enforce_record_cap(links, source, "eccenca discovery")
    return links


class _EccencaDetailParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_main = False
        self.in_first_heading = False
        self.have_heading = False
        self.heading: list[str] = []
        self.content: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "main" and (
            values.get("id") == "inhalt"
            or "page-main" in str(values.get("class") or "").split()
        ):
            self.in_main = True
        if self.in_main and tag.casefold() == "h1" and not self.have_heading:
            self.in_first_heading = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "h1" and self.in_first_heading:
            self.in_first_heading = False
            self.have_heading = True
        if tag.casefold() == "main" and self.in_main:
            self.in_main = False

    def handle_data(self, data: str) -> None:
        if self.in_main:
            self.content.append(data)
            if self.in_first_heading:
                self.heading.append(data)


def _eccenca_job_url(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    match = re.fullmatch(r"/about-us/jobs/([a-z0-9][a-z0-9-]*)", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "eccenca.com"
        or not match
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise FirstPartySourceError("eccenca job URL violates the exact host/path contract")
    return urlunparse(parsed), match.group(1)


def eccenca_records(payload, source: FirstPartySource) -> list[dict]:
    if not isinstance(payload, dict):
        raise FirstPartySourceError("eccenca payload requires discovery HTML and detail records")
    discovered = eccenca_discovery_links(payload.get("careersHtml"), source)
    details = payload.get("details")
    if not isinstance(details, list):
        raise FirstPartySourceError("eccenca detail payload requires an array")
    _enforce_record_cap(details, source, "eccenca detail payload")
    by_url = {}
    for detail in details:
        if not isinstance(detail, dict) or not isinstance(detail.get("html"), str):
            raise FirstPartySourceError("eccenca detail records require URL and HTML text")
        detail_url, _ = _eccenca_job_url(str(detail.get("url") or ""))
        if detail_url in by_url:
            raise FirstPartySourceError("eccenca detail payload contains a duplicate URL")
        by_url[detail_url] = detail["html"]
    if set(by_url) != set(discovered):
        raise FirstPartySourceError("eccenca details do not exactly match discovery")
    records = []
    for detail_url in discovered:
        _, slug = _eccenca_job_url(detail_url)
        parser = _EccencaDetailParser()
        parser.feed(by_url[detail_url])
        title = _strip_html(" ".join(parser.heading))
        description = _strip_html(" ".join(parser.content))
        if not title or len(description) < 80:
            raise FirstPartySourceError("eccenca detail page lacks reviewed job content")
        records.append(_base_record(
            source,
            source_id=slug,
            url=detail_url,
            title=title,
            description=description,
            location=None,
            date_posted=None,
            remote="remote" in title.casefold(),
            workplace_mode=title,
        ))
    return records


class _GraphwiseCareersParser(HTMLParser):
    """Discover only absolute Graphwise job-detail links from the reviewed careers page."""

    def __init__(self):
        super().__init__()
        self.links: set[str] = set()
        self._text: list[str] = []
        self._marker_attributes: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        for key, value in attrs:
            if key.casefold() in {"placeholder", "aria-label", "title", "id", "class"} and value:
                self._marker_attributes.append(value)
        if tag.casefold() != "a":
            return
        href = next((value for key, value in attrs if key.casefold() == "href"), None)
        if not href:
            return
        parsed = urlparse(html.unescape(href))
        if (
            parsed.scheme == "https"
            and parsed.hostname == "graphwise.ai"
            and re.fullmatch(r"/jobs/[a-z0-9][a-z0-9-]*/", parsed.path)
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        ):
            self.links.add(urlunparse(parsed))

    def handle_data(self, data: str) -> None:
        self._text.append(data)

    @property
    def contains_opening_marker(self) -> bool:
        text = " ".join([*self._text, *self._marker_attributes])
        text = re.sub(r"[-_\s]+", " ", text).casefold()
        return "open positions" in text or "search in jobs" in text


def graphwise_discovery_links(payload: str, source: FirstPartySource) -> list[str]:
    if source.adapter != GRAPHWISE_ADAPTER:
        raise FirstPartySourceError("Graphwise discovery requires the Graphwise adapter")
    if source.endpoint != GRAPHWISE_CAREERS_URL or source.allowed_host != "graphwise.ai":
        raise FirstPartySourceError("Graphwise discovery source contract changed")
    if not isinstance(payload, str):
        raise FirstPartySourceError("Graphwise careers payload must be HTML text")
    parser = _GraphwiseCareersParser()
    parser.feed(payload)
    links = sorted(parser.links)
    if not links and parser.contains_opening_marker:
        raise FirstPartySourceError(
            "Graphwise careers page contains opening markers but yielded zero job links"
        )
    if len(links) > source.max_records_per_run:
        raise FirstPartySourceError("Graphwise discovery exceeds its record cap")
    return links


def _graphwise_field(item: dict, group: str, field: str) -> str | None:
    toolset = item.get("toolset-meta")
    if not isinstance(toolset, dict):
        return None
    group_value = toolset.get(group)
    if not isinstance(group_value, dict):
        return None
    field_value = group_value.get(field)
    if not isinstance(field_value, dict):
        return None
    raw = field_value.get("raw")
    return str(raw).strip() if raw is not None and str(raw).strip() else None


def _graphwise_job_url(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    match = re.fullmatch(r"/jobs/([a-z0-9][a-z0-9-]*)/", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "graphwise.ai"
        or not match
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise FirstPartySourceError("Graphwise detail URL violates the exact host/path contract")
    return urlunparse(parsed), match.group(1)


def _graphwise_apply_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "graphwise.bamboohr.com"
        or not re.fullmatch(r"/careers/\d+", parsed.path)
        or parsed.params
        or parsed.fragment
        or any(key != "source" for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
    ):
        raise FirstPartySourceError("Graphwise apply URL violates the BambooHR host/path contract")
    return urlunparse(parsed)


def graphwise_records(payload, source: FirstPartySource) -> list[dict]:
    if not isinstance(payload, dict):
        raise FirstPartySourceError("Graphwise payload requires discovery HTML and detail records")
    discovered = graphwise_discovery_links(payload.get("careersHtml"), source)
    details = payload.get("details")
    if not isinstance(details, list):
        raise FirstPartySourceError("Graphwise detail payload requires an array")
    if len(details) > source.max_records_per_run:
        raise FirstPartySourceError("Graphwise detail payload exceeds its record cap")
    by_url = {}
    for item in details:
        if not isinstance(item, dict):
            raise FirstPartySourceError("Graphwise detail records must be objects")
        detail_url, slug = _graphwise_job_url(str(item.get("link") or ""))
        if str(item.get("slug") or "") != slug:
            raise FirstPartySourceError("Graphwise detail slug does not match its URL")
        if detail_url in by_url:
            raise FirstPartySourceError("Graphwise detail payload contains a duplicate URL")
        by_url[detail_url] = item
    missing = sorted(set(discovered) - set(by_url))
    if missing:
        raise FirstPartySourceError(
            f"Graphwise detail payload is missing {len(missing)} discovered opening(s)"
        )
    records = []
    for detail_url in discovered:
        item = by_url[detail_url]
        wordpress_id = item.get("id")
        if isinstance(wordpress_id, bool) or not isinstance(wordpress_id, int) or wordpress_id <= 0:
            raise FirstPartySourceError("Graphwise detail record lacks a stable WordPress item ID")
        title = item.get("title")
        content = item.get("content")
        if not isinstance(title, dict) or not isinstance(content, dict):
            raise FirstPartySourceError("Graphwise detail record lacks rendered title or content")
        apply_url = _graphwise_apply_url(_graphwise_field(item, "job-form", "bamboo-url") or "")
        location = _graphwise_field(item, "skills", "location")
        contract = _graphwise_field(item, "skills", "contract-type")
        record = _base_record(
            source,
            source_id=str(wordpress_id),
            url=apply_url,
            source_url=detail_url,
            title=str(title.get("rendered") or ""),
            description=str(content.get("rendered") or ""),
            location=location,
            date_posted=_iso_date(item.get("date")),
            remote="remote" in str(location or "").casefold(),
            workplace_mode=location,
            employment_type=contract,
        )
        # WordPress documents this value as a content item ID, not a requisition ID.
        record.pop("requisitionId", None)
        records.append(record)
    return records


def records_from_payload(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter == "firstparty-greenhouse":
        return greenhouse_records(payload, source)
    if source.adapter == "firstparty-lever":
        return lever_records(payload, source)
    if source.adapter == "firstparty-ashby":
        return ashby_records(payload, source)
    if source.adapter == "firstparty-schema":
        return schema_records(payload, source)
    if source.adapter == RIPPLING_ADAPTER:
        return rippling_records(payload, source)
    if source.adapter == ECCENCA_ADAPTER:
        return eccenca_records(payload, source)
    if source.adapter == GRAPHWISE_ADAPTER:
        return graphwise_records(payload, source)
    raise FirstPartySourceError(f"unsupported first-party adapter {source.adapter!r}")


def _fetch_body(source: FirstPartySource, endpoint: str) -> bytes:
    _https_exact_host(endpoint, source.allowed_host, "source endpoint")
    try:
        response = requests.get(
            endpoint,
            timeout=source.timeout_seconds,
            allow_redirects=False,
            headers={"User-Agent": "OKG-first-party-jobs/1.0 (+https://openknowledgegraphs.com/)"},
            stream=True,
        )
        if response.is_redirect or response.is_permanent_redirect:
            raise FirstPartySourceError(f"{source.key} returned a disallowed redirect")
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared:
            normalized_declared = str(declared).strip()
            if not re.fullmatch(r"\d+", normalized_declared):
                raise FirstPartySourceError(f"{source.key} returned malformed Content-Length")
            if int(normalized_declared) > source.max_response_bytes:
                raise FirstPartySourceError(f"{source.key} response exceeds its byte cap")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > source.max_response_bytes:
                raise FirstPartySourceError(f"{source.key} response exceeds its byte cap")
    except requests.RequestException as exc:
        raise FirstPartySourceError(f"{source.key} request failed: {type(exc).__name__}") from exc
    return bytes(body)


def fetch_source(source: FirstPartySource):
    parsed = urlparse(source.endpoint)
    _https_exact_host(source.endpoint, source.allowed_host, "source endpoint")
    endpoint = source.endpoint
    if source.adapter == "firstparty-greenhouse":
        params = [*parse_qsl(parsed.query, keep_blank_values=True), ("content", "true")]
        endpoint = urlunparse(parsed._replace(query=urlencode(params)))
    elif source.adapter == "firstparty-lever":
        params = [*parse_qsl(parsed.query, keep_blank_values=True), ("mode", "json")]
        endpoint = urlunparse(parsed._replace(query=urlencode(params)))
    if source.adapter == RIPPLING_ADAPTER:
        if source.max_requests_per_run < source.max_records_per_run + 1:
            raise FirstPartySourceError(
                "TopQuadrant adapter request cap cannot hydrate its bounded record cap"
            )
        list_body = _fetch_body(source, RIPPLING_LIST_ENDPOINT)
        try:
            listing = json.loads(list_body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FirstPartySourceError("TopQuadrant Rippling endpoint returned malformed JSON") from exc
        discovered = rippling_discovery(listing, source)
        if len(discovered) + 1 > source.max_requests_per_run:
            raise FirstPartySourceError("TopQuadrant discovery exceeds its request cap")
        details = []
        for item in discovered:
            detail_body = _fetch_body(source, f"{RIPPLING_DETAIL_PREFIX}{item['id']}")
            try:
                details.append(json.loads(detail_body.decode("utf-8-sig")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FirstPartySourceError("TopQuadrant detail endpoint returned malformed JSON") from exc
        return {"listing": listing, "details": details}
    if source.adapter == ECCENCA_ADAPTER:
        if source.max_requests_per_run < source.max_records_per_run + 1:
            raise FirstPartySourceError(
                "eccenca adapter request cap cannot hydrate its bounded record cap"
            )
        careers_body = _fetch_body(source, ECCENCA_CAREERS_URL)
        try:
            careers_html = careers_body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FirstPartySourceError("eccenca careers page returned invalid UTF-8") from exc
        links = eccenca_discovery_links(careers_html, source)
        if len(links) + 1 > source.max_requests_per_run:
            raise FirstPartySourceError("eccenca discovery exceeds its request cap")
        details = []
        for link in links:
            detail_body = _fetch_body(source, link)
            try:
                detail_html = detail_body.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise FirstPartySourceError("eccenca detail page returned invalid UTF-8") from exc
            details.append({"url": link, "html": detail_html})
        return {"careersHtml": careers_html, "details": details}
    if source.adapter == GRAPHWISE_ADAPTER:
        if source.max_requests_per_run < 2:
            raise FirstPartySourceError("Graphwise adapter requires two requests within its cap")
        careers_body = _fetch_body(source, GRAPHWISE_CAREERS_URL)
        try:
            careers_html = careers_body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FirstPartySourceError("Graphwise careers page returned invalid UTF-8") from exc
        # Fail before requesting details when the authoritative discovery page is broken.
        graphwise_discovery_links(careers_html, source)
        detail_endpoint = f"{GRAPHWISE_DETAIL_ENDPOINT}?{urlencode(GRAPHWISE_DETAIL_QUERY)}"
        detail_body = _fetch_body(source, detail_endpoint)
        try:
            details = json.loads(detail_body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FirstPartySourceError("Graphwise detail endpoint returned malformed JSON") from exc
        return {"careersHtml": careers_html, "details": details}
    body = _fetch_body(source, endpoint)
    if source.adapter in {"firstparty-greenhouse", "firstparty-lever", "firstparty-ashby"}:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FirstPartySourceError(f"{source.key} returned malformed JSON") from exc
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FirstPartySourceError(f"{source.key} returned invalid UTF-8 HTML") from exc
