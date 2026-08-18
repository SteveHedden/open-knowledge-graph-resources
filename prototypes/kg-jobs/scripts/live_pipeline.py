#!/usr/bin/env python3
"""Pull one bounded registered source into a local snapshot.

Network access is impossible unless the caller supplies ``--live``. In
production this script runs on an hourly schedule (see
``.github/workflows/update-jobs.yml``), once per registered source; each
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
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from classifier import load_match_terms  # noqa: E402
from live_records import (  # noqa: E402
    build_graph,
    classify_records,
    deduplicate,
    load_previous_records,
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

SOURCES_PATH = ROOT / "sources.ttl"
VOCAB_PATH = ROOT / "vocabularies" / "kg-jobs.ttl"
RUNTIME_DIR = ROOT / "runtime"


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


def run_pipeline(
    *,
    source_key: str = "himalayas",
    root: Path = ROOT,
    runtime_dir: Path = RUNTIME_DIR,
    retrieved_at: str | None = None,
    fetcher: Fetcher = fetch_json_http,
) -> dict:
    retrieved_at = retrieved_at or utc_now()
    sources = load_source_registry(root / "sources.ttl")
    if source_key not in sources:
        raise LivePipelineError(
            f"unknown or disabled source {source_key!r}; available: {', '.join(sorted(sources))}"
        )
    source = sources[source_key]
    if source.adapter not in {"arbeitnow", "remotive", "himalayas", "jobicy", "jooble"}:
        raise LivePipelineError(f"unsupported reviewed source adapter: {source.adapter!r}")
    if not source.source_queries or any(not query for query in source.source_queries):
        raise LivePipelineError(f"source {source.key} has an empty registry query")

    source_refreshes = enforce_refresh_interval(runtime_dir, source, retrieved_at)

    # Adapters that declare one query family per request (Himalayas, Jobicy)
    # search their own API with our reviewed vocabulary terms; local
    # classification below still remains the sole eligibility decision, as a
    # safety net against an unreliable or overly broad source-side filter.
    is_multi_query = source.adapter in {"himalayas", "jobicy", "jooble"}

    # Candidate retrieval is deliberately bounded; all KG eligibility
    # decisions below still come from the RDF vocabulary.
    payloads = []
    normalized = []
    query_results = []
    fetched_count = 0
    complete_source_snapshot = is_multi_query
    expected_source_total = None
    for request_number in range(1, source.max_requests_per_run + 1):
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
    current = classify_records(deduplicated, match_terms)
    all_previous = load_previous_records(runtime_dir)
    previous_same_source = [
        record for record in all_previous
        if record.get("sourceDataset") == source.dataset_uri
    ]
    other_source_records = [
        record for record in all_previous
        if record.get("sourceDataset") != source.dataset_uri
    ]
    refreshed = preserve_first_seen(current, previous_same_source, retrieved_at)
    # Refreshing one source must not discard another source's most recently
    # published records -- each source has its own independent refresh
    # cadence (see enforce_refresh_interval), and the local reviewer page is
    # meant to show the union of every enabled source.
    records = sorted(refreshed + other_source_records, key=lambda record: record["id"])

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
        "queryCount": len(payloads) if is_multi_query else 1,
        "queries": (
            list(source.source_queries[: len(payloads)])
            if is_multi_query
            else [source.source_query]
        ),
        "queryFamilies": [
            {
                "queryUri": family.uri,
                "query": family.text,
                "queryConcepts": list(family.concept_uris),
            }
            for family in (
                source.query_families[: len(payloads)]
                if is_multi_query
                else source.query_families
            )
        ],
        "requestCount": len(payloads),
        "fetchedCount": fetched_count,
        "rejectedCount": 0,
        "deduplicatedCount": len(records),
        "completeSourceSnapshot": complete_source_snapshot,
        "activeCount": sum(1 for record in records if record.get("active")),
        "classificationCounts": classification_counts,
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("network access is disabled by default; pass --live explicitly")
    try:
        run = run_pipeline(source_key=args.source)
    except RefreshNotDueError as exc:
        # Nothing to do yet -- an hourly scheduled caller legitimately hits
        # this for slower-cadence sources on most runs. Exit 0 so it is
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
    print(f"Runtime: {RUNTIME_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
