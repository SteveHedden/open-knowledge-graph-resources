"""Validated first-party career sources and network-free fixture adapters."""

from __future__ import annotations

import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from rdflib import Graph, Namespace
from rdflib.namespace import DCTERMS, RDF

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
KGJOBS = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
OKG = Namespace("https://openknowledgegraphs.com/ontology#")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
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
TEAMTAILOR_ADAPTER = "firstparty-teamtailor"
SAME_SITE_DETAIL_ADAPTER = "firstparty-same-site-detail"
TRIPLY_CAREERS_URL = "https://triply.cc/en-US/join-us"
WORKDAY_ADAPTER = "firstparty-workday"
WEBCRUITER_ADAPTER = "firstparty-webcruiter"
SUCCESSFACTORS_ADAPTER = "firstparty-successfactors"
UKG_ADAPTER = "firstparty-ukg"
SOFTGARDEN_ADAPTER = "firstparty-softgarden"
REFLINE_ADAPTER = "firstparty-refline"
EMPLY_ADAPTER = "firstparty-emply"
PEOPLEADMIN_ADAPTER = "firstparty-peopleadmin"
SELECTMINDS_ADAPTER = "firstparty-selectminds"
DRUPAL_RSS_ADAPTER = "firstparty-drupal-rss-detail"
CNRS_ADAPTER = "firstparty-cnrs-unit-detail"
MICROSOFT_RESEARCH_ADAPTER = "firstparty-microsoft-research"


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
    max_requests_per_batch: int
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


