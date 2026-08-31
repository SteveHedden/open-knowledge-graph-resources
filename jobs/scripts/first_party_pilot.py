#!/usr/bin/env python3
"""Run the bounded first-party jobs review harness without publication."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCAT, DCTERMS, PROV, RDF, XSD

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
RUNTIME_DIR = ROOT / "runtime" / "first-party"
FIXTURE_RUNTIME_DIR = ROOT / "runtime" / "first-party-fixtures"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from classifier import load_match_terms  # noqa: E402
from first_party_classifier import (  # noqa: E402
    classify_first_party_records,
    load_first_party_policy,
)
from first_party_sources import (  # noqa: E402
    GRAPHWISE_ADAPTER, GRAPHWISE_ADAPTER_REVISION,
    FirstPartySource, FirstPartySourceError, fetch_source,
    load_first_party_sources, records_from_payload,
)
from rdf_utils import write_deterministic_turtle  # noqa: E402
from reconcile import reconcile_records  # noqa: E402

SCHEMA = Namespace("https://schema.org/")
KGJOBS = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
PILOT = Namespace("https://openknowledgegraphs.com/jobs/first-party-pilot/")


class PilotError(RuntimeError):
    """The local pilot failed without replacing the last good snapshot."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError(f"invalid prior pilot JSON: {path}") from exc


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _normalize_name(value) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _organization_index(path: Path = REPO_ROOT / "data" / "organizations.json") -> tuple[dict, dict]:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise PilotError("organization registry projection is unavailable")
    by_alias = {}
    by_iri = {}
    for row in payload.get("organizations", []):
        if row.get("reviewStatus") != "evidence-reviewed" or not row.get("iri"):
            continue
        by_iri[row["iri"]] = row
        for name in [row.get("name"), *row.get("aliases", [])]:
            normalized = _normalize_name(name)
            if normalized and normalized not in by_alias:
                by_alias[normalized] = row["iri"]
            elif normalized and by_alias[normalized] != row["iri"]:
                by_alias[normalized] = None
    return by_alias, by_iri


def _location_keys(value) -> list[str]:
    import re
    return sorted({
        " ".join(unicodedata.normalize("NFKC", part).split()).casefold()
        for part in re.split(r"[,;/|]", str(value or "")) if part.strip()
    })


def load_aggregator_records(path: Path = REPO_ROOT / "data" / "jobs" / "jobs.json") -> list[dict]:
    payload = _read_json(path, [])
    if not isinstance(payload, list):
        raise PilotError("committed aggregator jobs snapshot must contain an array")
    aliases, _ = _organization_index()
    output = []
    for source_record in payload:
        record = dict(source_record)
        organization_iri = aliases.get(_normalize_name(record.get("hiringOrganization")))
        if organization_iri:
            record["organizationIri"] = organization_iri
        record["firstParty"] = False
        record["provider"] = str(record.get("sourceDataset") or "").rstrip("/").rsplit("/", 1)[-1]
        record.setdefault("tenant", None)
        record.setdefault("workplaceMode", "remote" if record.get("remote") else "unknown")
        record.setdefault("locationKeys", _location_keys(record.get("location")))
        record.setdefault("sourceOccurrences", [{
            "sourceDataset": record.get("sourceDataset"),
            "sourceRecordId": record.get("sourceRecordId"),
            "sourceUrl": record.get("sourceUrl") or record.get("canonicalUrl"),
            "provider": record.get("provider"),
            "tenant": record.get("tenant"),
            "firstParty": False,
        }])
        output.append(record)
    return sorted(output, key=lambda row: row.get("id", ""))


