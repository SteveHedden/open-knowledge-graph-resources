#!/usr/bin/env python3
"""Build Task 46 review artifacts without replacing the protected jobs snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from rebuild_catalog_mentions import build_candidate  # noqa: E402

PROTECTED = (
    "data/jobs/jobs.json",
    "data/jobs/jobs.ttl",
    "data/jobs/manifest.json",
    "data/manifest.json",
)
ALLOWED_JSON_FIELDS = {"catalogMentions", "jobTags"}
PINNED_TOPQUADRANT_IDS = {
    "firstparty:first-party-amazon:10476837",
    "firstparty:first-party-amazon:10494602",
    "firstparty:first-party-capital-one:R999238",
    "firstparty:first-party-capital-one:R999454",
    "firstparty:first-party-capital-one:R999461",
}
PINNED_CAPITAL_ONE_IDS = {
    "firstparty:first-party-capital-one:R999238",
    "firstparty:first-party-capital-one:R999454",
    "firstparty:first-party-capital-one:R999461",
}
REVIEWED_REMOVALS = {
    ("firstparty:first-party-neo4j:4555260006", "catalogMentions", "Q1628290"):
        "Neo4j occurs only in reviewed self-employer boilerplate removed from job-specific text.",
    ("firstparty:first-party-neo4j:4567487006", "catalogMentions", "Q1628290"):
        "Neo4j occurs only in reviewed self-employer boilerplate removed from job-specific text.",
    ("firstparty:first-party-neo4j:4707711006", "catalogMentions", "Q1628290"):
        "Neo4j occurs only in reviewed self-employer boilerplate removed from job-specific text.",
}
PINNED_FINAL_QIDS = [
    "Q54872", "Q1751819", "Q826165", "Q2288360", "Q29377821",
    "Q2066865", "Q140443441", "Q28136436", "Q91147741", "Q141112432",
]
PINNED_AMAZON_QIDS = [
    "Q1751819", "Q826165", "Q29377821", "Q2066865", "Q140443441", "Q28136436",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_review(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    protected_before = {name: _sha256(REPO_ROOT / name) for name in PROTECTED}
    source_path = REPO_ROOT / "data/jobs/jobs.json"
    records = json.loads(source_path.read_text(encoding="utf-8"))
    enriched, json_bytes, ttl_bytes = build_candidate(REPO_ROOT, ROOT)

    if [row["id"] for row in records] != [row["id"] for row in enriched]:
        raise ValueError("Task 46 rebuild changed record ordering or identity")
    changes = []
    removals = []
    for before, after in zip(records, enriched, strict=True):
        stable_before = {key: value for key, value in before.items() if key not in ALLOWED_JSON_FIELDS}
        stable_after = {key: value for key, value in after.items() if key not in ALLOWED_JSON_FIELDS}
        if stable_before != stable_after:
            raise ValueError(f"Task 46 changed a protected field on {before['id']}")
        before_enrichment = {key: before.get(key, []) for key in sorted(ALLOWED_JSON_FIELDS)}
        after_enrichment = {key: after.get(key, []) for key in sorted(ALLOWED_JSON_FIELDS)}
        if before_enrichment != after_enrichment:
            changes.append({
                "id": before["id"],
                "before": before_enrichment,
                "after": after_enrichment,
            })
        after_qids = {
            mention.get("qid") for mention in after.get("catalogMentions", [])
            if isinstance(mention, dict)
        }
        for mention in before.get("catalogMentions", []):
            if isinstance(mention, dict) and mention.get("qid") not in after_qids:
                removals.append((before["id"], "catalogMentions", mention.get("qid")))
        after_labels = {
            tag.get("label") for tag in after.get("jobTags", [])
            if isinstance(tag, dict)
        }
        for tag in before.get("jobTags", []):
            if isinstance(tag, dict) and tag.get("label") not in after_labels:
                removals.append((before["id"], "jobTags", tag.get("label")))

    changed_ids = {change["id"] for change in changes}
    if not PINNED_TOPQUADRANT_IDS <= changed_ids:
        raise ValueError("Task 46 rebuild omitted one or more pinned TopQuadrant jobs")
    if not PINNED_CAPITAL_ONE_IDS <= changed_ids:
        raise ValueError("Task 46 rebuild omitted one or more pinned Capital One jobs")
    by_id = {record["id"]: record for record in enriched}
    for record_id in PINNED_TOPQUADRANT_IDS:
        qids = [mention["qid"] for mention in by_id[record_id]["catalogMentions"]]
        expected = PINNED_FINAL_QIDS if record_id in PINNED_CAPITAL_ONE_IDS else PINNED_AMAZON_QIDS
        if qids != expected:
            raise ValueError(f"pinned exact mention targets drifted on {record_id}: {qids}")
        labels = [tag["label"] for tag in by_id[record_id].get("jobTags", [])]
        if labels != ["SPARQL"]:
            raise ValueError(f"pinned exact job tags drifted on {record_id}: {labels}")
    for record_id in PINNED_CAPITAL_ONE_IDS:
        qids = [mention["qid"] for mention in by_id[record_id]["catalogMentions"]]
        if qids.count("Q141112432") != 1:
            raise ValueError(f"pinned data.world target is not exact on {record_id}")
        if {"Q124653370", "Q48843359", "Q124653384"} & set(qids):
            raise ValueError(f"unadmitted AnzoGraph/Neptune target emitted on {record_id}")
    actual_removals = set(removals)
    reviewed_removals = set(REVIEWED_REMOVALS)
    if actual_removals != reviewed_removals:
        unexpected = sorted(actual_removals - reviewed_removals)
        missing = sorted(reviewed_removals - actual_removals)
        raise ValueError(
            f"Task 46 removal audit mismatch; unexpected={unexpected}, missing={missing}"
        )

    json_path = output_dir / "jobs.json"
    json_path.write_bytes(json_bytes)
    ttl_path = output_dir / "jobs.ttl"
    ttl_path.write_bytes(ttl_bytes)

    protected_after = {name: _sha256(REPO_ROOT / name) for name in PROTECTED}
    if protected_before != protected_after:
        raise ValueError("Task 46 review rebuild modified a protected file")
    audit = {
        "schemaVersion": 1,
        "task": 46,
        "mode": "temporary-offline-manager-review",
        "publicationPerformed": False,
        "sourceSnapshotSha256": protected_before["data/jobs/jobs.json"],
        "protectedFilesByteUnchanged": True,
        "standardAndReviewRebuildParity": True,
        "protectedFileSha256": protected_after,
        "outputSha256": {"jobs.json": _sha256(json_path), "jobs.ttl": _sha256(ttl_path)},
        "changedRecordCount": len(changes),
        "reviewedRemovals": [
            {
                "id": key[0],
                "collection": key[1],
                "semanticKey": key[2],
                "reason": reason,
            }
            for key, reason in sorted(REVIEWED_REMOVALS.items())
        ],
        "unapprovedRemovalCount": 0,
        "changes": changes,
    }
    audit_path = output_dir / "diff.json"
    audit_path.write_text(
        json.dumps(audit, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    audit = build_review(args.output_dir)
    print(f"Task 46 review rebuild: {audit['changedRecordCount']} changed records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