def _https_exact_host(url: str, host: str, label: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != host:
        raise FirstPartySourceError(f"{label} must use HTTPS on exact host {host!r}")
    if parsed.username or parsed.password or parsed.fragment:
        raise FirstPartySourceError(f"{label} contains disallowed URL components")


def load_first_party_sources(
    path: Path = REPO_ROOT / "sources.ttl",
    organizations_path: Path = REPO_ROOT / "data" / "organizations.json",
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
    for subject in sorted(set(graph.subjects(RDF.type, OKG.CareerSource)), key=str):
        if (subject, RDF.type, DCAT.DataService) not in graph:
            raise FirstPartySourceError(f"career source {subject} is not a dcat:DataService")
        key = str(_one(graph, subject, DCTERMS.identifier, "identifier"))
        organization_iri = str(_one(graph, subject, DCTERMS.publisher, "publisher organization"))
        organization = organizations.get(organization_iri)
        if not organization or organization.get("reviewStatus") != "evidence-reviewed":
            raise FirstPartySourceError(f"source {key} organization is not evidence-reviewed")
        if not organization.get("active"):
            raise FirstPartySourceError(f"source {key} organization is not active")
        host = str(_one(graph, subject, KGJOBS.allowedHost, "allowed host"))
        endpoint = str(_one(graph, subject, DCAT.endpointURL, "endpoint"))
        _https_exact_host(endpoint, host, f"source {key} endpoint")
        terms = str(_one(graph, subject, KGJOBS.termsURL, "terms URL"))
        robots = str(_one(graph, subject, KGJOBS.robotsURL, "robots URL"))
        attribution_url = str(_one(graph, subject, KGJOBS.attributionURL, "attribution URL"))
        careers_page = str(_one(graph, subject, DCAT.landingPage, "official careers page"))
        conforms_to = str(_one(graph, subject, DCTERMS.conformsTo, "source format contract"))
        for value, label in (
            (terms, "terms URL"), (robots, "robots URL"),
            (attribution_url, "attribution URL"), (careers_page, "official careers page"),
            (conforms_to, "source format contract"),
        ):
            if urlparse(value).scheme != "https" or not urlparse(value).hostname:
                raise FirstPartySourceError(f"source {key} {label} must be absolute HTTPS")
        adapter = str(_one(graph, subject, KGJOBS.adapter, "adapter"))
        provider = str(_one(graph, subject, KGJOBS.careerProvider, "provider"))
        allowed_adapters = {
            "greenhouse": {"firstparty-greenhouse"},
            "lever": {"firstparty-lever"},
            "ashby": {"firstparty-ashby"},
            "teamtailor": {TEAMTAILOR_ADAPTER},
            "workday": {WORKDAY_ADAPTER},
            "webcruiter": {WEBCRUITER_ADAPTER},
            "successfactors": {SUCCESSFACTORS_ADAPTER},
            "ukg": {UKG_ADAPTER},
            "softgarden": {SOFTGARDEN_ADAPTER},
            "refline": {REFLINE_ADAPTER},
            "emply": {EMPLY_ADAPTER},
            "peopleadmin": {PEOPLEADMIN_ADAPTER},
            "taleo-selectminds": {SELECTMINDS_ADAPTER},
            "drupal-rss": {DRUPAL_RSS_ADAPTER},
            "cnrs": {CNRS_ADAPTER},
            "microsoft-research": {MICROSOFT_RESEARCH_ADAPTER},
            "rippling": {RIPPLING_ADAPTER},
            "same-site": {
                "firstparty-schema", GRAPHWISE_ADAPTER, ECCENCA_ADAPTER,
                SAME_SITE_DETAIL_ADAPTER,
            },
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
        if adapter == TEAMTAILOR_ADAPTER:
            endpoint_parts = urlparse(endpoint)
            if (
                endpoint_parts.path != "/jobs"
                or endpoint_parts.query
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-teamtailor-html-detail"
            ):
                raise FirstPartySourceError(
                    "Teamtailor adapter requires its exact reviewed /jobs source contract"
                )
        if adapter == WORKDAY_ADAPTER:
            endpoint_parts = urlparse(endpoint)
            endpoint_match = re.fullmatch(
                r"/wday/cxs/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/jobs",
                endpoint_parts.path,
            )
            query = parse_qsl(endpoint_parts.query, keep_blank_values=True)
            if (
                not endpoint_match
                or endpoint_match.group(1) != str(
                    _one(graph, subject, KGJOBS.tenantIdentifier, "tenant")
                )
                or len(query) != len(dict(query))
                or any(
                    not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key)
                    or not re.fullmatch(r"[A-Fa-f0-9]{32}", value)
                    for key, value in query
                )
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-workday-cxs-public-api"
            ):
                raise FirstPartySourceError(
                    "Workday adapter requires an exact reviewed CXS jobs endpoint and facet filter"
                )
        if adapter == WEBCRUITER_ADAPTER:
            endpoint_parts = urlparse(endpoint)
            query = dict(parse_qsl(endpoint_parts.query, keep_blank_values=True))
            if (
                endpoint_parts.path.casefold() != "/nb-no/home/companyadverts"
                or query != {
                    "companylock": str(
                        _one(graph, subject, KGJOBS.tenantIdentifier, "tenant")
                    ),
                    "link_source_ID": "0",
                }
                or not re.fullmatch(r"[1-9]\d*", query["companylock"])
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-webcruiter-company-api"
            ):
                raise FirstPartySourceError(
                    "Webcruiter adapter requires its exact companylock listing contract"
                )
        if adapter == SUCCESSFACTORS_ADAPTER:
            if (
                key != "first-party-the-open-university"
                or host != "jobs.open.ac.uk"
                or endpoint != "https://jobs.open.ac.uk/services/recruiting/v1/jobs"
                or str(_one(graph, subject, KGJOBS.tenantIdentifier, "tenant")) != "open-university"
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-successfactors-rmk-api-detail"
            ):
                raise FirstPartySourceError(
                    "SuccessFactors adapter requires the exact reviewed Open University contract"
                )
        if adapter == UKG_ADAPTER:
            endpoint_parts = urlparse(endpoint)
            match = re.fullmatch(
                r"/([A-Z0-9]+)/JobBoard/([0-9a-f-]{36})/JobBoardView/LoadSearchResults",
                endpoint_parts.path,
            )
            if (
                key != "first-party-regenstrief-institute"
                or not match
                or match.group(1) != str(_one(graph, subject, KGJOBS.tenantIdentifier, "tenant"))
                or endpoint_parts.query
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-ukg-jobboard-api-detail"
            ):
                raise FirstPartySourceError("UKG adapter requires its exact reviewed board contract")
        if adapter == SOFTGARDEN_ADAPTER:
            endpoint_parts = urlparse(endpoint)
            if (
                key != "first-party-wikimedia-deutschland"
                or host != "wikimedia-deutschland.softgarden.io"
                or endpoint_parts.path != "/en/vacancies"
                or endpoint_parts.query
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-softgarden-html-schema-detail"
            ):
                raise FirstPartySourceError(
                    "Softgarden adapter requires its exact reviewed tenant listing contract"
                )
        if adapter == REFLINE_ADAPTER:
            endpoint_parts = urlparse(endpoint)
            if (
                key != "first-party-sib-swiss-institute-of-bioinformatics"
                or host != "apply.refline.ch"
                or endpoint_parts.path != "/499599/search.html"
                or dict(parse_qsl(endpoint_parts.query, keep_blank_values=True))
                != {"target": "search", "department": "sib"}
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-refline-organization-filter-detail"
            ):
                raise FirstPartySourceError(
                    "Refline adapter requires its exact reviewed department filter contract"
                )
        if adapter == EMPLY_ADAPTER:
            endpoint_parts = urlparse(endpoint)
            if (
                key != "first-party-danish-bibliographic-centre"
                or host != "dbc.career.emply.com"
                or endpoint_parts.path != "/ledige-stillinger"
                or endpoint_parts.query
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-emply-embedded-listing-html-detail"
            ):
                raise FirstPartySourceError(
                    "Emply adapter requires its exact reviewed tenant listing contract"
                )
        if adapter == PEOPLEADMIN_ADAPTER:
            endpoint_parts = urlparse(endpoint)
            query = parse_qsl(endpoint_parts.query, keep_blank_values=True)
            peopleadmin_contracts = {
                "first-party-renaissance-computing-institute": {
                    "tenant": "unc:2571",
                    "query": [
                        ("utf8", "✓"), ("query", ""),
                        ("query_v0_posted_at_date", ""),
                        ("query_organizational_tier_3_id[]", "2571"),
                        ("commit", "Search"),
                    ],
                    "mode": "bounded-peopleadmin-organization-filter-detail",
                },
                "first-party-university-of-north-carolina-at-chapel-hill": {
                    "tenant": "unc:all-staff",
                    "query": [
                        ("utf8", "✓"), ("query", ""),
                        ("query_v0_posted_at_date", ""),
                        ("1826[]", "1"), ("1826[]", "2"),
                        ("commit", "Search"),
                    ],
                    "mode": "bounded-peopleadmin-organization-board-detail",
                },
            }
            contract = peopleadmin_contracts.get(key)
            if (
                not contract or host != "unc.peopleadmin.com"
                or endpoint_parts.path != "/postings/search"
                or query != contract["query"]
                or str(_one(graph, subject, KGJOBS.tenantIdentifier, "tenant"))
                != contract["tenant"]
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != contract["mode"]
            ):
                raise FirstPartySourceError(
                    "PeopleAdmin adapter requires its exact reviewed organization filter"
                )
        if adapter == SELECTMINDS_ADAPTER:
            if (
                key != "first-party-stanford-university-school-of-medicine"
                or host != "stanford.referrals.selectminds.com"
                or endpoint != "https://stanford.referrals.selectminds.com/jobs/search/"
                or str(_one(graph, subject, KGJOBS.tenantIdentifier, "tenant"))
                != "default1696:location:79"
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-selectminds-exact-location-facet-detail"
            ):
                raise FirstPartySourceError(
                    "SelectMinds adapter requires Stanford Medicine's exact reviewed location facet"
                )
        if adapter == DRUPAL_RSS_ADAPTER:
            endpoint_parts = urlparse(endpoint)
            if (
                key != "first-party-inter-university-consortium-for-political-and-social-research"
                or host != "careers.umich.edu"
                or endpoint_parts.path != "/search/feed/advanced"
                or dict(parse_qsl(endpoint_parts.query, keep_blank_values=True)) != {
                    "career_interest": "All", "department": "icpsr", "job_id": "",
                    "keyword": "", "position": "All", "regular_temporary": "All",
                    "title": "", "work_location": "All",
                }
                or str(_one(graph, subject, KGJOBS.tenantIdentifier, "tenant")) != "umich:department:icpsr"
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-drupal-department-rss-detail"
            ):
                raise FirstPartySourceError(
                    "Drupal RSS adapter requires ICPSR's exact reviewed department feed"
                )
        if adapter == CNRS_ADAPTER:
            if (
                key != "first-party-institute-of-scientific-and-technical-information"
                or host != "emploi.cnrs.fr"
                or endpoint != "https://emploi.cnrs.fr/Unites/UAR76/Offres.aspx"
                or str(_one(graph, subject, KGJOBS.tenantIdentifier, "tenant")) != "UAR76"
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-cnrs-unit-listing-detail"
            ):
                raise FirstPartySourceError(
                    "CNRS adapter requires INIST's exact reviewed unit listing"
                )
        if adapter == MICROSOFT_RESEARCH_ADAPTER:
            endpoint_parts = urlparse(endpoint)
            if (
                key != "first-party-microsoft-research"
                or host != "www.microsoft.com"
                or endpoint_parts.path
                != "/en-us/research/wp-json/microsoft-research/v1/faceted-search"
                or dict(parse_qsl(endpoint_parts.query, keep_blank_values=True)) != {
                    "facet[tax][msr-post-type]": "msr-job-opportunity",
                    "page": "1", "sort_by": "most-recent",
                }
                or str(_one(graph, subject, KGJOBS.tenantIdentifier, "tenant"))
                != "microsoft-research:msr-job-opportunity"
                or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
                != "bounded-microsoft-research-faceted-api-complete-record"
            ):
                raise FirstPartySourceError(
                    "Microsoft Research adapter requires its exact reviewed faceted API"
                )
        if adapter == SAME_SITE_DETAIL_ADAPTER and (
            key != "first-party-triply"
            or host != "triply.cc"
            or endpoint != TRIPLY_CAREERS_URL
            or str(_one(graph, subject, KGJOBS.extractionMode, "extraction mode"))
            != "bounded-same-site-html-detail"
        ):
            raise FirstPartySourceError("same-site detail adapter requires its exact reviewed source contract")
        if key == "first-party-public-library-of-science":
            endpoint_parts = urlparse(endpoint)
            if (
                adapter != "firstparty-greenhouse"
                or host != "boards-api.greenhouse.io"
                or endpoint_parts.path != "/v1/boards/plos/jobs"
                or endpoint_parts.query
                or str(_one(graph, subject, KGJOBS.tenantIdentifier, "tenant")) != "plos"
            ):
                raise FirstPartySourceError(
                    "PLOS requires the exact reviewed Greenhouse tenant and API path"
                )
        review_status = str(_one(graph, subject, KGJOBS.sourceReviewStatus, "review status"))
        if review_status != "evidence-reviewed":
            raise FirstPartySourceError(f"source {key} is not evidence-reviewed")
        republication_status = str(
            _one(graph, subject, KGJOBS.republicationStatus, "republication status")
        )
        if republication_status not in {"production-approved", "local-review-only"}:
            raise FirstPartySourceError(
                f"source {key} has unsupported republication status {republication_status!r}"
            )
        production_approved = republication_status == "production-approved"
        refresh_interval = _positive_int(
            _one(graph, subject, KGJOBS.minRefreshIntervalSeconds, "refresh interval"),
            "refresh interval",
        )
        if refresh_interval < 86400:
            raise FirstPartySourceError(f"source {key} refresh interval is below 24 hours")
        max_requests_per_run = _positive_int(
            _one(graph, subject, KGJOBS.maxRequestsPerRun, "request cap"),
            "request cap",
        )
        batch_value = graph.value(subject, KGJOBS.maxRequestsPerBatch)
        max_requests_per_batch = (
            _positive_int(batch_value, "request batch cap")
            if batch_value is not None else max_requests_per_run
        )
        if max_requests_per_batch > max_requests_per_run:
            raise FirstPartySourceError(
                f"source {key} request batch cap exceeds its complete-run cap"
            )
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
            republication_status=republication_status,
            refresh_interval_seconds=refresh_interval,
            timeout_seconds=_positive_int(
                _one(graph, subject, KGJOBS.requestTimeoutSeconds, "request timeout"),
                "request timeout", 20,
            ),
            max_response_bytes=_positive_int(
                _one(graph, subject, KGJOBS.maxResponseBytes, "response cap"),
                "response cap", 15_000_000,
            ),
            max_requests_per_run=max_requests_per_run,
            max_requests_per_batch=max_requests_per_batch,
            max_records_per_run=_positive_int(
                _one(graph, subject, KGJOBS.maxRecordsPerRun, "record cap"),
                "record cap", 1_000,
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
        if production_approved != (organization.get("jobsProductionEnabled") is True):
            raise FirstPartySourceError(
                f"source {key} republication approval does not match its reviewed organization"
            )
        if key in output:
            raise FirstPartySourceError(f"duplicate first-party source identifier {key}")
        output[key] = config
    return output


def load_production_first_party_sources(
    path: Path = REPO_ROOT / "sources.ttl",
    organizations_path: Path = REPO_ROOT / "data" / "organizations.json",
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
        "greenhouse": {
            "boards.greenhouse.io", "job-boards.greenhouse.io",
            "job-boards.eu.greenhouse.io",
            *({"databricks.com"} if source.key == "first-party-databricks" else set()),
        },
        "lever": {"jobs.lever.co"},
        "ashby": {"jobs.ashbyhq.com"},
        "teamtailor": {source.allowed_host},
        "workday": {source.allowed_host},
        "webcruiter": {f"{source.tenant}.webcruiter.no"},
        "successfactors": {source.allowed_host},
        "ukg": {source.allowed_host},
        "softgarden": {source.allowed_host},
        "refline": {source.allowed_host},
        "emply": {source.allowed_host},
        "peopleadmin": {source.allowed_host},
        "taleo-selectminds": {source.allowed_host},
        "drupal-rss": {source.allowed_host},
        "cnrs": {source.allowed_host},
            "microsoft-research": {
                source.allowed_host, "apply.careers.microsoft.com",
            },
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
    if source.key == "first-party-public-library-of-science":
        if (
            parsed.hostname != "job-boards.eu.greenhouse.io"
            or not re.fullmatch(r"/plos/jobs/[1-9]\d*", parsed.path)
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise FirstPartySourceError(
                "PLOS posting URL violates its exact Greenhouse tenant/path contract"
            )
    if source.adapter == WORKDAY_ADAPTER:
        endpoint_parts = urlparse(source.endpoint)
        site = endpoint_parts.path.split("/")[4]
        if (
            not parsed.path.startswith(f"/{site}/job/")
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise FirstPartySourceError("Workday posting URL violates its exact site/path contract")
    if source.adapter == WEBCRUITER_ADAPTER:
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if (
            parsed.path != f"/Main/Recruit/Public/{source_id}"
            or query != {"language": "nb", "link_source_id": "0"}
            or parsed.params
            or parsed.fragment
        ):
            raise FirstPartySourceError(
                "Webcruiter posting URL violates its exact tenant/path/query contract"
            )
    if source.adapter == MICROSOFT_RESEARCH_ADAPTER:
        exact_research_path = (
            parsed.hostname == source.allowed_host
            and re.fullmatch(
                r"/en-us/research/opportunity/(?:[a-z0-9-]|%[0-9a-f]{2})+/",
                parsed.path,
            )
        )
        exact_careers_path = (
            parsed.hostname == "apply.careers.microsoft.com"
            and re.fullmatch(r"/careers/job/[1-9]\d*", parsed.path)
        )
        if not (exact_research_path or exact_careers_path) or (
            parsed.params or parsed.query or parsed.fragment
        ):
            raise FirstPartySourceError(
                "Microsoft Research posting URL violates its exact opportunity path contract"
            )
    if source.key == "first-party-databricks" and parsed.hostname == "databricks.com":
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if (
            parsed.path != "/company/careers/open-positions/job"
            or parsed.params
            or parsed.fragment
            or query != [("gh_jid", str(source_id))]
        ):
            raise FirstPartySourceError(
                "first-party-databricks posting URL violates its exact path/query contract"
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


class _BoundedListingParser(HTMLParser):
    """Collect only reviewed job and pagination links from one HTML page."""

    def __init__(self, source: FirstPartySource):
        super().__init__()
        self.source = source
        self.job_links: set[str] = set()
        self.page_links: set[str] = set()
        self._text: list[str] = []
        self._attributes: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {key.casefold(): value for key, value in attrs}
        for key in ("placeholder", "aria-label", "title", "id", "class"):
            if values.get(key):
                self._attributes.append(str(values[key]))
        if tag.casefold() != "a" or not values.get("href"):
            return
        href = urljoin(self.source.endpoint, str(values["href"]))
        parsed = urlparse(href)
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.source.allowed_host
            or parsed.params
            or parsed.fragment
        ):
            return
        if self.source.adapter == TEAMTAILOR_ADAPTER:
            if re.fullmatch(r"/jobs/[1-9]\d*-[a-z0-9][a-z0-9-]*", parsed.path) and not parsed.query:
                self.job_links.add(urlunparse(parsed))
            elif parsed.path == "/jobs/show_more":
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                if set(query) == {"page"} and re.fullmatch(r"[1-9]\d*", query["page"]):
                    self.page_links.add(urlunparse(parsed))
        elif self.source.adapter == SAME_SITE_DETAIL_ADAPTER:
            path = parsed.path
            if re.fullmatch(r"/vacancies/[a-z0-9][a-z0-9-]*", path):
                path = "/en-US" + path
            if re.fullmatch(r"/en-US/vacancies/[a-z0-9][a-z0-9-]*", path) and not parsed.query:
                self.job_links.add(urlunparse(parsed._replace(path=path)))

    def handle_data(self, data: str) -> None:
        self._text.append(data)

    @property
    def normalized_text(self) -> str:
        return re.sub(
            r"[-_\s]+", " ", " ".join([*self._text, *self._attributes])
        ).casefold()

    @property
    def explicitly_reports_no_openings(self) -> bool:
        return any(marker in self.normalized_text for marker in (
            "no open positions", "no current openings", "no open roles",
            "no job openings", "currently no openings", "none at this time",
        ))

    @property
    def contains_opening_marker(self) -> bool:
        return any(marker in self.normalized_text for marker in (
            "open positions", "current openings", "open roles", "job openings",
            "search jobs", "join us", "vacancies",
        ))


class _BoundedDetailParser(HTMLParser):
    """Extract only the reviewed job-content container and its page title."""

    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self, adapter: str):
        super().__init__()
        self.adapter = adapter
        self._main_depth = 0
        self._content_depth = 0
        self._ignored_depth = 0
        self._h1_depth = 0
        self._main_count = 0
        self._content_count = 0
        self._text: list[str] = []
        self._title: list[str] = []

    def _is_content_container(self, tag: str, attrs) -> bool:
        values = dict(attrs)
        classes = set(str(values.get("class") or "").split())
        if self.adapter == TEAMTAILOR_ADAPTER:
            return tag == "div" and {"max-w-750", "prose", "font-company-body"} <= classes
        if self.adapter == SAME_SITE_DETAIL_ADAPTER:
            return (
                tag == "div"
                and values.get("id") == "content"
                and any(value.startswith("Vacancies_vacancyContent__") for value in classes)
            )
        return False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        nested = tag not in self._VOID_TAGS
        if self._main_depth:
            self._main_depth += int(nested)
        elif tag == "main":
            self._main_depth = 1
            self._main_count += 1

        if self._content_depth:
            self._content_depth += int(nested)
        elif self._main_depth and self._is_content_container(tag, attrs):
            self._content_depth = 1
            self._content_count += 1

        if self._ignored_depth:
            self._ignored_depth += int(nested)
        elif tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth = 1

        if self._h1_depth:
            self._h1_depth += int(nested)
        elif tag == "h1" and self._main_depth and not self._ignored_depth and not self._title:
            self._h1_depth = 1

    def handle_endtag(self, _tag: str) -> None:
        if _tag.casefold() in self._VOID_TAGS:
            return
        if self._h1_depth:
            self._h1_depth -= 1
        if self._ignored_depth:
            self._ignored_depth -= 1
        if self._content_depth:
            self._content_depth -= 1
        if self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        if self._content_depth:
            self._text.append(value)
        if self._h1_depth:
            self._title.append(value)

    @property
    def title(self) -> str:
        return " ".join(self._title).strip()

    @property
    def description(self) -> str:
        return " ".join(self._text).strip()

    @property
    def has_exact_reviewed_structure(self) -> bool:
        return self._main_count == 1 and self._content_count == 1


def _listing_parser(payload: str, source: FirstPartySource) -> _BoundedListingParser:
    if not isinstance(payload, str):
        raise FirstPartySourceError(f"{source.key} listing payload must be HTML text")
    parser = _BoundedListingParser(source)
    parser.feed(payload)
    return parser


def _validated_detail_payload(payload, source: FirstPartySource) -> tuple[list[str], dict[str, str]]:
    if not isinstance(payload, dict):
        raise FirstPartySourceError(f"{source.key} requires listing and detail payloads")
    listing_pages = payload.get("listingPages")
    details = payload.get("details")
    if not isinstance(listing_pages, list) or not listing_pages:
        raise FirstPartySourceError(f"{source.key} requires a non-empty listingPages array")
    if not isinstance(details, list):
        raise FirstPartySourceError(f"{source.key} requires a details array")
    if len(listing_pages) + len(details) > source.max_requests_per_run:
        raise FirstPartySourceError(
            f"{source.key} listing and detail payload exceeds its request cap"
        )
    discovered: set[str] = set()
    listing_urls: set[str] = set()
    explicit_zero = False
    opening_marker = False
    for index, page in enumerate(listing_pages):
        if not isinstance(page, dict) or not isinstance(page.get("html"), str):
            raise FirstPartySourceError(f"{source.key} listing pages must contain HTML")
        page_url = str(page.get("url") or "")
        if index == 0 and page_url != source.endpoint:
            raise FirstPartySourceError(f"{source.key} first listing URL changed")
        _https_exact_host(page_url, source.allowed_host, f"{source.key} listing URL")
        parsed_page = urlparse(page_url)
        if index and source.adapter == TEAMTAILOR_ADAPTER:
            query = dict(parse_qsl(parsed_page.query, keep_blank_values=True))
            if (
                parsed_page.path != "/jobs/show_more"
                or set(query) != {"page"}
                or not re.fullmatch(r"[1-9]\d*", query["page"])
            ):
                raise FirstPartySourceError(
                    f"{source.key} pagination URL violates its exact path contract"
                )
        elif index:
            raise FirstPartySourceError(
                f"{source.key} adapter does not permit additional listing pages"
            )
        if page_url in listing_urls:
            raise FirstPartySourceError(f"{source.key} contains a duplicate listing URL")
        listing_urls.add(page_url)
        parser = _listing_parser(page["html"], source)
        discovered.update(parser.job_links)
        explicit_zero = explicit_zero or parser.explicitly_reports_no_openings
        opening_marker = opening_marker or parser.contains_opening_marker
    if not discovered:
        if explicit_zero:
            if details:
                raise FirstPartySourceError(f"{source.key} zero-opening payload has unexpected details")
            return [], {}
        detail = " contains opening markers but" if opening_marker else ""
        raise FirstPartySourceError(
            f"{source.key} careers page{detail} yielded zero exact job links"
        )
    _enforce_record_cap(list(discovered), source, "HTML discovery")
    by_url: dict[str, str] = {}
    for detail in details:
        if not isinstance(detail, dict) or not isinstance(detail.get("html"), str):
            raise FirstPartySourceError(f"{source.key} detail entries must contain HTML")
        url = str(detail.get("url") or "")
        _https_exact_host(url, source.allowed_host, f"{source.key} detail URL")
        if url in by_url:
            raise FirstPartySourceError(f"{source.key} contains a duplicate detail URL")
        by_url[url] = detail["html"]
    if set(discovered) != set(by_url):
        raise FirstPartySourceError(
            f"{source.key} detail payload does not exactly match discovered openings"
        )
    return sorted(discovered), by_url


def teamtailor_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != TEAMTAILOR_ADAPTER:
        raise FirstPartySourceError("Teamtailor records require the Teamtailor adapter")
    links, details = _validated_detail_payload(payload, source)
    records = []
    for url in links:
        parsed = urlparse(url)
        match = re.fullmatch(r"/jobs/([1-9]\d*)-[a-z0-9][a-z0-9-]*", parsed.path)
        if not match or parsed.query or parsed.fragment:
            raise FirstPartySourceError("Teamtailor detail URL violates its exact path contract")
        detail = _BoundedDetailParser(source.adapter)
        detail.feed(details[url])
        if (
            not detail.has_exact_reviewed_structure
            or not detail.title
            or len(detail.description) < 40
        ):
            raise FirstPartySourceError("Teamtailor detail page is missing job title or content")
        records.append(_base_record(
            source,
            source_id=match.group(1),
            url=url,
            title=detail.title,
            description=detail.description,
            location=None,
            date_posted=None,
            source_url=url,
        ))
        records[-1].pop("requisitionId", None)
    return records


def same_site_detail_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != SAME_SITE_DETAIL_ADAPTER:
        raise FirstPartySourceError("same-site records require the reviewed detail adapter")
    links, details = _validated_detail_payload(payload, source)
    records = []
    for url in links:
        parsed = urlparse(url)
        match = re.fullmatch(r"/en-US/vacancies/([a-z0-9][a-z0-9-]*)", parsed.path)
        if not match or parsed.query or parsed.fragment:
            raise FirstPartySourceError("same-site detail URL violates its exact path contract")
        detail = _BoundedDetailParser(source.adapter)
        detail.feed(details[url])
        if (
            not detail.has_exact_reviewed_structure
            or not detail.title
            or len(detail.description) < 40
        ):
            raise FirstPartySourceError("same-site detail page is missing job title or content")
        records.append(_base_record(
            source,
            source_id=match.group(1),
            url=url,
            title=detail.title,
            description=detail.description,
            location=None,
            date_posted=None,
            remote="remote" in detail.description.casefold(),
            source_url=url,
        ))
        records[-1].pop("requisitionId", None)
    return records


def workday_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != WORKDAY_ADAPTER:
        raise FirstPartySourceError("Workday records require the Workday adapter")
    if not isinstance(payload, dict):
        raise FirstPartySourceError("Workday payload must be an object")
    listings = payload.get("listingPages")
    details = payload.get("details")
    if not isinstance(listings, list) or not listings or not isinstance(details, list):
        raise FirstPartySourceError("Workday payload requires listingPages and details arrays")
    if len(listings) + len(details) > source.max_requests_per_run:
        raise FirstPartySourceError("Workday payload exceeds its complete-run request cap")
    batches = payload.get("requestBatches")
    if batches is not None:
        if (
            not isinstance(batches, list) or not batches
            or any(
                not isinstance(batch, dict)
                or batch.get("listingRequests", 0) + batch.get("detailRequests", 0)
                > source.max_requests_per_batch
                or batch.get("listingRequests", 0) < 0
                or batch.get("detailRequests", 0) < 0
                for batch in batches
            )
            or sum(batch["listingRequests"] for batch in batches) != len(listings)
            or sum(batch["detailRequests"] for batch in batches) != len(details)
            or len(listings) + len(details) > source.max_requests_per_run
        ):
            raise FirstPartySourceError("Workday request batches violate the per-batch cap")
    discovered: dict[str, dict] = {}
    expected_total = None
    for page in listings:
        if not isinstance(page, dict) or not isinstance(page.get("jobPostings"), list):
            raise FirstPartySourceError("Workday listing page is malformed")
        total = page.get("total")
        if not isinstance(total, int) or total < 0:
            raise FirstPartySourceError("Workday listing total is malformed")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise FirstPartySourceError("Workday listing total changed during pagination")
        for item in page["jobPostings"]:
            if not isinstance(item, dict):
                raise FirstPartySourceError("Workday listing entries must be objects")
            external_path = str(item.get("externalPath") or "")
            path_segments = external_path.split("/")
            if (
                not re.fullmatch(r"/job/[^/?#]+(?:/[^/?#]+)?", external_path)
                or len(path_segments) not in {3, 4}
                or any(segment in {".", ".."} for segment in path_segments[2:])
                or external_path in discovered
            ):
                raise FirstPartySourceError(
                    f"Workday listing contains an invalid or duplicate job path: {external_path!r}"
                )
            discovered[external_path] = item
    if expected_total != len(discovered):
        raise FirstPartySourceError(
            f"Workday listing is partial ({len(discovered)} of {expected_total})"
        )
    _enforce_record_cap(list(discovered), source, "payload")
    by_path = {}
    for detail in details:
        if (
            not isinstance(detail, dict)
            or not isinstance(detail.get("externalPath"), str)
            or not isinstance(detail.get("payload"), dict)
            or detail["externalPath"] in by_path
        ):
            raise FirstPartySourceError("Workday detail payload is malformed or duplicated")
        by_path[detail["externalPath"]] = detail["payload"]
    if set(by_path) != set(discovered):
        raise FirstPartySourceError("Workday details do not exactly match discovered openings")
    records = []
    for external_path in sorted(discovered):
        detail = by_path[external_path]
        info = detail.get("jobPostingInfo")
        if not isinstance(info, dict):
            raise FirstPartySourceError("Workday detail lacks jobPostingInfo")
        source_id = str(info.get("jobReqId") or "")
        url = str(info.get("externalUrl") or "")
        description = str(info.get("jobDescription") or "")
        if not source_id or len(_strip_html(description)) < 40:
            raise FirstPartySourceError("Workday detail lacks stable requisition ID or description")
        location = str(info.get("location") or discovered[external_path].get("locationsText") or "")
        records.append(_base_record(
            source,
            source_id=source_id,
            url=url,
            title=str(info.get("title") or discovered[external_path].get("title") or ""),
            description=description,
            location=location or None,
            date_posted=_iso_date(info.get("startDate")),
            valid_through=_iso_date(info.get("endDate")),
            remote="remote" in location.casefold(),
            workplace_mode=location,
            employment_type=str(info.get("timeType") or "") or None,
            requisition_id=source_id,
        ))
    return records


def _webcruiter_date(value) -> str | None:
    if value is None or value == "":
        return None
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", str(value))
    if match:
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    return _iso_date(value)


def webcruiter_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != WEBCRUITER_ADAPTER:
        raise FirstPartySourceError("Webcruiter records require the Webcruiter adapter")
    if not isinstance(payload, dict) or "listing" not in payload or "details" not in payload:
        raise FirstPartySourceError(
            "Webcruiter payload requires listing data and full hydrated details"
        )
    listing = payload.get("listing")
    details = payload.get("details")
    if not isinstance(listing, dict) or not isinstance(listing.get("Data"), list):
        raise FirstPartySourceError("Webcruiter payload requires a Data array")
    total = listing.get("Total")
    if not isinstance(total, int) or total < 0 or total != len(listing["Data"]):
        raise FirstPartySourceError("Webcruiter response total is malformed or partial")
    _enforce_record_cap(listing["Data"], source, "payload")
    detail_by_id = {}
    if not isinstance(details, list):
        raise FirstPartySourceError("Webcruiter detail payload must be an array")
    for detail in details:
        if not isinstance(detail, dict) or not isinstance(detail.get("html"), str):
            raise FirstPartySourceError("Webcruiter details require ID and HTML")
        detail_id = str(detail.get("id") or "")
        if detail_id in detail_by_id:
            raise FirstPartySourceError("Webcruiter detail payload contains a duplicate ID")
        detail_by_id[detail_id] = detail["html"]
    if set(detail_by_id) != {str(item.get("Id") or "") for item in listing["Data"]}:
        raise FirstPartySourceError("Webcruiter details do not exactly match discovery")
    records = []
    seen = set()
    for item in listing["Data"]:
        if not isinstance(item, dict):
            raise FirstPartySourceError("Webcruiter entries must be objects")
        source_id = str(item.get("Id") or "")
        if not re.fullmatch(r"[1-9]\d*", source_id) or source_id in seen:
            raise FirstPartySourceError("Webcruiter entry has an invalid or duplicate ID")
        seen.add(source_id)
        if str(item.get("TenantId") or "") != source.tenant:
            raise FirstPartySourceError("Webcruiter entry escaped the reviewed companylock")
        description = str(item.get("Presentation") or "")
        detail_parser = _WebcruiterDetailParser()
        detail_parser.feed(detail_by_id[source_id])
        description = detail_parser.description
        if detail_parser.container_count < 1 or len(description) < 80:
            raise FirstPartySourceError(
                "Webcruiter detail lacks complete reviewed job-description containers"
            )
        records.append(_base_record(
            source,
            source_id=source_id,
            url=str(item.get("OpenAdvertUrl") or ""),
            title=str(item.get("Heading") or ""),
            description=description,
            location=str(item.get("Workplace") or "") or None,
            date_posted=_webcruiter_date(item.get("PublishedDate")),
            valid_through=_iso_date(item.get("ApplicationDeadline")),
            employment_type=str(item.get("JobType") or "") or None,
            requisition_id=None,
        ))
    return records


class _ClassContainerParser(HTMLParser):
    """Capture text from exact provider-owned content containers."""

    def __init__(self, required_classes: set[str]):
        super().__init__()
        self.required_classes = required_classes
        self.depth = 0
        self.container_count = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if self.depth:
            self.depth += 1
            return
        classes = set(str(dict(attrs).get("class") or "").split())
        if tag.casefold() == "div" and self.required_classes <= classes:
            self.depth = 1
            self.container_count += 1

    def handle_endtag(self, _tag: str) -> None:
        if self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth and data.strip():
            self.parts.append(data)

    @property
    def description(self) -> str:
        return _strip_html(" ".join(self.parts))


class _WebcruiterDetailParser(_ClassContainerParser):
    def __init__(self):
        super().__init__({"we-editable", "disabled", "we-bullets"})


class _ItempropJobParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.job_scope_count = 0
        self.title_depth = 0
        self.description_depth = 0
        self.title_parts: list[str] = []
        self.description_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).casefold(): value for key, value in attrs}
        if values.get("itemtype") in {
            "http://schema.org/JobPosting", "https://schema.org/JobPosting"
        }:
            self.job_scope_count += 1
        if self.title_depth:
            self.title_depth += 1
        elif values.get("itemprop") == "title":
            self.title_depth = 1
        if self.description_depth:
            self.description_depth += 1
        elif values.get("itemprop") == "description":
            self.description_depth = 1

    def handle_endtag(self, _tag: str) -> None:
        if self.title_depth:
            self.title_depth -= 1
        if self.description_depth:
            self.description_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_parts.append(data)
        if self.description_depth:
            self.description_parts.append(data)

    @property
    def title(self) -> str:
        return _strip_html(" ".join(self.title_parts))

    @property
    def description(self) -> str:
        return _strip_html(" ".join(self.description_parts))


def _date_dmy(value) -> str | None:
    match = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", str(value or ""))
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else _iso_date(value)


def _date_mdy(value) -> str | None:
    match = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", str(value or ""))
    return f"{match.group(3)}-{match.group(1)}-{match.group(2)}" if match else _iso_date(value)


def successfactors_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != SUCCESSFACTORS_ADAPTER or not isinstance(payload, dict):
        raise FirstPartySourceError("SuccessFactors payload requires its reviewed adapter")
    listing = payload.get("listing")
    details = payload.get("details")
    if not isinstance(listing, dict) or not isinstance(listing.get("jobSearchResult"), list):
        raise FirstPartySourceError("SuccessFactors listing requires jobSearchResult")
    if listing.get("totalJobs") != len(listing["jobSearchResult"]):
        raise FirstPartySourceError("SuccessFactors listing is partial")
    _enforce_record_cap(listing["jobSearchResult"], source, "listing")
    if not isinstance(details, list):
        raise FirstPartySourceError("SuccessFactors payload requires hydrated details")
    by_id = {}
    for detail in details:
        if not isinstance(detail, dict) or not isinstance(detail.get("html"), str):
            raise FirstPartySourceError("SuccessFactors detail is malformed")
        detail_id = str(detail.get("id") or "")
        if detail_id in by_id:
            raise FirstPartySourceError("SuccessFactors detail ID is duplicated")
        by_id[detail_id] = detail
    expected = {
        str(item.get("response", {}).get("id") or "")
        for item in listing["jobSearchResult"] if isinstance(item, dict)
    }
    if set(by_id) != expected or "" in expected:
        raise FirstPartySourceError("SuccessFactors details do not exactly match discovery")
    records = []
    for wrapper in listing["jobSearchResult"]:
        item = wrapper.get("response") if isinstance(wrapper, dict) else None
        if not isinstance(item, dict):
            raise FirstPartySourceError("SuccessFactors result is malformed")
        job_id = str(item["id"])
        slug = str(item.get("urlTitle") or "")
        expected_url = f"https://{source.allowed_host}/job/{slug}/{job_id}-en_GB/"
        detail = by_id[job_id]
        if detail.get("url") != expected_url:
            raise FirstPartySourceError("SuccessFactors detail violates the exact locale/path contract")
        parser = _ItempropJobParser()
        parser.feed(detail["html"])
        if parser.job_scope_count != 1 or parser.title != _strip_html(item.get("unifiedStandardTitle")) or len(parser.description) < 80:
            raise FirstPartySourceError("SuccessFactors detail lacks exact complete JobPosting content")
        locations = item.get("mfield1") if isinstance(item.get("mfield1"), list) else []
        location = ", ".join(str(value) for value in locations if value)
        contracts = item.get("custContractTypeJR") if isinstance(item.get("custContractTypeJR"), list) else []
        records.append(_base_record(
            source, source_id=job_id, url=expected_url, title=parser.title,
            description=parser.description, location=location or None,
            date_posted=_date_dmy(item.get("unifiedStandardStart")),
            valid_through=_date_dmy(item.get("unifiedStandardEnd")),
            remote="remote" in location.casefold(), workplace_mode=location,
            employment_type=", ".join(map(str, contracts)) or None,
            requisition_id=job_id,
        ))
    return records


def ukg_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != UKG_ADAPTER or not isinstance(payload, dict):
        raise FirstPartySourceError("UKG payload requires its reviewed adapter")
    listing = payload.get("listing")
    details = payload.get("details")
    if not isinstance(listing, dict) or not isinstance(listing.get("opportunities"), list):
        raise FirstPartySourceError("UKG listing requires opportunities")
    if listing.get("totalCount") != len(listing["opportunities"]):
        raise FirstPartySourceError("UKG listing is partial")
    _enforce_record_cap(listing["opportunities"], source, "listing")
    if not isinstance(details, list):
        raise FirstPartySourceError("UKG payload requires hydrated details")
    by_id = {str(row.get("Id") or ""): row for row in details if isinstance(row, dict)}
    expected = {str(row.get("Id") or "") for row in listing["opportunities"]}
    if set(by_id) != expected or "" in expected or len(by_id) != len(details):
        raise FirstPartySourceError("UKG details do not exactly match discovery")
    endpoint = urlparse(source.endpoint)
    board_prefix = endpoint.path.split("/JobBoardView/", 1)[0]
    records = []
    for item in listing["opportunities"]:
        job_id = str(item["Id"])
        detail = by_id[job_id]
        description = str(detail.get("Description") or detail.get("BriefDescription") or "")
        if len(_strip_html(description)) < 80 or _strip_html(detail.get("Title")) != _strip_html(item.get("Title")):
            raise FirstPartySourceError("UKG detail lacks complete reviewed content")
        locations = detail.get("Locations") if isinstance(detail.get("Locations"), list) else item.get("Locations", [])
        location_parts = []
        for location in locations or []:
            address = location.get("Address", {}) if isinstance(location, dict) else {}
            location_parts.extend(str(address.get(key)) for key in ("City", "State", "Country") if address.get(key))
        location = ", ".join(location_parts)
        url = urlunparse(endpoint._replace(
            path=f"{board_prefix}/OpportunityDetail", query=urlencode({"opportunityId": job_id})
        ))
        records.append(_base_record(
            source, source_id=job_id, url=url, title=detail.get("Title") or "",
            description=description, location=location or None,
            date_posted=_iso_date(detail.get("PostedDate") or item.get("PostedDate")),
            remote="remote" in location.casefold(), workplace_mode=location,
            employment_type="FULL_TIME" if detail.get("FullTime") else None,
            requisition_id=str(detail.get("RequisitionNumber") or "") or None,
        ))
    return records


class _SoftgardenListingParser(HTMLParser):
    def __init__(self, source: FirstPartySource):
        super().__init__()
        self.source = source
        self.links: dict[str, str] = {}
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        parsed = urlparse(urljoin(self.source.endpoint, html.unescape(href)))
        match = re.fullmatch(r"/job/([1-9]\d*)/[A-Za-z0-9_%().,+&-]+", parsed.path)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if (
            parsed.scheme == "https" and parsed.hostname == self.source.allowed_host
            and match and set(query) == {"jobDbPVId", "l"}
            and re.fullmatch(r"[1-9]\d*", query["jobDbPVId"]) and query["l"] == "en"
            and not parsed.params and not parsed.fragment
        ):
            self.links[match.group(1)] = urlunparse(parsed)

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    @property
    def explicit_zero(self) -> bool:
        text = re.sub(r"\s+", " ", " ".join(self.text)).casefold()
        return any(marker in text for marker in ("no vacancies", "no open positions", "no jobs found"))


def _one_jsonld_job(html_payload: str) -> dict:
    parser = _JsonLdParser()
    parser.feed(html_payload)
    jobs = []
    for block in parser.blocks:
        try:
            jobs.extend(_walk_jsonld(json.loads(block)))
        except json.JSONDecodeError as exc:
            raise FirstPartySourceError("provider detail contains malformed JSON-LD") from exc
    if len(jobs) != 1:
        raise FirstPartySourceError("provider detail requires exactly one JobPosting JSON-LD record")
    return jobs[0]


def softgarden_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != SOFTGARDEN_ADAPTER or not isinstance(payload, dict):
        raise FirstPartySourceError("Softgarden payload requires its reviewed adapter")
    listing = payload.get("listingHtml")
    details = payload.get("details")
    if not isinstance(listing, str) or not isinstance(details, list):
        raise FirstPartySourceError("Softgarden payload requires listing HTML and details")
    parser = _SoftgardenListingParser(source)
    parser.feed(listing)
    if not parser.links and not parser.explicit_zero:
        raise FirstPartySourceError("Softgarden listing yielded zero exact jobs without a zero marker")
    _enforce_record_cap(list(parser.links), source, "listing")
    by_url = {str(row.get("url") or ""): row.get("html") for row in details if isinstance(row, dict)}
    if set(by_url) != set(parser.links.values()) or any(not isinstance(value, str) for value in by_url.values()):
        raise FirstPartySourceError("Softgarden details do not exactly match discovery")
    records = []
    for job_id, url in sorted(parser.links.items()):
        item = _one_jsonld_job(by_url[url])
        location = item.get("jobLocation")
        address = location.get("address", {}) if isinstance(location, dict) else {}
        location_text = ", ".join(str(address.get(key)) for key in ("addressLocality", "addressRegion", "addressCountry") if address.get(key))
        records.append(_base_record(
            source, source_id=job_id, url=url, title=item.get("title") or "",
            description=item.get("description") or "", location=location_text or None,
            date_posted=_iso_date(item.get("datePosted")), valid_through=_iso_date(item.get("validThrough")),
            remote=str(item.get("jobLocationType") or "").upper() == "TELECOMMUTE",
            workplace_mode=item.get("jobLocationType"),
            employment_type=", ".join(item.get("employmentType", [])) if isinstance(item.get("employmentType"), list) else item.get("employmentType"),
        ))
    return records


class _ReflineListingParser(HTMLParser):
    def __init__(self, source: FirstPartySource):
        super().__init__()
        self.source = source
        self.links: set[str] = set()
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() == "a" and dict(attrs).get("href"):
            parsed = urlparse(urljoin(self.source.endpoint, dict(attrs)["href"]))
            if parsed.scheme == "https" and parsed.hostname == self.source.allowed_host and re.fullmatch(r"/499599/[0-9]{4}/pub/[1-9]\d*/index\.html", parsed.path) and not parsed.query and not parsed.fragment:
                self.links.add(urlunparse(parsed))

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    @property
    def explicit_zero(self) -> bool:
        return "there are no opportunities" in " ".join(self.text).casefold()


def refline_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != REFLINE_ADAPTER or not isinstance(payload, dict):
        raise FirstPartySourceError("Refline payload requires its reviewed adapter")
    listing = payload.get("listingHtml")
    details = payload.get("details")
    if not isinstance(listing, str) or not isinstance(details, list):
        raise FirstPartySourceError("Refline payload requires listing HTML and details")
    parser = _ReflineListingParser(source)
    parser.feed(listing)
    if not parser.links and not parser.explicit_zero:
        raise FirstPartySourceError("Refline filter yielded zero jobs without its explicit marker")
    _enforce_record_cap(list(parser.links), source, "listing")
    by_url = {str(row.get("url") or ""): row.get("html") for row in details if isinstance(row, dict)}
    if set(by_url) != parser.links or any(not isinstance(value, str) for value in by_url.values()):
        raise FirstPartySourceError("Refline details do not exactly match filtered discovery")
    records = []
    for url in sorted(parser.links):
        item = _one_jsonld_job(by_url[url])
        match = re.fullmatch(r"/499599/([0-9]{4})/pub/([1-9]\d*)/index\.html", urlparse(url).path)
        source_id = f"{match.group(1)}:{match.group(2)}"
        location = item.get("jobLocation")
        address = location.get("address", {}) if isinstance(location, dict) else {}
        location_text = ", ".join(str(address.get(key)) for key in ("addressLocality", "addressRegion", "addressCountry") if address.get(key))
        records.append(_base_record(
            source, source_id=source_id, url=url, title=item.get("title") or "",
            description=item.get("description") or "", location=location_text or None,
            date_posted=_iso_date(item.get("datePosted")), valid_through=_iso_date(item.get("validThrough")),
            remote=str(item.get("jobLocationType") or "").upper() == "TELECOMMUTE",
            workplace_mode=item.get("jobLocationType"),
            employment_type=", ".join(item.get("employmentType", [])) if isinstance(item.get("employmentType"), list) else item.get("employmentType"),
        ))
    return records


class _EmplyDetailParser(_ClassContainerParser):
    def __init__(self):
        super().__init__({"csa_jobadText"})
        self.title_parts: list[str] = []
        self.title_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        classes = set(str(dict(attrs).get("class") or "").split())
        if tag.casefold() == "h1" and "css_headline" in classes:
            self.title_depth = 1
        elif self.title_depth:
            self.title_depth += 1
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.title_depth:
            self.title_depth -= 1
        super().handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_parts.append(data)
        super().handle_data(data)

    @property
    def title(self) -> str:
        return _strip_html(" ".join(self.title_parts))


def _emply_listing(payload: str, source: FirstPartySource) -> list[dict]:
    matches = re.findall(r"proceedBatch\(\{ vacancies : JSON\.parse\('(\[.*?\])'\), count : (\d+)\}\);", payload, re.DOTALL)
    if len(matches) != 1:
        raise FirstPartySourceError("Emply listing lacks one embedded bounded vacancy batch")
    try:
        rows = json.loads(matches[0][0].replace(r'\"', '"').replace(r"\'", "'"))
    except json.JSONDecodeError as exc:
        raise FirstPartySourceError("Emply embedded vacancy batch is malformed") from exc
    if not isinstance(rows, list) or int(matches[0][1]) != len(rows):
        raise FirstPartySourceError("Emply embedded vacancy batch is partial")
    jobs = [row for row in rows if isinstance(row, dict) and not row.get("talentPool")]
    _enforce_record_cap(jobs, source, "listing")
    return jobs


def emply_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != EMPLY_ADAPTER or not isinstance(payload, dict):
        raise FirstPartySourceError("Emply payload requires its reviewed adapter")
    listing = payload.get("listingHtml")
    details = payload.get("details")
    if not isinstance(listing, str) or not isinstance(details, list):
        raise FirstPartySourceError("Emply payload requires listing HTML and details")
    jobs = _emply_listing(listing, source)
    expected = {str(row.get("shortId") or "") for row in jobs}
    by_id = {str(row.get("id") or ""): row for row in details if isinstance(row, dict)}
    if set(by_id) != expected or "" in expected or len(by_id) != len(details):
        raise FirstPartySourceError("Emply details do not exactly match discovery")
    records = []
    for item in jobs:
        source_id = str(item["shortId"])
        slug = str(item.get("titleAsUrl") or "")
        url = f"https://{source.allowed_host}/ad/{slug}/{source_id}"
        detail = by_id[source_id]
        if detail.get("url") != url or not isinstance(detail.get("html"), str):
            raise FirstPartySourceError("Emply detail violates the exact tenant/path contract")
        parser = _EmplyDetailParser()
        parser.feed(detail["html"])
        if parser.container_count != 1 or len(parser.description) < 80 or not parser.title:
            raise FirstPartySourceError("Emply detail lacks exact complete job content")
        records.append(_base_record(
            source, source_id=source_id, url=url, title=parser.title,
            description=parser.description, location=str(item.get("location") or "") or None,
            date_posted=_iso_date(item.get("published")), valid_through=_iso_date(item.get("deadline")),
            remote="remote" in str(item.get("location") or "").casefold(),
            workplace_mode=item.get("location"), requisition_id=str(item.get("number") or "") or None,
        ))
    return records


class _PeopleAdminListingParser(HTMLParser):
    def __init__(self, source: FirstPartySource):
        super().__init__()
        self.source = source
        self.links: set[str] = set()
        self.page_links: set[str] = set()
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "a" or not dict(attrs).get("href"):
            return
        parsed = urlparse(urljoin(self.source.endpoint, dict(attrs)["href"]))
        if parsed.scheme != "https" or parsed.hostname != self.source.allowed_host:
            return
        if re.fullmatch(r"/postings/[1-9]\d*", parsed.path) and not parsed.query:
            self.links.add(urlunparse(parsed))
        if parsed.path == "/postings/search":
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            pages = [value for key, value in pairs if key == "page"]
            if len(pages) == 1 and re.fullmatch(r"[1-9]\d*", pages[0]):
                self.page_links.add(urlunparse(parsed))

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    @property
    def explicit_zero(self) -> bool:
        text = re.sub(r"\s+", " ", " ".join(self.text)).casefold()
        return "no postings found" in text or "no results found" in text


class _PeopleAdminDetailParser(HTMLParser):
    INCLUDED_FIELDS = (
        "Primary Purpose of Organizational Unit", "Position Summary",
        "Minimum Education and Experience Requirements",
        "Required Qualifications, Competencies, and Experience",
        "Preferred Qualifications, Competencies, and Experience",
    )

    def __init__(self):
        super().__init__()
        self.h2_depth = 0
        self.first_h2: list[str] = []
        self.in_row = False
        self.th_depth = 0
        self.td_depth = 0
        self.th: list[str] = []
        self.td: list[str] = []
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, _attrs) -> None:
        tag = tag.casefold()
        if tag == "h2" and not self.first_h2:
            self.h2_depth = 1
        elif self.h2_depth:
            self.h2_depth += 1
        if tag == "tr":
            self.in_row = True
            self.th, self.td = [], []
        elif self.in_row and tag == "th":
            self.th_depth = 1
        elif self.th_depth:
            self.th_depth += 1
        if self.in_row and tag == "td":
            self.td_depth = 1
        elif self.td_depth:
            self.td_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.h2_depth:
            self.h2_depth -= 1
        if self.th_depth:
            self.th_depth -= 1
        if self.td_depth:
            self.td_depth -= 1
        if tag == "tr" and self.in_row:
            key = _strip_html(" ".join(self.th))
            value = _strip_html(" ".join(self.td))
            if key and key not in self.fields:
                self.fields[key] = value
            self.in_row = False

    def handle_data(self, data: str) -> None:
        if self.h2_depth:
            self.first_h2.append(data)
        if self.th_depth:
            self.th.append(data)
        if self.td_depth:
            self.td.append(data)

    @property
    def title(self) -> str:
        return _strip_html(" ".join(self.first_h2))

    @property
    def description(self) -> str:
        return " ".join(
            f"{key}: {self.fields[key]}" for key in self.INCLUDED_FIELDS
            if self.fields.get(key)
        )


def peopleadmin_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != PEOPLEADMIN_ADAPTER or not isinstance(payload, dict):
        raise FirstPartySourceError("PeopleAdmin payload requires its reviewed adapter")
    listing = payload.get("listingHtml")
    listing_pages = payload.get("listingPages")
    details = payload.get("details")
    if listing_pages is None and isinstance(listing, str):
        listing_pages = [{"url": source.endpoint, "html": listing}]
    if not isinstance(listing_pages, list) or not listing_pages or not isinstance(details, list):
        raise FirstPartySourceError("PeopleAdmin payload requires listing pages and details")
    if len(listing_pages) + len(details) > source.max_requests_per_run:
        raise FirstPartySourceError(
            "PeopleAdmin payload exceeds its complete-run request cap"
        )
    batches = payload.get("requestBatches")
    if batches is not None and (
        not isinstance(batches, list)
        or not batches
        or any(
            not isinstance(batch, dict)
            or batch.get("listingRequests", 0) < 0
            or batch.get("detailRequests", 0) < 0
            or batch.get("listingRequests", 0) + batch.get("detailRequests", 0)
            > source.max_requests_per_batch
            for batch in batches
        )
        or sum(batch["listingRequests"] for batch in batches) != len(listing_pages)
        or sum(batch["detailRequests"] for batch in batches) != len(details)
        or len(listing_pages) + len(details) > source.max_requests_per_run
    ):
        raise FirstPartySourceError(
            "PeopleAdmin request batches violate the per-batch cap"
        )
    discovered: set[str] = set()
    supplied_pages = set()
    required_pages = set()
    explicit_zero = False
    for index, page in enumerate(listing_pages):
        if not isinstance(page, dict) or not isinstance(page.get("html"), str):
            raise FirstPartySourceError("PeopleAdmin listing page is malformed")
        page_url = str(page.get("url") or "")
        if index == 0 and page_url != source.endpoint:
            raise FirstPartySourceError("PeopleAdmin first listing URL changed")
        _https_exact_host(page_url, source.allowed_host, "PeopleAdmin listing URL")
        if page_url in supplied_pages:
            raise FirstPartySourceError("PeopleAdmin listing page is duplicated")
        supplied_pages.add(page_url)
        parser = _PeopleAdminListingParser(source)
        parser.feed(page["html"])
        discovered.update(parser.links)
        required_pages.update(parser.page_links)
        explicit_zero = explicit_zero or parser.explicit_zero
    if not required_pages <= supplied_pages:
        raise FirstPartySourceError("PeopleAdmin listing pagination is partial")
    if not discovered and not explicit_zero:
        raise FirstPartySourceError("PeopleAdmin filter yielded zero jobs without an explicit marker")
    _enforce_record_cap(list(discovered), source, "listing")
    by_url = {str(row.get("url") or ""): row.get("html") for row in details if isinstance(row, dict)}
    if set(by_url) != discovered or any(not isinstance(value, str) for value in by_url.values()):
        raise FirstPartySourceError("PeopleAdmin details do not exactly match filtered discovery")
    records = []
    for url in sorted(discovered):
        detail = _PeopleAdminDetailParser()
        detail.feed(by_url[url])
        source_id = urlparse(url).path.rsplit("/", 1)[-1]
        if not detail.title or len(detail.description) < 80:
            raise FirstPartySourceError("PeopleAdmin detail lacks reviewed job fields")
        records.append(_base_record(
            source, source_id=source_id, url=url, title=detail.title,
            description=detail.description,
            location=detail.fields.get("Work Location") or detail.fields.get("Position Location"),
            date_posted=_date_mdy(detail.fields.get("Posting Open Date")),
            valid_through=_date_mdy(detail.fields.get("Application Deadline")),
            remote="remote" in str(detail.fields.get("Work Location") or "").casefold(),
            workplace_mode=detail.fields.get("Work Location"),
            employment_type=detail.fields.get("Full-time/Part-time") or detail.fields.get("Position Type"),
            requisition_id=detail.fields.get("Vacancy ID"),
        ))
    return records


class _SelectMindsListingParser(HTMLParser):
    """Parse one exact-location SelectMinds result fragment."""

    def __init__(self, source: FirstPartySource, search_id: str):
        super().__init__()
        self.source = source
        self.search_id = search_id
        self.result_roots = 0
        self.links: set[str] = set()
        self.locations: list[str] = []
        self._location_depth = 0
        self._location_parts: list[str] = []
        self._page_count_depth = 0
        self._page_count_parts: list[str] = []
        self._current_page_depth = 0
        self._current_page_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).casefold(): value for key, value in attrs}
        classes = set(str(values.get("class") or "").split())
        nested = tag.casefold() not in {"area", "base", "br", "hr", "img", "input", "link", "meta"}
        if tag.casefold() == "div" and {"results_content", "jResultsContent"} <= classes:
            if (
                str(values.get("data-jsid") or "") != self.search_id
                or str(values.get("data-location-ids") or "") != "79"
            ):
                raise FirstPartySourceError(
                    "SelectMinds result escaped the exact filtered search/location contract"
                )
            self.result_roots += 1
        if tag.casefold() == "a" and "job_link" in classes:
            url = urljoin(self.source.endpoint, str(values.get("href") or ""))
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != self.source.allowed_host
                or not re.fullmatch(
                    r"/jobs/(?:[a-z0-9_-]|%[0-9A-Fa-f]{2})+-[1-9]\d*",
                    parsed.path,
                )
                or parsed.query
                or parsed.fragment
            ):
                raise FirstPartySourceError(
                    f"SelectMinds result contains an invalid job URL: {url}"
                )
            self.links.add(urlunparse(parsed))
        if self._location_depth:
            self._location_depth += int(nested)
        elif "location" in classes:
            self._location_depth = 1
            self._location_parts = []
        if values.get("id") == "jPaginateNumPages":
            self._page_count_depth = 1
        elif self._page_count_depth:
            self._page_count_depth += int(nested)
        if values.get("id") == "jPaginateCurrPage":
            self._current_page_depth = 1
        elif self._current_page_depth:
            self._current_page_depth += int(nested)

    def handle_endtag(self, _tag: str) -> None:
        if _tag.casefold() in {"area", "base", "br", "hr", "img", "input", "link", "meta"}:
            return
        if self._location_depth:
            self._location_depth -= 1
            if not self._location_depth:
                self.locations.append(_strip_html(" ".join(self._location_parts)))
        if self._page_count_depth:
            self._page_count_depth -= 1
        if self._current_page_depth:
            self._current_page_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._location_depth:
            self._location_parts.append(data)
        if self._page_count_depth:
            self._page_count_parts.append(data)
        if self._current_page_depth:
            self._current_page_parts.append(data)

    @property
    def page_count(self) -> int:
        value = _strip_html(" ".join(self._page_count_parts))
        match = re.fullmatch(r"([1-9]\d*)\.0", value)
        if not match:
            raise FirstPartySourceError("SelectMinds result lacks an exact page count")
        return int(match.group(1))

    @property
    def current_page(self) -> int:
        value = _strip_html(" ".join(self._current_page_parts))
        if not re.fullmatch(r"[1-9]\d*", value):
            raise FirstPartySourceError("SelectMinds result lacks an exact current page")
        return int(value)


