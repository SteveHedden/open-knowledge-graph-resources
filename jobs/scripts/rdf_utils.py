"""Deterministic RDF serialization helpers."""

from pathlib import Path

from rdflib import BNode, Graph
from rdflib.compare import to_canonical_graph


def write_deterministic_turtle(graph: Graph, destination: Path) -> None:
    """Write deterministic Turtle without needlessly canonicalizing named graphs.

    Live jobs graphs use stable IRIs for their internal resources.  RDFLib's
    blank-node canonicalizer becomes prohibitively expensive when thousands
    of structurally similar blank nodes are present, so only use it for
    legacy/input graphs that still contain blank nodes.
    """

    stable = Graph()
    for prefix, namespace in graph.namespace_manager.namespaces():
        stable.bind(prefix, namespace, replace=True)
    has_blank_nodes = any(
        isinstance(term, BNode)
        for triple in graph
        for term in triple
    )
    triples = to_canonical_graph(graph) if has_blank_nodes else graph
    for triple in triples:
        stable.add(triple)
    stable.serialize(destination=str(destination), format="turtle")
