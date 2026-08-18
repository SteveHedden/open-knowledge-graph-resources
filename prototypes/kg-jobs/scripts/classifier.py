"""Deterministic, network-free classifier for the kg-jobs prototype.

Loads match terms and strengths from vocabularies/kg-jobs.ttl -- the
vocabulary is never duplicated as source-code constants -- and
classifies job-posting records as qualified / review / not_match with
field-level, explainable evidence.

Rules:
  qualified  -- at least one unnegated "strong" concept match.
  review     -- no strong match, but at least two distinct unnegated
                "contextual" concept matches.
  not_match  -- otherwise.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from rdflib import RDF, Graph, Namespace

KGJOBS = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/ontology#")
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")

FIELD_ORDER = ("title", "description", "qualifications", "responsibilities")

# Negation cue words checked in the clause immediately preceding a match.
NEGATION_CUES = (
    "no", "not", "without", "excluding", "except", "never",
    "n't", "isn't", "aren't", "don't", "doesn't", "won't", "wasn't",
)

_SENTENCE_BOUNDARY = re.compile(r"[.!?;\n]")
_NEGATION_WINDOW_CHARS = 60


@dataclass(frozen=True)
class MatchTerm:
    text: str
    case_sensitive: bool
    concept_uri: str
    concept_label: str
    concept_scheme: str
    strength: str  # "strong" or "contextual"


@dataclass(frozen=True)
class Evidence:
    concept_uri: str
    concept_label: str
    concept_scheme: str
    strength: str
    matched_phrase: str
    source_field: str
    negated: bool


def normalize(text: str | None) -> str:
    """Deterministic text normalization: Unicode NFC, collapsed whitespace."""
    text = unicodedata.normalize("NFC", text or "")
    return re.sub(r"\s+", " ", text).strip()


def load_match_terms(vocab_path: Path) -> list[MatchTerm]:
    graph = Graph()
    graph.parse(vocab_path, format="turtle")

    scheme_labels: dict[str, str] = {}
    for scheme in graph.subjects(RDF.type, SKOS.ConceptScheme):
        label = graph.value(scheme, SKOS.prefLabel)
        scheme_labels[str(scheme)] = str(label) if label is not None else str(scheme)

    terms: list[MatchTerm] = []
    for concept in graph.subjects(RDF.type, SKOS.Concept):
        pref_label = graph.value(concept, SKOS.prefLabel)
        strength = graph.value(concept, KGJOBS.matchStrength)
        scheme = graph.value(concept, SKOS.inScheme)
        if pref_label is None or strength is None or scheme is None:
            continue
        scheme_label = scheme_labels.get(str(scheme), str(scheme))
        for term_node in graph.objects(concept, KGJOBS.hasMatchTerm):
            term_text = graph.value(term_node, KGJOBS.termText)
            case_sensitive = graph.value(term_node, KGJOBS.caseSensitive)
            if term_text is None or case_sensitive is None:
                continue
            terms.append(
                MatchTerm(
                    text=str(term_text),
                    case_sensitive=bool(case_sensitive),
                    concept_uri=str(concept),
                    concept_label=str(pref_label),
                    concept_scheme=scheme_label,
                    strength=str(strength),
                )
            )

    # Sort by (concept URI, term text) so match order never depends on
    # rdflib's internal graph iteration order -- required for
    # deterministic evidence output.
    terms.sort(key=lambda t: (t.concept_uri, t.text))
    return terms


def _term_pattern(term: MatchTerm) -> re.Pattern:
    flags = 0 if term.case_sensitive else re.IGNORECASE
    return re.compile(r"\b" + re.escape(term.text) + r"\b", flags)


def _is_negated(text: str, match_start: int) -> bool:
    window_start = max(0, match_start - _NEGATION_WINDOW_CHARS)
    window = text[window_start:match_start]
    boundaries = [m.end() for m in _SENTENCE_BOUNDARY.finditer(window)]
    clause_start = boundaries[-1] if boundaries else 0
    clause = window[clause_start:].lower()
    return any(re.search(r"\b" + re.escape(cue) + r"\b", clause) for cue in NEGATION_CUES)


def find_evidence(record: dict, terms: Iterable[MatchTerm]) -> list[Evidence]:
    evidence: list[Evidence] = []
    terms = list(terms)
    for field in FIELD_ORDER:
        raw = record.get(field)
        if not raw:
            continue
        text = normalize(raw)
        for term in terms:
            pattern = _term_pattern(term)
            for m in pattern.finditer(text):
                evidence.append(
                    Evidence(
                        concept_uri=term.concept_uri,
                        concept_label=term.concept_label,
                        concept_scheme=term.concept_scheme,
                        strength=term.strength,
                        matched_phrase=text[m.start():m.end()],
                        source_field=field,
                        negated=_is_negated(text, m.start()),
                    )
                )
    evidence.sort(key=lambda e: (FIELD_ORDER.index(e.source_field), e.concept_uri, e.matched_phrase))
    return evidence


def classify(evidence: list[Evidence]) -> str:
    positive = [e for e in evidence if not e.negated]
    strong_concepts = {e.concept_uri for e in positive if e.strength == "strong"}
    if strong_concepts:
        return "qualified"
    contextual_concepts = {e.concept_uri for e in positive if e.strength == "contextual"}
    if len(contextual_concepts) >= 2:
        return "review"
    return "not_match"


def matched_concept_names(evidence: list[Evidence], vocab_base: str) -> list[str]:
    """Distinct unnegated concept local names, for comparing against fixture expectations."""
    names = {
        e.concept_uri[len(vocab_base):]
        for e in evidence
        if not e.negated and e.concept_uri.startswith(vocab_base)
    }
    return sorted(names)