class _SelectMindsDetailParser(HTMLParser):
    """Extract only the SelectMinds title, location, and job-description container."""

    def __init__(self):
        super().__init__()
        self.title_depth = 0
        self.location_depth = 0
        self.description_depth = 0
        self.title_parts: list[str] = []
        self.location_parts: list[str] = []
        self.description_parts: list[str] = []
        self.description_count = 0
        self.job_id: str | None = None
        self.requisition_id: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).casefold(): value for key, value in attrs}
        classes = set(str(values.get("class") or "").split())
        nested = tag.casefold() not in {"area", "base", "br", "hr", "img", "input", "link", "meta"}
        if tag.casefold() == "input":
            if values.get("name") == "Job.id":
                self.job_id = str(values.get("value") or "")
            elif values.get("name") == "Job.taleo_job_number":
                self.requisition_id = str(values.get("value") or "")
        if self.title_depth:
            self.title_depth += int(nested)
        elif tag.casefold() == "h1" and "title" in classes:
            self.title_depth = 1
        if self.location_depth:
            self.location_depth += int(nested)
        elif "primary_location" in classes:
            self.location_depth = 1
        if self.description_depth:
            self.description_depth += int(nested)
        elif tag.casefold() == "div" and "job_description" in classes:
            self.description_depth = 1
            self.description_count += 1

    def handle_endtag(self, _tag: str) -> None:
        if _tag.casefold() in {"area", "base", "br", "hr", "img", "input", "link", "meta"}:
            return
        if self.title_depth:
            self.title_depth -= 1
        if self.location_depth:
            self.location_depth -= 1
        if self.description_depth:
            self.description_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_parts.append(data)
        if self.location_depth:
            self.location_parts.append(data)
        if self.description_depth:
            self.description_parts.append(data)

    @property
    def title(self) -> str:
        return _strip_html(" ".join(self.title_parts))

    @property
    def location(self) -> str:
        return _strip_html(" ".join(self.location_parts))

    @property
    def description(self) -> str:
        return _strip_html(" ".join(self.description_parts))


