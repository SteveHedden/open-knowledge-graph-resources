#!/usr/bin/env python3
"""Build Task 42's deterministic review artifact from captured discovery and diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from first_party_sources import load_first_party_sources, load_production_first_party_sources
from live_sources import load_production_source_registry


ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
DEFAULT_DISCOVERY = ROOT / "audits" / "task42-careers-discovery.json"
DEFAULT_OPERATIONAL_PLAN = ROOT / "audits" / "task42-nightly-operational-plan.json"
DEFAULT_OUTPUT = ROOT / "audits" / "task42-organization-source-audit.json"

TASK41_FIXED_20 = {
    "arcade-data-ltd", "artsy", "biblioteksentralen",
    "blackcat-informatics-inc", "cambridge-semantics", "databricks",
    "franz-inc", "marklogic", "memgraph", "metaweb", "ontopic",
    "ontotext", "openlink-software", "q107620349", "sage-publishing",
    "semantic-web-company", "terminusdb", "triply", "typedb", "zazuko",
}

TASK39_PRODUCTION_12 = {
    "eccenca", "enterprise-knowledge", "graphwise", "metaphacts",
    "neo4j-inc", "relationalai", "stardog-union", "tigergraph",
    "topquadrant", "weaviate", "wikimedia-foundation",
    "world-wide-web-consortium",
}

# Added after Task 42 closed; never let later registry growth rewrite its fixed cohort.
TASK43_FIXED_8 = {
    "accenture", "amazon", "bloomberg", "capital-one", "crowdstrike",
    "jpmorgan-chase", "sap", "siemens",
}

TASK44_FIXED_4 = {"oracle", "salesforce", "servicenow", "workday"}

TASK42_SOURCE_KEYS = frozenset({
    "first-party-danish-bibliographic-centre",
    "first-party-embl-ebi",
    "first-party-institute-of-scientific-and-technical-information",
    "first-party-inter-university-consortium-for-political-and-social-research",
    "first-party-linux-foundation",
    "first-party-metropolitan-museum-of-art",
    "first-party-microsoft-research",
    "first-party-national-library-of-norway",
    "first-party-public-library-of-science",
    "first-party-regenstrief-institute",
    "first-party-renaissance-computing-institute",
    "first-party-sib-swiss-institute-of-bioinformatics",
    "first-party-stanford-university-school-of-medicine",
    "first-party-the-open-university",
    "first-party-university-of-maryland",
    "first-party-university-of-north-carolina-at-chapel-hill",
    "first-party-wikimedia-deutschland",
})

BLOCKED_OVERRIDES = {
    "data-world": (
        "obsolete-careers-source",
        "the exact Greenhouse tenant linked by the official careers page returned HTTP 404",
    ),
    "defense-logistics-agency": (
        "credential-restricted-endpoint",
        "the branded USAJobs portal is public HTML, but its bounded data API requires an API key",
    ),
    "library-of-congress": (
        "inaccessible-endpoint",
        "the Library of Congress careers endpoint returned HTTP 403 and the USAJobs data API requires credentials",
    ),
    "american-folklife-center": (
        "no-organization-scoped-source",
        "the parent Library of Congress careers endpoint returned HTTP 403 and exposes no American Folklife Center filter",
    ),
    "national-library-of-australia": (
        "human-verification-required",
        "the exact NGA board returned an AWS WAF human-verification response instead of machine-readable openings",
    ),
    "national-wildfire-coordinating-group": (
        "credential-restricted-endpoint",
        "the linked DOI Fire Careers USAJobs board has no NWCG organization filter and its data API requires credentials",
    ),
}


class AuditError(RuntimeError):
    """The local audit contract is incomplete or inconsistent."""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read valid JSON from {path}") from exc


def fixed_cohort() -> list[dict]:
    payload = _read_json(REPO_ROOT / "data" / "organizations.json")
    rows = [
        row for row in payload.get("organizations", [])
        if row.get("reviewStatus") == "evidence-reviewed"
        and row.get("identifier") not in TASK39_PRODUCTION_12
        and row.get("identifier") not in TASK41_FIXED_20
        and row.get("identifier") not in TASK43_FIXED_8
        and row.get("identifier") not in TASK44_FIXED_4
    ]
    if len(rows) != 107:
        raise AuditError(f"Task 42 cohort drifted: expected 107, found {len(rows)}")
    linked = sum(bool(row.get("careersPage")) for row in rows)
    if linked != 85:
        raise AuditError(f"Task 42 careers-page baseline drifted: expected 85, found {linked}")
    return sorted(rows, key=lambda row: row["identifier"])


def task42_review_sources(cohort: list[dict] | None = None):
    """Return the fixed reviewed Task 42 source set across approval stages."""

    cohort = cohort or fixed_cohort()
    cohort_iris = {row["iri"] for row in cohort}
    sources = load_first_party_sources()
    selected = {
        key: sources[key] for key in sorted(TASK42_SOURCE_KEYS)
        if key in sources and sources[key].organization_iri in cohort_iris
    }
    if set(selected) != TASK42_SOURCE_KEYS:
        missing = sorted(TASK42_SOURCE_KEYS - set(selected))
        raise AuditError(f"Task 42 source registry drifted; missing: {missing}")
    return selected


def _discovery(path: Path, cohort: list[dict]) -> tuple[dict[str, dict], dict[str, dict], dict]:
    payload = _read_json(path)
    pages = payload.get("careersPages")
    expected = {
        row["identifier"] for row in cohort if row.get("careersPage")
    }
    if not isinstance(pages, list) or len(pages) != 85:
        raise AuditError("discovery capture must contain exactly 85 careers-page rows")
    by_id = {row.get("identifier"): row for row in pages if isinstance(row, dict)}
    if set(by_id) != expected or len(by_id) != 85:
        raise AuditError("discovery capture does not match the fixed 85-page cohort")
    required = {
        "careersPage", "contentSha256", "contentType", "discoveryReason",
        "finalUrl", "httpStatus", "identifier", "jobPostingCount", "linkCount",
        "providerCandidates", "responseBytes", "retrievalError", "secondaryProbes",
    }
    for identifier, row in by_id.items():
        missing = required - set(row)
        if missing or not row.get("discoveryReason"):
            raise AuditError(
                f"discovery row {identifier} is incomplete: {sorted(missing)}"
            )
    supplements = payload.get("supplementalChecks")
    if not isinstance(supplements, list):
        raise AuditError("discovery capture lacks named supplemental checks")
    supplement_by_id = {
        row.get("identifier"): row for row in supplements if isinstance(row, dict)
    }
    for identifier, row in supplement_by_id.items():
        if not row.get("url") or not row.get("reviewConclusion"):
            raise AuditError(f"supplemental discovery row {identifier} is incomplete")
    return by_id, supplement_by_id, payload


def _diagnostics(runtime_dir: Path, sources: dict) -> tuple[dict[str, list[dict]], dict]:
    run = _read_json(runtime_dir / "run.json")
    if run.get("mode") != "live-local-review" or run.get("publicationPerformed") is not False:
        raise AuditError("Task 42 requires a successful unpublished live-review run")
    results = {
        row.get("sourceKey"): row for row in run.get("sourceResults", [])
        if isinstance(row, dict)
    }
    if set(results) != set(sources):
        raise AuditError(
            "Task 42 runtime diagnostics must cover every and only the viable review source"
        )
    records = {}
    for key in sorted(sources):
        result = results[key]
        if result.get("status") not in {"refreshed", "refresh-interval-retained"}:
            raise AuditError(
                f"Task 42 source {key} did not refresh successfully: {result.get('status')}"
            )
        path = runtime_dir / "sources" / f"{key}.json"
        rows = _read_json(path)
        if not isinstance(rows, list):
            raise AuditError(f"Task 42 source diagnostics are not an array: {key}")
        counts = Counter(row.get("classification") for row in rows)
        expected_counts = {
            "qualified": counts["qualified"],
            "review": counts["review"],
            "notMatch": counts["not_match"],
            "records": len(rows),
        }
        if any(result.get(field) != value for field, value in expected_counts.items()):
            raise AuditError(f"Task 42 source diagnostic counts disagree for {key}")
        raw_candidates = [
            runtime_dir / "raw" / f"{key}.json",
            runtime_dir / "raw" / f"{key}.html",
        ]
        if not any(path.is_file() for path in raw_candidates):
            raise AuditError(f"Task 42 source {key} lacks retained reproducible raw input")
        records[key] = rows
    return records, run


def _candidate_score(candidate: dict) -> tuple[int, str]:
    url = str(candidate.get("endpoint") or "")
    parsed = urlparse(url)
    path = parsed.path.casefold()
    asset = path.endswith((
        ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ))
    jobish = any(marker in path for marker in (
        "job", "career", "vacanc", "position", "companyadvert", "search",
    ))
    return (
        (100 if candidate.get("organizationFilter") else 0)
        + (20 if jobish else 0)
        - (1000 if asset else 0),
        url,
    )


def _candidate_is_retrievable_document(candidate: dict) -> bool:
    parsed = urlparse(str(candidate.get("endpoint") or ""))
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.path.casefold().endswith((
            ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".ico",
            ".svg", ".woff", ".woff2", ".ttf", ".map",
        ))
    )


def _blocked_disposition(identifier: str, discovery: dict, supplement: dict | None) -> dict:
    probes = discovery.get("secondaryProbes") or []
    candidates = list(discovery.get("providerCandidates") or [])
    for probe in probes:
        if probe.get("provider"):
            candidates.append({
                "endpoint": probe.get("finalUrl") or probe.get("requestedUrl"),
                "organizationFilter": probe.get("organizationFilter"),
                "provider": probe.get("provider"),
            })
    candidates = [
        candidate for candidate in candidates
        if _candidate_is_retrievable_document(candidate)
    ]
    selected = max(candidates, key=_candidate_score) if candidates else None
    exact_endpoint = (
        supplement.get("url") if supplement else None
    ) or (selected or {}).get("endpoint") or discovery.get("finalUrl") or discovery.get("careersPage")
    provider = (selected or {}).get("provider")
    if not provider and supplement:
        host = (urlparse(supplement.get("url") or "").hostname or "").casefold()
        if "usajobs" in host:
            provider = "usajobs"
        elif "successfactors" in host or host == "jobs.helsinki.fi":
            provider = "successfactors"
        elif "selectminds" in host:
            provider = "taleo-selectminds"
        elif "loc.gov" in host:
            provider = "loc-careers"
        elif "myworkdayjobs" in host:
            provider = "workday"
    status = discovery.get("httpStatus")
    category = None
    reason = None
    if identifier in BLOCKED_OVERRIDES:
        category, reason = BLOCKED_OVERRIDES[identifier]
    elif discovery.get("retrievalError"):
        category = "inaccessible-endpoint"
        reason = f"the official careers URL failed bounded retrieval: {discovery['retrievalError']}"
    elif isinstance(status, int) and status >= 400:
        category = "inaccessible-endpoint"
        reason = f"the official careers URL returned HTTP {status} and exposed no retrievable organization-scoped source"
    elif selected:
        category = "no-organization-scoped-source"
        reason = (
            f"the retrieved {provider} endpoint is a parent/general board and the captured URL/query "
            "contains no verified filter identifying this Task 42 organization"
        )
    else:
        pdf_probes = [
            row for row in probes
            if urlparse(str(row.get("finalUrl") or row.get("requestedUrl") or "")).path.casefold().endswith(".pdf")
        ]
        category = "no-bounded-job-record-source"
        if pdf_probes:
            reason = (
                f"the official page and {len(pdf_probes)} probed PDF notice(s) were retrievable, but "
                "they expose no durable per-opening identity plus complete reusable detail contract"
            )
            provider = provider or "official-pdf-notices"
        else:
            reason = (
                f"the official page returned HTTP {status}; {len(probes)} deeper job/career link(s) "
                "were checked, but none exposed a bounded organization-scoped feed/board with stable "
                "per-opening identity and complete descriptions"
            )
            provider = provider or "official-html-careers"
    return {
        "adapterStatus": "externally-blocked",
        "blockerCategory": category,
        "exactEndpoint": exact_endpoint,
        "organizationFilter": (selected or {}).get("organizationFilter")
            or (dict(parse_qsl(urlparse(exact_endpoint).query)) if exact_endpoint else None)
            or None,
        "provider": provider or "official-careers",
        "sourceDisposition": "blocked",
        "sourceDispositionReason": reason,
        "supplementalRetrieval": supplement,
    }


def _raw_input(runtime_dir: Path, key: str) -> dict:
    paths = [runtime_dir / "raw" / f"{key}.json", runtime_dir / "raw" / f"{key}.html"]
    path = next((candidate for candidate in paths if candidate.is_file()), None)
    if path is None:
        raise AuditError(f"Task 42 source {key} lacks raw input")
    body = path.read_bytes()
    try:
        display_path = str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        display_path = str(path.resolve())
    return {
        "bytes": len(body), "path": display_path,
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _job_summary(record: dict) -> dict:
    return {
        "classification": record.get("classification"),
        "classificationEvidence": record.get("evidence", []),
        "classificationReason": (record.get("qualificationAudit") or {}).get("reason"),
        "id": record.get("id"),
        "organizationIri": record.get("organizationIri"),
        "postingUrl": record.get("canonicalUrl"),
        "sourceKey": (record.get("discoveredBy") or [None])[0],
        "sourceRecordId": record.get("sourceRecordId"),
        "title": record.get("title"),
    }


def build_audit(runtime_dir: Path, discovery_path: Path = DEFAULT_DISCOVERY) -> dict:
    cohort = fixed_cohort()
    discovery, supplements, discovery_payload = _discovery(discovery_path, cohort)
    sources = task42_review_sources(cohort)
    by_organization = {source.organization_iri: source for source in sources.values()}
    records_by_source, run = _diagnostics(runtime_dir, sources)
    source_results = {row["sourceKey"]: row for row in run["sourceResults"]}
    organization_rows = []
    all_records = []
    for organization in cohort:
        identifier = organization["identifier"]
        careers_page = organization.get("careersPage")
        row = {
            "careersPage": careers_page,
            "careersPageStatus": "recorded-official" if careers_page else "uncovered",
            "currentOpenings": None,
            "discoveryEvidence": discovery.get(identifier),
            "evidence": careers_page or organization.get("officialWebsite")
                or (organization.get("evidence") or [{}])[0].get("url"),
            "id": identifier,
            "name": organization["name"],
            "notMatch": None,
            "organizationIri": organization["iri"],
            "qualified": None,
            "review": None,
            "sourceKey": None,
        }
        if not careers_page:
            row.update({
                "adapterStatus": "not-applicable",
                "exactEndpoint": None,
                "organizationFilter": None,
                "provider": "none",
                "sourceDisposition": "uncovered",
                "sourceDispositionReason": "no durable official careers page is recorded",
                "supplementalRetrieval": None,
            })
        elif organization["iri"] in by_organization:
            source = by_organization[organization["iri"]]
            source_records = records_by_source[source.key]
            all_records.extend(source_records)
            counts = Counter(record.get("classification") for record in source_records)
            row.update({
                "adapterStatus": "network-free-and-live-reviewed",
                "currentOpenings": len(source_records),
                "exactEndpoint": source.endpoint,
                "notMatch": counts["not_match"],
                "organizationFilter": dict(parse_qsl(urlparse(source.endpoint).query)) or None,
                "provider": source.provider,
                "qualified": counts["qualified"],
                "review": counts["review"],
                "runStatus": source_results[source.key]["status"],
                "sourceDisposition": "pipeline-ready",
                "sourceDispositionReason": (
                    f"supported by exact {source.adapter} contract and successful live diagnostics"
                ),
                "sourceKey": source.key,
                "liveReviewInput": _raw_input(runtime_dir, source.key),
                "supplementalRetrieval": supplements.get(identifier),
            })
        else:
            row.update(_blocked_disposition(identifier, discovery[identifier], supplements.get(identifier)))
        organization_rows.append(row)

    by_classification = {
        classification: sorted(
            (_job_summary(record) for record in all_records
             if record.get("classification") == classification),
            key=lambda row: (row["sourceKey"] or "", row["id"] or ""),
        )
        for classification in ("qualified", "review", "not_match")
    }
    blocked = Counter(
        row.get("blockerCategory") for row in organization_rows
        if row.get("sourceDisposition") == "blocked"
    )
    production_first_party = len(load_production_first_party_sources())
    production_aggregators = len(
        load_production_source_registry(REPO_ROOT / "sources.ttl")
    )
    full_pipelines = [
        {
            "currentOpenings": row["currentOpenings"],
            "exactEndpoint": row["exactEndpoint"],
            "id": row["id"],
            "liveReviewInput": row["liveReviewInput"],
            "name": row["name"],
            "notMatch": row["notMatch"],
            "organizationFilter": row["organizationFilter"],
            "provider": row["provider"],
            "qualified": row["qualified"],
            "review": row["review"],
            "runStatus": row["runStatus"],
            "sourceKey": row["sourceKey"],
        }
        for row in organization_rows
        if row.get("sourceDisposition") == "pipeline-ready"
    ]
    externally_blocked = [
        {
            "blockerCategory": row["blockerCategory"],
            "exactEndpoint": row["exactEndpoint"],
            "id": row["id"],
            "name": row["name"],
            "organizationFilter": row["organizationFilter"],
            "provider": row["provider"],
            "reason": row["sourceDispositionReason"],
        }
        for row in organization_rows
        if row.get("sourceDisposition") == "blocked"
    ]
    return {
        "asOf": discovery_payload.get("capturedAt"),
        "cohortRule": "139 accepted organizations minus the fixed Task 39 cohort of 12 and Task 41 fixed 20",
        "counts": {
            "careersPages": 85,
            "notMatchJobs": len(by_classification["not_match"]),
            "organizations": 107,
            "proposedPublicJobs": len(by_classification["qualified"]),
            "reviewJobs": len(by_classification["review"]),
            "uncoveredOrganizations": 22,
            "pipelineReadySources": len(sources),
            "blockedOrganizations": sum(blocked.values()),
            "blockedByReason": dict(sorted(blocked.items())),
            "productionFirstPartySources": production_first_party,
            "productionAggregatorSources": production_aggregators,
            "productionSources": production_first_party + production_aggregators,
        },
        "discoveryBounds": discovery_payload.get("bounds"),
        "externallyBlockedSources": externally_blocked,
        "fullJobIngestionPipelines": full_pipelines,
        "generalRuleProposals": [],
        "managerReviewJobs": by_classification["review"],
        "notMatchJobs": by_classification["not_match"],
        "operationalDesign": _read_json(DEFAULT_OPERATIONAL_PLAN),
        "organizations": organization_rows,
        "productionFlagsModified": True,
        "proposedPublicJobs": by_classification["qualified"],
        "publicSnapshotModified": False,
        "runtimeRetrievedAt": run.get("retrievedAt"),
        "scheduleModified": True,
        "status": "production-wiring-complete-pending-final-review",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    payload = build_audit(args.runtime_dir, args.discovery)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            raise AuditError(f"Task 42 audit is stale: {args.output}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
