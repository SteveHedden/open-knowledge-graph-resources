"""Task 40 production cutover and qualification-policy contracts."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from rdflib import Graph, Namespace, RDF
from rdflib.namespace import DCAT, DCTERMS

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))

from audit_first_party_qualification import build_audit  # noqa: E402
from classifier import load_match_terms  # noqa: E402
from first_party_classifier import (  # noqa: E402
    classify_first_party_record,
    load_first_party_policy,
)
from first_party_sources import load_first_party_sources  # noqa: E402
import live_pipeline  # noqa: E402

OKG = Namespace("https://openknowledgegraphs.com/ontology#")
KGJOBS = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "first-party-run-183"


def test_frozen_run_183_provenance_hashes_and_counts_are_exact():
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text())
    assert manifest["artifact"] == {
        "archiveName": "first-party-diagnostics-33034495838 (1).zip",
        "artifactId": "9631740022",
        "githubActionsRunId": "33034495838",
        "githubActionsRunNumber": 183,
        "sha256": "2dbf4332812c64fa1dd03738834603cbea06ad61f62f7c75f28e6e5be2c0adec",
    }
    for relative, expected in manifest["files"].items():
        assert hashlib.sha256((FIXTURE_ROOT / relative).read_bytes()).hexdigest() == expected
    audit = build_audit()
    assert audit["baseline"] == {
        "classificationCounts": {"not_match": 19, "qualified": 13, "review": 50},
        "recordCount": 82,
        "uniqueRecordCount": 82,
        "withheldCount": 69,
    }
    assert len(audit["withheldRecords"]) == 69
    assert len(audit["records"]) == 82
    delta = audit["qualificationDelta"]
    assert delta["additionsCount"] == len(delta["additions"])
    assert delta["removalsCount"] == len(delta["removals"])
    assert delta["netQualifiedChange"] == (
        delta["additionsCount"] - delta["removalsCount"]
    )
    assert audit["policyResult"]["netQualifiedChange"] == delta["netQualifiedChange"]


def test_contextual_policy_is_first_party_only_and_preserves_raw_description():
    source = load_first_party_sources()["first-party-neo4j"]
    records = json.loads(
        (FIXTURE_ROOT / "sources" / "first-party-neo4j.json").read_text()
    )
    original = next(
        row for row in records if row["title"] == "Software Engineer - Sharding"
    )
    current = dict(original)
    current["sourceDataset"] = source.dataset_uri
    result = classify_first_party_record(
        current,
        load_match_terms(ROOT / "vocabularies" / "kg-jobs.ttl"),
        load_first_party_policy(ROOT / "vocabularies" / "kg-jobs.ttl"),
    )
    assert result["description"] == original["description"]
    assert result["classification"] == "qualified"
    assert result["qualificationAudit"]["route"] == "first-party-contextual"
    assert len(result["qualificationAudit"]["contextualConcepts"]) >= 2


def test_neo4j_company_boilerplate_is_removed_and_plural_graph_databases_is_supported():
    source = load_first_party_sources()["first-party-neo4j"]
    terms = load_match_terms(ROOT / "vocabularies" / "kg-jobs.ttl")
    policy = load_first_party_policy(ROOT / "vocabularies" / "kg-jobs.ttl")
    boilerplate = (
        "About Neo4j: We build enterprise knowledge graphs. Our Vision: "
        "We help the world make sense of data. We created, drive and lead the "
        "graph database category, and we’re disrupting how organizations leverage "
        "their data to innovate and stay competitive. "
    )
    base = {
        "id": "firstparty:first-party-neo4j:boilerplate-regression",
        "firstParty": True,
        "sourceDataset": source.dataset_uri,
        "title": "Software Engineer",
        "qualifications": None,
        "responsibilities": None,
    }

    unrelated = classify_first_party_record(
        {**base, "description": boilerplate + "Build billing APIs and internal tools."},
        terms,
        policy,
    )
    assert unrelated["classification"] == "not_match"
    assert unrelated["qualificationAudit"]["contextualConcepts"] == []
    assert unrelated["qualificationAudit"]["strippedBoilerplate"][0]["kind"] == "prefix"

    plural = classify_first_party_record(
        {
            **base,
            "id": "firstparty:first-party-neo4j:plural-regression",
            "description": boilerplate + "Develop graph databases and Cypher runtimes.",
        },
        terms,
        policy,
    )
    assert plural["classification"] == "qualified"
    assert plural["qualificationAudit"]["route"] == "first-party-contextual"
    assert "https://openknowledgegraphs.com/jobs/vocab/skill-graph-database" in (
        plural["qualificationAudit"]["contextualConcepts"]
    )


def test_pinned_placeholders_and_unrelated_wikimedia_records_remain_out():
    audit = build_audit()
    by_title = {row["title"]: row for row in audit["withheldRecords"]}
    assert by_title["Stay in touch"]["afterClassification"] == "not_match"
    assert by_title["Future Openings at TopQuadrant"]["afterClassification"] == "not_match"
    assert all(
        row["afterClassification"] != "qualified"
        for row in audit["withheldRecords"]
        if row["sourceKey"] == "first-party-wikimedia"
    )


def test_root_sources_distinguish_aggregators_from_first_party_career_services():
    graph = Graph().parse(REPO_ROOT / "sources.ttl", format="turtle")
    careers = set(graph.subjects(RDF.type, OKG.CareerSource))
    assert len(careers) == 12
    for source in careers:
        assert (source, RDF.type, DCAT.DataService) in graph
        assert len(list(graph.objects(source, DCTERMS.publisher))) == 1
        assert len(list(graph.objects(source, DCAT.landingPage))) == 1
        assert len(list(graph.objects(source, DCAT.endpointURL))) == 1
        assert len(list(graph.objects(source, DCTERMS.conformsTo))) == 1
        assert not list(graph.objects(source, KGJOBS.productionApproved))
        assert not list(graph.objects(source, KGJOBS.organization))
        assert not list(graph.objects(source, KGJOBS.sourceEndpoint))
    aggregators = {
        subject
        for subject in graph.subjects(RDF.type, DCAT.Dataset)
        if str(graph.value(subject, DCTERMS.identifier) or "")
        in {"adzuna", "arbeitnow", "himalayas", "jobicy", "jooble"}
    }
    assert len(aggregators) == 5
    assert not (aggregators & careers)


def test_first_refresh_migrates_legacy_source_iris_without_dropping_other_sources(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    committed = json.loads((REPO_ROOT / "data" / "jobs" / "jobs.json").read_text())
    prior = [
        dict(next(row for row in committed if row["sourceDataset"].endswith("/adzuna"))),
        dict(next(row for row in committed if row["sourceDataset"].endswith("/first-party-graphwise"))),
    ]
    legacy_prefix = "https://openknowledgegraphs.com/prototypes/kg-jobs/source/"
    for record in prior:
        source_key = record["sourceDataset"].rstrip("/").rsplit("/", 1)[-1]
        record["sourceDataset"] = legacy_prefix + source_key
        for occurrence in record.get("sourceOccurrences", []):
            occurrence_key = occurrence["sourceDataset"].rstrip("/").rsplit("/", 1)[-1]
            occurrence["sourceDataset"] = legacy_prefix + occurrence_key
    (runtime / "jobs.json").write_text(json.dumps(prior), encoding="utf-8")
    (runtime / "run.json").write_text(
        json.dumps({"sourceRefreshes": {}}), encoding="utf-8"
    )
    greenhouse = json.loads(
        (ROOT / "tests" / "fixtures" / "first-party-pilot" / "greenhouse.json").read_text()
    )

    live_pipeline.run_pipeline(
        source_key="first-party-neo4j",
        root=ROOT,
        runtime_dir=runtime,
        retrieved_at="2026-08-30T12:00:00Z",
        first_party_fetcher=lambda source: greenhouse,
    )

    published = json.loads((runtime / "jobs.json").read_text())
    assert {record["id"] for record in prior} <= {record["id"] for record in published}
    assert all(
        record["sourceDataset"].startswith("https://openknowledgegraphs.com/jobs/source/")
        for record in published
    )
    assert all(
        occurrence["sourceDataset"].startswith("https://openknowledgegraphs.com/jobs/source/")
        for record in published for occurrence in record.get("sourceOccurrences", [])
    )


def test_finalized_catalog_manifest_includes_root_organization_registry():
    manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_text())
    assert "organizations.ttl" in {entry["path"] for entry in manifest["artifacts"]}


def test_public_snapshot_contains_no_prototype_job_iris():
    outputs = [
        REPO_ROOT / "data" / "jobs" / "jobs.json",
        REPO_ROOT / "data" / "jobs" / "jobs.ttl",
        REPO_ROOT / "data" / "jobs" / "run.json",
        *(REPO_ROOT / "data" / "jobs" / "raw").glob("*.json"),
    ]
    assert not [
        path.relative_to(REPO_ROOT).as_posix()
        for path in outputs
        if b"/prototypes/kg-jobs/" in path.read_bytes()
    ]