def selectminds_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != SELECTMINDS_ADAPTER or not isinstance(payload, dict):
        raise FirstPartySourceError("SelectMinds payload requires its reviewed adapter")
    landing_html = payload.get("landingHtml")
    facet = payload.get("facetResponse")
    listings = payload.get("listingPages")
    details = payload.get("details")
    batches = payload.get("requestBatches")
    if (
        not isinstance(landing_html, str)
        or not isinstance(facet, dict)
        or not isinstance(listings, list)
        or not listings
        or not isinstance(details, list)
        or not isinstance(batches, list)
        or not batches
    ):
        raise FirstPartySourceError("SelectMinds payload is incomplete")
    base_ids = re.findall(r'data-jsid="([1-9]\d*)"', landing_html)
    if len(set(base_ids)) != 1:
        raise FirstPartySourceError("SelectMinds landing page lacks one base search ID")
    facet_paths = re.findall(
        rf'data-href="(/ajax/jobs/{base_ids[0]}/add/location/79)"', landing_html
    )
    if len(facet_paths) != 1 or "School of Medicine" not in landing_html:
        raise FirstPartySourceError("SelectMinds landing page lacks the exact Medicine facet")
    search_id = str(facet.get("Result") or "")
    total_text = str(facet.get("UserMessage") or "")
    if (
        facet.get("Status") != "OK"
        or not re.fullmatch(r"[1-9]\d*", search_id)
        or not re.fullmatch(r"\d+", total_text)
    ):
        raise FirstPartySourceError("SelectMinds facet response is malformed")
    total = int(total_text)
    if total > source.max_records_per_run:
        raise FirstPartySourceError("SelectMinds filtered result exceeds its record cap")
    if (
        any(
            not isinstance(batch, dict)
            or batch.get("listingRequests", 0) + batch.get("detailRequests", 0)
            > source.max_requests_per_batch
            or batch.get("listingRequests", 0) < 0
            or batch.get("detailRequests", 0) < 0
            for batch in batches
        )
        or sum(row["listingRequests"] for row in batches) != len(listings) + 2
        or sum(row["detailRequests"] for row in batches) != len(details)
        or len(listings) + len(details) + 2 > source.max_requests_per_run
    ):
        raise FirstPartySourceError("SelectMinds request batches violate the per-batch cap")
    discovered: set[str] = set()
    expected_pages = None
    supplied_pages = set()
    exact_location = "School of Medicine, Stanford, California, United States"
    for row in listings:
        if not isinstance(row, dict) or not isinstance(row.get("html"), str):
            raise FirstPartySourceError("SelectMinds listing page is malformed")
        parser = _SelectMindsListingParser(source, search_id)
        parser.feed(row["html"])
        if parser.result_roots != 1:
            raise FirstPartySourceError("SelectMinds listing lacks one filtered result root")
        if any(location != exact_location for location in parser.locations):
            raise FirstPartySourceError("SelectMinds listing escaped the School of Medicine facet")
        page = parser.current_page
        if page in supplied_pages:
            raise FirstPartySourceError("SelectMinds listing contains a duplicate page")
        supplied_pages.add(page)
        if expected_pages is None:
            expected_pages = parser.page_count
        elif parser.page_count != expected_pages:
            raise FirstPartySourceError("SelectMinds page count changed during pagination")
        overlap = discovered & parser.links
        if overlap:
            raise FirstPartySourceError("SelectMinds listing contains duplicate jobs")
        discovered.update(parser.links)
    if supplied_pages != set(range(1, (expected_pages or 0) + 1)):
        raise FirstPartySourceError("SelectMinds listing pagination is partial")
    if len(discovered) != total:
        raise FirstPartySourceError(
            f"SelectMinds listing is partial ({len(discovered)} of {total})"
        )
    by_url = {
        str(row.get("url") or ""): row.get("html")
        for row in details if isinstance(row, dict)
    }
    if set(by_url) != discovered or any(not isinstance(value, str) for value in by_url.values()):
        raise FirstPartySourceError("SelectMinds details do not exactly match filtered discovery")
    records = []
    for url in sorted(discovered):
        match = re.fullmatch(
            r"/jobs/(?:[a-z0-9_-]|%[0-9A-Fa-f]{2})+-([1-9]\d*)",
            urlparse(url).path,
        )
        detail_html = by_url[url]
        parser = _SelectMindsDetailParser()
        parser.feed(detail_html)
        detail_location = re.sub(r"^🔍\s*", "", parser.location)
        if (
            not match
            or parser.job_id != match.group(1)
            or parser.description_count != 1
            or detail_location != exact_location
            or len(parser.description) < 80
            or not parser.title
            or not re.fullmatch(r"[1-9]\d*", str(parser.requisition_id or ""))
            or not re.search(
                r'location:\s*\{\s*name:\s*"School of Medicine, Stanford, California, United States",\s*id:\s*"79"\s*\}',
                detail_html,
            )
        ):
            raise FirstPartySourceError(
                "SelectMinds detail violates its exact Medicine contract "
                f"(urlId={match.group(1) if match else None!r}, "
                f"jobId={parser.job_id!r}, requisitionId={parser.requisition_id!r}, "
                f"descriptions={parser.description_count}, "
                f"descriptionBytes={len(parser.description.encode('utf-8'))}, "
                f"location={detail_location!r}, titlePresent={bool(parser.title)})"
            )
        records.append(_base_record(
            source,
            source_id=parser.job_id,
            url=url,
            title=parser.title,
            description=parser.description,
            location=detail_location,
            date_posted=None,
            remote="remote" in parser.description.casefold(),
            workplace_mode=detail_location,
            requisition_id=parser.requisition_id,
        ))
    return records


