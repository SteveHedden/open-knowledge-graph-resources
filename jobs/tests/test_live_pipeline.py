"""End-to-end live-pipeline tests with injected, network-free fetchers."""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from rdflib import BNode, Graph, Literal, Namespace, RDF

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import live_pipeline  # noqa: E402
import live_records  # noqa: E402
import rdf_utils  # noqa: E402
from live_sources import LivePipelineError, RefreshNotDueError  # noqa: E402

REMOTIVE_FIXTURE = ROOT / "tests" / "fixtures" / "remotive.json"
HIMALAYAS_FIXTURE = ROOT / "tests" / "fixtures" / "himalayas.json"
ARBEITNOW_FIXTURES = {
    1: ROOT / "tests" / "fixtures" / "arbeitnow-page-1.json",
    2: ROOT / "tests" / "fixtures" / "arbeitnow-page-2.json",
}
NOW = "2026-08-17T18:00:00Z"


def remotive_payload():
    return json.loads(REMOTIVE_FIXTURE.read_text(encoding="utf-8"))


def himalayas_payload():
    return json.loads(HIMALAYAS_FIXTURE.read_text(encoding="utf-8"))


def arbeitnow_payload(page: int):
    return json.loads(ARBEITNOW_FIXTURES[page].read_text(encoding="utf-8"))


def arbeitnow_fetcher(calls):
    def fetch(url, source):
        calls.append(url)
        page = int(parse_qs(urlparse(url).query)["page"][0])
        return arbeitnow_payload(page)

    return fetch


def himalayas_fetcher(calls):
    def fetch(url, source):
        query = parse_qs(urlparse(url).query)["q"][0]
        calls.append(query)
        return himalayas_payload()

    return fetch


def test_explicit_live_flag_is_required():
    with pytest.raises(SystemExit) as exc:
        live_pipeline.main([])
    assert exc.value.code == 2


