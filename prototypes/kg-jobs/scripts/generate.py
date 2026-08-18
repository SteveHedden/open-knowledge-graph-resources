#!/usr/bin/env python3
"""Generate data/jobs.ttl and data/jobs.json from fixtures/jobs.json.

Network-free. Re-running against unchanged fixtures and vocabulary
must produce an isomorphic RDF graph and byte-identical JSON.

Usage:
    python scripts/generate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classifier import classify, find_evidence, load_match_terms, normalize  # noqa: E402
from entities import apply_confirmed_wikidata_matches, employer_uri  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VOCAB_PATH = ROOT / "vocabularies" / "kg-jobs.ttl"
FIXTURES_PATH = ROOT / "fixtures" / "jobs.json"
DATA_TTL_PATH = ROOT / "data" / "jobs.ttl"
DATA_JSON_PATH = ROOT / "data" / "jobs.json"

SCHEMA = Namespace("https://schema.org/")
KGJOBS = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/ontology#")
KGJD = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/data/")


def load_fixtures() -> list[dict]:
    with FIXTURES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def build_graph(records_with_results: list[tuple[dict, str, list]]) -> Graph:
    g = Graph()
    g.bind("schema", SCHEMA)
    g.bind("kgjobs", KGJOBS)
    g.bind("kgjd", KGJD)

    for record, classification, evidence in records_with_results:
        job = KGJD[f"job-{record['id']}"]
        g.add((job, RDF.type, SCHEMA.JobPosting))
        g.add((job, SCHEMA.identifier, Literal(record["id"])))
        g.add((job, SCHEMA.url, Literal(record["test_url"])))
        g.add((job, SCHEMA.title, Literal(normalize(record["title"]))))
        g.add((job, SCHEMA.description, Literal(normalize(record["description"]))))

        org = employer_uri(record["hiringOrganization"])
        if (org, RDF.type, SCHEMA.Organization) not in g:
            # First record seen for this employer slug sets the canonical
            # display name; later records reusing the same slug (e.g. minor
            # whitespace/case variants from different sources) just link to
            # the existing resource, keeping schema:name single-valued.
            g.add((org, RDF.type, SCHEMA.Organization))
            g.add((org, RDF.type, KGJOBS.Employer))
            g.add((org, SCHEMA.name, Literal(record["hiringOrganization"])))
        g.add((job, SCHEMA.hiringOrganization, org))

        if record.get("location"):
            place = BNode()
            g.add((place, RDF.type, SCHEMA.Place))
            g.add((place, SCHEMA.name, Literal(record["location"])))
            g.add((job, SCHEMA.jobLocation, place))
        if record.get("remote") is not None:
            g.add((job, SCHEMA.jobLocationType, Literal("TELECOMMUTE" if record["remote"] else "ON_SITE")))
        if record.get("datePosted"):
            g.add((job, SCHEMA.datePosted, Literal(record["datePosted"], datatype=XSD.date)))
        if record.get("qualifications"):
            g.add((job, SCHEMA.qualifications, Literal(normalize(record["qualifications"]))))
        if record.get("responsibilities"):
            g.add((job, SCHEMA.responsibilities, Literal(normalize(record["responsibilities"]))))

        g.add((job, KGJOBS.classification, Literal(classification)))

        for ev in evidence:
            node = BNode()
            g.add((node, RDF.type, KGJOBS.Evidence))
            g.add((node, KGJOBS.matchedConcept, URIRef(ev.concept_uri)))
            g.add((node, KGJOBS.conceptLabel, Literal(ev.concept_label)))
            g.add((node, KGJOBS.conceptScheme, Literal(ev.concept_scheme)))
            g.add((node, KGJOBS.matchStrength, Literal(ev.strength)))
            g.add((node, KGJOBS.matchedPhrase, Literal(ev.matched_phrase)))
            g.add((node, KGJOBS.sourceField, Literal(ev.source_field)))
            g.add((node, KGJOBS.negated, Literal(ev.negated, datatype=XSD.boolean)))
            g.add((job, KGJOBS.hasEvidence, node))

    apply_confirmed_wikidata_matches(g)
    return g


def build_json(records_with_results: list[tuple[dict, str, list]]) -> list[dict]:
    output = []
    for record, classification, evidence in records_with_results:
        entry = {
            "id": record["id"],
            "test_url": record["test_url"],
            "title": normalize(record["title"]),
            "description": normalize(record["description"]),
            "hiringOrganization": record["hiringOrganization"],
            "classification": classification,
        }
        if record.get("location"):
            entry["location"] = record["location"]
        if record.get("remote") is not None:
            entry["remote"] = record["remote"]
        if record.get("datePosted"):
            entry["datePosted"] = record["datePosted"]
        if record.get("qualifications"):
            entry["qualifications"] = normalize(record["qualifications"])
        if record.get("responsibilities"):
            entry["responsibilities"] = normalize(record["responsibilities"])
        entry["evidence"] = [
            {
                "concept_uri": ev.concept_uri,
                "concept_label": ev.concept_label,
                "concept_scheme": ev.concept_scheme,
                "strength": ev.strength,
                "matched_phrase": ev.matched_phrase,
                "source_field": ev.source_field,
                "negated": ev.negated,
            }
            for ev in evidence
        ]
        output.append(entry)
    return output


def run() -> list[tuple[dict, str, list]]:
    terms = load_match_terms(VOCAB_PATH)
    fixtures = load_fixtures()
    results = []
    for record in fixtures:
        evidence = find_evidence(record, terms)
        classification = classify(evidence)
        results.append((record, classification, evidence))
    return results


def main() -> None:
    results = run()

    DATA_TTL_PATH.parent.mkdir(parents=True, exist_ok=True)
    graph = build_graph(results)
    graph.serialize(destination=str(DATA_TTL_PATH), format="turtle")

    json_data = build_json(results)
    with DATA_JSON_PATH.open("w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    counts = {"qualified": 0, "review": 0, "not_match": 0}
    for _, classification, _ in results:
        counts[classification] += 1
    print(f"Generated {len(results)} records -> {DATA_TTL_PATH} / {DATA_JSON_PATH}")
    print(f"  qualified={counts['qualified']} review={counts['review']} not_match={counts['not_match']}")


if __name__ == "__main__":
    main()
