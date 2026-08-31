#!/usr/bin/env python3
"""Build the compact, tracked Issue 63 manager-review artifact from local live runs."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
DEFAULT_RUNTIME = ROOT / "runtime" / "task43-review"
DEFAULT_OUTPUT = ROOT / "audits" / "task43-peer-employer-review.json"

SOURCE_KEYS = (
    "first-party-accenture",
    "first-party-amazon",
    "first-party-capital-one",
    "first-party-crowdstrike",
    "first-party-jpmorgan-chase",
    "first-party-sap",
)

DECISIONS = [
    {
        "organization": "JPMorganChase",
        "organizationIri": "https://openknowledgegraphs.com/organization/jpmorgan-chase/",
        "identityEvidence": "https://www.wikidata.org/wiki/Q192314",
        "inclusionEvidence": "https://www.jpmorganchase.com/content/dam/jpmorganchase/documents/technology/2026-tech-trends-context-driven-architectures.pdf",
        "sourceOutcome": "pipeline-ready-reviewed",
        "sourceKey": "first-party-jpmorgan-chase",
    },
    {
        "organization": "Accenture",
        "organizationIri": "https://openknowledgegraphs.com/organization/accenture/",
        "identityEvidence": "https://www.wikidata.org/wiki/Q338825",
        "inclusionEvidence": "https://www.accenture.com/us-en/insights/data-ai/intelligent-digital-brain",
        "sourceOutcome": "pipeline-ready-reviewed",
        "sourceKey": "first-party-accenture",
    },
    {
        "organization": "Amazon",
        "organizationIri": "https://openknowledgegraphs.com/organization/amazon/",
        "identityEvidence": "https://www.wikidata.org/wiki/Q3884",
        "inclusionEvidence": "https://aws.amazon.com/neptune/graph-and-ai/",
        "sourceOutcome": "pipeline-ready-reviewed",
        "sourceKey": "first-party-amazon",
    },
    {
        "organization": "Capital One",
        "organizationIri": "https://openknowledgegraphs.com/organization/capital-one/",
        "identityEvidence": "https://www.wikidata.org/wiki/Q1034654",
        "inclusionEvidence": "https://www.capitalone.com/tech/machine-learning/learning-embeddings-of-financial-graphs/",
        "sourceOutcome": "pipeline-ready-reviewed",
        "sourceKey": "first-party-capital-one",
    },
    {
        "organization": "CrowdStrike",
        "organizationIri": "https://openknowledgegraphs.com/organization/crowdstrike/",
        "identityEvidence": "https://www.wikidata.org/wiki/Q24890758",
        "inclusionEvidence": "https://www.crowdstrike.com/en-us/blog/big-data-graph-and-the-cloud-three-keys-to-stopping-todays-threats/",
        "sourceOutcome": "pipeline-ready-reviewed",
        "sourceKey": "first-party-crowdstrike",
    },
    {
        "organization": "SAP",
        "organizationIri": "https://openknowledgegraphs.com/organization/sap/",
        "identityEvidence": "https://www.wikidata.org/wiki/Q552581",
        "inclusionEvidence": "https://www.sap.com/products/artificial-intelligence/knowledge-graph.html",
        "sourceOutcome": "pipeline-ready-reviewed",
        "sourceKey": "first-party-sap",
    },
    {
        "organization": "Siemens",
        "organizationIri": "https://openknowledgegraphs.com/organization/siemens/",
        "identityEvidence": "https://www.wikidata.org/wiki/Q81230",
        "inclusionEvidence": "https://www.siemens.com/en-us/solutions/data-analytics-artificial-intelligence/knowledge-graphs/",
        "sourceOutcome": "deferred",
        "provider": "avature",
        "checkedEndpoint": "https://jobs.siemens.com/en_US/externaljobs/SearchJobs/?keyword=ontology",
        "retrievalResult": "HTTP 200; query returned unrelated general-board jobs",
        "blocker": "The public Avature page does not apply the URL keyword as an organization-scoped machine filter, and its operational search contract is opaque. No deterministic bounded source was identified.",
    },
    {
        "organization": "Bloomberg L.P.",
        "organizationIri": "https://openknowledgegraphs.com/organization/bloomberg/",
        "identityEvidence": "https://www.wikidata.org/wiki/Q13977",
        "inclusionEvidence": "https://www.openfigi.com/",
        "sourceOutcome": "deferred",
        "provider": "avature",
        "checkedEndpoint": "https://bloomberg.avature.net/careers/SearchJobs/?q=knowledge%20graph",
        "retrievalResult": "HTTP 200; query returned unrelated general-board jobs",
        "blocker": "The public Avature page ignores the visible URL query for deterministic discovery, and its operational search contract is opaque. No safe bounded source was identified.",
    },
]


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(runtime_root: Path) -> dict:
    source_reviews = []
    all_counts = Counter()
    all_jobs = []
    for key in SOURCE_KEYS:
        directory = runtime_root / key
        run = _read_json(directory / "run.json")
        result = next(row for row in run["sourceResults"] if row["sourceKey"] == key)
        if result["status"] != "refreshed":
            raise ValueError(f"{key} did not complete a successful live review")
        records_path = directory / "sources" / f"{key}.json"
        records = _read_json(records_path)
        raw_candidates = sorted((directory / "raw").glob(f"{key}.*"))
        if len(raw_candidates) != 1:
            raise ValueError(f"{key} requires exactly one retained raw payload")
        counts = Counter(record["classification"] for record in records)
        all_counts.update(counts)
        jobs = []
        for record in sorted(records, key=lambda row: (row["classification"], row["title"], row["sourceRecordId"])):
            row = {
                "classification": record["classification"],
                "sourceRecordId": record["sourceRecordId"],
                "title": record["title"],
                "url": record["canonicalUrl"],
                "matchedPhrases": sorted({
                    evidence["matched_phrase"] for evidence in record.get("evidence", [])
                    if not evidence.get("negated")
                }),
            }
            jobs.append(row)
            all_jobs.append({"sourceKey": key, **row})
        source_reviews.append({
            "sourceKey": key,
            "status": result["status"],
            "retrievedAt": run["retrievedAt"],
            "rawEvidence": {
                "mediaType": "text/html" if raw_candidates[0].suffix == ".html" else "application/json",
                "sha256": _sha256(raw_candidates[0]),
            },
            "counts": {
                "records": len(records),
                "qualified": counts["qualified"],
                "review": counts["review"],
                "notMatch": counts["not_match"],
            },
            "jobs": jobs,
        })
    return {
        "issue": 63,
        "reviewStatus": "review-complete",
        "generatedFrom": "six isolated live-local-review runtimes",
        "constraints": {
            "classifierChanged": False,
            "productionFlagsChanged": False,
            "scheduled": False,
            "publicDataChanged": False,
            "published": False,
            "deployed": False,
        },
        "counts": {
            "evidenceQualifiedOrganizations": len(DECISIONS),
            "pipelineReadyReviewedSources": len(SOURCE_KEYS),
            "deferredSources": len(DECISIONS) - len(SOURCE_KEYS),
            "records": len(all_jobs),
            "qualified": all_counts["qualified"],
            "review": all_counts["review"],
            "notMatch": all_counts["not_match"],
        },
        "organizationAndSourceDecisions": DECISIONS,
        "sourceReviews": source_reviews,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = build(args.runtime_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Task 43 audit: {payload['counts']['records']} jobs; "
        f"{payload['counts']['qualified']} qualified; "
        f"{payload['counts']['review']} review; "
        f"{payload['counts']['notMatch']} not-match"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
