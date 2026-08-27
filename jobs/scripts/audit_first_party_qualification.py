#!/usr/bin/env python3
"""Verify the frozen run-183 corpus and emit the Task 40 qualification audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "first-party-run-183"
OUTPUT_PATH = ROOT / "audits" / "task40-first-party-qualification.json"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from classifier import load_match_terms  # noqa: E402
from first_party_classifier import (  # noqa: E402
    classify_first_party_record,
    load_first_party_policy,
)
from first_party_sources import load_first_party_sources  # noqa: E402


class QualificationAuditError(RuntimeError):
    """Frozen provenance or audit invariants failed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_summary(record: dict) -> list[dict]:
    def current_concept(uri: str) -> str:
        marker = "/vocab/"
        return (
            f"https://openknowledgegraphs.com/jobs/vocab/{uri.rsplit(marker, 1)[1]}"
            if marker in uri
            else uri
        )

    return sorted(
        [
            {
                "concept": current_concept(item["concept_uri"]),
                "field": item["source_field"],
                "negated": bool(item["negated"]),
                "phrase": item["matched_phrase"],
                "strength": item["strength"],
            }
            for item in record.get("evidence", [])
        ],
        key=lambda row: (
            row["field"], row["concept"], row["phrase"], row["negated"]
        ),
    )


def build_audit(
    fixture_root: Path = FIXTURE_ROOT,
    archive: Path | None = None,
) -> dict:
    manifest_path = fixture_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if archive is not None and _sha256(archive) != manifest["artifact"]["sha256"]:
        raise QualificationAuditError("selected baseline archive SHA-256 does not match run 183")
    for relative, expected in manifest["files"].items():
        path = fixture_root / relative
        if not path.is_file() or _sha256(path) != expected:
            raise QualificationAuditError(f"frozen fixture hash mismatch: {relative}")

    sources = load_first_party_sources()
    terms = load_match_terms(ROOT / "vocabularies" / "kg-jobs.ttl")
    policy = load_first_party_policy(ROOT / "vocabularies" / "kg-jobs.ttl")
    before_counts = Counter()
    after_counts = Counter()
    unique_ids = set()
    records_audit = []
    withheld = []
    after_by_title: dict[str, list[str]] = {}

    for path in sorted((fixture_root / "sources").glob("*.json")):
        source_key = path.stem
        source = sources[source_key]
        records = json.loads(path.read_text(encoding="utf-8"))
        for frozen in records:
            record_id = frozen["id"]
            if record_id in unique_ids:
                raise QualificationAuditError(f"duplicate frozen record identity: {record_id}")
            unique_ids.add(record_id)
            before_counts[frozen["classification"]] += 1
            current = dict(frozen)
            current["sourceDataset"] = source.dataset_uri
            after = classify_first_party_record(current, terms, policy)
            after_counts[after["classification"]] += 1
            after_by_title.setdefault(after["title"], []).append(after["classification"])
            qualification = after["qualificationAudit"]
            audit_row = {
                "afterClassification": after["classification"],
                "afterEvidence": _evidence_summary(after),
                "beforeClassification": frozen["classification"],
                "beforeEvidence": _evidence_summary(frozen),
                "contextualConcepts": qualification["contextualConcepts"],
                "id": record_id,
                "reason": qualification["reason"],
                "roleFamily": qualification["roleFamily"],
                "route": qualification["route"],
                "sourceKey": source_key,
                "strippedBoilerplate": qualification["strippedBoilerplate"],
                "title": after["title"],
            }
            records_audit.append(audit_row)
            if frozen["classification"] != "qualified":
                withheld.append(audit_row)

    expected = manifest["expected"]
    expected_counts = expected["classificationCounts"]
    if len(unique_ids) != expected["uniqueRecordCount"] or sum(before_counts.values()) != expected["recordCount"]:
        raise QualificationAuditError("frozen run does not contain exactly 82 unique records")
    if dict(sorted(before_counts.items())) != dict(sorted(expected_counts.items())):
        raise QualificationAuditError("frozen classification counts do not match 13/50/19")
    if len(withheld) != 69:
        raise QualificationAuditError(f"expected 69 withheld records, found {len(withheld)}")

    positive_titles = (
        "Software Engineer - Core Database (Kernel)",
        "Software Engineer - Sharding",
        "Senior Software Engineer - Graph Analytics for Snowflake",
        "Full-Stack Software Engineer - GDS",
        "Senior Software Engineer - GraphAware Hume",
        "AI Tech Lead - GraphAware Hume",
        "Tech Lead, Backend - GraphAware Hume",
    )
    for title in positive_titles:
        if not after_by_title.get(title) or set(after_by_title[title]) != {"qualified"}:
            raise QualificationAuditError(f"pinned positive failed: {title}")
    for title in ("Stay in touch", "Future Openings at TopQuadrant"):
        if after_by_title.get(title) != ["not_match"]:
            raise QualificationAuditError(f"pinned placeholder negative failed: {title}")
    for row in withheld:
        if row["sourceKey"] == "first-party-wikimedia" and row["afterClassification"] == "qualified":
            raise QualificationAuditError(f"unrelated Wikimedia negative was promoted: {row['title']}")

    additions = [
        row for row in records_audit
        if row["beforeClassification"] != "qualified"
        and row["afterClassification"] == "qualified"
    ]
    removals = [
        row for row in records_audit
        if row["beforeClassification"] == "qualified"
        and row["afterClassification"] != "qualified"
    ]
    delta_fields = ("id", "sourceKey", "title", "beforeClassification", "afterClassification")

    return {
        "artifact": manifest["artifact"],
        "baseline": {
            "classificationCounts": dict(sorted(before_counts.items())),
            "recordCount": sum(before_counts.values()),
            "uniqueRecordCount": len(unique_ids),
            "withheldCount": len(withheld),
        },
        "policyResult": {
            "additionsCount": len(additions),
            "classificationCounts": dict(sorted(after_counts.items())),
            "netQualifiedChange": len(additions) - len(removals),
            "newlyQualifiedCount": len(additions),
            "removalsCount": len(removals),
        },
        "qualificationDelta": {
            "additions": [
                {field: row[field] for field in delta_fields} for row in additions
            ],
            "additionsCount": len(additions),
            "netQualifiedChange": len(additions) - len(removals),
            "removals": [
                {field: row[field] for field in delta_fields} for row in removals
            ],
            "removalsCount": len(removals),
        },
        "records": sorted(
            records_audit, key=lambda row: (row["sourceKey"], row["id"])
        ),
        "withheldRecords": sorted(
            withheld, key=lambda row: (row["sourceKey"], row["id"])
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="optionally re-verify the downloaded ZIP")
    parser.add_argument("--check", action="store_true", help="verify without writing the audit")
    args = parser.parse_args(argv)
    try:
        payload = build_audit(archive=args.archive)
        if not args.check:
            OUTPUT_PATH.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except (OSError, json.JSONDecodeError, QualificationAuditError) as exc:
        print(f"Task 40 qualification audit failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Task 40 qualification audit valid: "
        f"{payload['baseline']['recordCount']} frozen records, "
        f"{payload['baseline']['withheldCount']} withheld audited, "
        f"{payload['policyResult']['additionsCount']} additions, "
        f"{payload['policyResult']['removalsCount']} removals, "
        f"net {payload['policyResult']['netQualifiedChange']:+d}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
