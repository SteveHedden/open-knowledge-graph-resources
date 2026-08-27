"""Strict, deterministic first-party-to-aggregator job reconciliation."""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

ROOT = Path(__file__).resolve().parent.parent
ALIASES_PATH = ROOT / "curation" / "location-aliases.json"


class ReconciliationError(RuntimeError):
    """Cross-source reconciliation input violates the reviewed contract."""


def _space(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_title(value: str) -> str:
    """Apply only the exact title transforms authorized by Task 38."""
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    text = text.translate(str.maketrans({
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    }))
    return _space(text).casefold()


def normalize_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold()
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunparse(("https", host + port, path, "", query, ""))


def load_location_aliases(path: Path = ALIASES_PATH) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconciliationError("location alias registry is missing or invalid") from exc
    aliases = payload.get("aliases")
    if payload.get("schemaVersion") != 1 or not isinstance(aliases, dict):
        raise ReconciliationError("location alias registry has an unsupported shape")
    normalized = {}
    for source, target in aliases.items():
        source_key = _space(unicodedata.normalize("NFKC", source)).casefold()
        target_key = _space(unicodedata.normalize("NFKC", target)).casefold()
        if not source_key or not target_key:
            raise ReconciliationError("location aliases cannot be empty")
        normalized[source_key] = target_key
    return normalized


def location_signature(record: dict, aliases: dict[str, str] | None = None) -> str | None:
    aliases = aliases or load_location_aliases()
    mode = str(record.get("workplaceMode") or "unknown").strip().casefold()
    if mode not in {"remote", "hybrid", "onsite", "unknown"}:
        raise ReconciliationError(f"unsupported workplace mode {mode!r}")
    raw_keys = record.get("locationKeys")
    if not isinstance(raw_keys, list) or not raw_keys:
        return None
    keys = []
    for value in raw_keys:
        key = _space(unicodedata.normalize("NFKC", str(value))).casefold()
        if key:
            keys.append(aliases.get(key, key))
    if not keys:
        return None
    return f"{mode}|{'|'.join(sorted(set(keys)))}"


def _posting_date(record: dict) -> date | None:
    value = record.get("datePosted")
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _description_digest(record: dict) -> str | None:
    value = record.get("description")
    if not isinstance(value, str) or not value.strip():
        return None
    text = unicodedata.normalize("NFKC", html.unescape(value))
    for wrapper in record.get("reviewedWrapperPatterns", []):
        if not isinstance(wrapper, str) or len(wrapper) > 500:
            raise ReconciliationError("reviewed wrapper patterns must be bounded strings")
        text = re.sub(wrapper, " ", text, flags=re.IGNORECASE)
    text = _space(re.sub(r"<[^>]+>", " ", text)).casefold()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def _contradiction(first_party: dict, aggregator: dict) -> str | None:
    pairs = (
        ("workplaceMode", "work mode"),
        ("employmentType", "employment type"),
        ("requisitionId", "requisition ID"),
        ("validThrough", "valid-through date"),
    )
    for field, label in pairs:
        left = _space(first_party.get(field)).casefold()
        right = _space(aggregator.get(field)).casefold()
        if left and right and left != right:
            return f"contradictory {label}"
    return None


def match_method(
    first_party: dict, aggregator: dict,
    aliases: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """Return the approved match method or an explicit no-merge reason."""
    org = first_party.get("organizationIri")
    if not org or aggregator.get("organizationIri") != org:
        return None, "reviewed organization mismatch or missing identity"
    contradiction = _contradiction(first_party, aggregator)
    if contradiction:
        return None, contradiction
    provider_identity = (
        first_party.get("provider"), first_party.get("tenant"), first_party.get("sourceRecordId")
    )
    aggregator_identity = (
        aggregator.get("provider"), aggregator.get("tenant"), aggregator.get("sourceRecordId")
    )
    if all(provider_identity) and provider_identity == aggregator_identity:
        return "exact-provider-tenant-posting-id", "strong source identity"
    first_urls = {
        normalize_url(first_party.get("canonicalUrl")),
        normalize_url(first_party.get("sourceUrl")),
    } - {""}
    aggregator_urls = {
        normalize_url(aggregator.get("canonicalUrl")),
        normalize_url(aggregator.get("sourceUrl")),
        normalize_url(aggregator.get("resolvedFirstPartyUrl")),
    } - {""}
    if first_urls & aggregator_urls:
        return "exact-approved-first-party-url", "strong canonical URL identity"
    if normalize_title(first_party.get("title")) != normalize_title(aggregator.get("title")):
        return None, "normalized titles differ"
    left_location = location_signature(first_party, aliases)
    right_location = location_signature(aggregator, aliases)
    if not left_location or not right_location or left_location != right_location:
        return None, "structured location signatures are missing or differ"
    left_date = _posting_date(first_party)
    right_date = _posting_date(aggregator)
    if left_date is None or right_date is None or abs((left_date - right_date).days) > 1:
        return None, "genuine posting dates are missing or more than one day apart"
    left_req = _space(first_party.get("requisitionId")).casefold()
    right_req = _space(aggregator.get("requisitionId")).casefold()
    shared_req = bool(left_req and left_req == right_req)
    left_digest = _description_digest(first_party)
    right_digest = _description_digest(aggregator)
    if not shared_req and not (left_digest and left_digest == right_digest):
        return None, "no shared requisition ID or exact reviewed full-description digest"
    return (
        "exact-reviewed-fields-requisition" if shared_req
        else "exact-reviewed-fields-description-digest",
        "all strict reviewed fields agree",
    )


def _occurrences(record: dict) -> list[dict]:
    existing = record.get("sourceOccurrences")
    if isinstance(existing, list) and existing:
        return [dict(value) for value in existing if isinstance(value, dict)]
    return [{
        "sourceDataset": record.get("sourceDataset"),
        "sourceRecordId": record.get("sourceRecordId"),
        "sourceUrl": record.get("sourceUrl") or record.get("canonicalUrl"),
        "provider": record.get("provider"),
        "tenant": record.get("tenant"),
        "firstParty": bool(record.get("firstParty")),
    }]


def _occurrence_identity(occurrence: dict) -> tuple[str, str, str]:
    dataset = _space(occurrence.get("sourceDataset"))
    source_id = _space(occurrence.get("sourceRecordId"))
    source_url = normalize_url(occurrence.get("sourceUrl"))
    # Stable source identity wins over a mutable URL (notably Graphwise's
    # WordPress slug). URL is only a fallback for sources lacking an ID.
    return (dataset, source_id, "" if source_id else source_url)


def merge_source_occurrences(*collections: list[dict]) -> list[dict]:
    """Merge occurrence projections deterministically and idempotently."""
    merged: dict[tuple[str, str, str], dict] = {}
    for collection in collections:
        for raw in collection or []:
            if not isinstance(raw, dict):
                continue
            occurrence = {
                "sourceDataset": raw.get("sourceDataset"),
                "sourceRecordId": raw.get("sourceRecordId"),
                "sourceUrl": raw.get("sourceUrl"),
                "provider": raw.get("provider"),
                "tenant": raw.get("tenant"),
                "firstParty": bool(raw.get("firstParty")),
            }
            identity = _occurrence_identity(occurrence)
            if not identity[0] or (not identity[1] and not identity[2]):
                raise ReconciliationError("source occurrence lacks a stable source identity")
            prior = merged.get(identity, {})
            merged[identity] = {
                key: value
                for key, value in {**prior, **occurrence}.items()
                if value is not None
            }
    return sorted(
        merged.values(),
        key=lambda row: (
            not bool(row.get("firstParty")), row.get("sourceDataset") or "",
            row.get("sourceRecordId") or "", row.get("sourceUrl") or "",
        ),
    )


def _timestamp(values: list[str], *, latest: bool) -> str | None:
    present = sorted(value for value in values if isinstance(value, str) and value)
    if not present:
        return None
    return present[-1] if latest else present[0]


def reconcile_records(records: list[dict]) -> tuple[list[dict], dict]:
    """Merge only unique aggregator-to-first-party matches, never transitively."""
    aliases = load_location_aliases()
    ordered = sorted((dict(record) for record in records), key=lambda row: row.get("id", ""))
    first_party = [record for record in ordered if record.get("firstParty")]
    aggregators = [record for record in ordered if not record.get("firstParty")]
    candidates_by_aggregator: dict[str, list[tuple[str, str]]] = {}
    for aggregator in aggregators:
        candidates = []
        for authoritative in first_party:
            method, reason = match_method(authoritative, aggregator, aliases)
            if method:
                candidates.append((authoritative["id"], method))
        candidates_by_aggregator[aggregator["id"]] = candidates

    assigned: dict[str, list[tuple[dict, str]]] = {record["id"]: [] for record in first_party}
    unresolved = []
    for aggregator in aggregators:
        candidates = candidates_by_aggregator[aggregator["id"]]
        if len(candidates) == 1:
            target, method = candidates[0]
            assigned[target].append((aggregator, method))
        elif len(candidates) > 1:
            unresolved.append({
                "aggregatorId": aggregator["id"],
                "reason": "multiple first-party candidates passed; merged none",
                "candidateIds": sorted(target for target, _ in candidates),
            })

    output = []
    merged_aggregator_ids = set()
    for authoritative in first_party:
        merged = dict(authoritative)
        matches = assigned[authoritative["id"]]
        occurrences = _occurrences(authoritative)
        methods = []
        first_seen = [authoritative.get("firstSeenAt")]
        last_seen = [authoritative.get("lastSeenAt")]
        for aggregator, method in matches:
            merged_aggregator_ids.add(aggregator["id"])
            occurrences.extend(_occurrences(aggregator))
            methods.append(method)
            first_seen.append(aggregator.get("firstSeenAt"))
            last_seen.append(aggregator.get("lastSeenAt"))
        merged["sourceOccurrences"] = merge_source_occurrences(occurrences)
        if matches:
            merged["reconciliationMethod"] = sorted(set(methods))
            merged["reconciliationReason"] = "unique first-party authority with retained source provenance"
        merged["firstSeenAt"] = _timestamp(first_seen, latest=False)
        merged["lastSeenAt"] = _timestamp(last_seen, latest=True)
        output.append(merged)
    for aggregator in aggregators:
        if aggregator["id"] not in merged_aggregator_ids:
            row = dict(aggregator)
            row["sourceOccurrences"] = merge_source_occurrences(_occurrences(row))
            candidates = candidates_by_aggregator[aggregator["id"]]
            if not candidates:
                reasons = [
                    match_method(authoritative, aggregator, aliases)[1]
                    for authoritative in first_party
                    if authoritative.get("organizationIri") == aggregator.get("organizationIri")
                ]
                row["reconciliationReason"] = sorted(set(reasons))[0] if reasons else "no reviewed first-party candidate"
            else:
                row["reconciliationReason"] = "multiple first-party candidates passed; merged none"
            output.append(row)
    output.sort(key=lambda row: row.get("id", ""))
    audit = {
        "inputRecords": len(records),
        "firstPartyRecords": len(first_party),
        "aggregatorRecords": len(aggregators),
        "mergedAggregatorOccurrences": len(merged_aggregator_ids),
        "outputRecords": len(output),
        "ambiguous": unresolved,
    }
    return output, audit