def _drupal_rss_items(feed_xml: str, source: FirstPartySource) -> list[dict]:
    if not isinstance(feed_xml, str):
        raise FirstPartySourceError("Drupal RSS feed must be XML text")
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError as exc:
        raise FirstPartySourceError("Drupal RSS feed is malformed") from exc
    channel = root.find("channel") if root.tag == "rss" else None
    if (
        channel is None
        or (channel.findtext("title") or "").strip() != "New Career Opportunities at U-M"
        or (channel.findtext("link") or "").strip() != "https://careers.umich.edu/"
    ):
        raise FirstPartySourceError("Drupal RSS feed identity is malformed")
    rows = []
    for item in channel.findall("item"):
        url = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        title = (item.findtext("title") or "").strip()
        match = re.fullmatch(
            r"https://careers\.umich\.edu/job_detail/([1-9]\d*)/[a-z0-9][a-z0-9-]*",
            url,
        )
        title_match = re.fullmatch(r"(.+?) \(([1-9]\d*)\)", title)
        if not match or guid != url or not title_match or title_match.group(2) != match.group(1):
            raise FirstPartySourceError("Drupal RSS item violates its exact detail identity")
        try:
            posted = parsedate_to_datetime((item.findtext("pubDate") or "").strip())
        except (TypeError, ValueError) as exc:
            raise FirstPartySourceError("Drupal RSS item has an invalid publication date") from exc
        rows.append({
            "id": match.group(1), "url": url,
            "title": html.unescape(title_match.group(1)),
            "datePosted": posted.date().isoformat(),
        })
    _enforce_record_cap(rows, source, "Drupal RSS feed")
    return rows


