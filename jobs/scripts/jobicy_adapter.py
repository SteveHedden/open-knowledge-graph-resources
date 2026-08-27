"""Normalization adapter for Jobicy's public remote-jobs API.

Jobicy's ``tag`` parameter is a genuine keyword search (verified against the
live API with a true-negative control query, which correctly returned zero
results). It is used here with one bounded query per declared
kgjobs:QueryFamily in sources.ttl, the same pattern as Himalayas. Two bare
single words -- "knowledge" and "owl" -- were found to silently fall back to
an unfiltered result set rather than actually filtering, so the registry
deliberately queries "knowledge graph" and "ontology" instead of those bare
words. Local RDF vocabulary classification remains the sole eligibility
decision regardless of what a source-side search returns.
"""

from __future__ import annotations

import hashlib
import re

from live_sources import LivePipelineError, SourceConfig
from remotive_adapter import canonicalize_url, html_to_text, _date_only


def _geo_location(value) -> str | None:
    if not value:
        return None
    text = html_to_text(str(value))
    cleaned = re.sub(r"\s*,\s*", ", ", text).strip(", ").strip()
    return cleaned or None


def _job_types(value) -> str | None:
    if not isinstance(value, list):
        return None
    types = sorted(
        {text for item in value if (text := html_to_text(str(item)))},
        key=lambda text: (text.casefold(), text),
    )
    return ", ".join(types) if types else None


def _industries(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {text for item in value if (text := html_to_text(str(item)))},
        key=lambda text: (text.casefold(), text),
    )


def normalize_jobicy_job(
    item: dict,
    source: SourceConfig,
    retrieved_at: str,
    query: str,
) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = html_to_text(str(item.get("jobTitle") or ""))
    description = html_to_text(str(item.get("jobDescription") or item.get("jobExcerpt") or ""))
    organization = html_to_text(str(item.get("companyName") or ""))
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
        # Jobicy is a remote-jobs-only board.
        "remote": True,
        "canonicalFingerprint": fingerprint,
        "firstSeenAt": retrieved_at,
        "lastSeenAt": retrieved_at,
        "retrievedAt": retrieved_at,
        "active": True,
        "discoveredBy": [query],
    }
    location = _geo_location(item.get("jobGeo"))
    if location:
        record["location"] = location
    date_posted = _date_only(item.get("pubDate"))
    if date_posted:
        record["datePosted"] = date_posted
    employment_type = _job_types(item.get("jobType"))
    if employment_type:
        record["employmentType"] = employment_type
    tags = _industries(item.get("jobIndustry"))
    if tags:
        record["tags"] = tags
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
        raise LivePipelineError("Jobicy response is missing its jobs array")
    total = payload.get("jobCount")
    if not isinstance(total, int) or isinstance(total, bool) or total < len(jobs):
        raise LivePipelineError("Jobicy response has invalid jobCount metadata")
    effective_limit = source.max_records_per_run if limit is None else max(0, limit)
    bounded = jobs[:effective_limit]
    records = []
    for index, item in enumerate(bounded):
        normalized = normalize_jobicy_job(item, source, retrieved_at, query)
        if normalized is None:
            raise LivePipelineError(
                f"Jobicy job for query {query!r} at index {index} failed normalization"
            )
        records.append(normalized)
    complete = total == len(jobs) and len(jobs) <= effective_limit
    return records, len(bounded), complete
