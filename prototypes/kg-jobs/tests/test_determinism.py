"""Determinism and RDF/JSON parity checks. Network-free.

Re-running the generator against unchanged inputs must produce a
semantically identical (isomorphic) RDF graph and byte-identical JSON.
"""

import filecmp
import json
import shutil
import subprocess
import sys
from pathlib import Path

from rdflib import Graph
from rdflib.compare import isomorphic

ROOT = Path(__file__).resolve().parent.parent


def _generate_into(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate.py")], check=True, cwd=str(ROOT))
    shutil.copy(ROOT / "data" / "jobs.ttl", out_dir / "jobs.ttl")
    shutil.copy(ROOT / "data" / "jobs.json", out_dir / "jobs.json")


def test_two_builds_are_isomorphic_and_byte_identical_json(tmp_path):
    build_a = tmp_path / "a"
    build_b = tmp_path / "b"
    _generate_into(build_a)
    _generate_into(build_b)

    assert filecmp.cmp(build_a / "jobs.json", build_b / "jobs.json", shallow=False), (
        "JSON projection must be byte-identical across two builds"
    )

    g_a = Graph()
    g_a.parse(build_a / "jobs.ttl", format="turtle")
    g_b = Graph()
    g_b.parse(build_b / "jobs.ttl", format="turtle")
    assert isomorphic(g_a, g_b), "generated RDF graph must be isomorphic across two builds"


def test_json_matches_rdf_record_identity_and_classification():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate.py")], check=True, cwd=str(ROOT))

    with (ROOT / "data" / "jobs.json").open(encoding="utf-8") as f:
        json_records = {r["id"]: r for r in json.load(f)}

    graph = Graph()
    graph.parse(ROOT / "data" / "jobs.ttl", format="turtle")

    from rdflib import Namespace
    SCHEMA = Namespace("https://schema.org/")
    KGJOBS = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/ontology#")

    rdf_ids = set()
    for job, ident in graph.subject_objects(SCHEMA.identifier):
        rdf_ids.add(str(ident))
        classification = graph.value(job, KGJOBS.classification)
        assert json_records[str(ident)]["classification"] == str(classification)

    assert rdf_ids == set(json_records.keys())


def test_json_optional_employer_qid_not_required():
    """Employer Wikidata identity is optional -- the record schema never
    requires a QID for hiringOrganization."""
    with (ROOT / "data" / "jobs.json").open(encoding="utf-8") as f:
        records = json.load(f)
    for record in records:
        assert isinstance(record["hiringOrganization"], str)
        assert "wikidataId" not in record


def test_records_are_typed_as_job_postings():
    graph = Graph()
    graph.parse(ROOT / "data" / "jobs.ttl", format="turtle")
    from rdflib import RDF, Namespace
    SCHEMA = Namespace("https://schema.org/")
    postings = set(graph.subjects(RDF.type, SCHEMA.JobPosting))
    assert len(postings) == 20


def test_generated_ordering_is_stable_across_runs():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate.py")], check=True, cwd=str(ROOT))
    with (ROOT / "data" / "jobs.json").open(encoding="utf-8") as f:
        first = [r["id"] for r in json.load(f)]

    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate.py")], check=True, cwd=str(ROOT))
    with (ROOT / "data" / "jobs.json").open(encoding="utf-8") as f:
        second = [r["id"] for r in json.load(f)]

    assert first == second