def drupal_rss_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != DRUPAL_RSS_ADAPTER or not isinstance(payload, dict):
        raise FirstPartySourceError("Drupal RSS payload requires its reviewed adapter")
    rows = _drupal_rss_items(payload.get("feedXml"), source)
    details = payload.get("details")
    if not isinstance(details, list):
        raise FirstPartySourceError("Drupal RSS payload requires hydrated details")
    by_url = {
        str(row.get("url") or ""): row.get("html")
        for row in details if isinstance(row, dict)
    }
    if set(by_url) != {row["url"] for row in rows} or any(
        not isinstance(value, str) for value in by_url.values()
    ):
        raise FirstPartySourceError("Drupal RSS details do not exactly match the feed")
    records = []
    for row in rows:
        parser = _ClassContainerParser({"details-listing", "field_job_description"})
        parser.feed(by_url[row["url"]])
        if parser.container_count != 1 or len(parser.description) < 80:
            raise FirstPartySourceError("Drupal job detail lacks its exact content container")
        records.append(_base_record(
            source, source_id=row["id"], url=row["url"], title=row["title"],
            description=parser.description, location=None,
            date_posted=row["datePosted"], requisition_id=row["id"],
        ))
    return records


class _CnrsListingParser(HTMLParser):
    def __init__(self, source: FirstPartySource):
        super().__init__()
        self.source = source
        self.links: set[str] = set()
        self.heading_depth = 0
        self.heading_parts: list[str] = []
        self.headings: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        normalized_tag = tag.casefold()
        nested = normalized_tag not in {
            "area", "base", "br", "hr", "img", "input", "link", "meta",
        }
        if self.heading_depth:
            self.heading_depth += int(nested)
        elif normalized_tag == "h1":
            self.heading_depth = 1
            self.heading_parts = []

        href = dict(attrs).get("href")
        if normalized_tag != "a" or not href:
            return
        url = urljoin(self.source.endpoint, str(href))
        parsed = urlparse(url)
        if (
            parsed.scheme == "https"
            and parsed.hostname == self.source.allowed_host
            and re.fullmatch(
                r"/Offres/[A-Za-z]+/UAR76-[A-Z0-9-]+/Default\.aspx", parsed.path
            )
            and not parsed.query
            and not parsed.fragment
        ):
            self.links.add(urlunparse(parsed))

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {
            "area", "base", "br", "hr", "img", "input", "link", "meta",
        }:
            return
        if self.heading_depth:
            self.heading_depth -= 1
            if not self.heading_depth:
                self.headings.append(_strip_html(" ".join(self.heading_parts)))

    def handle_data(self, data: str) -> None:
        if self.heading_depth:
            self.heading_parts.append(data)

    @property
    def reviewed_empty_identity(self) -> bool:
        return "Les offres d'emploi de UAR76 (INIST)" in self.headings


class _CnrsDetailParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_depth = 0
        self.description_depth = 0
        self.description_count = 0
        self.title_parts: list[str] = []
        self.description_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        nested = tag.casefold() not in {"area", "base", "br", "hr", "img", "input", "link", "meta"}
        if self.title_depth:
            self.title_depth += int(nested)
        elif tag.casefold() == "h1" and not self.title_parts:
            self.title_depth = 1
        if self.description_depth:
            self.description_depth += int(nested)
        elif values.get("id") == "CphMain_FullOfferDisplay_Description":
            self.description_depth = 1
            self.description_count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"area", "base", "br", "hr", "img", "input", "link", "meta"}:
            return
        if self.title_depth:
            self.title_depth -= 1
        if self.description_depth:
            self.description_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_parts.append(data)
        if self.description_depth:
            self.description_parts.append(data)

    @property
    def title(self) -> str:
        return _strip_html(" ".join(self.title_parts))

    @property
    def description(self) -> str:
        return _strip_html(" ".join(self.description_parts))


def cnrs_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != CNRS_ADAPTER or not isinstance(payload, dict):
        raise FirstPartySourceError("CNRS payload requires its reviewed adapter")
    listing_html = payload.get("listingHtml")
    details = payload.get("details")
    if not isinstance(listing_html, str) or not isinstance(details, list):
        raise FirstPartySourceError("CNRS payload requires listing HTML and details")
    listing = _CnrsListingParser(source)
    listing.feed(listing_html)
    if not listing.links and not listing.reviewed_empty_identity:
        raise FirstPartySourceError(
            "CNRS unit listing yielded zero jobs without its reviewed UAR76/INIST identity"
        )
    _enforce_record_cap(list(listing.links), source, "CNRS unit listing")
    by_url = {
        str(row.get("url") or ""): row.get("html")
        for row in details if isinstance(row, dict)
    }
    if set(by_url) != listing.links or any(not isinstance(value, str) for value in by_url.values()):
        raise FirstPartySourceError("CNRS details do not exactly match its unit listing")
    records = []
    for url in sorted(listing.links):
        parser = _CnrsDetailParser()
        parser.feed(by_url[url])
        source_id = urlparse(url).path.split("/")[-2]
        if parser.description_count != 1 or not parser.title or len(parser.description) < 80:
            raise FirstPartySourceError("CNRS detail lacks its exact title/content container")
        records.append(_base_record(
            source, source_id=source_id, url=url, title=parser.title,
            description=parser.description, location=None, date_posted=None,
            remote="télétravail" in parser.description.casefold(),
            requisition_id=source_id,
        ))
    return records


def microsoft_research_records(payload, source: FirstPartySource) -> list[dict]:
    if source.adapter != MICROSOFT_RESEARCH_ADAPTER or not isinstance(payload, dict):
        raise FirstPartySourceError("Microsoft Research payload requires its reviewed adapter")
    pages = payload.get("listingPages")
    batches = payload.get("requestBatches")
    if not isinstance(pages, list) or not pages or not isinstance(batches, list):
        raise FirstPartySourceError("Microsoft Research payload is incomplete")
    total = None
    max_pages = None
    listings: dict[str, dict] = {}
    supplied_pages = set()
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("posts"), list):
            raise FirstPartySourceError("Microsoft Research listing page is malformed")
        if total is None:
            total, max_pages = page.get("foundPosts"), page.get("maxPages")
        if page.get("foundPosts") != total or page.get("maxPages") != max_pages:
            raise FirstPartySourceError("Microsoft Research totals changed during pagination")
        page_number = page.get("page")
        if not isinstance(page_number, int) or page_number < 1 or page_number in supplied_pages:
            raise FirstPartySourceError("Microsoft Research page identity is malformed")
        supplied_pages.add(page_number)
        for item in page["posts"]:
            source_id = str(item.get("id") or "") if isinstance(item, dict) else ""
            if (
                not re.fullmatch(r"[1-9]\d*", source_id)
                or item.get("type") != "msr-job-opportunity"
                or item.get("status") != "publish"
                or source_id in listings
            ):
                raise FirstPartySourceError("Microsoft Research listing item is malformed")
            slug = str(item.get("slug") or "")
            permalink = str(item.get("permalink") or "")
            research_url = (
                f"https://www.microsoft.com/en-us/research/opportunity/{slug}/"
            )
            apply_url = str(item.get("applyUrl") or "")
            url = str(item.get("recordUrl") or "")
            if (
                not re.fullmatch(r"(?:[a-z0-9-]|%[0-9a-f]{2})+", slug)
                or not permalink
                or permalink not in {research_url, apply_url}
                or url != (apply_url or research_url)
                or len(_strip_html(str(item.get("content") or ""))) < 80
            ):
                raise FirstPartySourceError(
                    "Microsoft Research complete API record violates its exact "
                    f"permalink/content contract (id={source_id!r}, slug={slug!r}, "
                    f"permalink={permalink!r}, recordUrl={url!r}, contentBytes="
                    f"{len(_strip_html(str(item.get('content') or '')).encode('utf-8'))})"
                )
            if apply_url and not re.fullmatch(
                r"https://apply\.careers\.microsoft\.com/careers/job/[1-9]\d*",
                apply_url,
            ):
                raise FirstPartySourceError("Microsoft Research apply URL escaped its exact host/path")
            listings[source_id] = item
    if (
        not isinstance(total, int) or total < 0 or total > source.max_records_per_run
        or not isinstance(max_pages, int) or max_pages < 1
        or supplied_pages != set(range(1, max_pages + 1))
        or len(listings) != total
    ):
        raise FirstPartySourceError("Microsoft Research listing pagination is partial")
    if (
        any(
            not isinstance(batch, dict)
            or batch.get("listingRequests", 0) + batch.get("detailRequests", 0)
            > source.max_requests_per_batch
            for batch in batches
        )
        or sum(row.get("listingRequests", 0) for row in batches) != len(pages)
        or sum(row.get("detailRequests", 0) for row in batches) != 0
        or len(pages) > source.max_requests_per_run
    ):
        raise FirstPartySourceError("Microsoft Research request batches violate the cap")
    records = []
    for source_id in sorted(listings, key=int):
        listing = listings[source_id]
        title = html.unescape(str(listing.get("title") or ""))
        description = str(listing.get("content") or "")
        location = " | ".join(
            str(value) for value in listing.get("locations", []) if value
        ) or None
        employment_type = " | ".join(
            str(value) for value in listing.get("opportunityTypes", []) if value
        ) or None
        apply_url = str(listing.get("applyUrl") or "")
        records.append(_base_record(
            source, source_id=source_id, url=listing["recordUrl"], title=title,
            description=description, location=location,
            date_posted=_iso_date(listing.get("date")), valid_through=None,
            remote="remote" in str(location or "").casefold(),
            workplace_mode=location, employment_type=employment_type,
            requisition_id=apply_url.rsplit("/", 1)[-1] if apply_url else None,
        ))
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
    if source.adapter == TEAMTAILOR_ADAPTER:
        return teamtailor_records(payload, source)
    if source.adapter == SAME_SITE_DETAIL_ADAPTER:
        return same_site_detail_records(payload, source)
    if source.adapter == WORKDAY_ADAPTER:
        return workday_records(payload, source)
    if source.adapter == WEBCRUITER_ADAPTER:
        return webcruiter_records(payload, source)
    if source.adapter == SUCCESSFACTORS_ADAPTER:
        return successfactors_records(payload, source)
    if source.adapter == UKG_ADAPTER:
        return ukg_records(payload, source)
    if source.adapter == SOFTGARDEN_ADAPTER:
        return softgarden_records(payload, source)
    if source.adapter == REFLINE_ADAPTER:
        return refline_records(payload, source)
    if source.adapter == EMPLY_ADAPTER:
        return emply_records(payload, source)
    if source.adapter == PEOPLEADMIN_ADAPTER:
        return peopleadmin_records(payload, source)
    if source.adapter == SELECTMINDS_ADAPTER:
        return selectminds_records(payload, source)
    if source.adapter == DRUPAL_RSS_ADAPTER:
        return drupal_rss_records(payload, source)
    if source.adapter == CNRS_ADAPTER:
        return cnrs_records(payload, source)
    if source.adapter == MICROSOFT_RESEARCH_ADAPTER:
        return microsoft_research_records(payload, source)
    raise FirstPartySourceError(f"unsupported first-party adapter {source.adapter!r}")


def request_count_from_payload(payload, source: FirstPartySource) -> int:
    """Return the actual logical HTTP requests represented by one raw payload."""

    if not isinstance(payload, dict):
        count = 1
    elif isinstance(payload.get("requestBatches"), list):
        try:
            count = sum(
                int(batch.get("listingRequests", 0))
                + int(batch.get("detailRequests", 0))
                for batch in payload["requestBatches"]
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise FirstPartySourceError("raw request batch evidence is malformed") from exc
    elif source.adapter == GRAPHWISE_ADAPTER:
        count = 2
    elif isinstance(payload.get("listingPages"), list):
        count = len(payload["listingPages"]) + len(payload.get("details") or [])
    elif "landingHtml" in payload and "facetResponse" in payload:
        count = 2 + len(payload.get("details") or [])
    elif "listingHtml" in payload or "careersHtml" in payload:
        count = 1 + len(payload.get("details") or [])
    elif "listing" in payload and isinstance(payload.get("details"), list):
        # Webcruiter performs a landing request plus its company-scoped API
        # listing request; the other listing/detail adapters use one listing.
        count = (
            2 if source.adapter == WEBCRUITER_ADAPTER else 1
        ) + len(payload["details"])
    elif "feedXml" in payload:
        count = 1 + len(payload.get("details") or [])
    else:
        count = 1
    if count < 1 or count > source.max_requests_per_run:
        raise FirstPartySourceError(
            f"{source.key} used {count} requests outside its complete-run cap "
            f"of {source.max_requests_per_run}"
        )
    return count


def _response_body(source: FirstPartySource, response) -> bytes:
    try:
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
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status is not None else type(exc).__name__
        raise FirstPartySourceError(f"{source.key} request failed: {detail}") from exc
    return bytes(body)


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
    except requests.RequestException as exc:
        raise FirstPartySourceError(f"{source.key} request failed: {type(exc).__name__}") from exc
    return _response_body(source, response)


def _fetch_body_from_hosts(
    source: FirstPartySource, endpoint: str, allowed_hosts: set[str]
) -> bytes:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https" or parsed.hostname not in allowed_hosts
        or parsed.username or parsed.password or parsed.fragment
    ):
        raise FirstPartySourceError(
            f"{source.key} detail URL escaped its reviewed hosts: {endpoint}"
        )
    try:
        response = requests.get(
            endpoint, timeout=source.timeout_seconds, allow_redirects=False,
            headers={"User-Agent": "OKG-first-party-jobs/1.0 (+https://openknowledgegraphs.com/)"},
            stream=True,
        )
    except requests.RequestException as exc:
        raise FirstPartySourceError(
            f"{source.key} request failed: {type(exc).__name__}"
        ) from exc
    return _response_body(source, response)


