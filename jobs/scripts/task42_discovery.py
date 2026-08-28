#!/usr/bin/env python3
"""Capture bounded, cited provider discovery evidence for Task 42 careers pages."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

import requests

from task42_source_audit import fixed_cohort


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "audits" / "task42-careers-discovery.json"
MAX_RESPONSE_BYTES = 2_000_000
REQUEST_TIMEOUT_SECONDS = 20
MAX_REDIRECTS = 5
MAX_WORKERS = 8
MAX_SECONDARY_PROBES = 2
JOBISH_URL = re.compile(
    r"(?:job|career|vacan|recruit|emploi|stellen|stilling|position|work-with|"
    r"allas|concurs|convoc|trabaj|opportunit)", re.IGNORECASE,
)
SKIP_SUFFIXES = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".map", ".xml",
)

SUPPLEMENTAL_PROBES = {
    "defense-logistics-agency": {
        "method": "GET",
        "url": "https://dla.usajobs.gov/search/results",
        "reviewConclusion": "unsupported: branded DLA portal is dynamic and the bounded USAJobs data API requires credentials",
    },
    "helsinki-university-library": {
        "method": "GET",
        "url": "https://jobs.helsinki.fi/?locale=en_GB",
        "reviewConclusion": "unsupported: SuccessFactors board is University of Helsinki-wide and exposes no reviewed library organization filter",
    },
    "library-of-congress": {
        "method": "GET",
        "url": "https://www.loc.gov/search/?fa=partof:careers&fo=json&c=1",
        "reviewConclusion": "unsupported: exact LOC careers collection endpoint returns HTTP 403 to the bounded client; USAJobs data API requires credentials",
    },
    "metropolitan-museum-of-art": {
        "method": "POST",
        "url": "https://metmuseum.wd5.myworkdayjobs.com/wday/cxs/metmuseum/metmuseumcareers/jobs",
        "json": {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
        "reviewConclusion": "supported: exact Met Workday tenant/site CXS endpoint",
    },
    "stanford-university-school-of-medicine": {
        "method": "GET",
        "url": "https://stanford.referrals.selectminds.com/page/school-of-medicine-65",
        "reviewConclusion": "unsupported: prior orgIds=27938 portal redirects to HTTP 404 and the replacement page does not preserve an exact School of Medicine filter",
    },
}

PROVIDER_HOST_MARKERS = (
    ("greenhouse", ("greenhouse.io",)),
    ("lever", ("lever.co",)),
    ("ashby", ("ashbyhq.com",)),
    ("teamtailor", ("teamtailor.com",)),
    ("workday", ("myworkdayjobs.com",)),
    ("webcruiter", ("webcruiter.com",)),
    ("successfactors", ("successfactors.com", "jobs.helsinki.fi", "jobs.open.ac.uk")),
    ("usajobs", ("usajobs.gov",)),
    ("icims", ("icims.com",)),
    ("taleo", ("taleo.net",)),
    ("softgarden", ("softgarden.io",)),
    ("peopleadmin", ("peopleadmin.com",)),
    ("ukg", ("ultipro.com", "ukg.com")),
    ("emply", ("emply.com",)),
    ("ciphr", ("ciphr-irecruit.com", "ciphr.com")),
    ("hrworks", ("hrworks.de",)),
    ("refline", ("refline.ch",)),
    ("nga", ("nga.net.au",)),
    ("phenom", ("phenompeople.com",)),
    ("microsoft-careers", ("jobs.careers.microsoft.com", "apply.careers.microsoft.com")),
    ("cornerstone", ("csod.com",)),
    ("interfolio", ("interfolio.com",)),
    ("smartrecruiters", ("smartrecruiters.com",)),
)


class LinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: set[str] = set()
        self.json_ld: list[str] = []
        self._json_ld_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).casefold(): value for key, value in attrs}
        if tag.casefold() in {"a", "link", "iframe", "script", "form"}:
            for key in ("href", "src", "action"):
                value = values.get(key)
                if value:
                    url = urljoin(self.base_url, str(value).strip())
                    parsed = urlparse(url)
                    if parsed.scheme in {"http", "https"} and parsed.hostname:
                        self.links.add(urlunparse(parsed._replace(fragment="")))
        if (
            tag.casefold() == "script"
            and str(values.get("type") or "").casefold() == "application/ld+json"
        ):
            self._json_ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._json_ld_parts is not None:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._json_ld_parts is not None:
            self.json_ld.append("".join(self._json_ld_parts))
            self._json_ld_parts = None


def _provider(url: str) -> str | None:
    host = (urlparse(url).hostname or "").casefold()
    for provider, markers in PROVIDER_HOST_MARKERS:
        if any(host == marker or host.endswith(f".{marker}") for marker in markers):
            return provider
    return None


def _organization_filter(url: str) -> dict | None:
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    relevant = {
        key: value for key, value in pairs
        if key.casefold() in {
            "a", "company", "companyid", "companylock", "department",
            "departments", "employer", "filter", "orgid", "orgids", "q",
            "search", "tenant", "unit", "where",
        }
    }
    return relevant or None


def _jobposting_count(blocks: list[str]) -> int:
    count = 0
    for block in blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, dict):
                kind = value.get("@type")
                kinds = kind if isinstance(kind, list) else [kind]
                if "JobPosting" in kinds:
                    count += 1
                stack.extend(value.values())
    return count


def _specific_reason(status: int | None, error: str | None, candidates: list[dict], jobposting_count: int, link_count: int, content_type: str | None) -> str:
    if error:
        return f"retrieval-failed:{error}"
    if status is None:
        return "retrieval-failed:no-http-status"
    if status >= 400:
        return f"careers-page-http-{status}"
    if jobposting_count:
        return f"retrieved-schema-jobposting:{jobposting_count}"
    if candidates:
        providers = ",".join(sorted({row["provider"] for row in candidates}))
        return f"retrieved-recognized-provider:{providers}"
    if content_type and "html" not in content_type.casefold():
        return f"retrieved-non-html:{content_type}"
    return f"retrieved-html-with-{link_count}-links:no-recognized-provider-or-jobposting"


def _secondary_probe(url: str) -> dict:
    result = {
        "requestedUrl": url, "finalUrl": None, "httpStatus": None,
        "contentType": None, "responseBytes": 0, "contentSha256": None,
        "retrievalError": None, "provider": _provider(url),
        "organizationFilter": _organization_filter(url),
        "jobPostingCount": 0, "linkCount": 0,
    }
    try:
        response = requests.get(
            url, allow_redirects=True,
            headers={"User-Agent": "OKG-career-source-review/1.0 (+https://openknowledgegraphs.com/)"},
            stream=True, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"response-exceeded-{MAX_RESPONSE_BYTES}-byte-cap")
        result.update({
            "contentSha256": hashlib.sha256(body).hexdigest(),
            "contentType": response.headers.get("Content-Type"),
            "finalUrl": response.url, "httpStatus": response.status_code,
            "responseBytes": len(body),
            "provider": _provider(response.url) or result["provider"],
            "organizationFilter": _organization_filter(response.url)
                or result["organizationFilter"],
        })
        if "html" in str(result["contentType"] or "").casefold():
            parser = LinkParser(response.url)
            parser.feed(body.decode(response.encoding or "utf-8", errors="replace"))
            result["jobPostingCount"] = _jobposting_count(parser.json_ld)
            result["linkCount"] = len(parser.links)
    except (requests.RequestException, RuntimeError) as exc:
        result["retrievalError"] = f"{type(exc).__name__}:{exc}"
    return result


def _probe_candidates(requested_url: str, final_url: str, links: set[str]) -> list[str]:
    distinct = []
    for url in links:
        parsed = urlparse(url)
        if (
            url in {requested_url, final_url}
            or parsed.path.casefold().endswith(SKIP_SUFFIXES)
            or parsed.scheme not in {"http", "https"}
        ):
            continue
        provider = _provider(url)
        if provider or JOBISH_URL.search(url):
            distinct.append(url)
    return sorted(
        set(distinct),
        key=lambda url: (
            0 if _provider(url) else 1,
            0 if _organization_filter(url) else 1,
            len(url), url,
        ),
    )[:MAX_SECONDARY_PROBES]


def inspect_page(row: dict) -> dict:
    requested_url = row["careersPage"]
    result = {
        "careersPage": requested_url,
        "contentSha256": None,
        "contentType": None,
        "finalUrl": None,
        "httpStatus": None,
        "identifier": row["identifier"],
        "jobPostingCount": 0,
        "linkCount": 0,
        "providerCandidates": [],
        "responseBytes": 0,
        "retrievalError": None,
    }
    try:
        session = requests.Session()
        session.max_redirects = MAX_REDIRECTS
        response = session.get(
            requested_url,
            allow_redirects=True,
            headers={"User-Agent": "OKG-career-source-review/1.0 (+https://openknowledgegraphs.com/)"},
            stream=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"response-exceeded-{MAX_RESPONSE_BYTES}-byte-cap")
        result.update({
            "contentSha256": hashlib.sha256(body).hexdigest(),
            "contentType": response.headers.get("Content-Type"),
            "finalUrl": response.url,
            "httpStatus": response.status_code,
            "responseBytes": len(body),
        })
        text = body.decode(response.encoding or "utf-8", errors="replace")
        parser = LinkParser(response.url)
        parser.feed(text)
        # Some portals place absolute provider URLs inside serialized JavaScript.
        for match in re.finditer(r"https?:\\?/\\?/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", text):
            candidate = match.group(0).replace("\\/", "/").rstrip('"\'<>),;')
            parsed = urlparse(candidate)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                parser.links.add(urlunparse(parsed._replace(fragment="")))
        result["linkCount"] = len(parser.links)
        result["jobPostingCount"] = _jobposting_count(parser.json_ld)
        candidates = []
        for url in sorted({response.url, *parser.links}):
            provider = _provider(url)
            if provider:
                candidates.append({
                    "endpoint": url,
                    "organizationFilter": _organization_filter(url),
                    "provider": provider,
                })
        # Keep the evidence bounded while retaining distinct exact endpoints.
        result["providerCandidates"] = candidates[:25]
        result["secondaryProbes"] = [
            _secondary_probe(url)
            for url in _probe_candidates(requested_url, response.url, parser.links)
        ]
    except (requests.RequestException, RuntimeError) as exc:
        result["retrievalError"] = f"{type(exc).__name__}:{exc}"
        result["secondaryProbes"] = []
    result["discoveryReason"] = _specific_reason(
        result["httpStatus"], result["retrievalError"],
        result["providerCandidates"], result["jobPostingCount"],
        result["linkCount"], result["contentType"],
    )
    return result


def inspect_supplement(identifier: str, probe: dict) -> dict:
    result = {
        "identifier": identifier,
        "method": probe["method"],
        "url": probe["url"],
        "httpStatus": None,
        "responseBytes": 0,
        "contentSha256": None,
        "retrievalError": None,
        "resultCount": None,
        "reviewConclusion": probe["reviewConclusion"],
    }
    try:
        response = requests.request(
            probe["method"], probe["url"], json=probe.get("json"),
            allow_redirects=True,
            headers={"User-Agent": "OKG-career-source-review/1.0 (+https://openknowledgegraphs.com/)"},
            stream=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"response-exceeded-{MAX_RESPONSE_BYTES}-byte-cap")
        result.update({
            "contentSha256": hashlib.sha256(body).hexdigest(),
            "httpStatus": response.status_code,
            "responseBytes": len(body),
            "url": response.url,
        })
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            for key in ("total", "Total", "totalResults"):
                if isinstance(payload.get(key), int):
                    result["resultCount"] = payload[key]
                    break
    except (requests.RequestException, RuntimeError) as exc:
        result["retrievalError"] = f"{type(exc).__name__}:{exc}"
    return result


def capture() -> dict:
    cohort = fixed_cohort()
    pages = [row for row in cohort if row.get("careersPage")]
    output = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(inspect_page, row): row["identifier"] for row in pages}
        for future in as_completed(futures):
            output.append(future.result())
    output.sort(key=lambda row: row["identifier"])
    if len(output) != 85 or len({row["identifier"] for row in output}) != 85:
        raise RuntimeError("Task 42 discovery did not capture exactly 85 distinct careers pages")
    supplements = [
        inspect_supplement(identifier, probe)
        for identifier, probe in sorted(SUPPLEMENTAL_PROBES.items())
    ]
    return {
        "bounds": {
            "maxRedirects": MAX_REDIRECTS,
            "maxResponseBytes": MAX_RESPONSE_BYTES,
            "requestTimeoutSeconds": REQUEST_TIMEOUT_SECONDS,
            "maxSecondaryProbesPerCareersPage": MAX_SECONDARY_PROBES,
            "requestsPerCareersPageMaximum": 1 + MAX_SECONDARY_PROBES,
        },
        "careersPages": output,
        "capturedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "count": len(output),
        "supplementalChecks": supplements,
    }


def validate(payload: dict) -> None:
    pages = payload.get("careersPages")
    expected = {
        row["identifier"] for row in fixed_cohort() if row.get("careersPage")
    }
    if not isinstance(pages, list) or len(pages) != 85:
        raise RuntimeError("Task 42 discovery requires exactly 85 evidence rows")
    if {row.get("identifier") for row in pages} != expected:
        raise RuntimeError("Task 42 discovery identifiers do not match the fixed cohort")
    required = {
        "careersPage", "contentSha256", "contentType", "discoveryReason",
        "finalUrl", "httpStatus", "identifier", "jobPostingCount", "linkCount",
        "providerCandidates", "responseBytes", "retrievalError",
        "secondaryProbes",
    }
    for row in pages:
        missing = required - set(row)
        if missing:
            raise RuntimeError(f"discovery evidence for {row.get('identifier')} lacks {sorted(missing)}")
        if not row["discoveryReason"]:
            raise RuntimeError(f"discovery evidence for {row['identifier']} lacks a reason")
        if len(row["secondaryProbes"]) > MAX_SECONDARY_PROBES:
            raise RuntimeError(f"discovery evidence for {row['identifier']} exceeded probe cap")
    supplements = payload.get("supplementalChecks")
    if (
        not isinstance(supplements, list)
        or {row.get("identifier") for row in supplements} != set(SUPPLEMENTAL_PROBES)
    ):
        raise RuntimeError("Task 42 discovery lacks the required named supplemental checks")
    for row in supplements:
        if not row.get("reviewConclusion") or not row.get("url"):
            raise RuntimeError(f"supplemental check for {row.get('identifier')} is incomplete")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.live:
        payload = capture()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        payload = json.loads(args.output.read_text(encoding="utf-8"))
    validate(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
