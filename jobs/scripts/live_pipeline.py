#!/usr/bin/env python3
"""Pull one bounded registered source into a local snapshot.

Network access is impossible unless the caller supplies ``--live``. In
production this script is orchestrated by the nightly 04:00 UTC jobs workflow
(see ``.github/workflows/update-jobs.yml``), once per due registered source; each
source's own registry-declared refresh interval (``sources.ttl``) still
governs how often it is actually fetched, and the resulting snapshot is
committed to repo-root ``data/jobs/jobs.json`` / ``data/jobs/jobs.ttl`` (kept
separate from the authoritative catalog's own ``data/`` files) for the live
OKG site to read. It can also still be run locally exactly as before for
manual review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from classifier import load_match_terms  # noqa: E402
from catalog_mentions import (  # noqa: E402
    CatalogMentionError,
    add_catalog_mentions,
    load_match_index,
)
from live_records import (  # noqa: E402
    build_graph,
    classify_records,
    deduplicate,
    load_previous_records,
    load_source_snapshots,
    publish_snapshot,
    preserve_first_seen,
)
from live_sources import (  # noqa: E402
    Fetcher,
    LivePipelineError,
    RefreshNotDueError,
    SourceConfig,
    build_feed_url,
    fetch_json_http,
    load_production_source_registry,
    load_source_registry,
)
from arbeitnow_adapter import (  # noqa: E402
    pagination_from_payload as arbeitnow_pagination,
    records_from_payload as arbeitnow_records,
)
from remotive_adapter import records_from_payload as remotive_records  # noqa: E402
from himalayas_adapter import records_from_payload as himalayas_records  # noqa: E402
from jobicy_adapter import records_from_payload as jobicy_records  # noqa: E402
from jooble_adapter import records_from_payload as jooble_records  # noqa: E402
from adzuna_adapter import records_from_payload as adzuna_records  # noqa: E402
from first_party_sources import (  # noqa: E402
    FirstPartySource,
    FirstPartySourceError,
    fetch_source as fetch_first_party_source,
    load_production_first_party_sources,
    request_count_from_payload as first_party_request_count,
    records_from_payload as first_party_records,
)
from first_party_classifier import (  # noqa: E402
    classify_first_party_records,
    load_first_party_policy,
)
from reconcile import reconcile_records  # noqa: E402

SOURCES_PATH = REPO_ROOT / "sources.ttl"
VOCAB_PATH = ROOT / "vocabularies" / "kg-jobs.ttl"
RUNTIME_DIR = ROOT / "runtime"
LEGACY_SOURCE_PREFIX = "https://openknowledgegraphs.com/prototypes/kg-jobs/source/"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise LivePipelineError(f"invalid prior run timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise LivePipelineError("prior run timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def enforce_refresh_interval(
    runtime_dir: Path,
    source: SourceConfig,
    retrieved_at: str,
) -> dict[str, str]:
    run_path = runtime_dir / "run.json"
    if not run_path.exists():
        return {}
    try:
        previous = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LivePipelineError("existing runtime/run.json is not valid JSON") from exc
    if not isinstance(previous, dict):
        raise LivePipelineError("existing runtime/run.json must contain an object")
    raw_history = previous.get("sourceRefreshes", {})
    if not isinstance(raw_history, dict):
        raise LivePipelineError("existing sourceRefreshes must contain an object")
    history = {}
    for key, value in raw_history.items():
        if not isinstance(key, str) or not key or not isinstance(value, str) or not value:
            raise LivePipelineError("existing sourceRefreshes contains an invalid entry")
        _parse_utc(value)
        history[key] = value

    # Read snapshots created before per-source refresh history existed.
    previous_source = previous.get("sourceKey")
    previous_retrieved = previous.get("retrievedAt")
    if (
        isinstance(previous_source, str)
        and previous_source
        and isinstance(previous_retrieved, str)
        and previous_retrieved
        and previous_source not in history
    ):
        _parse_utc(previous_retrieved)
        history[previous_source] = previous_retrieved

    if source.key not in history:
        return history
    previous_time = _parse_utc(history[source.key])
    current_time = _parse_utc(retrieved_at)
    elapsed = (current_time - previous_time).total_seconds()
    if elapsed < 0:
        raise LivePipelineError("current time precedes the previous successful refresh")
    remaining = int(source.min_refresh_interval_seconds - elapsed)
    if remaining > 0:
        raise RefreshNotDueError(
            f"{source.key} permits one refresh every "
            f"{int(source.min_refresh_interval_seconds)} seconds; retry in {remaining} seconds"
        )
    return history


def _run_id(retrieved_at: str, record_ids: list[str]) -> str:
    digest = hashlib.sha256("\n".join(sorted(record_ids)).encode("utf-8")).hexdigest()[:12]
    stamp = retrieved_at.replace("-", "").replace(":", "").replace("+00:00", "Z")
    return f"{stamp}-{digest}"


def _normalized_identity(value) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))
    ).strip().casefold()


def _organization_alias_index(path: Path) -> dict[str, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LivePipelineError("reviewed organization projection is missing or invalid") from exc
    index: dict[str, str | None] = {}
    for organization in payload.get("organizations", []):
        if (
            organization.get("reviewStatus") != "evidence-reviewed"
            or not organization.get("iri")
        ):
            continue
        for raw_name in [organization.get("name"), *organization.get("aliases", [])]:
            name = _normalized_identity(raw_name)
            if not name:
                continue
            if name not in index:
                index[name] = organization["iri"]
            elif index[name] != organization["iri"]:
                index[name] = None
    return index


def _location_keys(value) -> list[str]:
    return sorted({
        _normalized_identity(part)
        for part in re.split(r"[,;/|]", str(value or ""))
        if _normalized_identity(part)
    })


def _source_key_for_dataset(dataset_uri: object, sources: dict[str, object]) -> str:
    value = str(dataset_uri or "")
    current = {
        str(config.dataset_uri): key for key, config in sources.items()
    }
    if value in current:
        return current[value]
    if value.startswith(LEGACY_SOURCE_PREFIX):
        key = value.removeprefix(LEGACY_SOURCE_PREFIX)
        if key in sources and value == f"{LEGACY_SOURCE_PREFIX}{key}":
            return key
    raise LivePipelineError(
        f"existing jobs snapshot uses an unknown source dataset IRI: {value!r}"
    )


def _migrate_record_source_iris(
    source_record: dict, sources: dict[str, object]
) -> tuple[str, dict]:
    """Cut prior runtime records over to registered source IRIs without loss."""

    record = dict(source_record)
    source_key = _source_key_for_dataset(record.get("sourceDataset"), sources)
    record["sourceDataset"] = sources[source_key].dataset_uri
    occurrences = []
    for raw_occurrence in record.get("sourceOccurrences", []):
        if not isinstance(raw_occurrence, dict):
            raise LivePipelineError("existing jobs snapshot has an invalid source occurrence")
        occurrence = dict(raw_occurrence)
        occurrence_key = _source_key_for_dataset(
            occurrence.get("sourceDataset"), sources
        )
        occurrence["sourceDataset"] = sources[occurrence_key].dataset_uri
        occurrences.append(occurrence)
    if record.get("sourceOccurrences") is not None:
        record["sourceOccurrences"] = occurrences
    return source_key, record


def _prepare_for_reconciliation(
    records: list[dict], organization_aliases: dict[str, str | None]
) -> list[dict]:
    output = []
    for source_record in records:
        record = dict(source_record)
        record.setdefault("firstParty", False)
        record.setdefault(
            "provider",
            str(record.get("sourceDataset") or "").rstrip("/").rsplit("/", 1)[-1],
        )
        record.setdefault("tenant", None)
        record.setdefault(
            "workplaceMode", "remote" if record.get("remote") else "unknown"
        )
        record.setdefault("locationKeys", _location_keys(record.get("location")))
        if not record.get("organizationIri"):
            organization_iri = organization_aliases.get(
                _normalized_identity(record.get("hiringOrganization"))
            )
            if organization_iri:
                record["organizationIri"] = organization_iri
        output.append(record)
    return sorted(output, key=lambda record: record["id"])


def run_pipeline(
    *,
    source_key: str = "himalayas",
    root: Path = ROOT,
    runtime_dir: Path = RUNTIME_DIR,
    retrieved_at: str | None = None,
    fetcher: Fetcher = fetch_json_http,
    first_party_fetcher=fetch_first_party_source,
    catalog_root: Path | None = None,
    include_review_aggregators: bool = False,
) -> dict:
    retrieved_at = retrieved_at or utc_now()
    repo_root = root.parent
    sources = (
        load_source_registry(repo_root / "sources.ttl")
        if include_review_aggregators
        else load_production_source_registry(repo_root / "sources.ttl")
    )
    try:
        production_first_party = load_production_first_party_sources(
            repo_root / "sources.ttl", repo_root / "data" / "organizations.json"
        )
    except FirstPartySourceError as exc:
        raise LivePipelineError(f"first-party production registry failed: {exc}") from exc
    overlap = set(sources) & set(production_first_party)
    if overlap:
        raise LivePipelineError(
            f"source keys overlap across production loaders: {', '.join(sorted(overlap))}"
        )
    sources.update(production_first_party)
    if source_key not in sources:
        raise LivePipelineError(
            f"unknown or disabled source {source_key!r}; available: {', '.join(sorted(sources))}"
        )
    source = sources[source_key]
    is_first_party = isinstance(source, FirstPartySource)
    supported = {
        "arbeitnow", "remotive", "himalayas", "jobicy", "jooble", "adzuna",
        "firstparty-greenhouse", "firstparty-lever", "firstparty-ashby",
        "firstparty-schema", "firstparty-graphwise", "firstparty-rippling",
        "firstparty-eccenca", "firstparty-teamtailor",
        "firstparty-same-site-detail",
        "firstparty-workday", "firstparty-webcruiter",
        "firstparty-workday-keyword", "firstparty-oracle-recruiting",
        "firstparty-amazon-jobs", "firstparty-successfactors-rmk-html",
        "firstparty-successfactors", "firstparty-ukg",
        "firstparty-softgarden", "firstparty-refline",
        "firstparty-emply", "firstparty-peopleadmin",
        "firstparty-selectminds",
        "firstparty-drupal-rss-detail", "firstparty-cnrs-unit-detail",
        "firstparty-microsoft-research",
    }
    if source.adapter not in supported:
        raise LivePipelineError(f"unsupported reviewed source adapter: {source.adapter!r}")
    if not is_first_party and (
        not source.source_queries or any(not query for query in source.source_queries)
    ):
        raise LivePipelineError(f"source {source.key} has an empty registry query")

    catalog_root = catalog_root or repo_root
    try:
        mention_index = load_match_index(
            catalog_root, root / "catalog-mention-policy.json"
        )
    except CatalogMentionError as exc:
        raise LivePipelineError(str(exc)) from exc

    source_refreshes = enforce_refresh_interval(runtime_dir, source, retrieved_at)

    # Adapters that declare one query family per request (Himalayas, Jobicy)
    # search their own API with our reviewed vocabulary terms; local
    # classification below still remains the sole eligibility decision, as a
    # safety net against an unreliable or overly broad source-side filter.
    is_multi_query = (
        not is_first_party
        and source.adapter in {"himalayas", "jobicy", "jooble", "adzuna"}
    )

    # Candidate retrieval is deliberately bounded; all KG eligibility
    # decisions below still come from the RDF vocabulary.
    payloads = []
    normalized = []
    query_results = []
    fetched_count = 0
    complete_source_snapshot = True if is_first_party else is_multi_query
    expected_source_total = None
    if is_first_party:
        try:
            payload = first_party_fetcher(source)
            normalized = first_party_records(payload, source)
        except FirstPartySourceError as exc:
            raise LivePipelineError(f"{source.key} failed safely: {exc}") from exc
        payloads.append(payload)
        fetched_count = len(normalized)
    request_numbers = (
        () if is_first_party else range(1, source.max_requests_per_run + 1)
    )
    for request_number in request_numbers:
        payload = fetcher(build_feed_url(source, request_number), source)
        payloads.append(payload)
        remaining = source.max_records_per_run - fetched_count
        if source.adapter == "arbeitnow":
            current_page, _, source_total = arbeitnow_pagination(payload)
            if current_page != request_number:
                raise LivePipelineError(
                    f"Arbeitnow returned page {current_page} for requested page {request_number}"
                )
            if expected_source_total is None:
                expected_source_total = source_total
            elif source_total != expected_source_total:
                raise LivePipelineError("Arbeitnow total changed during the paginated pull")
            page_records, page_count, page_complete = arbeitnow_records(
                payload, source, retrieved_at, limit=remaining
            )
        elif source.adapter == "remotive":
            page_records, page_count, page_complete = remotive_records(
                payload, source, retrieved_at
            )
        elif source.adapter == "jobicy":
            query_family = source.query_families[request_number - 1]
            page_records, page_count, page_complete = jobicy_records(
                payload, source, retrieved_at, query_family.text, limit=remaining
            )
            query_results.append(
                {
                    "queryUri": query_family.uri,
                    "query": query_family.text,
                    "queryConcepts": list(query_family.concept_uris),
                    "returnedCount": page_count,
                    "totalCount": payload["jobCount"],
                    "complete": page_complete,
                }
            )
        elif source.adapter == "jooble":
            query_family = source.query_families[request_number - 1]
            page_records, page_count, page_complete = jooble_records(
                payload, source, retrieved_at, query_family.text, limit=remaining
            )
            query_results.append(
                {
                    "queryUri": query_family.uri,
                    "query": query_family.text,
                    "queryConcepts": list(query_family.concept_uris),
                    "returnedCount": page_count,
                    "totalCount": payload["totalCount"],
                    "complete": page_complete,
                }
            )
        elif source.adapter == "adzuna":
            query_family = source.query_families[request_number - 1]
            page_records, page_count, page_complete = adzuna_records(
                payload, source, retrieved_at, query_family.text, limit=remaining
            )
            query_results.append(
                {
                    "queryUri": query_family.uri,
                    "query": query_family.text,
                    "queryConcepts": list(query_family.concept_uris),
                    "returnedCount": page_count,
                    "totalCount": payload["count"],
                    "complete": page_complete,
                }
            )
        else:
            query_family = source.query_families[request_number - 1]
            query = query_family.text
            page_records, page_count, page_complete = himalayas_records(
                payload, source, retrieved_at, query, limit=remaining
            )
            query_results.append(
                {
                    "queryUri": query_family.uri,
                    "query": query,
                    "queryConcepts": list(query_family.concept_uris),
                    "returnedCount": page_count,
                    "totalCount": payload["totalCount"],
                    "complete": page_complete,
                }
            )
        normalized.extend(page_records)
        fetched_count += page_count
        if is_multi_query:
            complete_source_snapshot = complete_source_snapshot and page_complete
        else:
            complete_source_snapshot = page_complete
        if fetched_count >= source.max_records_per_run:
            break
        if not is_multi_query and page_complete:
            break

    if not payloads:
        raise LivePipelineError(f"{source.key} produced no source responses")
    if is_multi_query and len(payloads) != len(source.source_queries):
        complete_source_snapshot = False
    if complete_source_snapshot and expected_source_total is not None:
        if fetched_count != expected_source_total:
            raise LivePipelineError(
                f"Arbeitnow claimed {expected_source_total} records but returned {fetched_count}"
            )
    deduplicated = deduplicate(normalized)
    match_terms = load_match_terms(root / "vocabularies" / "kg-jobs.ttl")
    current = (
        classify_first_party_records(
            deduplicated,
            match_terms,
            load_first_party_policy(root / "vocabularies" / "kg-jobs.ttl"),
        )
        if is_first_party
        else classify_records(deduplicated, match_terms)
    )
    all_previous = load_previous_records(runtime_dir)
    source_snapshots = {}
    for declared_key, prior_records in load_source_snapshots(runtime_dir).items():
        if declared_key not in sources:
            continue
        migrated_records = []
        for record in prior_records:
            actual_key, migrated = _migrate_record_source_iris(record, sources)
            if actual_key != declared_key:
                raise LivePipelineError(
                    f"source snapshot {declared_key!r} contains record from {actual_key!r}"
                )
            migrated_records.append(migrated)
        source_snapshots[declared_key] = migrated_records
    if not source_snapshots and all_previous:
        for record in all_previous:
            prior_key, migrated = _migrate_record_source_iris(record, sources)
            source_snapshots.setdefault(prior_key, []).append(migrated)
    previous_same_source = source_snapshots.get(source.key, [])
    refreshed = preserve_first_seen(current, previous_same_source, retrieved_at)
    source_snapshots[source.key] = refreshed
    source_classification_counts = {"qualified": 0, "review": 0, "not_match": 0}
    for record in refreshed:
        source_classification_counts[record["classification"]] += 1

    # Retain every first-party outcome in its per-source diagnostic snapshot,
    # but publish only qualified first-party records. Aggregator snapshots keep
    # their existing qualified-or-review behavior unchanged.
    unreconciled = [
        record
        for key in sorted(source_snapshots)
        for record in source_snapshots[key]
        if not isinstance(sources[key], FirstPartySource)
        or record.get("classification") == "qualified"
    ]
    organization_aliases = _organization_alias_index(
        repo_root / "data" / "organizations.json"
    )
    reconciled, reconciliation_audit = reconcile_records(
        _prepare_for_reconciliation(unreconciled, organization_aliases)
    )
    records = add_catalog_mentions(
        reconciled,
        mention_index,
    )

    classification_counts = {"qualified": 0, "review": 0, "not_match": 0}
    for record in records:
        classification_counts[record["classification"]] += 1
    run_id = _run_id(retrieved_at, [record["id"] for record in records])
    source_refreshes[source.key] = retrieved_at
    run = {
        "runId": run_id,
        "retrievedAt": retrieved_at,
        "sourceKey": source.key,
        "sourceName": source.attribution_text,
        "sourceDataset": source.dataset_uri,
        "sourceAttributionUrl": source.attribution_url,
        "sourceRefreshes": dict(sorted(source_refreshes.items())),
        "queryCount": 0 if is_first_party else (len(payloads) if is_multi_query else 1),
        "queries": [] if is_first_party else (
            list(source.source_queries[: len(payloads)])
            if is_multi_query else [source.source_query]
        ),
        "queryFamilies": [
            {
                "queryUri": family.uri,
                "query": family.text,
                "queryConcepts": list(family.concept_uris),
            }
            for family in (() if is_first_party else (
                source.query_families[: len(payloads)]
                if is_multi_query
                else source.query_families
            ))
        ],
        "requestCount": (
            first_party_request_count(payloads[0], source)
            if is_first_party else len(payloads)
        ),
        "fetchedCount": fetched_count,
        "rejectedCount": 0,
        "deduplicatedCount": len(records),
        "sourceClassificationCounts": source_classification_counts,
        "publicSourceCount": (
            source_classification_counts["qualified"]
            if is_first_party else len(refreshed)
        ),
        "publicationPolicy": (
            "first-party-qualified-only"
            if is_first_party else "existing-aggregator-policy"
        ),
        "completeSourceSnapshot": complete_source_snapshot,
        "activeCount": sum(1 for record in records if record.get("active")),
        "classificationCounts": classification_counts,
        "reconciliation": reconciliation_audit,
    }
    if is_multi_query:
        run["queryResults"] = query_results
    graph = build_graph(records, run, source)
    if is_multi_query:
        raw_payload = {
            "sourceKey": source.key,
            "responses": [
                {
                    "queryUri": family.uri,
                    "query": family.text,
                    "queryConcepts": list(family.concept_uris),
                    "payload": payload,
                }
                for family, payload in zip(source.query_families, payloads)
            ],
        }
    else:
        raw_payload = payloads[0] if len(payloads) == 1 else {
            "sourceKey": source.key,
            "pages": payloads,
        }
    publish_snapshot(
        records, run, graph, root, runtime_dir,
        raw_payload=raw_payload, source_key=source.key,
        source_snapshots=source_snapshots,
    )
    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a live KG jobs snapshot from a registered source."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly permit the registry-bounded network requests for this run",
    )
    parser.add_argument(
        "--source",
        default="himalayas",
        help="enabled dcterms:identifier from sources.ttl (default: himalayas)",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=RUNTIME_DIR,
        help="local snapshot directory override (default: jobs/runtime/)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("network access is disabled by default; pass --live explicitly")
    try:
        run = run_pipeline(source_key=args.source, runtime_dir=args.runtime_dir)
    except RefreshNotDueError as exc:
        # Nothing to do yet -- a scheduled or manual caller can legitimately
        # hit this before a source's cadence elapses. Exit 0 so it is
        # never mistaken for a fetch or validation failure.
        print(f"Skipping {args.source}: {exc}")
        return 0
    except LivePipelineError as exc:
        print(f"Live ingestion failed safely: {exc}", file=sys.stderr)
        return 1
    print(
        "Live snapshot published: "
        f"{run['deduplicatedCount']} unique candidates, "
        f"{run['classificationCounts']['qualified']} qualified, "
        f"{run['classificationCounts']['review']} review"
    )
    print(f"Runtime: {args.runtime_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
