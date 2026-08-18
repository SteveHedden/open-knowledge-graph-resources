#!/usr/bin/env python3
"""Validate and reproduce a captured Cloudflare Worker deployment."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any


VERSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")


def deployment_versions(payload: Any) -> list[tuple[str, float]]:
    if not isinstance(payload, dict):
        raise ValueError("Worker deployment status must be a JSON object")
    versions = payload.get("versions")
    if not isinstance(versions, list) or not versions:
        raise ValueError("Worker deployment status has no active versions")

    result: list[tuple[str, float]] = []
    for entry in versions:
        if not isinstance(entry, dict):
            raise ValueError("Worker deployment version entry must be an object")
        version_id = entry.get("version_id")
        percentage = entry.get("percentage")
        if not isinstance(version_id, str) or not VERSION_ID_RE.fullmatch(version_id):
            raise ValueError(f"Invalid Worker version ID: {version_id!r}")
        if isinstance(percentage, bool) or not isinstance(percentage, (int, float)):
            raise ValueError(f"Invalid traffic percentage for {version_id}: {percentage!r}")
        percentage = float(percentage)
        if not math.isfinite(percentage) or percentage <= 0 or percentage > 100:
            raise ValueError(f"Invalid traffic percentage for {version_id}: {percentage!r}")
        result.append((version_id, percentage))

    total = sum(percentage for _, percentage in result)
    if not math.isclose(total, 100.0, abs_tol=0.001):
        raise ValueError(f"Worker deployment traffic totals {total:g}%, not 100%")
    return result


def read_status(path: Path) -> list[tuple[str, float]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read Worker deployment status {path}: {error}") from error
    return deployment_versions(payload)


def percentage_text(value: float) -> str:
    return str(int(value)) if value.is_integer() else format(value, ".12g")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "specs"))
    parser.add_argument("--status-file", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    versions = read_status(args.status_file)
    if args.command == "validate":
        print(f"Captured {len(versions)} active Worker version(s) at 100% total traffic")
    else:
        for version_id, percentage in versions:
            print(f"{version_id}@{percentage_text(percentage)}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
