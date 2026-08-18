"""Normalization adapter for Jooble's keyed job-search API.

Jooble requires a POST request with a JSON body ({"keywords": ...}) and an
API key issued to a real, approved account -- a GET request with the same
parameters returns nothing at all (verified directly against the live API).
The key is never stored in sources.ttl or any committed file: it is read
from the JOOBLE_API_KEY environment variable at request time by
live_sources.fetch_json_http, which also builds the real endpoint URL
(https://jooble.org/api/<key>) only in memory, never logging it.
"""

from __future__ import annotations

import hashlib

from live_sources import LivePipelineError, SourceConfig
from remotive_adapter import canonicalize_url, html_to_text, _date_only


def normalize_jooble_job(
    item: dict,
    source: SourceConfig,
    retrieved_at: str,
    query: str,
) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = html_to_text(str(item.get("title") or ""))
    # Jooble's API returns a short match snippet, not the full job
    # description -- there is no other text field available.
    description = html_to_text(str(item.get("snippet") or ""))
    organization = html_to_text(str(item.get("company") or ""))
    raw_id = item.get("id")
    source_record_id = str(raw_id).strip() if raw_id is not None else ""
    if not title or not description or not organization or not source_record_id:
        return None
    try:
        canonical_url = canonicalize_url(
            str(item.get("link") or ""), allowed_host=source.allowed_host
        )
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
    location = html_to_text(str(item.get("location") or ""))
    if location:
        record["location"] = location
        # Jooble has no dedicated remote-work flag; it sometimes signals
        # remote by putting the literal word "Remote" in the location
        # field itself instead. Any other location string is a real place,
        # not a remote/on-site signal either way -- leave "remote" unset
        # rather than assume on-site.
        if location.strip().casefold() == "remote":
            record["remote"] = True
    date_posted = _date_only(item.get("updated"))
    if date_posted:
        record["datePosted"] = date_posted
    employment_type = html_to_text(str(item.get("type") or ""))
    if employment_type:
        record["employmentType"] = employment_type
    salary = html_to_text(str(item.get("salary") or ""))
    if salary:
        record["salary"] = salary
    return record


def records_from_payload(
    payload: dict,
    source: SourceConfig,
    retrieved_at: str,
    query: str,
    limit: int | None = None,
) -> tuple[list[dict], int, bool]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise LivePipelineError("Jooble response is missing its jobs array")
    total = payload.get("totalCount")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise LivePipelineError("Jooble response has invalid totalCount metadata")
    effective_limit = source.max_records_per_run if limit is None else max(0, limit)
    bounded = jobs[:effective_limit]
    records = []
    for index, item in enumerate(bounded):
        normalized = normalize_jooble_job(item, source, retrieved_at, query)
        if normalized is None:
            raise LivePipelineError(
                f"Jooble job for query {query!r} at index {index} failed normalization"
            )
        records.append(normalized)
    # Jooble does not report how many results a single page actually holds
    # server-side beyond the fixed page size; treat "fewer jobs than a full
    # page" as the only reliable completeness signal.
    complete = len(jobs) < 30
    return records, len(bounded), complete