def _bounded_request_batches(
    listing_requests: int, detail_requests: int, batch_cap: int
) -> list[dict[str, int]]:
    """Describe a complete invocation as ordered batches within ``batch_cap``."""

    if listing_requests < 0 or detail_requests < 0 or batch_cap <= 0:
        raise FirstPartySourceError("request batch inputs must be non-negative")
    batches: list[dict[str, int]] = []
    remaining = {
        "listingRequests": listing_requests,
        "detailRequests": detail_requests,
    }
    while any(remaining.values()):
        batch = {
            "batch": len(batches) + 1,
            "listingRequests": 0,
            "detailRequests": 0,
        }
        capacity = batch_cap
        for field in ("listingRequests", "detailRequests"):
            taken = min(remaining[field], capacity)
            batch[field] = taken
            remaining[field] -= taken
            capacity -= taken
        batches.append(batch)
    return batches


def _fetch_workday(source: FirstPartySource) -> dict:
    parsed = urlparse(source.endpoint)
    target = urlunparse(parsed._replace(query=""))
    facets = {key: [value] for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
    page_size = min(20, source.max_records_per_run)
    listings = []
    discovered: dict[str, dict] = {}
    offset = 0
    total = None
    while total is None or offset < total:
        if len(listings) + 1 > source.max_requests_per_run:
            raise FirstPartySourceError("Workday listing pagination exceeds its request cap")
        try:
            response = requests.post(
                target,
                json={
                    "appliedFacets": facets,
                    "limit": page_size,
                    "offset": offset,
                    "searchText": "",
                },
                timeout=source.timeout_seconds,
                allow_redirects=False,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "OKG-first-party-jobs/1.0 (+https://openknowledgegraphs.com/)",
                },
                stream=True,
            )
        except requests.RequestException as exc:
            raise FirstPartySourceError(
                f"{source.key} request failed: {type(exc).__name__}"
            ) from exc
        body = _response_body(source, response)
        try:
            page = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FirstPartySourceError("Workday listing returned malformed JSON") from exc
        if not isinstance(page, dict) or not isinstance(page.get("jobPostings"), list):
            raise FirstPartySourceError("Workday listing response is malformed")
        if not isinstance(page.get("total"), int) or page["total"] < 0:
            raise FirstPartySourceError("Workday listing total is malformed")
        if total is None:
            total = page["total"]
            if total > source.max_records_per_run:
                raise FirstPartySourceError(
                    f"{source.key} Workday payload exceeds its record cap "
                    f"({total} > {source.max_records_per_run})"
                )
        elif page["total"] != total:
            raise FirstPartySourceError("Workday listing total changed during pagination")
        listings.append(page)
        for item in page["jobPostings"]:
            if not isinstance(item, dict) or not item.get("externalPath"):
                raise FirstPartySourceError("Workday listing entry is malformed")
            path = str(item["externalPath"])
            if path in discovered:
                raise FirstPartySourceError("Workday listing contains a duplicate job path")
            discovered[path] = item
        offset += len(page["jobPostings"])
        if offset < total and not page["jobPostings"]:
            raise FirstPartySourceError("Workday listing pagination made no progress")
    if len(listings) + len(discovered) > source.max_requests_per_run:
        raise FirstPartySourceError("Workday complete invocation exceeds its request cap")
    details = []
    request_batches = _bounded_request_batches(
        len(listings), len(discovered), source.max_requests_per_batch
    )
    base_path = parsed.path.removesuffix("/jobs")
    for external_path in sorted(discovered):
        detail_url = urlunparse(parsed._replace(
            path=f"{base_path}{external_path}", query=""
        ))
        body = _fetch_body(source, detail_url)
        try:
            detail = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FirstPartySourceError("Workday detail returned malformed JSON") from exc
        details.append({"externalPath": external_path, "payload": detail})
    return {
        "listingPages": listings,
        "details": details,
        "requestBatches": request_batches,
    }


def _fetch_webcruiter(source: FirstPartySource) -> dict:
    parsed = urlparse(source.endpoint)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if source.max_requests_per_run < 2:
        raise FirstPartySourceError("Webcruiter adapter requires listing requests within its cap")
    session = requests.Session()
    headers = {
        "User-Agent": "OKG-first-party-jobs/1.0 (+https://openknowledgegraphs.com/)"
    }
    try:
        landing_response = session.get(
            source.endpoint,
            timeout=source.timeout_seconds,
            allow_redirects=False,
            headers=headers,
            stream=True,
        )
    except requests.RequestException as exc:
        raise FirstPartySourceError(
            f"{source.key} request failed: {type(exc).__name__}"
        ) from exc
    try:
        landing = _response_body(source, landing_response).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FirstPartySourceError("Webcruiter page returned invalid UTF-8") from exc
    tokens = re.findall(
        r'name="__RequestVerificationToken"[^>]+value="([^"]+)"', landing
    )
    if len(tokens) != 1:
        raise FirstPartySourceError("Webcruiter page lacks one request-verification token")
    api_url = f"https://{source.allowed_host}/api/odvert/companysearch/{source.tenant}"
    form = {
        "page": 1,
        "pageSize": source.max_records_per_run,
        "skip": 0,
        "take": source.max_records_per_run,
        "sort[0][field]": "1",
        "sort[0][dir]": "desc",
    }
    try:
        response = session.post(
            api_url,
            data=form,
            timeout=source.timeout_seconds,
            allow_redirects=False,
            headers={
                **headers,
                "Referer": source.endpoint,
                "X-Accept-Language": "nb",
                "X-RequestVerificationToken": tokens[0],
                "X-Requested-With": "XMLHttpRequest",
            },
            stream=True,
        )
    except requests.RequestException as exc:
        raise FirstPartySourceError(
            f"{source.key} request failed: {type(exc).__name__}"
        ) from exc
    body = _response_body(source, response)
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirstPartySourceError("Webcruiter endpoint returned malformed JSON") from exc
    if not isinstance(payload, dict) or payload.get("LastFilterQuery") != (
        f"(companyIds/any(b: b eq '{query['companylock']}'))"
    ):
        raise FirstPartySourceError("Webcruiter response lacks the exact companylock filter")
    rows = payload.get("Data")
    if not isinstance(rows, list):
        raise FirstPartySourceError("Webcruiter response lacks its Data array")
    if len(rows) + 2 > source.max_requests_per_run:
        raise FirstPartySourceError("Webcruiter listing and details exceed its request cap")
    details = []
    detail_host = f"{source.tenant}.webcruiter.no"
    for item in rows:
        if not isinstance(item, dict):
            raise FirstPartySourceError("Webcruiter listing entry is malformed")
        job_id = str(item.get("Id") or "")
        detail_url = str(item.get("OpenAdvertUrl") or "")
        body = _fetch_body_from_hosts(source, detail_url, {detail_host})
        try:
            detail_html = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FirstPartySourceError("Webcruiter detail returned invalid UTF-8") from exc
        details.append({"id": job_id, "url": detail_url, "html": detail_html})
    return {"listing": payload, "details": details}