def _fixture_payload(directory: Path, source: FirstPartySource):
    suffix = ".json" if source.adapter in {
        "firstparty-greenhouse", "firstparty-lever", "firstparty-ashby",
        "firstparty-graphwise", "firstparty-rippling", "firstparty-eccenca",
        "firstparty-teamtailor", "firstparty-same-site-detail",
        "firstparty-workday", "firstparty-workday-keyword",
        "firstparty-oracle-recruiting", "firstparty-amazon-jobs",
        "firstparty-webcruiter", "firstparty-successfactors",
        "firstparty-successfactors-rmk-html", "firstparty-ukg",
        "firstparty-softgarden", "firstparty-refline", "firstparty-emply",
        "firstparty-peopleadmin",
        "firstparty-selectminds",
        "firstparty-drupal-rss-detail", "firstparty-cnrs-unit-detail",
        "firstparty-microsoft-research",
    } else ".html"
    fallback = {
        "firstparty-greenhouse": "greenhouse.json",
        "firstparty-lever": "lever.json",
        "firstparty-ashby": "ashby.json",
        "firstparty-schema": "schema.html",
        "firstparty-graphwise": "first-party-graphwise.json",
        "firstparty-rippling": "rippling.json",
        "firstparty-eccenca": "first-party-eccenca.json",
        "firstparty-teamtailor": "first-party-sage-publishing.json",
        "firstparty-same-site-detail": "first-party-triply.json",
        "firstparty-workday": "first-party-metropolitan-museum-of-art.json",
        "firstparty-workday-keyword": "first-party-accenture.json",
        "firstparty-oracle-recruiting": "first-party-jpmorgan-chase.json",
        "firstparty-amazon-jobs": "first-party-amazon.json",
        "firstparty-webcruiter": "first-party-national-library-of-norway.json",
        "firstparty-successfactors": "first-party-the-open-university.json",
        "firstparty-successfactors-rmk-html": "first-party-sap.json",
        "firstparty-ukg": "first-party-regenstrief-institute.json",
        "firstparty-softgarden": "first-party-wikimedia-deutschland.json",
        "firstparty-refline": "first-party-sib-swiss-institute-of-bioinformatics.json",
        "firstparty-emply": "first-party-danish-bibliographic-centre.json",
        "firstparty-peopleadmin": "first-party-renaissance-computing-institute.json",
        "firstparty-selectminds": "first-party-stanford-university-school-of-medicine.json",
        "firstparty-drupal-rss-detail": "first-party-inter-university-consortium-for-political-and-social-research.json",
        "firstparty-cnrs-unit-detail": "first-party-institute-of-scientific-and-technical-information.json",
        "firstparty-microsoft-research": "first-party-microsoft-research.json",
    }[source.adapter]
    specific = directory / f"{source.key}{suffix}"
    path = specific if specific.exists() else directory / fallback
    if not path.exists():
        raise FirstPartySourceError(f"fixture missing for {source.key}: {path.name}")
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise FirstPartySourceError(f"fixture for {source.key} is malformed JSON") from exc
    return text


def _prior_source_records(runtime_dir: Path, key: str) -> list[dict]:
    payload = _read_json(runtime_dir / "sources" / f"{key}.json", [])
    return payload if isinstance(payload, list) else []


def _adapter_revision(source: FirstPartySource) -> str | None:
    return GRAPHWISE_ADAPTER_REVISION if source.adapter == GRAPHWISE_ADAPTER else None


def _prior_success_at(
    prior: dict, source: FirstPartySource, previous_records: list[dict],
) -> str | None:
    result = next(
        (row for row in prior.get("sourceResults", []) if row.get("sourceKey") == source.key),
        None,
    )
    if result and result.get("lastSuccessfulAt"):
        return result["lastSuccessfulAt"]
    if result and result.get("status") in {"refreshed", "refresh-interval-retained"}:
        return prior.get("retrievedAt")
    values = sorted(
        row.get("retrievedAt") for row in previous_records
        if isinstance(row.get("retrievedAt"), str) and row.get("retrievedAt")
    )
    return values[-1] if values else None


