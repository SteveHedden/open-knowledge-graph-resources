"""Structural tests for vocabularies/kg-jobs.ttl. Network-free."""

import re
from pathlib import Path

import pytest
from rdflib import RDF, Graph, Namespace

ROOT = Path(__file__).resolve().parent.parent
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
KGJOBS = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/ontology#")


@pytest.fixture(scope="module")
def vocab_graph() -> Graph:
    g = Graph()
    g.parse(ROOT / "vocabularies" / "kg-jobs.ttl", format="turtle")
    return g


def test_exactly_three_concept_schemes(vocab_graph):
    schemes = set(vocab_graph.subjects(RDF.type, SKOS.ConceptScheme))
    assert len(schemes) == 3


def test_every_concept_in_exactly_one_scheme(vocab_graph):
    concepts = set(vocab_graph.subjects(RDF.type, SKOS.Concept))
    assert concepts, "vocabulary must define at least one concept"
    for concept in concepts:
        schemes = list(vocab_graph.objects(concept, SKOS.inScheme))
        assert len(schemes) == 1, f"{concept} must belong to exactly one scheme"


def test_every_concept_has_required_fields(vocab_graph):
    concepts = set(vocab_graph.subjects(RDF.type, SKOS.Concept))
    for concept in concepts:
        assert vocab_graph.value(concept, SKOS.prefLabel) is not None, f"{concept} missing prefLabel"
        assert vocab_graph.value(concept, SKOS.definition) is not None, f"{concept} missing definition"
        uri = str(concept)
        assert re.match(r"^https://openknowledgegraphs\.com/prototypes/kg-jobs/vocab/[a-z0-9-]+$", uri), (
            f"{concept} does not have a stable, slug-shaped URI"
        )


def test_every_concept_has_valid_match_strength(vocab_graph):
    concepts = set(vocab_graph.subjects(RDF.type, SKOS.Concept))
    for concept in concepts:
        strength = vocab_graph.value(concept, KGJOBS.matchStrength)
        assert strength is not None, f"{concept} missing kgjobs:matchStrength"
        assert str(strength) in ("strong", "contextual"), f"{concept} has invalid matchStrength {strength}"


def test_broader_links_stay_within_the_vocabulary(vocab_graph):
    concepts = set(vocab_graph.subjects(RDF.type, SKOS.Concept))
    for concept in concepts:
        for broader in vocab_graph.objects(concept, SKOS.broader):
            assert broader in concepts, f"{concept} skos:broader points outside the vocabulary: {broader}"


def test_every_concept_has_at_least_one_match_term(vocab_graph):
    concepts = set(vocab_graph.subjects(RDF.type, SKOS.Concept))
    for concept in concepts:
        terms = list(vocab_graph.objects(concept, KGJOBS.hasMatchTerm))
        assert terms, f"{concept} has no kgjobs:hasMatchTerm entries"
        for term in terms:
            assert vocab_graph.value(term, KGJOBS.termText) is not None
            assert vocab_graph.value(term, KGJOBS.caseSensitive) is not None


SHORT_ACRONYMS = {"RDF", "RDFS", "OWL", "SPARQL", "SHACL", "SKOS"}


def test_short_acronyms_are_bounded_case_sensitive(vocab_graph):
    """Short acronyms named in the task spec must match as bounded,
    case-sensitive tokens so ordinary words can't trigger them."""
    for concept in vocab_graph.subjects(RDF.type, SKOS.Concept):
        for term in vocab_graph.objects(concept, KGJOBS.hasMatchTerm):
            text = str(vocab_graph.value(term, KGJOBS.termText))
            if text in SHORT_ACRONYMS:
                case_sensitive = vocab_graph.value(term, KGJOBS.caseSensitive)
                assert bool(case_sensitive) is True, f"{text} match term must be case-sensitive"


def test_avoids_isolated_broad_terms(vocab_graph):
    """Terms like bare 'graph', 'knowledge', 'data', 'model', or 'ontology'
    in isolation would create obvious false positives and must not appear
    as standalone match terms."""
    banned = {"graph", "knowledge", "data", "model", "models", "ontology"}
    for concept in vocab_graph.subjects(RDF.type, SKOS.Concept):
        for term in vocab_graph.objects(concept, KGJOBS.hasMatchTerm):
            text = str(vocab_graph.value(term, KGJOBS.termText)).strip().lower()
            assert text not in banned, f"isolated broad term found: {text!r}"
