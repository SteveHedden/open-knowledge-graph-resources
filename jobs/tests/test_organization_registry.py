"""Network-free contracts for the authoritative root organization registry."""

import json
import sys
from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Namespace
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))

from organization_registry import build_projection  # noqa: E402

SCHEMA = Namespace("https://schema.org/")
OKG = Namespace("https://openknowledgegraphs.com/ontology#")
KGJOBS = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
ORG_BASE = "https://openknowledgegraphs.com/organization/"
ORGTYPE_BASE = "https://openknowledgegraphs.com/organization-type/"


def registry_graph() -> Graph:
    return Graph().parse(REPO_ROOT / "organizations.ttl", format="turtle")


def test_root_registry_defines_exactly_six_kind_and_nine_role_classes():
    graph = registry_graph()
    classes = {
        subject: str(graph.value(subject, OKG.classificationDimension))
        for subject in graph.subjects(RDF.type, OWL.Class)
        if str(subject).startswith(ORGTYPE_BASE)
    }
    assert sum(value == "kind" for value in classes.values()) == 6
    assert sum(value == "role" for value in classes.values()) == 9
    assert set(classes.values()) == {"kind", "role"}
    for subject in classes:
        assert (subject, RDFS.subClassOf, SCHEMA.Organization) in graph
        assert list(graph.objects(subject, RDFS.label))


def test_registry_contains_only_139_accepted_organizations_and_rejections_are_audit_only():
    graph = registry_graph()
    subjects = set(graph.subjects(RDF.type, OKG.Organization))
    assert len(subjects) == 139
    assert all(str(subject).startswith(ORG_BASE) for subject in subjects)
    audit = json.loads((ROOT / "audits" / "organization-registry-audit.json").read_text())
    assert audit["counts"]["accepted"] == 139
    assert audit["counts"]["rejected"] == 3
    assert audit["counts"]["unresolved"] == 0
    rejected_iris = {row.get("iri") for row in audit["rejected"] if row.get("iri")}
    assert not (rejected_iris & {str(subject) for subject in subjects})


def test_each_accepted_organization_has_permanent_identity_evidence_types_and_one_jobs_flag():
    graph = registry_graph()
    dimensions = {
        subject: str(graph.value(subject, OKG.classificationDimension))
        for subject in graph.subjects(RDF.type, OWL.Class)
        if str(subject).startswith(ORGTYPE_BASE)
    }
    enabled = 0
    for subject in graph.subjects(RDF.type, OKG.Organization):
        identifier = list(graph.objects(subject, DCTERMS.identifier))
        flags = list(graph.objects(subject, OKG.jobsProductionEnabled))
        types = set(graph.objects(subject, RDF.type))
        assert len(identifier) == 1
        assert str(subject) == f"{ORG_BASE}{identifier[0]}/"
        assert len(flags) == 1 and isinstance(flags[0].toPython(), bool)
        enabled += int(flags[0].toPython())
        assert (subject, RDF.type, SCHEMA.Organization) in graph
        assert {dimensions[value] for value in types if value in dimensions} >= {"kind", "role"}
        assert list(graph.objects(subject, KGJOBS.inclusionEvidence))
        assert list(map(str, graph.objects(subject, KGJOBS.reviewStatus))) == ["evidence-reviewed"]
        assert not list(graph.objects(subject, KGJOBS.careersPage))
        assert not list(graph.objects(subject, KGJOBS.productionApproved))
        assert not list(graph.objects(subject, KGJOBS.pilotSelected))
    assert enabled == 12


def test_json_is_only_a_deterministic_projection_of_the_root_turtle():
    expected = build_projection()
    committed = json.loads((REPO_ROOT / "data" / "organizations.json").read_text())
    assert committed == expected
    assert committed["schemaVersion"] == 2
    assert committed["counts"] == {
        "accepted": 139,
        "jobsProductionEnabled": 12,
        "rejectedAuditOnly": 3,
        "unresolvedAuditOnly": 0,
    }
    assert all("careersPage" not in row for row in committed["organizations"])
    assert all("productionApproved" not in row for row in committed["organizations"])


def test_producer_relationships_survive_the_derived_projection():
    payload = build_projection()
    relationships = [
        relationship
        for row in payload["organizations"]
        for relationship in row["catalogRelationships"]
    ]
    # Three relationships belong to the three rejected audit-only candidates;
    # the authoritative accepted graph therefore projects the remaining 182.
    assert len(relationships) == 182
    assert {role for row in relationships for role in row["producerRoles"]} >= {
        "author", "creator", "developer"
    }


def test_combined_authoritative_registries_conform_to_jobs_shapes():
    data = Graph()
    for path in (
        REPO_ROOT / "ontology.ttl",
        REPO_ROOT / "organizations.ttl",
        REPO_ROOT / "sources.ttl",
        ROOT / "vocabularies" / "kg-jobs.ttl",
    ):
        data.parse(path, format="turtle")
    shapes = Graph().parse(ROOT / "ontology.ttl", format="turtle")
    conforms, _, report = validate(
        data, shacl_graph=shapes, ont_graph=shapes, inference="none"
    )
    assert conforms, report
