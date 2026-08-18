"""Normalization adapter for Himalayas' official keyword-search API."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone

from live_sources import LivePipelineError, SourceConfig
from remotive_adapter import canonicalize_url, html_to_text


def _unix_date(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            text
            for item in value
            if (text := html_to_text(str(item)))
        },
        key=lambda text: (text.casefold(), text),
    )


def _text_or_list(value) -> str:
    if isinstance(value, list):
        return ", ".join(_strings(value))
    return html_to_text(str(value or ""))


def _number(value) -> int | float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _salary(item: dict) -> tuple[str, dict] | None:
    minimum = _number(item.get("minSalary"))
    maximum = _number(item.get("maxSalary"))
    if minimum is None and maximum is None:
        return None
    currency = html_to_text(str(item.get("currency") or ""))
    period = html_to_text(str(item.get("salaryPeriod") or ""))
    bounds = "–".join(
        f"{value:,}" for value in (minimum, maximum) if value is not None
    )
    value = " ".join(part for part in (currency, bounds) if part)
    display = f"{value} / {period}" if period else value
    structured = {
        key: component
        for key, component in (
            ("currency", currency or None),
            ("minValue", minimum),
            ("maxValue", maximum),
            ("unitText", period or None),
        )
        if component is not None
    }
    return display, structured


def normalize_himalayas_job(
    item: dict,
    source: SourceConfig,
    retrieved_at: str,
    query: str,
) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = html_to_text(str(item.get("title") or ""))
    description = html_to_text(str(item.get("description") or item.get("excerpt") or ""))
    organization = html_to_text(str(item.get("companyName") or ""))
    guid = str(item.get("guid") or "").strip()
    application_link = str(item.get("applicationLink") or "").strip()
    if not title or not description or not organization or not guid or not application_link:
        return None
    try:
        source_url = canonicalize_url(guid, allowed_host=source.allowed_host)
        canonical_url = canonicalize_url(
            application_link, allowed_host=source.allowed_host
        )
    except LivePipelineError:
        return None
    if source_url != canonical_url:
        return None

    fingerprint = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    record = {
        "id": f"{source.key}-{fingerprint[:16]}",
        "sourceRecordId": guid,
        "canonicalUrl": canonical_url,
        "sourceName": source.attribution_text,
        "sourceUrl": source_url,
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
        "discoveredBy": [query],
    }
    locations = _strings(item.get("locationRestrictions"))
    if locations:
        record["applicantLocationRequirements"] = locations
        # Himalayas has no separate city/place field -- locationRestrictions
        # (eligible remote regions, e.g. "United States") is the only
        # location-ish data it provides, so it also doubles as the display
        # location rather than leaving the UI's Location column empty.
        record["location"] = ", ".join(locations)
    date_posted = _unix_date(item.get("pubDate"))
    if date_posted:
        record["datePosted"] = date_posted
    employment_type = _text_or_list(item.get("employmentType"))
    if employment_type:
        record["employmentType"] = employment_type
    categories = _strings(item.get("categories"))
    if categories:
        record["tags"] = categories
    seniority = _text_or_list(item.get("seniority"))
    if seniority:
        record["seniority"] = seniority
    valid_through = _unix_date(item.get("expiryDate"))
    if valid_through:
        record["validThrough"] = valid_through
    salary = _salary(item)
    if salary:
        record["salary"], record["baseSalary"] = salary
    return record


def records_from_payload(
    payload: dict,
    source: SourceConfig,
    retrieved_at: str,
    query: str,
    limit: int | None = None,
) -> tuple[list[dict], int, bool]:
    jobs = payload.get("jobs")
    response_limit = payload.get("limit")
    offset = payload.get("offset")
    total = payload.get("totalCount")
    if not isinstance(jobs, list):
        raise LivePipelineError("Himalayas response is missing its jobs array")
    if (
        not isinstance(response_limit, int)
        or isinstance(response_limit, bool)
        or response_limit < 1
        or response_limit > 20
    ):
        raise LivePipelineError("Himalayas response has an invalid limit")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise LivePipelineError("Himalayas response has an invalid offset")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total < offset + len(jobs)
    ):
        raise LivePipelineError("Himalayas response has an invalid totalCount")

    effective_limit = min(20, source.max_records_per_run)
    if limit is not None:
        effective_limit = min(effective_limit, max(0, limit))
    bounded = jobs[:effective_limit]
    records = []
    for index, item in enumerate(bounded):
        normalized = normalize_himalayas_job(item, source, retrieved_at, query)
        if normalized is None:
            raise LivePipelineError(
                f"Himalayas job for query {query!r} at index {index} failed normalization"
            )
        records.append(normalized)
    complete = offset + len(jobs) >= total and len(jobs) <= effective_limit
    return records, len(bounded), complete
