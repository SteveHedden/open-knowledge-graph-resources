"""Normalization adapter for Arbeitnow's public job-board API."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from live_sources import LivePipelineError, SourceConfig
from remotive_adapter import canonicalize_url, html_to_text, salary_text, _date_only


def _created_date(value) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    return _date_only(str(value)) if value is not None else None


def _boolean(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return False


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


def _salary(item: dict) -> str | None:
    direct = salary_text(item.get("salary"))
    if direct:
        return direct
    minimum = item.get("salary_min")
    maximum = item.get("salary_max")
    currency = html_to_text(str(item.get("currency") or ""))
    bounds = "–".join(str(value) for value in (minimum, maximum) if value is not None)
    text = " ".join(part for part in (currency, bounds) if part).strip()
    return text or None


def normalize_arbeitnow_job(
    item: dict,
    source: SourceConfig,
    retrieved_at: str,
) -> dict | None:
    if not isinstance(item, dict):
        return None
    title = html_to_text(str(item.get("title") or ""))
    description = html_to_text(str(item.get("description") or ""))
    organization = html_to_text(str(item.get("company_name") or ""))
    source_record_id = str(item.get("slug") or item.get("id") or "").strip()
    if not title or not description or not organization or not source_record_id:
        return None
    try:
        # Arbeitnow's feed intentionally supplies the employer's canonical
        # application/listing URL for some records, not always an Arbeitnow
        # URL. The API endpoint itself remains pinned to the registered host;
        # outbound record links must still be absolute, credential-free HTTPS.
        canonical_url = canonicalize_url(str(item.get("url") or ""))
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
        "remote": _boolean(item.get("remote")),
        "canonicalFingerprint": fingerprint,
        "firstSeenAt": retrieved_at,
        "lastSeenAt": retrieved_at,
        "retrievedAt": retrieved_at,
        "active": True,
        "discoveredBy": [source.source_query],
    }
    location = html_to_text(str(item.get("location") or ""))
    if location:
        record["location"] = location
    date_posted = _created_date(item.get("created_at"))
    if date_posted:
        record["datePosted"] = date_posted
    job_types = _strings(item.get("job_types"))
    if job_types:
        record["employmentType"] = ", ".join(job_types)
    tags = _strings(item.get("tags"))
    if tags:
        record["tags"] = tags
    salary = _salary(item)
    if salary:
        record["salary"] = salary
    return record


def pagination_from_payload(payload: dict) -> tuple[int, bool, int | None]:
    data = payload.get("data")
    meta = payload.get("meta")
    links = payload.get("links")
    if not isinstance(data, list) or not isinstance(meta, dict) or not isinstance(links, dict):
        raise LivePipelineError("Arbeitnow response requires data, meta, and links objects")
    current_page = meta.get("current_page")
    last_page = meta.get("last_page")
    total = meta.get("total")
    if not isinstance(current_page, int) or isinstance(current_page, bool) or current_page < 1:
        raise LivePipelineError("Arbeitnow response has invalid pagination metadata")
    next_url = links.get("next")
    if next_url is not None and (not isinstance(next_url, str) or not next_url.strip()):
        raise LivePipelineError("Arbeitnow response has an invalid next-page link")
    has_next = next_url is not None

    # The live API currently omits last_page/total even though older examples
    # include them. Accept either shape and cross-check the richer form when
    # it is supplied.
    if last_page is not None:
        if (
            not isinstance(last_page, int)
            or isinstance(last_page, bool)
            or last_page < current_page
            or has_next != (current_page < last_page)
        ):
            raise LivePipelineError("Arbeitnow response has inconsistent page metadata")
    if total is not None:
        if not isinstance(total, int) or isinstance(total, bool) or total < len(data):
            raise LivePipelineError("Arbeitnow response has invalid total metadata")
    return current_page, has_next, total


def records_from_payload(
    payload: dict,
    source: SourceConfig,
    retrieved_at: str,
    limit: int | None = None,
) -> tuple[list[dict], int, bool]:
    current_page, has_next, _ = pagination_from_payload(payload)
    jobs = payload["data"]
    effective_limit = source.max_records_per_run if limit is None else max(0, limit)
    bounded = jobs[:effective_limit]
    records = []
    for index, item in enumerate(bounded):
        normalized = normalize_arbeitnow_job(item, source, retrieved_at)
        if normalized is None:
            raise LivePipelineError(
                f"Arbeitnow job at page {current_page}, index {index} failed normalization"
            )
        records.append(normalized)
    complete = not has_next and len(jobs) <= effective_limit
    return records, len(bounded), complete
