"""Production contracts for the 12 explicitly approved first-party sources."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
from rdflib import Graph, Namespace, RDF

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
FIXTURES = ROOT / "tests" / "fixtures" / "first-party-pilot"
sys.path.insert(0, str(ROOT / "scripts"))

import first_party_sources as fps  # noqa: E402
import live_pipeline  # noqa: E402
import source_schedule  # noqa: E402
from task42_source_audit import TASK42_SOURCE_KEYS  # noqa: E402
from live_sources import (  # noqa: E402
    LivePipelineError, RefreshNotDueError, load_production_source_registry,
)


APPROVED = {
    "first-party-neo4j", "first-party-relationalai", "first-party-tigergraph",
    "first-party-wikimedia", "first-party-stardog", "first-party-weaviate",
    "first-party-graphwise", "first-party-enterprise-knowledge",
    "first-party-metaphacts", "first-party-topquadrant", "first-party-eccenca",
    "first-party-w3c",
}


def fixture_payload(source):
    name = {
        "firstparty-greenhouse": "greenhouse.json",
        "firstparty-lever": "lever.json",
        "firstparty-ashby": "ashby.json",
        "firstparty-schema": "schema.html",
        "firstparty-graphwise": "first-party-graphwise.json",
        "firstparty-rippling": "rippling.json",
        "firstparty-eccenca": "first-party-eccenca.json",
    }[source.adapter]
    path = FIXTURES / name
    return json.loads(path.read_text()) if path.suffix == ".json" else path.read_text()


def directory_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
    return digest.hexdigest()


def test_production_loader_admits_task39_and_task42_approved_sources():
    sources = fps.load_production_first_party_sources()
    assert set(sources) == APPROVED | set(TASK42_SOURCE_KEYS)
    assert all(source.production_approved for source in sources.values())
    assert all(source.review_status == "evidence-reviewed" for source in sources.values())
    assert all(source.republication_status == "production-approved" for source in sources.values())
    organizations = json.loads((REPO_ROOT / "data" / "organizations.json").read_text())
    approved_organizations = {
        row["iri"] for row in organizations["organizations"]
        if row.get("jobsProductionEnabled")
    }
    assert approved_organizations == {
        source.organization_iri for source in sources.values()
    }


@pytest.mark.parametrize("source_key", sorted(APPROVED))
def test_every_approved_source_runs_end_to_end_through_production_pipeline(
    tmp_path, source_key,
):
    sources = fps.load_production_first_party_sources()
    source = sources[source_key]
    runtime = tmp_path / source_key
    run = live_pipeline.run_pipeline(
        source_key=source_key,
        root=ROOT,
        runtime_dir=runtime,
        retrieved_at="2026-08-27T12:00:00Z",
        first_party_fetcher=lambda selected: fixture_payload(selected),
    )
    records = json.loads((runtime / "jobs.json").read_text())
    diagnostics = json.loads(
        (runtime / "sources" / f"{source_key}.json").read_text()
    )
    assert run["sourceKey"] == source_key
    assert run["queryCount"] == 0
    assert run["requestCount"] == fps.request_count_from_payload(
        fixture_payload(source), source
    )
    assert run["fetchedCount"] == {
        "first-party-graphwise": 2,
        "first-party-eccenca": 2,
    }.get(source_key, 1)
    assert len(diagnostics) == run["fetchedCount"]
    assert all(record["classification"] == "qualified" for record in records)
    assert len(records) == run["sourceClassificationCounts"]["qualified"]
    assert run["publicSourceCount"] == len(records)
    assert run["publicationPolicy"] == "first-party-qualified-only"
    assert all(record["sourceName"] == source.attribution_text for record in records)
    assert all(record["sourceAttributionUrl"] == source.attribution_url for record in records)
    assert all(record["firstParty"] is True for record in records)
    assert all(len(record["sourceOccurrences"]) == 1 for record in records)
    assert (runtime / "raw" / f"{source_key}.json").is_file()
    assert (runtime / "sources" / f"{source_key}.json").is_file()
    if records:
        assert any(record.get("catalogMentions") for record in records)


def test_first_party_review_and_not_match_stay_diagnostic_while_aggregator_review_remains_public(
    tmp_path,
):
    runtime = tmp_path / "runtime"
    (runtime / "sources").mkdir(parents=True)
    aggregator = copy.deepcopy(json.loads((REPO_ROOT / "data/jobs/jobs.json").read_text())[0])
    aggregator["id"] = "aggregator-review-policy-contract"
    aggregator["sourceRecordId"] = "aggregator-review-policy-contract"
    aggregator["classification"] = "review"
    (runtime / "sources" / "adzuna.json").write_text(
        json.dumps([aggregator]), encoding="utf-8"
    )

    payload = fixture_payload(
        fps.load_production_first_party_sources()["first-party-neo4j"]
    )
    payload["jobs"].extend([
        {
            **payload["jobs"][0],
            "id": 1002,
            "absolute_url": "https://boards.greenhouse.io/example/jobs/1002",
            "title": "Metadata Specialist",
            "content": "Lead taxonomy management for our public records catalog.",
            "requisition_id": "REQ-1002",
        },
        {
            **payload["jobs"][0],
            "id": 1003,
            "absolute_url": "https://boards.greenhouse.io/example/jobs/1003",
            "title": "Quality Assurance Technician",
            "content": "Inspect steel components against tolerance specifications.",
            "requisition_id": "REQ-1003",
        },
    ])
    run = live_pipeline.run_pipeline(
        source_key="first-party-neo4j", root=ROOT, runtime_dir=runtime,
        retrieved_at="2026-08-27T12:00:00Z",
        first_party_fetcher=lambda source: payload,
    )

    diagnostics = json.loads(
        (runtime / "sources" / "first-party-neo4j.json").read_text()
    )
    public = json.loads((runtime / "jobs.json").read_text())
    assert run["sourceClassificationCounts"] == {
        "qualified": 1, "review": 1, "not_match": 1,
    }
    assert {row["classification"] for row in diagnostics} == {
        "qualified", "review", "not_match",
    }
    assert [row["classification"] for row in public if row.get("firstParty")] == [
        "qualified"
    ]
    assert any(
        row["id"] == "aggregator-review-policy-contract"
        and row["classification"] == "review"
        for row in public
    )


def test_legitimate_zero_is_published_but_parse_failure_preserves_last_good_bytes(tmp_path):
    runtime = tmp_path / "runtime"
    live_pipeline.run_pipeline(
        source_key="first-party-neo4j", root=ROOT, runtime_dir=runtime,
        retrieved_at="2026-08-27T12:00:00Z",
        first_party_fetcher=lambda source: fixture_payload(source),
    )
    zero = live_pipeline.run_pipeline(
        source_key="first-party-neo4j", root=ROOT, runtime_dir=runtime,
        retrieved_at="2026-08-28T12:00:00Z",
        first_party_fetcher=lambda source: {"jobs": []},
    )
    assert zero["fetchedCount"] == 0
    assert json.loads((runtime / "jobs.json").read_text()) == []

    live_pipeline.run_pipeline(
        source_key="first-party-neo4j", root=ROOT, runtime_dir=runtime,
        retrieved_at="2026-08-29T12:00:00Z",
        first_party_fetcher=lambda source: fixture_payload(source),
    )
    before = directory_digest(runtime)
    with pytest.raises(LivePipelineError, match="failed safely"):
        live_pipeline.run_pipeline(
            source_key="first-party-neo4j", root=ROOT, runtime_dir=runtime,
            retrieved_at="2026-08-30T12:00:00Z",
            first_party_fetcher=lambda source: {},
        )
    assert directory_digest(runtime) == before


def test_schema_blank_or_challenge_page_preserves_last_good_bytes(tmp_path):
    runtime = tmp_path / "runtime"
    live_pipeline.run_pipeline(
        source_key="first-party-w3c", root=ROOT, runtime_dir=runtime,
        retrieved_at="2026-08-27T12:00:00Z",
        first_party_fetcher=lambda source: fixture_payload(source),
    )
    before = directory_digest(runtime)
    for index, payload in enumerate((
        "",
        "<html><body><h1>Just a moment...</h1><p>Checking your browser.</p></body></html>",
    ), start=1):
        with pytest.raises(LivePipelineError, match="failed safely"):
            live_pipeline.run_pipeline(
                source_key="first-party-w3c", root=ROOT, runtime_dir=runtime,
                retrieved_at=f"2026-08-{27 + index}T12:00:00Z",
                first_party_fetcher=lambda source, value=payload: value,
            )
        assert directory_digest(runtime) == before


def test_24_hour_skip_is_byte_identical_and_never_calls_source(tmp_path):
    runtime = tmp_path / "runtime"
    live_pipeline.run_pipeline(
        source_key="first-party-graphwise", root=ROOT, runtime_dir=runtime,
        retrieved_at="2026-08-27T12:00:00Z",
        first_party_fetcher=lambda source: fixture_payload(source),
    )
    before = directory_digest(runtime)
    with pytest.raises(RefreshNotDueError):
        live_pipeline.run_pipeline(
            source_key="first-party-graphwise", root=ROOT, runtime_dir=runtime,
            retrieved_at="2026-08-27T13:00:00Z",
            first_party_fetcher=lambda source: (_ for _ in ()).throw(
                AssertionError("refresh guard did not run first")
            ),
        )
    assert directory_digest(runtime) == before


def test_occurrence_provenance_has_json_rdf_parity(tmp_path):
    runtime = tmp_path / "runtime"
    live_pipeline.run_pipeline(
        source_key="first-party-graphwise", root=ROOT, runtime_dir=runtime,
        retrieved_at="2026-08-27T12:00:00Z",
        first_party_fetcher=lambda source: fixture_payload(source),
    )
    records = json.loads((runtime / "jobs.json").read_text())
    graph = Graph().parse(runtime / "jobs.ttl", format="turtle")
    kgjobs = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
    occurrences = set(graph.subjects(RDF.type, Namespace("http://www.w3.org/ns/prov#").Entity))
    occurrence_nodes = set(graph.objects(None, kgjobs.sourceOccurrence))
    assert len(occurrence_nodes) == sum(len(row["sourceOccurrences"]) for row in records)
    assert occurrence_nodes <= occurrences
    assert {
        str(value) for value in graph.objects(None, kgjobs.occurrenceRecordId)
    } == {
        occurrence["sourceRecordId"]
        for record in records for occurrence in record["sourceOccurrences"]
    }


def test_workflow_derives_production_sources_and_keeps_first_party_diagnostics_off_pages():
    workflow = (REPO_ROOT / ".github" / "workflows" / "update-jobs.yml").read_text()
    expected = {
        *load_production_source_registry(REPO_ROOT / "sources.ttl"),
        *fps.load_production_first_party_sources(),
    }
    batches = source_schedule.bounded_batches()
    assert {key for batch in batches for key in batch} == expected
    assert all(
        sum(source_schedule.production_source_weights()[key] for key in batch)
        <= source_schedule.DEFAULT_BATCH_REQUEST_CAP
        for batch in batches
    )
    assert "scripts/task42_nightly.py" in workflow
    assert "--batch-request-cap" not in workflow  # runner owns the reviewed default
    assert "sources=(" not in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "runtime/raw/first-party-*.json" in workflow
    assert "runtime/sources/first-party-*.json" in workflow
    assert "first-party-diagnostics-${{ github.run_id }}" in workflow
    assert "actions/cache/restore@v4" in workflow
    assert "actions/cache/save@v4" in workflow
    assert "kg-jobs-private-last-good-${{ github.ref_name }}-" in workflow
    assert "scripts/promote_jobs_snapshot.py" in workflow
    assert "data/jobs/sources" not in workflow
    assert "inputs.dry_run != true" in workflow


def test_site_keeps_first_party_description_internal_and_renders_attribution_contract():
    app = (REPO_ROOT / "site" / "app.js").read_text()
    assert "item.sourceAttributionUrl" in app
    assert "`Source: ${item.sourceName || \"original board\"}`" in app
    assert "appendCardDescription(card, item.description)" not in app
    assert "description.textContent = value" in app  # used only by non-job catalog cards
