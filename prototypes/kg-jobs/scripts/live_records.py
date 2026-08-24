"""Deterministic live-record reconciliation and RDF/JSON publication."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from pyshacl import validate
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from classifier import Evidence, classify, find_evidence
from entities import KGJD, apply_confirmed_wikidata_matches, employer_uri
from live_sources import LivePipelineError, SourceConfig

SCHEMA = Namespace("https://schema.org/")
KGJOBS = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/ontology#")
KGJDLIVE = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/live/")
PROV = Namespace("http://www.w3.org/ns/prov#")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCTERMS = Namespace("http://purl.org/dc/terms/")

def evidence_json(evidence: list[Evidence]) -> list[dict]:
    return [
        {
            "concept_uri": item.concept_uri,
            "concept_label": item.concept_label,
            "concept_scheme": item.concept_scheme,
            "strength": item.strength,
            "matched_phrase": item.matched_phrase,
            "source_field": item.source_field,
            "negated": item.negated,
        }
        for item in evidence
    ]


def deduplicate(records: list[dict]) -> list[dict]:
    """Collapse repeated query hits using source identity and canonical URL.

    Input ordering never affects the result: records are ordered by stable
    source identity before the first representative is selected.
    """
    ordered = sorted(
        records,
        key=lambda record: (
            record.get("sourceDataset", ""),
            record.get("sourceRecordId", ""),
            record.get("canonicalFingerprint", ""),
            record.get("canonicalUrl", ""),
            record.get("id", ""),
        ),
    )
    output: list[dict] = []
    by_source_id: dict[tuple[str, str], int] = {}
    by_fingerprint: dict[str, int] = {}
    by_url: dict[str, int] = {}
    by_fallback: dict[tuple[str, str, str, str], int] = {}

    def identity(value) -> str:
        return re.sub(
            r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))
        ).strip().casefold()

    for record in ordered:
        source_record_id = identity(record.get("sourceRecordId"))
        source_key = (
            record.get("sourceDataset", ""),
            source_record_id,
        )
        fingerprint = identity(record.get("canonicalFingerprint"))
        stable_urls = tuple(
            dict.fromkeys(
                value
                for value in (
                    identity(record.get("canonicalUrl")),
                    identity(record.get("sourceUrl")),
                )
                if value
            )
        )
        has_stable_url = bool(fingerprint or stable_urls)
        has_stable_identity = bool(source_record_id or has_stable_url)
        fallback = (
            record.get("sourceDataset", ""),
            identity(record.get("hiringOrganization")),
            identity(record.get("title")),
            identity(record.get("location")),
        )
        index = by_source_id.get(source_key) if source_record_id else None
        if index is None and fingerprint:
            index = by_fingerprint.get(fingerprint)
        if index is None:
            index = next(
                (by_url[url] for url in stable_urls if url in by_url),
                None,
            )
        # Employer/title/location is deliberately a last-resort identity. A
        # source record with a stable ID or URL represents a distinct vacancy
        # even when its visible fields happen to match another posting.
        if index is None and not has_stable_identity:
            index = by_fallback.get(fallback)
        if index is None:
            candidate = dict(record)
            candidate["discoveredBy"] = sorted(
                set(record.get("discoveredBy", [])), key=lambda value: (value.casefold(), value)
            )
            index = len(output)
            output.append(candidate)
        else:
            merged = set(output[index].get("discoveredBy", []))
            merged.update(record.get("discoveredBy", []))
            output[index]["discoveredBy"] = sorted(
                merged, key=lambda value: (value.casefold(), value)
            )
        if source_record_id:
            by_source_id[source_key] = index
        if fingerprint:
            by_fingerprint[fingerprint] = index
        for url in stable_urls:
            by_url[url] = index
        if not has_stable_identity:
            by_fallback[fallback] = index

    return sorted(output, key=lambda record: record["id"])


def classify_records(records: list[dict], match_terms) -> list[dict]:
    output = []
    for record in records:
        evidence = find_evidence(record, match_terms)
        enriched = dict(record)
        enriched["classification"] = classify(evidence)
        enriched["evidence"] = evidence_json(evidence)
        output.append(enriched)
    return sorted(output, key=lambda record: record["id"])


def load_previous_records(runtime_dir: Path) -> list[dict]:
    path = runtime_dir / "jobs.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LivePipelineError("existing runtime/jobs.json is not valid JSON") from exc
    if not isinstance(data, list):
        raise LivePipelineError("existing runtime/jobs.json must contain an array")
    return [record for record in data if isinstance(record, dict) and record.get("id")]


def preserve_first_seen(
    current: list[dict],
    previous: list[dict],
    retrieved_at: str,
) -> list[dict]:
    previous_by_id = {record["id"]: record for record in previous}
    output = []
    for record in current:
        enriched = dict(record)
        prior = previous_by_id.get(record["id"])
        if prior and prior.get("firstSeenAt"):
            enriched["firstSeenAt"] = prior["firstSeenAt"]
        enriched["lastSeenAt"] = retrieved_at
        enriched["retrievedAt"] = retrieved_at
        enriched["active"] = True
        output.append(enriched)
    # The published prototype is the bounded current sample from this run,
    # not an accumulating history. Records absent from the new sample are
    # omitted even when the upstream feed is larger than our declared cap.
    return sorted(output, key=lambda record: record["id"])


def _evidence_to_rdf(graph: Graph, job, record: dict) -> None:
    for item in record.get("evidence", []):
        node = BNode()
        graph.add((node, RDF.type, KGJOBS.Evidence))
        graph.add((node, KGJOBS.matchedConcept, URIRef(item["concept_uri"])))
        graph.add((node, KGJOBS.conceptLabel, Literal(item["concept_label"])))
        graph.add((node, KGJOBS.conceptScheme, Literal(item["concept_scheme"])))
        graph.add((node, KGJOBS.matchStrength, Literal(item["strength"])))
        graph.add((node, KGJOBS.matchedPhrase, Literal(item["matched_phrase"])))
        graph.add((node, KGJOBS.sourceField, Literal(item["source_field"])))
        graph.add((node, KGJOBS.negated, Literal(item["negated"], datatype=XSD.boolean)))
        graph.add((job, KGJOBS.hasEvidence, node))


def build_graph(records: list[dict], run: dict, source: SourceConfig) -> Graph:
    graph = Graph()
    for prefix, namespace in (
        ("schema", SCHEMA), ("kgjobs", KGJOBS), ("kgjlive", KGJDLIVE), ("kgjd", KGJD),
        ("prov", PROV), ("dcat", DCAT), ("dcterms", DCTERMS),
    ):
        graph.bind(prefix, namespace)

    activity = KGJDLIVE[f"activity/{quote(run['runId'], safe='')}"]
    dataset = KGJDLIVE[f"dataset/{quote(run['runId'], safe='')}"]
    graph.add((activity, RDF.type, PROV.Activity))
    graph.add((activity, PROV.used, URIRef(source.dataset_uri)))
    graph.add((activity, PROV.used, URIRef("https://openknowledgegraphs.com/prototypes/kg-jobs/vocab")))
    graph.add((activity, PROV.endedAtTime, Literal(run["retrievedAt"], datatype=XSD.dateTime)))
    graph.add((dataset, RDF.type, DCAT.Dataset))
    graph.add((dataset, DCTERMS.title, Literal("KG Jobs local live snapshot", lang="en")))
    graph.add((dataset, PROV.wasGeneratedBy, activity))
    graph.add((dataset, DCTERMS.source, URIRef(source.dataset_uri)))

    for index, result in enumerate(run.get("queryResults", []), start=1):
        execution = KGJDLIVE[
            f"activity/{quote(run['runId'], safe='')}/query/{index}"
        ]
        query_family = URIRef(result["queryUri"])
        graph.add((activity, KGJOBS.hasQueryExecution, execution))
        graph.add((execution, RDF.type, KGJOBS.QueryExecution))
        graph.add((execution, RDF.type, PROV.Activity))
        graph.add((execution, KGJOBS.queryFamily, query_family))
        graph.add((execution, PROV.used, query_family))
        graph.add((execution, KGJOBS.executedQueryText, Literal(result["query"])))
        graph.add((
            execution,
            KGJOBS.returnedCount,
            Literal(result["returnedCount"], datatype=XSD.nonNegativeInteger),
        ))
        graph.add((
            execution,
            KGJOBS.totalCount,
            Literal(result["totalCount"], datatype=XSD.nonNegativeInteger),
        ))
        graph.add((
            execution,
            KGJOBS.queryComplete,
            Literal(result["complete"], datatype=XSD.boolean),
        ))

    for record in records:
        job = KGJDLIVE[f"job/{quote(record['id'], safe='')}"]
        graph.add((job, RDF.type, SCHEMA.JobPosting))
        graph.add((job, RDF.type, PROV.Entity))
        graph.add((job, SCHEMA.identifier, Literal(record["id"])))
        graph.add((job, SCHEMA.url, URIRef(record["canonicalUrl"])))
        graph.add((job, SCHEMA.title, Literal(record["title"])))
        graph.add((job, SCHEMA.description, Literal(record["description"])))
        organization = employer_uri(record["hiringOrganization"])
        if (organization, RDF.type, SCHEMA.Organization) not in graph:
            # First record seen for this employer slug sets the canonical
            # display name; later records reusing the same slug (e.g. minor
            # whitespace/case variants from different sources) just link to
            # the existing resource, keeping schema:name single-valued.
            graph.add((organization, RDF.type, SCHEMA.Organization))
            graph.add((organization, RDF.type, KGJOBS.Employer))
            graph.add((organization, SCHEMA.name, Literal(record["hiringOrganization"])))
        graph.add((job, SCHEMA.hiringOrganization, organization))
        if record.get("location"):
            place = BNode()
            graph.add((place, RDF.type, SCHEMA.Place))
            graph.add((place, SCHEMA.name, Literal(record["location"])))
            graph.add((job, SCHEMA.jobLocation, place))
        for requirement in record.get("applicantLocationRequirements", []):
            area = BNode()
            graph.add((area, RDF.type, SCHEMA.AdministrativeArea))
            graph.add((area, SCHEMA.name, Literal(requirement)))
            graph.add((job, SCHEMA.applicantLocationRequirements, area))
        if record.get("remote"):
            graph.add((job, SCHEMA.jobLocationType, Literal("TELECOMMUTE")))
        if record.get("datePosted"):
            graph.add((job, SCHEMA.datePosted, Literal(record["datePosted"], datatype=XSD.date)))
        if record.get("validThrough"):
            graph.add((job, SCHEMA.validThrough, Literal(record["validThrough"], datatype=XSD.date)))
        if record.get("employmentType"):
            graph.add((job, SCHEMA.employmentType, Literal(record["employmentType"])))
        if record.get("seniority"):
            graph.add((job, SCHEMA.experienceRequirements, Literal(record["seniority"])))
        structured_salary = record.get("baseSalary")
        if isinstance(structured_salary, dict):
            monetary_amount = BNode()
            quantitative_value = BNode()
            graph.add((monetary_amount, RDF.type, SCHEMA.MonetaryAmount))
            graph.add((quantitative_value, RDF.type, SCHEMA.QuantitativeValue))
            if structured_salary.get("currency"):
                graph.add((
                    monetary_amount,
                    SCHEMA.currency,
                    Literal(structured_salary["currency"]),
                ))
            if structured_salary.get("minValue") is not None:
                graph.add((
                    quantitative_value,
                    SCHEMA.minValue,
                    Literal(structured_salary["minValue"]),
                ))
            if structured_salary.get("maxValue") is not None:
                graph.add((
                    quantitative_value,
                    SCHEMA.maxValue,
                    Literal(structured_salary["maxValue"]),
                ))
            if structured_salary.get("unitText"):
                graph.add((
                    quantitative_value,
                    SCHEMA.unitText,
                    Literal(structured_salary["unitText"]),
                ))
            graph.add((monetary_amount, SCHEMA.value, quantitative_value))
            graph.add((job, SCHEMA.baseSalary, monetary_amount))
        elif record.get("salary"):
            graph.add((job, SCHEMA.baseSalary, Literal(record["salary"])))
        graph.add((job, KGJOBS.classification, Literal(record["classification"])))
        graph.add((job, KGJOBS.sourceRecordId, Literal(record["sourceRecordId"])))
        graph.add((job, KGJOBS.canonicalFingerprint, Literal(record["canonicalFingerprint"])))
        graph.add((job, KGJOBS.firstSeenAt, Literal(record["firstSeenAt"], datatype=XSD.dateTime)))
        graph.add((job, KGJOBS.lastSeenAt, Literal(record["lastSeenAt"], datatype=XSD.dateTime)))
        graph.add((job, KGJOBS.active, Literal(record["active"], datatype=XSD.boolean)))
        graph.add((job, DCTERMS.source, URIRef(record["sourceDataset"])))
        graph.add((job, PROV.wasDerivedFrom, URIRef(record["sourceUrl"])))
        graph.add((job, PROV.generatedAtTime, Literal(record["retrievedAt"], datatype=XSD.dateTime)))
        for mention in record.get("catalogMentions", []):
            canonical_url = mention.get("canonicalUrl") if isinstance(mention, dict) else None
            if canonical_url:
                graph.add((job, SCHEMA.mentions, URIRef(canonical_url)))
        graph.add((dataset, DCAT.resource, job))
        _evidence_to_rdf(graph, job, record)
    apply_confirmed_wikidata_matches(graph)
    return graph


def validate_graph(graph: Graph, root: Path) -> None:
    data = Graph()
    for triple in graph:
        data.add(triple)
    data.parse(root / "vocabularies" / "kg-jobs.ttl", format="turtle")
    data.parse(root / "sources.ttl", format="turtle")
    shapes = Graph()
    shapes.parse(root / "ontology.ttl", format="turtle")
    conforms, _, report = validate(
        data, shacl_graph=shapes, ont_graph=shapes,
        inference="none", abort_on_first=False,
    )
    if not conforms:
        raise LivePipelineError(f"live RDF failed SHACL validation:\n{report}")


def _recover_interrupted_publication(runtime_dir: Path) -> None:
    backup = runtime_dir.with_name(f".{runtime_dir.name}-previous")
    if backup.exists() and not runtime_dir.exists():
        os.replace(backup, runtime_dir)
    elif backup.exists() and runtime_dir.exists():
        shutil.rmtree(backup)
    for stale_stage in runtime_dir.parent.glob(".kg-jobs-live-*"):
        if stale_stage.is_dir():
            shutil.rmtree(stale_stage)


def _atomic_replace_directory(stage: Path, runtime_dir: Path) -> None:
    backup = runtime_dir.with_name(f".{runtime_dir.name}-previous")
    if backup.exists():
        raise LivePipelineError(f"stale runtime backup prevents publication: {backup}")
    moved_previous = False
    try:
        if runtime_dir.exists():
            os.replace(runtime_dir, backup)
            moved_previous = True
        os.replace(stage, runtime_dir)
    except BaseException:
        if moved_previous and not runtime_dir.exists() and backup.exists():
            os.replace(backup, runtime_dir)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def publish_snapshot(
    records: list[dict],
    run: dict,
    graph: Graph,
    root: Path,
    runtime_dir: Path,
    raw_payload: dict,
    source_key: str,
) -> None:
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    _recover_interrupted_publication(runtime_dir)
    stage = Path(tempfile.mkdtemp(prefix=".kg-jobs-live-", dir=runtime_dir.parent))
    try:
        ignore_source = runtime_dir / ".gitignore"
        ignore_text = ignore_source.read_text(encoding="utf-8") if ignore_source.exists() else "*\n!.gitignore\n"
        (stage / ".gitignore").write_text(ignore_text, encoding="utf-8")
        (stage / "jobs.json").write_text(
            json.dumps(records, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (stage / "run.json").write_text(
            json.dumps(run, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raw_dir = stage / "raw"
        raw_dir.mkdir()
        previous_raw_dir = runtime_dir / "raw"
        if previous_raw_dir.exists():
            for previous_raw_file in previous_raw_dir.glob("*.json"):
                if previous_raw_file.stem != source_key:
                    shutil.copy2(previous_raw_file, raw_dir / previous_raw_file.name)
        (raw_dir / f"{source_key}.json").write_text(
            json.dumps(raw_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        graph.serialize(destination=str(stage / "jobs.ttl"), format="turtle")
        validate_graph(graph, root)
        _atomic_replace_directory(stage, runtime_dir)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
