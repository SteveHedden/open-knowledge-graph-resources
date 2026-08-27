"""SHACL conformance for the generated kg-jobs graph. Network-free."""

import subprocess
import sys
from pathlib import Path

from pyshacl import validate
from rdflib import Graph

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent


def _regenerate():
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate.py")],
        check=True,
        cwd=str(ROOT),
    )


def test_generated_graph_conforms_to_shapes():
    _regenerate()

    data = Graph()
    data.parse(ROOT / "data" / "jobs.ttl", format="turtle")
    data.parse(ROOT / "vocabularies" / "kg-jobs.ttl", format="turtle")
    data.parse(REPO_ROOT / "sources.ttl", format="turtle")
    data.parse(REPO_ROOT / "organizations.ttl", format="turtle")
    data.parse(REPO_ROOT / "ontology.ttl", format="turtle")

    shapes = Graph()
    shapes.parse(ROOT / "ontology.ttl", format="turtle")

    conforms, _, results_text = validate(
        data, shacl_graph=shapes, ont_graph=shapes, inference="none", abort_on_first=False
    )
    assert conforms, results_text


def test_all_source_files_parse_as_valid_turtle():
    for path in [
        ROOT / "ontology.ttl",
        ROOT / "vocabularies" / "kg-jobs.ttl",
        REPO_ROOT / "sources.ttl",
        REPO_ROOT / "organizations.ttl",
        ROOT / "data" / "jobs.ttl",
    ]:
        g = Graph()
        g.parse(path, format="turtle")
        assert len(g) > 0, f"{path} parsed but produced an empty graph"
