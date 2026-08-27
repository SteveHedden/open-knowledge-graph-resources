"""Stable RDF identity for real-world entities referenced by job postings.

Currently covers employers only. Each distinct employer name gets one
deterministic, reusable URI instead of a fresh blank node per job posting,
so the same company appearing across many postings is a single resource --
one place to later attach an owl:sameAs link to its Wikidata item, once a
human has verified the match. No Wikidata mapping happens automatically
anywhere in this module or the jobs service: QIDs are never guessed.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from rdflib import RDF, Graph, Namespace, OWL, URIRef

KGJD = Namespace("https://openknowledgegraphs.com/jobs/data/")
KGJOBS = Namespace("https://openknowledgegraphs.com/jobs/ontology#")

EMPLOYERS_PATH = Path(__file__).resolve().parent.parent / "employers.ttl"


def employer_slug(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug or "unknown"


def employer_uri(name: str) -> URIRef:
    return KGJD[f"employer-{employer_slug(name)}"]


def apply_confirmed_wikidata_matches(graph: Graph, path: Path = EMPLOYERS_PATH) -> int:
    """Merge human-confirmed owl:sameAs links from employers.ttl into graph.

    Only applies a match for an employer URI that graph already asserts as
    kgjobs:Employer (i.e. an employer actually present in this run's data) --
    employers.ttl may accumulate confirmed matches for companies that don't
    appear in every run, and those are silently skipped rather than injected
    as untyped, unreferenced triples. Returns the number of matches applied.
    """
    if not path.exists():
        return 0
    registry = Graph()
    registry.parse(path, format="turtle")
    present_employers = set(graph.subjects(RDF.type, KGJOBS.Employer))
    applied = 0
    for subject, _, wikidata_item in registry.triples((None, OWL.sameAs, None)):
        if subject in present_employers:
            graph.add((subject, OWL.sameAs, wikidata_item))
            applied += 1
    return applied