def _live_refresh_due(
    runtime_dir: Path, source: FirstPartySource, retrieved_at: str,
    previous_records: list[dict] | None = None,
) -> tuple[bool, str | None]:
    prior = _read_json(runtime_dir / "run.json", {})
    if not isinstance(prior, dict) or prior.get("mode") != "live-local-review":
        return True, None
    previous_source = next(
        (row for row in prior.get("sources", []) if row.get("sourceKey") == source.key),
        None,
    )
    if not previous_source or any((
        previous_source.get("endpoint") != source.endpoint,
        previous_source.get("adapter") != source.adapter,
        previous_source.get("extractionMode") != source.extraction_mode,
        _adapter_revision(source) is not None
        and previous_source.get("adapterRevision") != _adapter_revision(source),
    )):
        return True, None
    last_success = _prior_success_at(prior, source, previous_records or [])
    if not last_success:
        return True, None
    try:
        current = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        previous = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True, None
    return (
        (current - previous).total_seconds() >= source.refresh_interval_seconds,
        last_success,
    )


def _preserve_times(records: list[dict], prior: list[dict], retrieved_at: str) -> list[dict]:
    prior_by_id = {row.get("id"): row for row in prior}
    output = []
    for source_record in records:
        record = dict(source_record)
        old = prior_by_id.get(record.get("id"), {})
        record["firstSeenAt"] = old.get("firstSeenAt") or retrieved_at
        record["lastSeenAt"] = retrieved_at
        record["retrievedAt"] = retrieved_at
        record["active"] = True
        output.append(record)
    return sorted(output, key=lambda row: row["id"])


