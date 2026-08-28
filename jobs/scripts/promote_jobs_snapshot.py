#!/usr/bin/env python3
"""Atomically promote a validated runtime snapshot into repo-root data/jobs/."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path


class PromotionError(RuntimeError):
    """The candidate snapshot cannot be promoted safely."""


def _validate_candidate(runtime: Path) -> None:
    for name in ("jobs.json", "jobs.ttl", "run.json"):
        path = runtime / name
        if not path.is_file() or path.stat().st_size == 0:
            raise PromotionError(f"runtime candidate is missing non-empty {name}")
    try:
        jobs = json.loads((runtime / "jobs.json").read_text(encoding="utf-8"))
        run = json.loads((runtime / "run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionError("runtime candidate contains invalid JSON") from exc
    if not isinstance(jobs, list) or not isinstance(run, dict):
        raise PromotionError("runtime candidate has an invalid snapshot shape")


def promote(runtime: Path, destination: Path) -> None:
    """Replace destination as one directory, rolling back a failed swap."""

    _validate_candidate(runtime)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".jobs-promotion-", dir=destination.parent))
    backup = destination.with_name(f".{destination.name}-previous")
    if backup.exists():
        shutil.rmtree(stage)
        raise PromotionError(f"stale promotion backup prevents publication: {backup}")
    try:
        for name in ("jobs.json", "jobs.ttl", "run.json"):
            shutil.copy2(runtime / name, stage / name)
        raw_stage = stage / "raw"
        raw_stage.mkdir()
        raw_source = runtime / "raw"
        if raw_source.is_dir():
            for path in sorted(raw_source.glob("*.json")):
                if not path.name.startswith("first-party-"):
                    shutil.copy2(path, raw_stage / path.name)
        manifest = destination / "manifest.json"
        if manifest.is_file():
            shutil.copy2(manifest, stage / "manifest.json")

        moved_previous = False
        try:
            if destination.exists():
                os.replace(destination, backup)
                moved_previous = True
            os.replace(stage, destination)
        except BaseException:
            if moved_previous and not destination.exists() and backup.exists():
                os.replace(backup, destination)
            raise
        else:
            if backup.exists():
                shutil.rmtree(backup)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        promote(args.runtime, args.destination)
    except (PromotionError, OSError) as exc:
        parser.exit(1, f"Jobs snapshot promotion failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
