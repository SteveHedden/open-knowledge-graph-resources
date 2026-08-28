#!/usr/bin/env python3
"""Build one atomic nightly production jobs snapshot in bounded parallel waves.

Workers fetch and validate sources in isolated runtimes. The parent replays only
successful raw payloads into one candidate runtime, so a failed source retains
its previous records and evidence. The 68-page careers monitor is run into that
candidate but never contributes records to the public jobs snapshot.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
from math import ceil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from career_discovery_monitor import (  # noqa: E402
    MAX_WORKERS as DISCOVERY_WORKERS,
    TIMEOUT_SECONDS as DISCOVERY_TIMEOUT_SECONDS,
    run_monitor,
)
from first_party_sources import (  # noqa: E402
    FirstPartySource,
    FirstPartySourceError,
    load_production_first_party_sources,
)
from live_pipeline import enforce_refresh_interval, run_pipeline, utc_now  # noqa: E402
from live_records import _atomic_replace_directory  # noqa: E402
from live_sources import (  # noqa: E402
    LivePipelineError,
    RefreshNotDueError,
    load_production_source_registry,
)
from source_schedule import (  # noqa: E402
    DEFAULT_BATCH_REQUEST_CAP,
    DEFAULT_BATCH_SOURCE_CAP,
    SourceScheduleError,
    bounded_weight_batches,
)
from task42_source_audit import TASK42_SOURCE_KEYS  # noqa: E402

TARGET_CRON = "0 3 * * *"
CATALOG_CRON = "0 6 * * *"
TARGET_WORKFLOW = ".github/workflows/update-jobs.yml"
REFRESH_INTERVAL_SECONDS = 86_400
MAX_PARALLEL_SOURCES = DEFAULT_BATCH_SOURCE_CAP
BATCH_REQUEST_CAP = DEFAULT_BATCH_REQUEST_CAP
SOURCE_TIMEOUT_SECONDS = 12 * 60
TARGET_WORKFLOW_TIMEOUT_SECONDS = 150 * 60
WORKFLOW_OVERHEAD_BUDGET_SECONDS = 12 * 60
EXPECTED_PRODUCTION_SOURCE_COUNT = 34
EXPECTED_TASK42_SOURCE_COUNT = 17
EXPECTED_DISCOVERY_COUNT = 68
EXPECTED_UNCOVERED_COUNT = 22
DEFAULT_RUNTIME = ROOT / "runtime"


class NightlyRunError(RuntimeError):
    """The production nightly orchestration contract is invalid."""


def production_sources() -> dict[str, object]:
    aggregators = load_production_source_registry(REPO_ROOT / "sources.ttl")
    first_party = load_production_first_party_sources(
        REPO_ROOT / "sources.ttl", REPO_ROOT / "data" / "organizations.json"
    )
    overlap = set(aggregators) & set(first_party)
    if overlap:
        raise NightlyRunError(
            f"production source identifiers overlap: {', '.join(sorted(overlap))}"
        )
    sources = {**aggregators, **first_party}
    if len(sources) != EXPECTED_PRODUCTION_SOURCE_COUNT:
        raise NightlyRunError(
            f"production source drift: expected {EXPECTED_PRODUCTION_SOURCE_COUNT}, "
            f"found {len(sources)}"
        )
    if not TASK42_SOURCE_KEYS <= set(first_party):
        missing = sorted(TASK42_SOURCE_KEYS - set(first_party))
        raise NightlyRunError(
            f"Task 42 production approvals are incomplete: {', '.join(missing)}"
        )
    if len(TASK42_SOURCE_KEYS) != EXPECTED_TASK42_SOURCE_COUNT:
        raise NightlyRunError("Task 42 source-count contract drifted")
    wrong_intervals = {
        key: sources[key].refresh_interval_seconds
        for key in TASK42_SOURCE_KEYS
        if sources[key].refresh_interval_seconds != REFRESH_INTERVAL_SECONDS
    }
    if wrong_intervals:
        raise NightlyRunError(
            f"Task 42 sources must use a 24-hour refresh interval: {wrong_intervals}"
        )
    return dict(sorted(sources.items()))


def bounded_parallel_batches(
    sources: dict[str, object], *, request_cap: int = BATCH_REQUEST_CAP,
    max_parallel: int = MAX_PARALLEL_SOURCES,
) -> list[list[str]]:
    if max_parallel <= 0 or max_parallel > MAX_PARALLEL_SOURCES:
        raise NightlyRunError(
            f"maximum parallel sources must be between 1 and {MAX_PARALLEL_SOURCES}"
        )
    weights = {
        key: source.max_requests_per_batch for key, source in sources.items()
    }
    try:
        return bounded_weight_batches(
            weights, request_cap=request_cap, source_cap=max_parallel
        )
    except SourceScheduleError as exc:
        raise NightlyRunError(str(exc)) from exc


def worst_case_seconds(
    *, source_batches: int, discovery_count: int = EXPECTED_DISCOVERY_COUNT,
    source_timeout_seconds: int = SOURCE_TIMEOUT_SECONDS,
) -> int:
    discovery_waves = ceil(discovery_count / DISCOVERY_WORKERS)
    discovery_budget = discovery_waves * DISCOVERY_TIMEOUT_SECONDS
    if discovery_budget > WORKFLOW_OVERHEAD_BUDGET_SECONDS:
        raise NightlyRunError(
            "discovery monitor exceeds the reviewed workflow-overhead budget"
        )
    return source_batches * source_timeout_seconds + WORKFLOW_OVERHEAD_BUDGET_SECONDS


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _source_worker(source_key: str, output: str) -> None:
    result = {"error": None, "rawPayload": None, "run": None}
    # ``publish_snapshot`` recovers stale ``.kg-jobs-live-*`` stages by
    # scanning the runtime's parent. Give every concurrent worker an exclusive
    # parent so one source cannot remove another source's in-progress stage.
    worker_root = Path(output).with_suffix("") / "runtime"
    try:
        run = run_pipeline(source_key=source_key, runtime_dir=worker_root)
        raw_path = worker_root / "raw" / f"{source_key}.json"
        result["rawPayload"] = json.loads(raw_path.read_text(encoding="utf-8"))
        result["run"] = run
    except (LivePipelineError, FirstPartySourceError, OSError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    _atomic_json(Path(output), result)


def execute_process_batch(
    batch: list[str], output_dir: Path, *,
    source_timeout_seconds: int = SOURCE_TIMEOUT_SECONDS,
    context=None, worker=_source_worker,
) -> dict[str, dict]:
    """Execute one bounded wave and hard-stop each source at its wall-clock cap."""

    if len(batch) > MAX_PARALLEL_SOURCES:
        raise NightlyRunError("nightly batch exceeds the parallel-source cap")
    if source_timeout_seconds <= 0 or source_timeout_seconds > SOURCE_TIMEOUT_SECONDS:
        raise NightlyRunError(
            f"source timeout must be between 1 and {SOURCE_TIMEOUT_SECONDS} seconds"
        )
    context = context or multiprocessing.get_context("spawn")
    started: dict[str, float] = {}
    processes = {}
    paths = {}
    for key in batch:
        path = output_dir / f"{key}.json"
        process = context.Process(target=worker, args=(key, str(path)))
        process.start()
        started[key] = time.monotonic()
        processes[key] = process
        paths[key] = path
    outcomes = {}
    for key in batch:
        process = processes[key]
        remaining = max(
            0.0, source_timeout_seconds - (time.monotonic() - started[key])
        )
        process.join(remaining)
        if process.is_alive():
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
            outcomes[key] = {
                "error": f"source exceeded {source_timeout_seconds}s wall-clock cap",
                "rawPayload": None,
                "run": None,
                "status": "timed-out",
            }
            continue
        if process.exitcode != 0 or not paths[key].is_file():
            outcomes[key] = {
                "error": f"source worker exited {process.exitcode} without a result",
                "rawPayload": None,
                "run": None,
                "status": "worker-failure",
            }
            continue
        try:
            outcome = json.loads(paths[key].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            outcomes[key] = {
                "error": f"invalid source worker result: {type(exc).__name__}",
                "rawPayload": None,
                "run": None,
                "status": "worker-failure",
            }
            continue
        outcome["status"] = "fetched" if not outcome.get("error") else "fetch-failure"
        outcomes[key] = outcome
    return outcomes


def _default_batch_executor(batch, _sources, source_timeout_seconds):
    with tempfile.TemporaryDirectory(prefix="okg-task42-nightly-workers-") as directory:
        return execute_process_batch(
            batch, Path(directory), source_timeout_seconds=source_timeout_seconds
        )


def _replay_source(
    source_key: str, source: object, raw_payload: object, candidate: Path,
    retrieved_at: str,
) -> dict:
    if isinstance(source, FirstPartySource):
        return run_pipeline(
            source_key=source_key,
            runtime_dir=candidate,
            retrieved_at=retrieved_at,
            first_party_fetcher=lambda _source: raw_payload,
        )
    if (
        isinstance(raw_payload, dict)
        and isinstance(raw_payload.get("responses"), list)
    ):
        payloads = [row["payload"] for row in raw_payload["responses"]]
    elif (
        isinstance(raw_payload, dict)
        and isinstance(raw_payload.get("pages"), list)
    ):
        payloads = list(raw_payload["pages"])
    else:
        payloads = [raw_payload]
    queue = list(payloads)

    def replay_fetcher(_url, _source):
        if not queue:
            raise LivePipelineError(f"{source_key} replay exhausted its raw responses")
        return queue.pop(0)

    run = run_pipeline(
        source_key=source_key,
        runtime_dir=candidate,
        retrieved_at=retrieved_at,
        fetcher=replay_fetcher,
    )
    if queue:
        raise LivePipelineError(
            f"{source_key} replay left {len(queue)} raw responses unused"
        )
    return run


def _prior_refreshes(runtime_dir: Path) -> set[str]:
    path = runtime_dir / "run.json"
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    values = payload.get("sourceRefreshes", {}) if isinstance(payload, dict) else {}
    return set(values) if isinstance(values, dict) else set()


def _candidate_runtime(runtime_dir: Path) -> Path:
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".kg-jobs-nightly-", dir=runtime_dir.parent))
    if runtime_dir.exists():
        shutil.copytree(runtime_dir, stage, dirs_exist_ok=True)
    else:
        (stage / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    return stage


def run_nightly(
    *, runtime_dir: Path = DEFAULT_RUNTIME, retrieved_at: str | None = None,
    selected_source: str = "all", max_parallel: int = MAX_PARALLEL_SOURCES,
    batch_request_cap: int = BATCH_REQUEST_CAP,
    source_timeout_seconds: int = SOURCE_TIMEOUT_SECONDS,
    batch_executor=_default_batch_executor, monitor_runner=run_monitor,
) -> dict:
    """Prepare and atomically install one complete last-good production runtime."""

    retrieved_at = retrieved_at or utc_now()
    sources = production_sources()
    if selected_source != "all":
        if selected_source not in sources:
            raise NightlyRunError(f"unknown production source {selected_source!r}")
        selected = {selected_source: sources[selected_source]}
    else:
        selected = sources
    planned_batches = bounded_parallel_batches(
        selected, request_cap=batch_request_cap, max_parallel=max_parallel
    )
    budget = worst_case_seconds(
        source_batches=len(planned_batches),
        source_timeout_seconds=source_timeout_seconds,
    )
    if budget >= TARGET_WORKFLOW_TIMEOUT_SECONDS:
        raise NightlyRunError(
            f"nightly worst-case budget {budget}s reaches the workflow timeout"
        )

    prior_refreshes = _prior_refreshes(runtime_dir)
    due: dict[str, object] = {}
    not_due = []
    for key, source in selected.items():
        try:
            enforce_refresh_interval(runtime_dir, source, retrieved_at)
        except RefreshNotDueError:
            not_due.append(key)
        else:
            due[key] = source
    due_batches = bounded_parallel_batches(
        due, request_cap=batch_request_cap, max_parallel=max_parallel
    )
    outcomes = {}
    for batch in due_batches:
        result = batch_executor(batch, due, source_timeout_seconds)
        if set(result) != set(batch):
            raise NightlyRunError("batch executor did not return every requested source")
        outcomes.update(result)

    candidate = _candidate_runtime(runtime_dir)
    source_results = [
        {"sourceKey": key, "status": "refresh-interval-retained", "error": None}
        for key in sorted(not_due)
    ]
    try:
        for key in sorted(due):
            outcome = outcomes.get(key) or {
                "error": "missing source worker outcome", "rawPayload": None
            }
            error = outcome.get("error")
            if not error:
                try:
                    run = _replay_source(
                        key, due[key], outcome.get("rawPayload"), candidate, retrieved_at
                    )
                except (LivePipelineError, FirstPartySourceError, OSError, ValueError) as exc:
                    error = f"replay failed: {type(exc).__name__}: {exc}"
                else:
                    source_results.append({
                        "sourceKey": key,
                        "status": "refreshed",
                        "error": None,
                        "fetchedCount": run["fetchedCount"],
                        "publicSourceCount": run["publicSourceCount"],
                        "sourceClassificationCounts": run["sourceClassificationCounts"],
                    })
                    continue
            had_last_good = (
                key in prior_refreshes
                or (runtime_dir / "sources" / f"{key}.json").is_file()
                or (runtime_dir / "raw" / f"{key}.json").is_file()
            )
            source_results.append({
                "sourceKey": key,
                "status": (
                    "retained-last-good" if had_last_good else "isolated-failure"
                ),
                "error": str(error),
            })

        monitor = monitor_runner(
            output=candidate / "careers-discovery" / "run.json",
            retrieved_at=retrieved_at,
        )
        monitored_count = (monitor.get("counts") or {}).get("pages")
        if monitored_count != EXPECTED_DISCOVERY_COUNT:
            raise NightlyRunError(
                f"nightly discovery drift: expected {EXPECTED_DISCOVERY_COUNT}, "
                f"found {monitored_count}"
            )
        failures = [
            row for row in source_results
            if row["status"] in {"retained-last-good", "isolated-failure"}
        ]
        summary = {
            "schemaVersion": 2,
            "mode": "production-nightly-atomic",
            "retrievedAt": retrieved_at,
            "runtimePublicationPerformed": True,
            "repositoryPublicationPerformed": False,
            "targetCron": TARGET_CRON,
            "catalogCron": CATALOG_CRON,
            "targetWorkflow": TARGET_WORKFLOW,
            "refreshIntervalSeconds": REFRESH_INTERVAL_SECONDS,
            "productionSourceCount": len(sources),
            "selectedSourceCount": len(selected),
            "task42SourceCount": len(TASK42_SOURCE_KEYS),
            "plannedBatches": planned_batches,
            "executedBatches": due_batches,
            "batchRequestCap": batch_request_cap,
            "maxParallelSources": max_parallel,
            "sourceTimeoutSeconds": source_timeout_seconds,
            "workflowOverheadBudgetSeconds": WORKFLOW_OVERHEAD_BUDGET_SECONDS,
            "worstCaseSeconds": budget,
            "workflowTimeoutSeconds": TARGET_WORKFLOW_TIMEOUT_SECONDS,
            "sourceFailures": len(failures),
            "sourceResults": sorted(source_results, key=lambda row: row["sourceKey"]),
            "discovery": monitor.get("counts"),
            "discoveryPublicationPerformed": False,
            "uncoveredOrganizations": EXPECTED_UNCOVERED_COUNT,
        }
        _atomic_json(candidate / "nightly-run.json", summary)
        _atomic_replace_directory(candidate, runtime_dir)
        return summary
    except BaseException:
        if candidate.exists():
            shutil.rmtree(candidate)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--source", default="all")
    parser.add_argument("--max-parallel", type=int, default=MAX_PARALLEL_SOURCES)
    parser.add_argument("--batch-request-cap", type=int, default=BATCH_REQUEST_CAP)
    parser.add_argument(
        "--source-timeout-seconds", type=int, default=SOURCE_TIMEOUT_SECONDS
    )
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("network access is disabled by default; pass --live explicitly")
    try:
        summary = run_nightly(
            runtime_dir=args.runtime_dir,
            selected_source=args.source,
            max_parallel=args.max_parallel,
            batch_request_cap=args.batch_request_cap,
            source_timeout_seconds=args.source_timeout_seconds,
        )
    except (
        NightlyRunError, LivePipelineError, FirstPartySourceError,
        SourceScheduleError, OSError, ValueError,
    ) as exc:
        print(f"Nightly production refresh failed before publication: {exc}", file=sys.stderr)
        return 1
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"failures={summary['sourceFailures']}\n")
            stream.write(f"sources={summary['selectedSourceCount']}\n")
    print(json.dumps({
        "discovery": summary["discovery"],
        "sourceFailures": summary["sourceFailures"],
        "sources": summary["selectedSourceCount"],
    }, sort_keys=True))
    # Isolated source failures are reported after the last-good candidate has
    # been published. The workflow raises its final alert only after safely
    # promoting and committing any other successful source changes.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