def _post_json(source: FirstPartySource, endpoint: str, payload: dict) -> dict:
    _https_exact_host(endpoint, source.allowed_host, "source endpoint")
    try:
        response = requests.post(
            endpoint, json=payload, timeout=source.timeout_seconds,
            allow_redirects=False,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "OKG-first-party-jobs/1.0 (+https://openknowledgegraphs.com/)",
            },
            stream=True,
        )
    except requests.RequestException as exc:
        raise FirstPartySourceError(
            f"{source.key} request failed: {type(exc).__name__}"
        ) from exc
    try:
        return json.loads(_response_body(source, response).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirstPartySourceError(f"{source.key} endpoint returned malformed JSON") from exc


def _fetch_successfactors(source: FirstPartySource) -> dict:
    listing = _post_json(source, source.endpoint, {
        "keywords": "", "locale": "en_GB", "location": "",
        "pageNumber": 0, "sortBy": "recent",
    })
    rows = listing.get("jobSearchResult") if isinstance(listing, dict) else None
    if not isinstance(rows, list) or listing.get("totalJobs") != len(rows):
        raise FirstPartySourceError("SuccessFactors result is malformed or paginated")
    if len(rows) > source.max_records_per_run or len(rows) + 1 > source.max_requests_per_run:
        raise FirstPartySourceError("SuccessFactors listing exceeds its reviewed caps")
    details = []
    for wrapper in rows:
        item = wrapper.get("response") if isinstance(wrapper, dict) else None
        if not isinstance(item, dict):
            raise FirstPartySourceError("SuccessFactors listing entry is malformed")
        job_id = str(item.get("id") or "")
        slug = str(item.get("urlTitle") or "")
        if not re.fullmatch(r"[1-9]\d*", job_id) or not re.fullmatch(r"[A-Za-z0-9%()_-]+", slug):
            raise FirstPartySourceError("SuccessFactors listing escaped the exact ID/slug contract")
        url = f"https://{source.allowed_host}/job/{slug}/{job_id}-en_GB/"
        details.append({"id": job_id, "url": url, "html": _fetch_html(source, url)})
    return {"listing": listing, "details": details}


def _fetch_ukg(source: FirstPartySource) -> dict:
    listing = _post_json(source, source.endpoint, {
        "opportunitySearch": {
            "QueryString": "", "Filters": [],
            "Top": source.max_records_per_run, "Skip": 0,
            "OrderBy": [{
                "Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False,
            }],
        }
    })
    rows = listing.get("opportunities") if isinstance(listing, dict) else None
    if not isinstance(rows, list) or listing.get("totalCount") != len(rows):
        raise FirstPartySourceError("UKG result is malformed or exceeds one bounded page")
    if len(rows) > source.max_records_per_run or len(rows) + 1 > source.max_requests_per_run:
        raise FirstPartySourceError("UKG listing exceeds its reviewed caps")
    parsed = urlparse(source.endpoint)
    board_prefix = parsed.path.split("/JobBoardView/", 1)[0]
    details = []
    for item in rows:
        job_id = str(item.get("Id") or "") if isinstance(item, dict) else ""
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", job_id):
            raise FirstPartySourceError("UKG listing contains an invalid opportunity ID")
        url = urlunparse(parsed._replace(
            path=f"{board_prefix}/OpportunityDetail", query=urlencode({"opportunityId": job_id})
        ))
        detail_html = _fetch_html(source, url)
        match = re.search(
            r"new US\.Opportunity\.CandidateOpportunityDetail\((\{.*?\})\);",
            detail_html, re.DOTALL,
        )
        if not match:
            raise FirstPartySourceError("UKG detail lacks its exact candidate-opportunity payload")
        try:
            details.append(json.loads(match.group(1)))
        except json.JSONDecodeError as exc:
            raise FirstPartySourceError("UKG detail payload is malformed") from exc
    return {"listing": listing, "details": details}


def _fetch_softgarden(source: FirstPartySource) -> dict:
    listing_html = _fetch_html(source, source.endpoint)
    parser = _SoftgardenListingParser(source)
    parser.feed(listing_html)
    if len(parser.links) + 1 > source.max_requests_per_run:
        raise FirstPartySourceError("Softgarden listing and details exceed its request cap")
    return {
        "listingHtml": listing_html,
        "details": [
            {"url": url, "html": _fetch_html(source, url)}
            for url in sorted(parser.links.values())
        ],
    }


def _fetch_refline(source: FirstPartySource) -> dict:
    parsed = urlparse(source.endpoint)
    endpoint = urlunparse(parsed._replace(query="target=search"))
    try:
        response = requests.post(
            endpoint, data={"department": "sib", "workplace": "all"},
            timeout=source.timeout_seconds, allow_redirects=False,
            headers={"User-Agent": "OKG-first-party-jobs/1.0 (+https://openknowledgegraphs.com/)"},
            stream=True,
        )
    except requests.RequestException as exc:
        raise FirstPartySourceError(f"{source.key} request failed: {type(exc).__name__}") from exc
    try:
        listing_html = _response_body(source, response).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FirstPartySourceError("Refline filter returned invalid UTF-8") from exc
    parser = _ReflineListingParser(source)
    parser.feed(listing_html)
    if len(parser.links) + 1 > source.max_requests_per_run:
        raise FirstPartySourceError("Refline listing and details exceed its request cap")
    return {
        "listingHtml": listing_html,
        "details": [{"url": url, "html": _fetch_html(source, url)} for url in sorted(parser.links)],
    }


def _fetch_emply(source: FirstPartySource) -> dict:
    listing_html = _fetch_html(source, source.endpoint)
    jobs = _emply_listing(listing_html, source)
    if len(jobs) + 1 > source.max_requests_per_run:
        raise FirstPartySourceError("Emply listing and details exceed its request cap")
    details = []
    for item in jobs:
        source_id = str(item.get("shortId") or "")
        slug = str(item.get("titleAsUrl") or "")
        if not re.fullmatch(r"[a-z0-9]+", source_id) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            raise FirstPartySourceError("Emply listing contains an invalid job identity")
        url = f"https://{source.allowed_host}/ad/{slug}/{source_id}"
        details.append({"id": source_id, "url": url, "html": _fetch_html(source, url)})
    return {"listingHtml": listing_html, "details": details}


def _fetch_peopleadmin(source: FirstPartySource) -> dict:
    listing_pages = []
    pending = [source.endpoint]
    visited = set()
    links = set()
    request_batches = []

    def note_request(field: str) -> None:
        if sum(
            batch["listingRequests"] + batch["detailRequests"]
            for batch in request_batches
        ) >= source.max_requests_per_run:
            raise FirstPartySourceError(
                "PeopleAdmin complete invocation exceeds its request cap"
            )
        if (
            not request_batches
            or request_batches[-1]["listingRequests"]
            + request_batches[-1]["detailRequests"]
            >= source.max_requests_per_batch
        ):
            request_batches.append({
                "batch": len(request_batches) + 1,
                "listingRequests": 0,
                "detailRequests": 0,
            })
        request_batches[-1][field] += 1

    while pending:
        url = pending.pop(0)
        if url in visited:
            continue
        if len(visited) + 1 > source.max_records_per_run:
            raise FirstPartySourceError(
                "PeopleAdmin pagination exceeds its reviewed page/record bound"
            )
        note_request("listingRequests")
        listing_html = _fetch_html(source, url)
        visited.add(url)
        listing_pages.append({"url": url, "html": listing_html})
        parser = _PeopleAdminListingParser(source)
        parser.feed(listing_html)
        links.update(parser.links)
        pending.extend(sorted(parser.page_links - visited - set(pending)))
    _enforce_record_cap(list(links), source, "listing")
    details = []
    for url in sorted(links):
        note_request("detailRequests")
        details.append({"url": url, "html": _fetch_html(source, url)})
    if len(listing_pages) + len(details) > source.max_requests_per_run:
        raise FirstPartySourceError(
            "PeopleAdmin complete invocation exceeds its request cap"
        )
    return {
        "listingPages": listing_pages,
        "details": details,
        "requestBatches": request_batches,
    }


def _fetch_selectminds(source: FirstPartySource) -> dict:
    session = requests.Session()
    headers = {
        # This reviewed SelectMinds tenant rejects the generic OKG agent but
        # serves its public board to curl-compatible clients. Keep the
        # source-specific transport identity local to this exact adapter.
        "User-Agent": "curl/8.7.1"
    }

    def request(
        method: str,
        url: str,
        *,
        token: str | None = None,
        allow_initial_search_redirect: bool = False,
    ) -> bytes:
        _https_exact_host(url, source.allowed_host, "SelectMinds request URL")
        request_headers = dict(headers)
        if token:
            request_headers.update({
                "Referer": source.endpoint,
                "X-Requested-With": "XMLHttpRequest",
                "tss-token": token,
            })
        try:
            response = session.request(
                method, url, timeout=source.timeout_seconds, allow_redirects=False,
                headers=request_headers, stream=True,
            )
        except requests.RequestException as exc:
            raise FirstPartySourceError(
                f"{source.key} request failed: {type(exc).__name__}"
            ) from exc
        if allow_initial_search_redirect and response.status_code == 307:
            location = response.headers.get("Location") or ""
            redirect_url = urljoin(url, location)
            parsed_redirect = urlparse(redirect_url)
            if (
                parsed_redirect.scheme != "https"
                or parsed_redirect.hostname != source.allowed_host
                or parsed_redirect.username
                or parsed_redirect.password
                or parsed_redirect.query
                or parsed_redirect.fragment
                or not re.fullmatch(r"/jobs/search/[1-9]\d*", parsed_redirect.path)
            ):
                response.close()
                raise FirstPartySourceError(
                    "SelectMinds landing redirect escaped its exact search contract"
                )
            response.close()
            try:
                response = session.request(
                    method,
                    redirect_url,
                    timeout=source.timeout_seconds,
                    allow_redirects=False,
                    headers=request_headers,
                    stream=True,
                )
            except requests.RequestException as exc:
                raise FirstPartySourceError(
                    f"{source.key} request failed: {type(exc).__name__}"
                ) from exc
        return _response_body(source, response)

    try:
        landing_html = request(
            "GET", source.endpoint, allow_initial_search_redirect=True
        ).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FirstPartySourceError("SelectMinds landing page returned invalid UTF-8") from exc
    tokens = re.findall(r'id\s*=\s*"tsstoken"\s+value\s*=\s*"([^"]+)"', landing_html)
    base_ids = re.findall(r'data-jsid="([1-9]\d*)"', landing_html)
    if len(tokens) != 1 or len(set(base_ids)) != 1:
        raise FirstPartySourceError("SelectMinds landing page lacks one token/search ID")
    base_id = base_ids[0]
    facet_path = f"/ajax/jobs/{base_id}/add/location/79"
    if landing_html.count(f'data-href="{facet_path}"') != 1:
        raise FirstPartySourceError("SelectMinds landing page lacks the exact Medicine facet")
    facet_url = f"https://{source.allowed_host}{facet_path}?uid=1"
    try:
        facet = json.loads(request("POST", facet_url, token=tokens[0]).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirstPartySourceError("SelectMinds facet endpoint returned malformed JSON") from exc
    search_id = str(facet.get("Result") or "") if isinstance(facet, dict) else ""
    total = str(facet.get("UserMessage") or "") if isinstance(facet, dict) else ""
    if facet.get("Status") != "OK" or not re.fullmatch(r"[1-9]\d*", search_id) or not total.isdigit():
        raise FirstPartySourceError("SelectMinds facet endpoint returned an invalid result")
    if int(total) > source.max_records_per_run:
        raise FirstPartySourceError("SelectMinds filtered result exceeds its record cap")
    listing_pages = []
    page = 1
    page_count = None
    discovered: set[str] = set()
    while page_count is None or page <= page_count:
        if len(listing_pages) + 3 > source.max_requests_per_run:
            raise FirstPartySourceError(
                "SelectMinds listing invocation exceeds its request cap"
            )
        query = {
            "JobSearch.id": search_id,
            "site-name": "default1696",
            "include_site": "true",
            "uid": str(page + 1),
        }
        if page > 1:
            query["page_index"] = str(page)
        results_url = (
            f"https://{source.allowed_host}/ajax/content/job_results?{urlencode(query)}"
        )
        try:
            wrapper = json.loads(request("POST", results_url, token=tokens[0]).decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FirstPartySourceError("SelectMinds results endpoint returned malformed JSON") from exc
        html_fragment = wrapper.get("Result") if isinstance(wrapper, dict) else None
        if wrapper.get("Status") != "OK" or not isinstance(html_fragment, str):
            raise FirstPartySourceError("SelectMinds results endpoint returned an invalid result")
        parser = _SelectMindsListingParser(source, search_id)
        parser.feed(html_fragment)
        if parser.result_roots != 1 or parser.current_page != page:
            raise FirstPartySourceError("SelectMinds results page identity is malformed")
        if page_count is None:
            page_count = parser.page_count
        elif parser.page_count != page_count:
            raise FirstPartySourceError("SelectMinds page count changed during pagination")
        if discovered & parser.links:
            raise FirstPartySourceError("SelectMinds results contain duplicate jobs")
        discovered.update(parser.links)
        listing_pages.append({"page": page, "url": results_url, "html": html_fragment})
        page += 1
    if len(discovered) != int(total):
        raise FirstPartySourceError(
            f"SelectMinds listing is partial ({len(discovered)} of {total})"
        )
    total_requests = len(listing_pages) + len(discovered) + 2
    if total_requests > source.max_requests_per_run:
        raise FirstPartySourceError(
            "SelectMinds complete invocation exceeds its request cap"
        )
    request_batches = _bounded_request_batches(
        len(listing_pages) + 2,
        len(discovered),
        source.max_requests_per_batch,
    )
    details = []
    for url in sorted(discovered):
        try:
            detail_html = request("GET", url).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FirstPartySourceError("SelectMinds detail returned invalid UTF-8") from exc
        details.append({"url": url, "html": detail_html})
    return {
        "landingHtml": landing_html,
        "facetResponse": facet,
        "listingPages": listing_pages,
        "details": details,
        "requestBatches": request_batches,
    }


def _fetch_drupal_rss(source: FirstPartySource) -> dict:
    try:
        feed_xml = _fetch_body(source, source.endpoint).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FirstPartySourceError("Drupal RSS feed returned invalid UTF-8") from exc
    rows = _drupal_rss_items(feed_xml, source)
    if len(rows) + 1 > source.max_requests_per_run:
        raise FirstPartySourceError("Drupal RSS feed/details exceed its request cap")
    return {
        "feedXml": feed_xml,
        "details": [
            {"url": row["url"], "html": _fetch_html(source, row["url"])}
            for row in rows
        ],
    }


def _fetch_cnrs(source: FirstPartySource) -> dict:
    listing_html = _fetch_html(source, source.endpoint)
    parser = _CnrsListingParser(source)
    parser.feed(listing_html)
    if len(parser.links) + 1 > source.max_requests_per_run:
        raise FirstPartySourceError("CNRS listing/details exceed its request cap")
    return {
        "listingHtml": listing_html,
        "details": [
            {"url": url, "html": _fetch_html(source, url)}
            for url in sorted(parser.links)
        ],
    }


def _microsoft_listing_item(wrapper) -> dict:
    item = wrapper.get("data") if isinstance(wrapper, dict) else None
    if not isinstance(item, dict):
        raise FirstPartySourceError("Microsoft Research API item is malformed")
    meta = item.get("meta")
    apply_values = meta.get("msr_opportunity_hta") if isinstance(meta, dict) else None
    apply_url = None
    if isinstance(apply_values, list):
        nonempty = [
            row.get("value") for row in apply_values
            if isinstance(row, dict) and row.get("value")
        ]
        if len(nonempty) > 1:
            raise FirstPartySourceError(
                "Microsoft Research API item has multiple application URLs"
            )
        apply_url = nonempty[0] if nonempty else None

    def term_names(taxonomy: str) -> list[str]:
        terms = item.get("terms")
        values = terms.get(taxonomy) if isinstance(terms, dict) else None
        if not isinstance(values, list):
            return []
        names = []
        for value in values:
            name = value.get("name") if isinstance(value, dict) else None
            if isinstance(name, str) and name and name not in names:
                names.append(name)
        return names

    publication_values = (
        meta.get("msr_opportunity_pubdate") if isinstance(meta, dict) else None
    )
    publication_date = None
    if isinstance(publication_values, list) and len(publication_values) == 1:
        row = publication_values[0]
        if isinstance(row, dict) and row.get("value"):
            publication_date = row["value"]
    slug = str(item.get("post_name") or "")
    research_url = (
        f"https://www.microsoft.com/en-us/research/opportunity/{slug}/"
    )
    return {
        "applyUrl": apply_url,
        "content": item.get("post_content"),
        "date": publication_date or item.get("post_date_gmt"),
        "id": item.get("ID"),
        "locations": term_names("msr-city"),
        "opportunityTypes": term_names("msr-job-opportunity-type"),
        "permalink": item.get("permalink"),
        "recordUrl": apply_url or research_url,
        "slug": slug,
        "status": item.get("post_status"),
        "title": item.get("post_title"),
        "type": item.get("post_type"),
    }


def _fetch_microsoft_research(source: FirstPartySource) -> dict:
    parsed = urlparse(source.endpoint)
    base_query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    listing_pages = []
    page = 1
    max_pages = None
    discovered: dict[str, dict] = {}
    while max_pages is None or page <= max_pages:
        if len(listing_pages) + 1 > source.max_requests_per_run:
            raise FirstPartySourceError(
                "Microsoft Research listing exceeds its reviewed request cap"
            )
        query = {**base_query, "page": str(page)}
        url = urlunparse(parsed._replace(query=urlencode(query)))
        body = _fetch_body(source, url)
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FirstPartySourceError("Microsoft Research API returned malformed JSON") from exc
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("posts"), list)
            or not isinstance(payload.get("found_posts"), int)
            or not isinstance(payload.get("max_num_pages"), int)
            or payload.get("page") != page
        ):
            raise FirstPartySourceError("Microsoft Research API page is malformed")
        if max_pages is None:
            max_pages = payload["max_num_pages"]
            if payload["found_posts"] > source.max_records_per_run:
                raise FirstPartySourceError("Microsoft Research result exceeds its record cap")
        compact_posts = [_microsoft_listing_item(item) for item in payload["posts"]]
        listing_pages.append({
            "foundPosts": payload["found_posts"],
            "maxPages": payload["max_num_pages"],
            "page": page,
            "posts": compact_posts,
        })
        for item in compact_posts:
            source_id = str(item.get("id") or "")
            if not source_id or source_id in discovered:
                raise FirstPartySourceError("Microsoft Research API returned duplicate identities")
            discovered[source_id] = item
        page += 1
    if len(discovered) != listing_pages[0]["foundPosts"]:
        raise FirstPartySourceError("Microsoft Research API pagination is partial")
    if len(listing_pages) > source.max_requests_per_run:
        raise FirstPartySourceError(
            "Microsoft Research listing exceeds its reviewed request cap"
        )
    return {
        "listingPages": listing_pages,
        "requestBatches": _bounded_request_batches(
            len(listing_pages), 0, source.max_requests_per_batch
        ),
    }


def _fetch_html(source: FirstPartySource, url: str) -> str:
    try:
        return _fetch_body(source, url).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FirstPartySourceError(f"{source.key} returned invalid UTF-8 HTML") from exc


def _fetch_bounded_detail_source(source: FirstPartySource) -> dict:
    listing_pages = []
    pending = [source.endpoint]
    visited: set[str] = set()
    job_links: set[str] = set()
    while pending:
        page_url = pending.pop(0)
        if page_url in visited:
            continue
        if len(visited) + 1 > source.max_requests_per_run:
            raise FirstPartySourceError(f"{source.key} listing pages exceed its request cap")
        html_payload = _fetch_html(source, page_url)
        visited.add(page_url)
        listing_pages.append({"url": page_url, "html": html_payload})
        parser = _listing_parser(html_payload, source)
        job_links.update(parser.job_links)
        if source.adapter == TEAMTAILOR_ADAPTER:
            pending.extend(sorted(parser.page_links - visited - set(pending)))
    _enforce_record_cap(list(job_links), source, "HTML discovery")
    if len(visited) + len(job_links) > source.max_requests_per_run:
        raise FirstPartySourceError(
            f"{source.key} listing and detail requests exceed its request cap"
        )
    details = [
        {"url": url, "html": _fetch_html(source, url)}
        for url in sorted(job_links)
    ]
    return {"listingPages": listing_pages, "details": details}


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
    if source.adapter == WORKDAY_ADAPTER:
        return _fetch_workday(source)
    if source.adapter == WEBCRUITER_ADAPTER:
        return _fetch_webcruiter(source)
    if source.adapter == SUCCESSFACTORS_ADAPTER:
        return _fetch_successfactors(source)
    if source.adapter == UKG_ADAPTER:
        return _fetch_ukg(source)
    if source.adapter == SOFTGARDEN_ADAPTER:
        return _fetch_softgarden(source)
    if source.adapter == REFLINE_ADAPTER:
        return _fetch_refline(source)
    if source.adapter == EMPLY_ADAPTER:
        return _fetch_emply(source)
    if source.adapter == PEOPLEADMIN_ADAPTER:
        return _fetch_peopleadmin(source)
    if source.adapter == SELECTMINDS_ADAPTER:
        return _fetch_selectminds(source)
    if source.adapter == DRUPAL_RSS_ADAPTER:
        return _fetch_drupal_rss(source)
    if source.adapter == CNRS_ADAPTER:
        return _fetch_cnrs(source)
    if source.adapter == MICROSOFT_RESEARCH_ADAPTER:
        return _fetch_microsoft_research(source)
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
    if source.adapter in {TEAMTAILOR_ADAPTER, SAME_SITE_DETAIL_ADAPTER}:
        return _fetch_bounded_detail_source(source)
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
