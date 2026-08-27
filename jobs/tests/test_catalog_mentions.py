"""Network-free regression coverage for page-backed catalog mentions."""

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from rdflib import Namespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog_mentions import (  # noqa: E402
    add_catalog_mentions,
    build_match_index,
    load_match_index,
    load_policy,
)
from live_records import build_graph  # noqa: E402
from live_sources import load_source_registry  # noqa: E402
from rebuild_catalog_mentions import _publish_verified_snapshot  # noqa: E402

REPO_ROOT = ROOT.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from catalog_snapshot import verify_jobs_manifest, write_jobs_manifest  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "catalog-mentions.json"
POLICY_PATH = ROOT / "catalog-mention-policy.json"
SCHEMA = Namespace("https://schema.org/")
KGJDLIVE = Namespace("https://openknowledgegraphs.com/jobs/live/")


def fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def mention_index(policy=None):
    data = fixture()
    return build_match_index(
        data["ontologies"],
        data["software"],
        data["pageQids"],
        policy or load_policy(POLICY_PATH),
    )


def test_required_mentions_resolve_to_current_detail_pages_in_stable_order():
    mentions = add_catalog_mentions(
        [
            {
                "id": "job-1",
                "title": "TopBraid EDG and Neo4j engineer",
                "description": "Publish controlled vocabularies using SKOS and RDF.",
            }
        ],
        mention_index(),
    )[0]["catalogMentions"]
    assert [mention["title"] for mention in mentions] == [
        "TopBraid EDG",
        "Neo4j",
        "SKOS",
        "Resource Description Framework",
    ]
    assert mentions == [
        {
            "title": "TopBraid EDG",
            "dataset": "software",
            "qid": "Q141112436",
            "canonicalUrl": "https://openknowledgegraphs.com/software/topbraid-edg/",
            "matchedPhrase": "TopBraid EDG",
        },
        {
            "title": "Neo4j",
            "dataset": "software",
            "qid": "Q1628290",
            "canonicalUrl": "https://openknowledgegraphs.com/software/neo4j/",
            "matchedPhrase": "Neo4j",
        },
        {
            "title": "SKOS",
            "dataset": "resource",
            "qid": "Q2288360",
            "canonicalUrl": "https://openknowledgegraphs.com/resource/skos/",
            "matchedPhrase": "SKOS",
        },
        {
            "title": "Resource Description Framework",
            "dataset": "resource",
            "qid": "Q54872",
            "canonicalUrl": "https://openknowledgegraphs.com/resource/resource-description-framework/",
            "matchedPhrase": "RDF",
        },
    ]


def test_short_acronyms_are_case_sensitive_and_generic_or_substring_hits_are_rejected():
    mentions = add_catalog_mentions(
        [
            {
                "id": "job-2",
                "title": "rdf skos neo4j Framework Processor ICE BIO",
                "description": "Neo4jacent SKOSish RDFology and a Dead Link.",
            }
        ],
        mention_index(),
    )[0]["catalogMentions"]
    assert mentions == []


def test_longest_match_suppresses_only_overlapping_span_and_deduplicates_earliest_target():
    mentions = add_catalog_mentions(
        [
            {
                "id": "job-3",
                "title": "RDF Schema specialist using RDF",
                "description": "RDF appears again after the title.",
            }
        ],
        mention_index(),
    )[0]["catalogMentions"]
    assert [(mention["title"], mention["matchedPhrase"]) for mention in mentions] == [
        ("RDF Schema", "RDF Schema"),
        ("Resource Description Framework", "RDF"),
    ]


def test_ambiguous_alias_is_rejected_until_a_reviewed_override_selects_one_target():
    assert add_catalog_mentions(
        [{"id": "job-4", "title": "Shared Technology engineer", "description": ""}],
        mention_index(),
    )[0]["catalogMentions"] == []

    policy = copy.deepcopy(load_policy(POLICY_PATH))
    policy["disambiguationOverrides"]["Shared Technology"] = {
        "dataset": "resource",
        "qid": "Q40",
    }
    mentions = add_catalog_mentions(
        [{"id": "job-4", "title": "Shared Technology engineer", "description": ""}],
        mention_index(policy),
    )[0]["catalogMentions"]
    assert [(mention["dataset"], mention["qid"]) for mention in mentions] == [
        ("resource", "Q40")
    ]


def test_reviewed_exact_aliases_and_mixed_case_short_name_resolve():
    mentions = add_catalog_mentions(
        [
            {
                "id": "job-reviewed-aliases",
                "title": "RDFox engineer",
                "description": "Deploy PoolParty and publish PROV-O provenance.",
            }
        ],
        mention_index(),
    )[0]["catalogMentions"]
    assert [(mention["qid"], mention["matchedPhrase"]) for mention in mentions] == [
        ("Q105745603", "RDFox"),
        ("Q28136436", "PoolParty"),
        ("Q62213429", "PROV-O"),
    ]


