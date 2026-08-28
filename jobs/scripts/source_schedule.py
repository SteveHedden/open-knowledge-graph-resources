#!/usr/bin/env python3
"""Derive serialized production job-source batches from the RDF registry."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from first_party_sources import (  # noqa: E402
    FirstPartySourceError,
    load_production_first_party_sources,
)
from live_sources import (  # noqa: E402
    LivePipelineError,
    load_production_source_registry,
)


DEFAULT_BATCH_REQUEST_CAP = 128
DEFAULT_BATCH_SOURCE_CAP = 4


class SourceScheduleError(RuntimeError):
    """The requested source or batch contract is invalid."""


def production_source_weights(repo_root: Path = REPO_ROOT) -> dict[str, int]:
    aggregators = load_production_source_registry(repo_root / "sources.ttl")
    first_party = load_production_first_party_sources(
        repo_root / "sources.ttl", repo_root / "data" / "organizations.json"
    )
    overlap = set(aggregators) & set(first_party)
    if overlap:
        raise SourceScheduleError(
            f"source identifiers overlap: {', '.join(sorted(overlap))}"
        )
    return {
        **{key: source.max_requests_per_batch for key, source in aggregators.items()},
        **{key: source.max_requests_per_batch for key, source in first_party.items()},
    }


def bounded_batches(
    requested: str = "all", *, request_cap: int = DEFAULT_BATCH_REQUEST_CAP,
    source_cap: int = DEFAULT_BATCH_SOURCE_CAP,
    repo_root: Path = REPO_ROOT,
) -> list[list[str]]:
    if request_cap <= 0:
        raise SourceScheduleError("batch request cap must be positive")
    if source_cap <= 0:
        raise SourceScheduleError("batch source cap must be positive")
    weights = production_source_weights(repo_root)
    if requested != "all":
        if requested not in weights:
            raise SourceScheduleError(
                f"unknown or non-production source {requested!r}; "
                f"available: {', '.join(sorted(weights))}"
            )
        return [[requested]]
    return bounded_weight_batches(
        weights, request_cap=request_cap, source_cap=source_cap
    )


def bounded_weight_batches(
    weights: dict[str, int], *, request_cap: int = DEFAULT_BATCH_REQUEST_CAP,
    source_cap: int = DEFAULT_BATCH_SOURCE_CAP,
) -> list[list[str]]:
    """Pack an explicit source subset by its declared per-batch request weight."""

    if request_cap <= 0:
        raise SourceScheduleError("batch request cap must be positive")
    if source_cap <= 0:
        raise SourceScheduleError("batch source cap must be positive")
    if any(not key or not isinstance(weight, int) or weight <= 0 for key, weight in weights.items()):
        raise SourceScheduleError("source weights must be positive integers")
    batches: list[list[str]] = []
    batch_weights: list[int] = []
    for key in sorted(weights, key=lambda item: (-weights[item], item)):
        weight = weights[key]
        if weight > request_cap:
            raise SourceScheduleError(
                f"source {key} request cap {weight} exceeds batch cap {request_cap}"
            )
        target = next(
            (
                index for index, batch in enumerate(batches)
                if len(batch) < source_cap
                and batch_weights[index] + weight <= request_cap
            ),
            None,
        )
        if target is None:
            batches.append([key])
            batch_weights.append(weight)
        else:
            batches[target].append(key)
            batch_weights[target] += weight
    return [sorted(batch) for batch in batches]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="all")
    parser.add_argument(
        "--batch-request-cap", type=int, default=DEFAULT_BATCH_REQUEST_CAP
    )
    parser.add_argument(
        "--batch-source-cap", type=int, default=DEFAULT_BATCH_SOURCE_CAP
    )
    args = parser.parse_args(argv)
    try:
        batches = bounded_batches(
            args.source,
            request_cap=args.batch_request_cap,
            source_cap=args.batch_source_cap,
        )
    except (SourceScheduleError, LivePipelineError, FirstPartySourceError) as exc:
        print(f"Source schedule failed: {exc}", file=sys.stderr)
        return 1
    for batch in batches:
        print(" ".join(batch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
