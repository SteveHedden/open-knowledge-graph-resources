"""Normalization adapter for Adzuna's public job-search API.

Adzuna authenticates via app_id/app_key query parameters rather than a URL
path segment like Jooble -- scripts/live_sources.py reads both from the
ADZUNA_APP_ID / ADZUNA_APP_KEY environment variables at request time and
appends them to the registry-built URL, never storing or logging them (see
the matching comment on JOOBLE_API_KEY_ENV in live_sources.py).

canonicalize_url is called without an allowed_host restriction here,
matching Arbeitnow rather than Himalayas/Jobicy/Jooble: Adzuna's
redirect_url points to a tracking page on a different adzuna.com subdomain
than the api.adzuna.com search endpoint, not the same host used for the
registered API distribution.
"""

from __future__ import annotations

import hashlib

from live_sources import ADZUNA_PAGE_SIZE, LivePipelineError, SourceConfig
from remotive_adapter import canonicalize_url, html_to_text, _date_only


def _number(value) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _salary(item: dict) -> tuple[str, dict] | None:
    minimum = _number(item.get("salary_min"))
    maximum = _number(item.get("salary_max"))
    if minimum is None and maximum is None:
        return None
    bounds = "–".join(f"{value:,.0f}" for value in (minimum, maximum) if value is not None)
    # This source is registered against Adzuna's "us" country endpoint only
    # (see sources.ttl) -- the API does not return a currency code on the
    # job payload itself, so the currency is fixed to match the registered
    # country rather than guessed per record.
    display = f"USD {bounds}"
    structured = {
        key: value
        for key, value in (
            ("currency", "USD"),
            ("minValue", minimum),
            ("maxValue", maximum),
        )
        if value is not None
    }
    return display, structured


def _employment_type(item: dict) -> str | None:
    parts = [
        html_to_text(str(item.get(field) or "")).replace("_", " ").strip()
        for field in ("contract_type", "contract_time")
    ]
    parts = [part.title() for part in parts if part]
    return ", ".join(parts) if parts else None


def normalize_adzuna_job(
    item: dict,
    source: SourceConfig,
    retrieved_at: str,
    query: str,
) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = html_to_text(str(item.get("title") or ""))
    description = html_to_text(str(item.get("description") or ""))
    company = item.get("company")
    organization = (
        html_to_text(str(company.get("display_name") or ""))
        if isinstance(company, dict)
        else ""
    )
    source_record_id = str(item.get("id") or "").strip()
    if not title or not description or not organization or not source_record_id:
        return None
    try:
        canonical_url = canonicalize_url(str(item.get("redirect_url") or ""))
    except LivePipelineError:
        return None

    fingerprint = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    record = {
        "id": f"{source.key}-{source_record_id}",
        "sourceRecordId": source_record_id,
        "canonicalUrl": canonical_url,
        "sourceName": source.attribution_text,
        "sourceUrl": canonical_url,
        "sourceAttributionUrl": source.attribution_url,
        "sourceDataset": source.dataset_uri,
        "title": title,
        "description": description,
        "hiringOrganization": organization,
        "canonicalFingerprint": fingerprint,
        "firstSeenAt": retrieved_at,
        "lastSeenAt": retrieved_at,
        "retrievedAt": retrieved_at,
        "active": True,
        "discoveredBy": [query],
    }
    location = item.get("location")
    if isinstance(location, dict):
        display_location = html_to_text(str(location.get("display_name") or ""))
        if display_location:
            record["location"] = display_location
    date_posted = _date_only(item.get("created"))
    if date_posted:
        record["datePosted"] = date_posted
    employment_type = _employment_type(item)
    if employment_type:
        record["employmentType"] = employment_type
    category = item.get("category")
    if isinstance(category, dict):
        label = html_to_text(str(category.get("label") or ""))
        if label:
            record["tags"] = [label]
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
    jobs = payload.get("results")
    if not isinstance(jobs, list):
        raise LivePipelineError("Adzuna response is missing its results array")
    total = payload.get("count")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise LivePipelineError("Adzuna response has invalid count metadata")
    effective_limit = source.max_records_per_run if limit is None else max(0, limit)
    bounded = jobs[:effective_limit]
    records = []
    for index, item in enumerate(bounded):
        normalized = normalize_adzuna_job(item, source, retrieved_at, query)
        if normalized is None:
            raise LivePipelineError(
                f"Adzuna job for query {query!r} at index {index} failed normalization"
            )
        records.append(normalized)
    # Adzuna reports a grand total (count) but not how many results a single
    # page actually held short of it; "fewer results than requested" is the
    # only reliable per-page completeness signal available.
    complete = len(jobs) < ADZUNA_PAGE_SIZE
    return records, len(bounded), complete
