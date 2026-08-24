#!/usr/bin/env python3
"""Rebuild only the page-backed catalog-mention projection, without network access."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from rdflib import Graph, URIRef

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from catalog_snapshot import (  # noqa: E402
    read_jobs_manifest,
    verify_jobs_manifest,
    write_jobs_manifest,
)
from catalog_mentions import add_catalog_mentions, load_match_index  # noqa: E402
from live_records import KGJDLIVE, SCHEMA, validate_graph  # noqa: E402


def _replace_bytes(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _publish_verified_snapshot(
    repo_root: Path,
    json_bytes: bytes,
    ttl_bytes: bytes,
    *,
    completed_at: str | None = None,
) -> dict:
    """Stage, manifest, verify, and failure-atomically publish all three files."""
    current_manifest = read_jobs_manifest(repo_root)
    jobs_dir = repo_root / "data" / "jobs"
    destinations = (
        jobs_dir / "jobs.json",
        jobs_dir / "jobs.ttl",
        jobs_dir / "manifest.json",
    )
    originals = {path: path.read_bytes() for path in destinations}

    with tempfile.TemporaryDirectory(
        prefix=".okg-jobs-mention-stage.", dir=repo_root
    ) as directory:
        staging_root = Path(directory)
        shutil.copytree(jobs_dir, staging_root / "data" / "jobs")
        _replace_bytes(staging_root / "data" / "jobs" / "jobs.json", json_bytes)
        _replace_bytes(staging_root / "data" / "jobs" / "jobs.ttl", ttl_bytes)
        write_jobs_manifest(
            staging_root,
            started_at=str(current_manifest["startedAt"]),
            source_retrieved_at=str(current_manifest["sourceRetrievedAt"]),
            completed_at=completed_at or _utc_now(),
        )
        staged_manifest = verify_jobs_manifest(staging_root)
        replacements = {
            jobs_dir / "jobs.json": json_bytes,
            jobs_dir / "jobs.ttl": ttl_bytes,
            jobs_dir / "manifest.json": (
                staging_root / "data" / "jobs" / "manifest.json"
            ).read_bytes(),
        }

        try:
            for path in destinations:
                _replace_bytes(path, replacements[path])
            verify_jobs_manifest(repo_root)
        except Exception:
            for path in destinations:
                _replace_bytes(path, originals[path])
            verify_jobs_manifest(repo_root)
            raise
    return staged_manifest


def rebuild(
    repo_root: Path = REPO_ROOT,
    prototype_root: Path = ROOT,
    ttl_source: Path | None = None,
    completed_at: str | None = None,
) -> tuple[int, int, str]:
    jobs_path = repo_root / "data" / "jobs" / "jobs.json"
    ttl_path = repo_root / "data" / "jobs" / "jobs.ttl"
    records = json.loads(jobs_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("jobs JSON projection must contain an array")

    before = {
        record["id"]: (record.get("classification"), record.get("evidence"))
        for record in records
    }
    index = load_match_index(
        repo_root, prototype_root / "catalog-mention-policy.json"
    )
    enriched = add_catalog_mentions(records, index)
    after = {
        record["id"]: (record.get("classification"), record.get("evidence"))
        for record in enriched
    }
    if before != after:
        raise ValueError("catalog mention rebuilding changed classification evidence")

    graph = Graph()
    graph.parse(ttl_source or ttl_path, format="turtle")
    graph.remove((None, SCHEMA.mentions, None))
    for record in enriched:
        job = KGJDLIVE[f"job/{quote(record['id'], safe='')}"]
        for mention in record["catalogMentions"]:
            graph.add((job, SCHEMA.mentions, URIRef(mention["canonicalUrl"])))
    validate_graph(graph, prototype_root)

    json_bytes = (
        json.dumps(enriched, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(suffix=".ttl", delete=False) as handle:
        temporary_ttl = Path(handle.name)
    try:
        graph.serialize(destination=str(temporary_ttl), format="turtle")
        ttl_bytes = temporary_ttl.read_bytes()
    finally:
        temporary_ttl.unlink(missing_ok=True)
    manifest = _publish_verified_snapshot(
        repo_root,
        json_bytes,
        ttl_bytes,
        completed_at=completed_at,
    )
    mention_count = sum(len(record["catalogMentions"]) for record in enriched)
    mentioned_jobs = sum(bool(record["catalogMentions"]) for record in enriched)
    return mentioned_jobs, mention_count, str(manifest["generationId"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ttl-source",
        type=Path,
        help="optional known-good jobs.ttl baseline to enrich instead of the current file",
    )
    parser.add_argument(
        "--completed-at",
        help="optional deterministic UTC completion timestamp for the jobs manifest",
    )
    args = parser.parse_args()
    mentioned_jobs, mention_count, generation_id = rebuild(
        ttl_source=args.ttl_source,
        completed_at=args.completed_at,
    )
    print(
        f"Rebuilt catalog mentions without network access: "
        f"{mention_count} mentions across {mentioned_jobs} jobs; "
        f"verified generation {generation_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