def test_production_catalog_backs_required_rdf_and_reviewed_aliases():
    index = load_match_index(REPO_ROOT, POLICY_PATH)
    mentions = add_catalog_mentions(
        [
            {
                "id": "production-regression",
                "title": "Applied AI/ML Engineer",
                "description": "Build RDF with RDFox, PoolParty, and PROV-O.",
            }
        ],
        index,
    )[0]["catalogMentions"]
    assert [(mention["qid"], mention["matchedPhrase"]) for mention in mentions] == [
        ("Q54872", "RDF"),
        ("Q105745603", "RDFox"),
        ("Q28136436", "PoolParty"),
        ("Q62213429", "PROV-O"),
    ]
    rdf = mentions[0]
    assert rdf["canonicalUrl"] == (
        "https://openknowledgegraphs.com/resource/resource-description-framework/"
    )

    production_jobs = json.loads(
        (REPO_ROOT / "data" / "jobs" / "jobs.json").read_text(encoding="utf-8")
    )
    applied = next(job for job in production_jobs if job["title"] == "Applied AI/ML Engineer")
    assert any(mention["qid"] == "Q54872" for mention in applied["catalogMentions"])


def test_catalog_mentions_are_additive_and_have_json_rdf_parity():
    original = {
        "id": "fixture-job",
        "title": "Neo4j and SKOS engineer",
        "description": "Build graph systems.",
        "classification": "qualified",
        "evidence": [],
        "canonicalUrl": "https://jobs.example.test/fixture-job",
        "hiringOrganization": "Fixture Employer",
        "sourceRecordId": "fixture-job",
        "canonicalFingerprint": "fixture-fingerprint",
        "firstSeenAt": "2026-08-24T12:00:00Z",
        "lastSeenAt": "2026-08-24T12:00:00Z",
        "retrievedAt": "2026-08-24T12:00:00Z",
        "active": True,
        "sourceDataset": "https://openknowledgegraphs.com/jobs/source/himalayas",
        "sourceUrl": "https://jobs.example.test/fixture-job",
    }
    enriched = add_catalog_mentions([original], mention_index())[0]
    assert enriched["classification"] == original["classification"]
    assert enriched["evidence"] == original["evidence"]

    run = {
        "runId": "fixture-run",
        "retrievedAt": "2026-08-24T12:00:00Z",
        "queryResults": [],
    }
    source = load_source_registry(REPO_ROOT / "sources.ttl")["himalayas"]
    graph = build_graph([enriched], run, source)
    job = KGJDLIVE[f"job/{quote(enriched['id'], safe='')}"]
    rdf_mentions = {str(value) for value in graph.objects(job, SCHEMA.mentions)}
    assert rdf_mentions == {
        mention["canonicalUrl"] for mention in enriched["catalogMentions"]
    }


def _manifest_fixture(root: Path) -> None:
    jobs = root / "data" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "jobs.json").write_text("[]\n", encoding="utf-8")
    (jobs / "jobs.ttl").write_text("@prefix ex: <https://example.test/> .\n", encoding="utf-8")
    (jobs / "run.json").write_text('{}\n', encoding="utf-8")
    write_jobs_manifest(
        root,
        started_at="2026-08-24T12:00:00Z",
        source_retrieved_at="2026-08-24T12:00:01Z",
        completed_at="2026-08-24T12:00:02Z",
    )


def test_offline_publish_updates_and_verifies_the_jobs_manifest():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _manifest_fixture(root)
        manifest = _publish_verified_snapshot(
            root,
            b'[ {"id": "changed"} ]\n',
            b'@prefix ex: <https://example.test/> . ex:job ex:name "Changed" .\n',
            completed_at="2026-08-24T12:00:03Z",
        )
        assert verify_jobs_manifest(root) == manifest
        assert manifest["completedAt"] == "2026-08-24T12:00:03Z"


def test_offline_publish_rolls_back_a_partial_replacement(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _manifest_fixture(root)
        paths = [
            root / "data" / "jobs" / name
            for name in ("jobs.json", "jobs.ttl", "manifest.json")
        ]
        before = {path: path.read_bytes() for path in paths}
        real_replace = os.replace
        failed = False

        def fail_once(source, destination):
            nonlocal failed
            if not failed and Path(destination) == paths[1]:
                failed = True
                raise OSError("simulated second-file publication failure")
            return real_replace(source, destination)

        monkeypatch.setattr(os, "replace", fail_once)
        try:
            _publish_verified_snapshot(
                root,
                b'[ {"id": "changed"} ]\n',
                b'@prefix ex: <https://example.test/> . ex:job ex:name "Changed" .\n',
                completed_at="2026-08-24T12:00:03Z",
            )
        except OSError as exc:
            assert "simulated" in str(exc)
        else:
            raise AssertionError("partial publication failure was not raised")
        assert {path: path.read_bytes() for path in paths} == before
        verify_jobs_manifest(root)
