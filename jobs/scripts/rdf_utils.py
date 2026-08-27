"""Deterministic RDF serialization helpers."""

from pathlib import Path

from rdflib import Graph
from rdflib.compare import to_canonical_graph


def write_deterministic_turtle(graph: Graph, destination: Path) -> None:
    """Write Turtle with canonical blank-node identifiers."""

    stable = Graph()
    for prefix, namespace in graph.namespace_manager.namespaces():
        stable.bind(prefix, namespace, replace=True)
    for triple in to_canonical_graph(graph):
        stable.add(triple)
    stable.serialize(destination=str(destination), format="turtle")
