"""Network-free contract tests for the reviewed organization registry."""

import json
import sys
from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Namespace
from rdflib.namespace import RDF, SKOS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from organization_registry import (  # noqa: E402
    KIND_LABELS, ROLE_LABELS, _reserve_uri, _type_verdict, build_registry,
)

ORGV = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/organization-vocabulary/")
SCHEMA = Namespace("https://schema.org/")


def test_taxonomy_contains_only_the_reviewed_kinds_and_roles():
    graph = Graph().parse(ROOT / "vocabularies" / "organizations.ttl", format="turtle")
    schemes = set(graph.subjects(RDF.type, SKOS.ConceptScheme))
    assert schemes == {ORGV.kinds, ORGV.roles}
    assert set(map(str, graph.subjects(SKOS.inScheme, ORGV.kinds))) == set(KIND_LABELS)
    assert set(map(str, graph.subjects(SKOS.inScheme, ORGV.roles))) == set(ROLE_LABELS)
    for concept in graph.subjects(RDF.type, SKOS.Concept):
        assert list(graph.objects(concept, SKOS.prefLabel))
        assert list(graph.objects(concept, SKOS.definition))


def test_current_snapshot_audits_every_dynamic_catalog_candidate_and_relationship():
    snapshot = json.loads((ROOT / "audits" / "organization-source-snapshot.json").read_text())
    audit = json.loads((ROOT / "audits" / "organization-registry-audit.json").read_text())
    organizations = json.loads((ROOT / "data" / "organizations.json").read_text())
    counts = snapshot["catalogCounts"]
    assert counts == {
        "apparentOrganizationCandidates": 129,
        "catalogRecords": 3324,
        "catalogRelationships": 185,
        "linkedCatalogRecords": 157,
    }
    assert len(snapshot["candidates"]) == counts["apparentOrganizationCandidates"]
    assert sum(len(row["catalogRelationships"]) for row in snapshot["candidates"]) == counts["catalogRelationships"]
    catalog_outcomes = (
        audit["counts"]["catalogAccepted"]
        + audit["counts"]["catalogRejected"]
        + audit["counts"]["catalogUnresolved"]
    )
    assert catalog_outcomes == counts["apparentOrganizationCandidates"]
    assert (
        audit["counts"]["accepted"]
        + audit["counts"]["rejected"]
        + audit["counts"]["unresolved"]
    ) == len(organizations["organizations"])
    assert len(audit["missingRelationships"]) == audit["counts"]["missingRelationship"]


def test_every_accepted_organization_has_stable_identity_evidence_taxonomy_and_provenance():
    payload, audit, graph, _ = build_registry(write=False)
    accepted = [row for row in payload["organizations"] if row["reviewStatus"] == "evidence-reviewed"]
    assert len(accepted) == audit["counts"]["accepted"] == 139
    assert sum(row["pilotSelected"] for row in accepted) == 12
    assert 10 <= sum(row["supplemental"] for row in accepted) <= 20
    assert len({kind["label"] for row in accepted if row["pilotSelected"] for kind in row["organizationKinds"]}) >= 3
    approved = {row["iri"] for row in accepted if row["productionApproved"]}
    selected = {row["iri"] for row in accepted if row["pilotSelected"]}
    assert approved == selected
    assert len(approved) == 12
    for row in accepted:
        assert row["iri"].startswith("https://openknowledgegraphs.com/organization/")
        assert row["iri"].endswith("/")
        assert row["organizationKinds"] and row["ecosystemRoles"] and row["evidence"]
        assert all(item["url"].startswith("https://") for item in row["evidence"])
        assert all(item["reviewedOn"] == row["lastVerified"] for item in row["evidence"])
        assert row["productionApproved"] is row["pilotSelected"]
    projected = {str(subject) for subject in graph.subjects(RDF.type, SCHEMA.Organization)}
    assert projected == {row["iri"] for row in accepted}
    shapes = Graph().parse(ROOT / "ontology.ttl", format="turtle")
    data = Graph()
    for triple in graph:
        data.add(triple)
    data.parse(ROOT / "vocabularies" / "organizations.ttl", format="turtle")
    conforms, _, report = validate(data, shacl_graph=shapes, ont_graph=shapes, inference="none")
    assert conforms, report


def test_uri_reservations_are_immutable_across_renames():
    registry = {"schemaVersion": 1, "organizations": {
        "wikidata:Q1": {"slug": "original-name", "reservedName": "Original Name"}
    }}
    assert _reserve_uri(registry, "wikidata:Q1", "Completely Renamed") == (
        "https://openknowledgegraphs.com/organization/original-name/"
    )
    assert registry["organizations"]["wikidata:Q1"]["reservedName"] == "Original Name"


def test_clear_people_papers_and_unlabeled_values_are_not_accepted_as_organizations():
    person = _type_verdict(["human"], "Example Person")
    paper = _type_verdict(["scholarly article"], "Example Paper")
    unlabeled = _type_verdict([], "Q999999999")
    assert person[0] == "rejected"
    assert paper[0] == "rejected"
    assert unlabeled[0] == "unresolved"


def test_producer_relationship_roles_and_source_properties_survive_projection():
    payload = json.loads((ROOT / "data" / "organizations.json").read_text())
    relationships = [
        rel for row in payload["organizations"] for rel in row.get("catalogRelationships", [])
    ]
    assert len(relationships) == 185
    assert {role for rel in relationships for role in rel.get("producerRoles", [])} >= {
        "author", "creator", "developer"
    }
    assert {prop for rel in relationships for prop in rel.get("sourceProperties", [])} >= {
        "P50", "P170", "P178"
    }


def test_registry_rdf_and_json_builds_are_byte_deterministic():
    first_payload, first_audit, first_graph, first_registry = build_registry(write=False)
    second_payload, second_audit, second_graph, second_registry = build_registry(write=False)
    assert first_payload == second_payload
    assert first_audit == second_audit
    assert first_registry == second_registry
    assert first_graph.serialize(format="turtle") == second_graph.serialize(format="turtle")
