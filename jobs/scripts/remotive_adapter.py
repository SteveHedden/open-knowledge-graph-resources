"""Normalization adapter for Remotive's public remote-jobs response."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from live_sources import LivePipelineError, SourceConfig


class _TextExtractor(HTMLParser):
    BREAK_TAGS = {"br", "div", "h1", "h2", "h3", "h4", "li", "p", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self.BREAK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BREAK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.parts)).strip()


def html_to_text(value: str | None) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    parser.close()
    return parser.text()


def canonicalize_url(value: str, allowed_host: str | None = None) -> str:
    parsed = urlparse((value or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise LivePipelineError("job listing URL must be an absolute HTTPS URL")
    if allowed_host is not None and parsed.hostname != allowed_host:
        raise LivePipelineError(
            f"job listing URL must use the registered host {allowed_host!r}"
        )
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"ref", "referrer"}
    ]
    return urlunparse(
        (
            "https",
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.params,
            urlencode(query),
            "",
        )
    )


def _date_only(value: str | None) -> str | None:
    if not value:
        return None
    candidate = str(value).strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return None
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def salary_text(value) -> str | None:
    """Retain source salary text without guessing units or currency."""
    if value is None:
        return None
    if isinstance(value, dict):
        currency = html_to_text(str(value.get("currency") or ""))
        minimum = html_to_text(str(value.get("min") or value.get("minimum") or ""))
        maximum = html_to_text(str(value.get("max") or value.get("maximum") or ""))
        bounds = "–".join(part for part in (minimum, maximum) if part)
        text = " ".join(part for part in (currency, bounds) if part)
        return text or None
    text = html_to_text(str(value))
    return text or None


def normalize_remotive_job(
    item: dict,
    source: SourceConfig,
    retrieved_at: str,
) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = html_to_text(str(item.get("title") or ""))
    description = html_to_text(str(item.get("description") or ""))
    organization = html_to_text(str(item.get("company_name") or ""))
    source_record_id = str(item.get("id") or "").strip()
    if not title or not description or not organization or not source_record_id:
        return None
    try:
        canonical_url = canonicalize_url(
            str(item.get("url") or ""), allowed_host=source.allowed_host
        )
    except LivePipelineError:
        return None

    fingerprint = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    stable_id = f"{source.key}-{source_record_id}"
    record = {
        "id": stable_id,
        "sourceRecordId": source_record_id,
        "canonicalUrl": canonical_url,
        "sourceName": source.attribution_text,
        "sourceUrl": canonical_url,
        "sourceAttributionUrl": source.attribution_url,
        "sourceDataset": source.dataset_uri,
        "title": title,
        "description": description,
        "hiringOrganization": organization,
        "remote": True,
        "canonicalFingerprint": fingerprint,
        "firstSeenAt": retrieved_at,
        "lastSeenAt": retrieved_at,
        "retrievedAt": retrieved_at,
        "active": True,
        "discoveredBy": [source.source_query],
    }
    location = html_to_text(str(item.get("candidate_required_location") or ""))
    if location:
        record["location"] = location
    date_posted = _date_only(item.get("publication_date"))
    if date_posted:
        record["datePosted"] = date_posted
    employment_type = html_to_text(str(item.get("job_type") or ""))
    if employment_type:
        record["employmentType"] = employment_type
    category = html_to_text(str(item.get("category") or ""))
    if category:
        record["category"] = category
    tags = sorted(
        {html_to_text(str(tag)) for tag in item.get("tags", []) if html_to_text(str(tag))},
        key=lambda value: (value.casefold(), value),
    )
    if tags:
        record["tags"] = tags
    salary = salary_text(item.get("salary"))
    if salary:
        record["salary"] = salary
    return record


def records_from_payload(
    payload: dict,
    source: SourceConfig,
    retrieved_at: str,
) -> tuple[list[dict], int, bool]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise LivePipelineError("Remotive response is missing its jobs array")
    total = payload.get("job-count")
    if not isinstance(total, int) or isinstance(total, bool) or total < len(jobs):
        raise LivePipelineError("Remotive response has invalid job-count metadata")
    bounded = jobs[: source.max_records_per_run]
    records = []
    for index, item in enumerate(bounded):
        normalized = normalize_remotive_job(item, source, retrieved_at)
        if normalized is None:
            raise LivePipelineError(f"Remotive job at index {index} failed normalization")
        records.append(normalized)
    complete = total == len(jobs) and len(jobs) <= source.max_records_per_run
    return records, len(bounded), complete