def test_default_himalayas_pipeline_runs_four_query_families_and_deduplicates(
    tmp_path, monkeypatch
):
    def reject_slow_blank_node_canonicalization(_graph):
        raise AssertionError("named live graphs must bypass blank-node canonicalization")

    monkeypatch.setattr(
        rdf_utils,
        "to_canonical_graph",
        reject_slow_blank_node_canonicalization,
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    calls = []
    run = live_pipeline.run_pipeline(
        root=ROOT,
        runtime_dir=runtime,
        retrieved_at=NOW,
        fetcher=himalayas_fetcher(calls),
    )
    assert calls == ["knowledge graph", "ontology", "semantic web", "SPARQL"]
    assert run["sourceKey"] == "himalayas"
    assert run["requestCount"] == 4
    assert run["queryCount"] == 4
    assert run["queries"] == calls
    assert run["queryResults"] == [
        {
            "queryUri": source_uri,
            "query": query,
            "queryConcepts": concepts,
            "returnedCount": 2,
            "totalCount": 2,
            "complete": True,
        }
        for source_uri, query, concepts in (
            (
                "https://openknowledgegraphs.com/jobs/source/himalayas-query-knowledge-graph",
                "knowledge graph",
                ["https://openknowledgegraphs.com/jobs/vocab/skill-knowledge-graph"],
            ),
            (
                "https://openknowledgegraphs.com/jobs/source/himalayas-query-ontology",
                "ontology",
                [
                    "https://openknowledgegraphs.com/jobs/vocab/role-ontologist",
                    "https://openknowledgegraphs.com/jobs/vocab/role-ontology-engineer",
                ],
            ),
            (
                "https://openknowledgegraphs.com/jobs/source/himalayas-query-semantic-web",
                "semantic web",
                ["https://openknowledgegraphs.com/jobs/vocab/skill-semantic-web"],
            ),
            (
                "https://openknowledgegraphs.com/jobs/source/himalayas-query-sparql",
                "SPARQL",
                ["https://openknowledgegraphs.com/jobs/vocab/skill-sparql"],
            ),
        )
    ]
    assert run["queryFamilies"] == [
        {
            "queryUri": result["queryUri"],
            "query": result["query"],
            "queryConcepts": result["queryConcepts"],
        }
        for result in run["queryResults"]
    ]
    assert run["fetchedCount"] == 8
    assert run["deduplicatedCount"] == 2
    assert run["completeSourceSnapshot"] is True
    assert run["classificationCounts"] == {
        "qualified": 1,
        "review": 0,
        "not_match": 1,
    }

    records = json.loads((runtime / "jobs.json").read_text(encoding="utf-8"))
    ontology_job = next(record for record in records if record["classification"] == "qualified")
    assert ontology_job["discoveredBy"] == calls
    assert ontology_job["sourceName"] == "Himalayas"
    assert ontology_job["sourceAttributionUrl"] == "https://himalayas.app/"
    assert ontology_job["canonicalUrl"].startswith("https://himalayas.app/companies/")
    raw = json.loads((runtime / "raw" / "himalayas.json").read_text())
    assert [response["query"] for response in raw["responses"]] == calls
    assert [response["queryUri"] for response in raw["responses"]] == [
        result["queryUri"] for result in run["queryResults"]
    ]
    graph = Graph()
    graph.parse(runtime / "jobs.ttl", format="turtle")
    assert not any(
        isinstance(term, BNode)
        for triple in graph
        for term in triple
    )
    schema = Namespace("https://schema.org/")
    kgjobs = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
    prov = Namespace("http://www.w3.org/ns/prov#")
    executions = set(graph.subjects(RDF.type, kgjobs.QueryExecution))
    assert len(executions) == 4
    assert all((execution, RDF.type, prov.Activity) in graph for execution in executions)
    parent_activities = {
        parent
        for parent, execution in graph.subject_objects(kgjobs.hasQueryExecution)
        if execution in executions
    }
    assert len(parent_activities) == 1
    assert {
        str(graph.value(execution, kgjobs.queryFamily))
        for execution in executions
    } == {result["queryUri"] for result in run["queryResults"]}
    execution_results = {
        str(graph.value(execution, kgjobs.queryFamily)): {
            "query": str(graph.value(execution, kgjobs.executedQueryText)),
            "returnedCount": int(graph.value(execution, kgjobs.returnedCount)),
            "totalCount": int(graph.value(execution, kgjobs.totalCount)),
            "complete": bool(graph.value(execution, kgjobs.queryComplete).toPython()),
        }
        for execution in executions
    }
    assert execution_results == {
        result["queryUri"]: {
            "query": result["query"],
            "returnedCount": result["returnedCount"],
            "totalCount": result["totalCount"],
            "complete": result["complete"],
        }
        for result in run["queryResults"]
    }
    assert len(list(graph.triples((None, schema.validThrough, None)))) == 2
    assert len(list(graph.triples((None, schema.experienceRequirements, None)))) == 2
    assert len(list(graph.triples((None, schema.jobLocation, None)))) == 2
    applicant_areas = list(
        graph.objects(None, schema.applicantLocationRequirements)
    )
    assert len(applicant_areas) == 3
    assert all((area, RDF.type, schema.AdministrativeArea) in graph for area in applicant_areas)
    assert {str(graph.value(area, schema.name)) for area in applicant_areas} == {
        "Americas", "Europe", "Worldwide",
    }

    ontology_job = next(graph.subjects(schema.title, Literal("Ontology Engineer")))
    monetary_amount = graph.value(ontology_job, schema.baseSalary)
    assert (monetary_amount, RDF.type, schema.MonetaryAmount) in graph
    assert str(graph.value(monetary_amount, schema.currency)) == "USD"
    quantitative_value = graph.value(monetary_amount, schema.value)
    assert (quantitative_value, RDF.type, schema.QuantitativeValue) in graph
    assert int(graph.value(quantitative_value, schema.minValue)) == 120000
    assert int(graph.value(quantitative_value, schema.maxValue)) == 150000
    assert str(graph.value(quantitative_value, schema.unitText)) == "year"


def test_arbeitnow_pipeline_is_bounded_and_publishes_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    calls = []
    run = live_pipeline.run_pipeline(
        source_key="arbeitnow",
        root=ROOT,
        runtime_dir=runtime,
        retrieved_at=NOW,
        fetcher=arbeitnow_fetcher(calls),
    )
    assert len(calls) == 2
    assert run["sourceKey"] == "arbeitnow"
    assert run["requestCount"] == 2
    assert run["fetchedCount"] == 5
    assert run["deduplicatedCount"] == 5
    assert run["rejectedCount"] == 0
    assert run["completeSourceSnapshot"] is True
    assert run["classificationCounts"] == {
        "qualified": 3,
        "review": 1,
        "not_match": 1,
    }

    records = json.loads((runtime / "jobs.json").read_text(encoding="utf-8"))
    assert len(records) == 5
    assert all(record["active"] for record in records)
    raw = json.loads((runtime / "raw" / "arbeitnow.json").read_text())
    assert raw == {
        "sourceKey": "arbeitnow",
        "pages": [arbeitnow_payload(1), arbeitnow_payload(2)],
    }
    assert json.loads((runtime / "run.json").read_text()) == run

    graph = Graph()
    graph.parse(runtime / "jobs.ttl", format="turtle")
    schema = Namespace("https://schema.org/")
    prov = Namespace("http://www.w3.org/ns/prov#")
    jobs = set(graph.subjects(RDF.type, schema.JobPosting))
    assert len(jobs) == len(records)
    assert any(graph.triples((None, prov.wasGeneratedBy, None)))
    assert len(list(graph.triples((None, schema.baseSalary, None)))) == 2
    rdf_ids = {str(value) for value in graph.objects(None, schema.identifier)}
    assert rdf_ids == {record["id"] for record in records}


def test_himalayas_refresh_guard_enforces_its_24_hour_registry_interval(tmp_path):
    runtime = tmp_path / "runtime"
    calls = []
    fetcher = himalayas_fetcher(calls)
    live_pipeline.run_pipeline(
        root=ROOT, runtime_dir=runtime, retrieved_at=NOW, fetcher=fetcher
    )
    with pytest.raises(RefreshNotDueError, match="retry in"):
        live_pipeline.run_pipeline(
            root=ROOT, runtime_dir=runtime,
            retrieved_at="2026-08-18T17:00:00Z", fetcher=fetcher,
        )
    assert len(calls) == 4
    live_pipeline.run_pipeline(
        root=ROOT, runtime_dir=runtime,
        retrieved_at="2026-08-18T19:00:00Z", fetcher=fetcher,
    )
    assert len(calls) == 8


def test_refresh_guard_blocks_before_network_then_allows_later_run(tmp_path):
    runtime = tmp_path / "runtime"
    calls = []
    fetcher = arbeitnow_fetcher(calls)
    live_pipeline.run_pipeline(
        source_key="arbeitnow", root=ROOT, runtime_dir=runtime,
        retrieved_at=NOW, fetcher=fetcher
    )
    with pytest.raises(LivePipelineError, match="retry in"):
        live_pipeline.run_pipeline(
            source_key="arbeitnow", root=ROOT,
            runtime_dir=runtime,
            retrieved_at="2026-08-17T18:30:00Z",
            fetcher=fetcher,
        )
    assert len(calls) == 2
    live_pipeline.run_pipeline(
        source_key="arbeitnow", root=ROOT,
        runtime_dir=runtime,
        retrieved_at="2026-08-17T20:00:00Z",
        fetcher=fetcher,
    )
    assert len(calls) == 4


def test_refresh_guard_is_preserved_when_switching_sources(tmp_path):
    runtime = tmp_path / "runtime"
    arbeitnow_calls = []
    live_pipeline.run_pipeline(
        source_key="arbeitnow", root=ROOT, runtime_dir=runtime, retrieved_at=NOW,
        fetcher=arbeitnow_fetcher(arbeitnow_calls),
    )
    live_pipeline.run_pipeline(
        source_key="remotive", root=ROOT, runtime_dir=runtime,
        include_review_aggregators=True,
        retrieved_at="2026-08-17T18:10:00Z",
        fetcher=lambda url, source: remotive_payload(),
    )
    run = json.loads((runtime / "run.json").read_text())
    assert run["sourceRefreshes"] == {
        "arbeitnow": NOW,
        "remotive": "2026-08-17T18:10:00Z",
    }
    with pytest.raises(LivePipelineError, match="retry in"):
        live_pipeline.run_pipeline(
            source_key="arbeitnow", root=ROOT, runtime_dir=runtime,
            retrieved_at="2026-08-17T18:30:00Z",
            fetcher=arbeitnow_fetcher(arbeitnow_calls),
        )
    assert len(arbeitnow_calls) == 2


def test_complete_pull_replaces_snapshot_and_absent_jobs_disappear(tmp_path):
    runtime = tmp_path / "runtime"
    live_pipeline.run_pipeline(
        source_key="remotive", root=ROOT, runtime_dir=runtime,
        include_review_aggregators=True,
        retrieved_at=NOW, fetcher=lambda url, source: remotive_payload(),
    )
    smaller = remotive_payload()
    smaller["jobs"] = smaller["jobs"][:1]
    smaller["job-count"] = 1
    live_pipeline.run_pipeline(
        source_key="remotive", root=ROOT, runtime_dir=runtime,
        include_review_aggregators=True,
        retrieved_at="2026-08-18T01:00:00Z",
        fetcher=lambda url, source: smaller,
    )
    records = json.loads((runtime / "jobs.json").read_text())
    assert [record["sourceRecordId"] for record in records] == ["7001"]


def test_capped_feed_publishes_only_the_current_bounded_sample(tmp_path):
    runtime = tmp_path / "runtime"
    calls = []
    live_pipeline.run_pipeline(
        source_key="arbeitnow", root=ROOT, runtime_dir=runtime, retrieved_at=NOW,
        fetcher=arbeitnow_fetcher(calls),
    )
    initial_ids = {
        record["id"] for record in json.loads((runtime / "jobs.json").read_text())
    }

    def incomplete(url, source):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        payload = arbeitnow_payload(1)
        payload["meta"]["current_page"] = page
        payload["meta"]["last_page"] = 10
        payload["data"] = payload["data"][:1]
        return payload

    run = live_pipeline.run_pipeline(
        source_key="arbeitnow", root=ROOT, runtime_dir=runtime,
        retrieved_at="2026-08-17T20:00:00Z", fetcher=incomplete,
    )
    assert run["requestCount"] == 3
    assert run["completeSourceSnapshot"] is False
    current_ids = {
        record["id"] for record in json.loads((runtime / "jobs.json").read_text())
    }
    assert current_ids < initial_ids
    assert current_ids == {"arbeitnow-mock-knowledge-graph-engineer-8001"}


def test_fetch_mapping_or_validation_failure_preserves_last_good_snapshot(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    live_pipeline.run_pipeline(
        source_key="remotive", root=ROOT, runtime_dir=runtime,
        include_review_aggregators=True,
        retrieved_at=NOW, fetcher=lambda url, source: remotive_payload(),
    )
    before = {
        path.relative_to(runtime): path.read_bytes()
        for path in runtime.rglob("*") if path.is_file()
    }

    with pytest.raises(LivePipelineError, match="failed normalization"):
        broken = remotive_payload()
        broken["jobs"][0].pop("title")
        live_pipeline.run_pipeline(
            source_key="remotive", root=ROOT, runtime_dir=runtime,
            include_review_aggregators=True,
            retrieved_at="2026-08-18T01:00:00Z",
            fetcher=lambda url, source: broken,
        )

    monkeypatch.setattr(
        live_records, "validate_graph",
        lambda *args, **kwargs: (_ for _ in ()).throw(LivePipelineError("mock SHACL failure")),
    )
    with pytest.raises(LivePipelineError, match="mock SHACL failure"):
        live_pipeline.run_pipeline(
            source_key="remotive", root=ROOT, runtime_dir=runtime,
            include_review_aggregators=True,
            retrieved_at="2026-08-18T01:00:00Z",
            fetcher=lambda url, source: remotive_payload(),
        )
    after = {
        path.relative_to(runtime): path.read_bytes()
        for path in runtime.rglob("*") if path.is_file()
    }
    assert after == before


def test_atomic_promotion_recovers_from_keyboard_interrupt(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / ".kg-jobs-live-test"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_replace = live_records.os.replace
    calls = 0

    def interrupt_second_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt()
        return real_replace(source, target)

    monkeypatch.setattr(live_records.os, "replace", interrupt_second_replace)
    with pytest.raises(KeyboardInterrupt):
        live_records._atomic_replace_directory(stage, runtime)
    assert (runtime / "old.txt").read_text() == "old"
    assert not runtime.with_name(".runtime-previous").exists()


def test_deterministic_replay_has_identical_json_and_isomorphic_rdf(tmp_path):
    outputs = []
    for name in ("one", "two"):
        runtime = tmp_path / name
        live_pipeline.run_pipeline(
            source_key="arbeitnow", root=ROOT, runtime_dir=runtime, retrieved_at=NOW,
            fetcher=arbeitnow_fetcher([]),
        )
        graph = Graph()
        graph.parse(runtime / "jobs.ttl", format="turtle")
        outputs.append(((runtime / "jobs.json").read_bytes(), graph))
    assert outputs[0][0] == outputs[1][0]
    assert outputs[0][1].isomorphic(outputs[1][1])


def test_cli_exits_zero_on_refresh_not_due_but_nonzero_on_a_real_failure(monkeypatch, capsys):
    """The scheduled workflow relies on this exact distinction to treat a
    too-soon refresh as a no-op skip while still failing on a genuine error."""

    def not_due(**kwargs):
        raise RefreshNotDueError("himalayas permits one refresh every 86400 seconds; retry in 42 seconds")

    monkeypatch.setattr(live_pipeline, "run_pipeline", not_due)
    assert live_pipeline.main(["--live", "--source", "himalayas"]) == 0
    assert "Skipping himalayas" in capsys.readouterr().out

    def hard_failure(**kwargs):
        raise LivePipelineError("himalayas produced no source responses")

    monkeypatch.setattr(live_pipeline, "run_pipeline", hard_failure)
    assert live_pipeline.main(["--live", "--source", "himalayas"]) == 1
    assert "Live ingestion failed safely" in capsys.readouterr().err