def build_pilot_graph(records: list[dict], run: dict, organizations: dict) -> Graph:
    graph = Graph()
    for prefix, namespace in (
        ("schema", SCHEMA), ("kgjobs", KGJOBS), ("pilot", PILOT),
        ("prov", PROV), ("dcat", DCAT), ("dcterms", DCTERMS),
    ):
        graph.bind(prefix, namespace)
    dataset = PILOT[f"dataset/{run['runId']}"]
    activity = PILOT[f"activity/{run['runId']}"]
    graph.add((dataset, RDF.type, DCAT.Dataset))
    graph.add((dataset, DCTERMS.title, Literal("Local first-party KG jobs pilot", lang="en")))
    graph.add((dataset, PROV.wasGeneratedBy, activity))
    graph.add((activity, RDF.type, PROV.Activity))
    graph.add((activity, PROV.endedAtTime, Literal(run["retrievedAt"], datatype=XSD.dateTime)))
    for organization in organizations.values():
        subject = URIRef(organization["iri"])
        graph.add((subject, RDF.type, SCHEMA.Organization))
        graph.add((subject, RDF.type, KGJOBS.Employer))
        graph.add((subject, SCHEMA.name, Literal(organization["name"])))
    for record in records:
        job = PILOT[f"job/{record['id']}"]
        graph.add((job, RDF.type, SCHEMA.JobPosting))
        graph.add((job, RDF.type, PROV.Entity))
        graph.add((job, SCHEMA.identifier, Literal(record["id"])))
        graph.add((job, SCHEMA.url, URIRef(record["canonicalUrl"])))
        graph.add((job, SCHEMA.title, Literal(record["title"])))
        graph.add((job, SCHEMA.description, Literal(record.get("description") or "")))
        organization_iri = record.get("organizationIri")
        if organization_iri and organization_iri in organizations:
            organization = URIRef(organization_iri)
        else:
            organization = PILOT[f"employer/{record['id']}"]
            graph.add((organization, RDF.type, SCHEMA.Organization))
            graph.add((organization, RDF.type, KGJOBS.Employer))
            graph.add((organization, SCHEMA.name, Literal(record["hiringOrganization"])))
        graph.add((job, SCHEMA.hiringOrganization, organization))
        graph.add((job, KGJOBS.classification, Literal(record["classification"])))
        graph.add((job, KGJOBS.sourceRecordId, Literal(record["sourceRecordId"])))
        graph.add((job, KGJOBS.canonicalFingerprint, Literal(record["canonicalFingerprint"])))
        graph.add((job, KGJOBS.firstSeenAt, Literal(record["firstSeenAt"], datatype=XSD.dateTime)))
        graph.add((job, KGJOBS.lastSeenAt, Literal(record["lastSeenAt"], datatype=XSD.dateTime)))
        graph.add((job, KGJOBS.active, Literal(bool(record.get("active")), datatype=XSD.boolean)))
        graph.add((job, DCTERMS.source, URIRef(record["sourceDataset"])))
        if record.get("datePosted"):
            graph.add((job, SCHEMA.datePosted, Literal(record["datePosted"], datatype=XSD.date)))
        if record.get("validThrough"):
            graph.add((job, SCHEMA.validThrough, Literal(record["validThrough"], datatype=XSD.date)))
        if record.get("reconciliationMethod"):
            for method in record["reconciliationMethod"]:
                graph.add((job, KGJOBS.reconciliationMethod, Literal(method)))
        if record.get("reconciliationReason"):
            graph.add((job, KGJOBS.reconciliationReason, Literal(record["reconciliationReason"])))
        for index, occurrence in enumerate(record.get("sourceOccurrences", []), start=1):
            node = PILOT[f"occurrence/{record['id']}/{index}"]
            graph.add((node, RDF.type, PROV.Entity))
            if occurrence.get("sourceDataset"):
                graph.add((node, DCTERMS.source, URIRef(occurrence["sourceDataset"])))
            if occurrence.get("sourceRecordId"):
                graph.add((node, KGJOBS.occurrenceRecordId, Literal(occurrence["sourceRecordId"])))
            if occurrence.get("sourceUrl"):
                graph.add((node, SCHEMA.url, URIRef(occurrence["sourceUrl"])))
                graph.add((job, PROV.wasDerivedFrom, node))
            if occurrence.get("provider"):
                graph.add((node, KGJOBS.careerProvider, Literal(occurrence["provider"])))
            if occurrence.get("tenant"):
                graph.add((node, KGJOBS.tenantIdentifier, Literal(occurrence["tenant"])))
            graph.add((
                node, KGJOBS.firstParty,
                Literal(bool(occurrence.get("firstParty")), datatype=XSD.boolean),
            ))
            graph.add((job, KGJOBS.sourceOccurrence, node))
        for evidence_index, evidence in enumerate(record.get("evidence", []), start=1):
            node = PILOT[f"evidence/{record['id']}/{evidence_index}"]
            graph.add((node, RDF.type, KGJOBS.Evidence))
            graph.add((node, KGJOBS.matchedConcept, URIRef(evidence["concept_uri"])))
            graph.add((node, KGJOBS.conceptLabel, Literal(evidence["concept_label"])))
            graph.add((node, KGJOBS.conceptScheme, Literal(evidence["concept_scheme"])))
            graph.add((node, KGJOBS.matchStrength, Literal(evidence["strength"])))
            graph.add((node, KGJOBS.matchedPhrase, Literal(evidence["matched_phrase"])))
            graph.add((node, KGJOBS.sourceField, Literal(evidence["source_field"])))
            graph.add((node, KGJOBS.negated, Literal(evidence["negated"], datatype=XSD.boolean)))
            graph.add((job, KGJOBS.hasEvidence, node))
        graph.add((dataset, URIRef(f"{DCAT}resource"), job))
    return graph


def validate_pilot_graph(graph: Graph) -> None:
    data = Graph()
    for triple in graph:
        data.add(triple)
    data.parse(ROOT / "vocabularies" / "kg-jobs.ttl", format="turtle")
    data.parse(REPO_ROOT / "sources.ttl", format="turtle")
    data.parse(REPO_ROOT / "organizations.ttl", format="turtle")
    data.parse(REPO_ROOT / "ontology.ttl", format="turtle")
    shapes = Graph().parse(ROOT / "ontology.ttl", format="turtle")
    conforms, _, report = validate(data, shacl_graph=shapes, ont_graph=shapes, inference="none")
    if not conforms:
        raise PilotError(f"first-party pilot RDF failed SHACL:\n{report}")


