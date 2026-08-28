#!/usr/bin/env python3
"""Build a deterministic, tracked replay archive for the Task 42 live review."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from first_party_sources import load_first_party_sources  # noqa: E402
from task42_source_audit import task42_review_sources  # noqa: E402

DEFAULT_RUNTIME = ROOT / "runtime" / "task42-review-final"
DEFAULT_OUTPUT = ROOT / "audits" / "task42-live-review-inputs.zip"
DEFAULT_MANIFEST = ROOT / "audits" / "task42-live-review-inputs-manifest.json"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class ArchiveError(RuntimeError):
    """The live-review runtime cannot produce a complete replay archive."""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"cannot read valid JSON from {path}") from exc


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _contract(source) -> dict:
    return {
        "adapter": source.adapter,
        "allowedHost": source.allowed_host,
        "attributionText": source.attribution_text,
        "attributionUrl": source.attribution_url,
        "careersPage": source.careers_page,
        "datasetUri": source.dataset_uri,
        "endpoint": source.endpoint,
        "extractionMode": source.extraction_mode,
        "key": source.key,
        "maxRecordsPerRun": source.max_records_per_run,
        "maxRequestsPerRun": source.max_requests_per_run,
        "maxRequestsPerBatch": source.max_requests_per_batch,
        "maxResponseBytes": source.max_response_bytes,
        "organizationIri": source.organization_iri,
        "provider": source.provider,
        "refreshIntervalSeconds": source.refresh_interval_seconds,
        "republicationStatus": source.republication_status,
        "tenant": source.tenant,
        "timeoutSeconds": source.timeout_seconds,
    }


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def build_archive(
    runtime_dir: Path = DEFAULT_RUNTIME,
    output: Path = DEFAULT_OUTPUT,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict:
    run = _read_json(runtime_dir / "run.json")
    if run.get("mode") != "live-local-review" or run.get("publicationPerformed") is not False:
        raise ArchiveError("runtime must be a successful nonpublishing live review")
    sources = task42_review_sources()
    results = {
        row.get("sourceKey"): row
        for row in run.get("sourceResults", [])
        if isinstance(row, dict) and row.get("sourceKey") in sources
    }
    if set(results) != set(sources):
        raise ArchiveError("runtime does not cover every Task 42 review source")
    if any(
        row.get("status") not in {"refreshed", "refresh-interval-retained"}
        for row in results.values()
    ):
        raise ArchiveError("runtime contains an unsuccessful Task 42 source")

    entries: dict[str, bytes] = {
        "run.json": _json_bytes(run),
        "source-contracts.json": _json_bytes({
            "schemaVersion": 1,
            "sources": [_contract(sources[key]) for key in sorted(sources)],
        }),
    }
    for key in sorted(sources):
        raw_candidates = sorted((runtime_dir / "raw").glob(f"{key}.*"))
        if len(raw_candidates) != 1:
            raise ArchiveError(f"{key} requires exactly one retained raw response")
        normalized = runtime_dir / "sources" / f"{key}.json"
        if not normalized.is_file():
            raise ArchiveError(f"{key} lacks its normalized diagnostic snapshot")
        entries[f"raw/{raw_candidates[0].name}"] = raw_candidates[0].read_bytes()
        entries[f"sources/{normalized.name}"] = normalized.read_bytes()

    manifest = {
        "archiveFormat": "task42-live-review-replay-v1",
        "publicationPerformed": False,
        "retrievedAt": run.get("retrievedAt"),
        "sourceCount": len(sources),
        "sources": sorted(sources),
        "entries": {
            name: {
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            for name, body in sorted(entries.items())
        },
        "trackedDependencies": {
            "catalogMentionPolicySha256": hashlib.sha256(
                (ROOT / "catalog-mention-policy.json").read_bytes()
            ).hexdigest(),
            "classifierVocabularySha256": hashlib.sha256(
                (ROOT / "vocabularies" / "kg-jobs.ttl").read_bytes()
            ).hexdigest(),
            "sourcesRegistrySha256": hashlib.sha256(
                (REPO_ROOT / "sources.ttl").read_bytes()
            ).hexdigest(),
        },
    }
    entries["manifest.json"] = _json_bytes(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, body in sorted(entries.items()):
            archive.writestr(_zip_info(name), body)
    manifest["archive"] = {
        "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    manifest_path.write_bytes(_json_bytes(manifest))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    try:
        manifest = build_archive(args.runtime_dir, args.output, args.manifest)
    except ArchiveError as exc:
        print(f"Task 42 archive failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "archive": str(args.output),
        "bytes": manifest["archive"]["bytes"],
        "sha256": manifest["archive"]["sha256"],
        "sources": manifest["sourceCount"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