def _publish(
    runtime_dir: Path, records: list[dict], graph: Graph, run: dict,
    per_source: dict[str, list[dict]], raw_payloads: dict[str, object], audit: dict,
    retained_raw_keys: set[str],
) -> None:
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".first-party-stage-", dir=runtime_dir.parent))
    backup = runtime_dir.with_name(f".{runtime_dir.name}-previous")
    try:
        _write_json(stage / "jobs.json", records)
        _write_json(stage / "run.json", run)
        _write_json(stage / "reconciliation-audit.json", audit)
        for key, source_records in per_source.items():
            _write_json(stage / "sources" / f"{key}.json", source_records)
        for key in sorted(retained_raw_keys):
            for suffix in (".html", ".json"):
                prior_raw = runtime_dir / "raw" / f"{key}{suffix}"
                if prior_raw.is_file():
                    destination = stage / "raw" / prior_raw.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(prior_raw, destination)
        for key, payload in raw_payloads.items():
            if isinstance(payload, str):
                raw_path = stage / "raw" / f"{key}.html"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(payload, encoding="utf-8")
            else:
                _write_json(stage / "raw" / f"{key}.json", payload)
        write_deterministic_turtle(graph, stage / "jobs.ttl")
        validate_pilot_graph(graph)
        if backup.exists():
            shutil.rmtree(backup)
        if runtime_dir.exists():
            os.replace(runtime_dir, backup)
        try:
            os.replace(stage, runtime_dir)
        except BaseException:
            if backup.exists() and not runtime_dir.exists():
                os.replace(backup, runtime_dir)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def run_pilot(
    *, live: bool = False, fixtures: Path | None = None,
    runtime_dir: Path = RUNTIME_DIR, retrieved_at: str | None = None,
    selected_sources: list[str] | None = None,
    first_party_fetcher=None,
) -> dict:
    if live == bool(fixtures):
        raise PilotError("choose exactly one of live network mode or fixture mode")
    retrieved_at = retrieved_at or utc_now()
    sources = load_first_party_sources()
    requested = sorted(selected_sources or sources)
    unknown = sorted(set(requested) - set(sources))
    if unknown:
        raise PilotError(f"unknown first-party sources: {', '.join(unknown)}")
    match_terms = load_match_terms(ROOT / "vocabularies" / "kg-jobs.ttl")
    qualification_policy = load_first_party_policy(
        ROOT / "vocabularies" / "kg-jobs.ttl"
    )
    per_source = {}
    raw_payloads = {}
    retained_raw_keys = set()
    source_report = []
    for key in requested:
        source = sources[key]
        previous = _prior_source_records(runtime_dir, key)
        refresh_due, last_success = (
            _live_refresh_due(runtime_dir, source, retrieved_at, previous)
            if live else (True, None)
        )
        if live and not refresh_due:
            per_source[key] = previous
            retained_raw_keys.add(key)
            counts = Counter(row["classification"] for row in previous)
            source_report.append({
                "sourceKey": key, "status": "refresh-interval-retained",
                "records": len(previous), "qualified": counts["qualified"],
                "review": counts["review"], "notMatch": counts["not_match"],
                "lastSuccessfulAt": last_success,
            })
            continue
        try:
            payload = (
                (first_party_fetcher or fetch_source)(source)
                if live else _fixture_payload(fixtures, source)
            )
            normalized = records_from_payload(payload, source)
            classified = classify_first_party_records(
                normalized, match_terms, qualification_policy
            )
            current = _preserve_times(classified, previous, retrieved_at)
            per_source[key] = current
            raw_payloads[key] = payload
            counts = Counter(row["classification"] for row in current)
            source_report.append({
                "sourceKey": key, "status": "refreshed", "records": len(current),
                "qualified": counts["qualified"], "review": counts["review"],
                "notMatch": counts["not_match"],
                "lastSuccessfulAt": retrieved_at,
            })
        except FirstPartySourceError as exc:
            retained_raw_keys.add(key)
            if previous:
                per_source[key] = previous
                counts = Counter(row["classification"] for row in previous)
                source_report.append({
                    "sourceKey": key, "status": "retained-last-good",
                    "records": len(previous), "qualified": counts["qualified"],
                    "review": counts["review"], "notMatch": counts["not_match"],
                    "lastSuccessfulAt": last_success, "error": str(exc),
                })
            else:
                per_source[key] = []
                source_report.append({
                    "sourceKey": key, "status": "isolated-failure",
                    "records": 0, "lastSuccessfulAt": last_success, "error": str(exc),
                })
    # A partial source selection retains other sources' last-good records.
    for key in sorted(set(sources) - set(requested)):
        per_source[key] = _prior_source_records(runtime_dir, key)
        retained_raw_keys.add(key)
    first_party_records = sorted(
        [record for source_records in per_source.values() for record in source_records],
        key=lambda row: row["id"],
    )
    aggregator_records = load_aggregator_records()
    merged, reconciliation_audit = reconcile_records(first_party_records + aggregator_records)
    _, organizations = _organization_index()
    run_id = retrieved_at.replace("-", "").replace(":", "")
    run = {
        "runId": run_id,
        "retrievedAt": retrieved_at,
        "mode": "live-local-review" if live else "network-free-fixtures",
        "runtimeIsolation": "live" if live else "fixtures",
        "publicationPerformed": False,
        "sourceCount": len(sources),
        "requestCap": sum(source.max_requests_per_run for source in sources.values()),
        "firstPartyRecordCount": len(first_party_records),
        "aggregatorRecordCount": len(aggregator_records),
        "mergedRecordCount": len(merged),
        "sourceResults": source_report,
        "classificationCounts": dict(sorted(Counter(row["classification"] for row in merged).items())),
        "sources": [{
            "sourceKey": source.key,
            "organizationIri": source.organization_iri,
            "provider": source.provider,
            "adapter": source.adapter,
            "adapterRevision": _adapter_revision(source),
            "extractionMode": source.extraction_mode,
            "endpoint": source.endpoint,
            "careersPage": source.careers_page,
            "termsUrl": source.terms_url,
            "robotsUrl": source.robots_url,
            "attributionText": source.attribution_text,
            "attributionUrl": source.attribution_url,
            "republicationStatus": source.republication_status,
            "reviewStatus": source.review_status,
            "allowedHost": source.allowed_host,
            "maxRequestsPerRun": source.max_requests_per_run,
            "maxResponseBytes": source.max_response_bytes,
            "maxRecordsPerRun": source.max_records_per_run,
            "requestTimeoutSeconds": source.timeout_seconds,
        } for source in sources.values()],
    }
    graph = build_pilot_graph(merged, run, organizations)
    _publish(
        runtime_dir, merged, graph, run, per_source, raw_payloads,
        reconciliation_audit, retained_raw_keys,
    )
    return run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="permit the selected bounded reviewed requests")
    mode.add_argument("--fixtures", type=Path, help="read one network-free fixture per source")
    parser.add_argument("--source", action="append", help="run only one declared source; repeatable")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="override the ignored local review runtime directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime_dir = args.runtime_dir or (
            RUNTIME_DIR if args.live else FIXTURE_RUNTIME_DIR
        )
        run = run_pilot(
            live=args.live, fixtures=args.fixtures, runtime_dir=runtime_dir,
            selected_sources=args.source,
        )
    except (PilotError, FirstPartySourceError) as exc:
        print(f"First-party pilot failed safely: {exc}", file=sys.stderr)
        return 1
    print(
        f"Local first-party pilot: {run['firstPartyRecordCount']} first-party records; "
        f"{run['mergedRecordCount']} reconciled records; local pilot output remains unpublished"
    )
    for result in run["sourceResults"]:
        print(
            f"{result['sourceKey']}: {result['status']}; {result['records']} jobs; "
            f"{result.get('qualified', 0)} qualified; {result.get('review', 0)} review"
        )
    print(f"Runtime: {runtime_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
