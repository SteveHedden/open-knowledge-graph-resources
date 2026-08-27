#!/usr/bin/env python3
"""Validate the authoritative organization registry and derive its JSON projection."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import DCTERMS, OWL, PROV, RDF, RDFS, SKOS, XSD

JOBS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = JOBS_ROOT.parent
ORGANIZATIONS_PATH = REPO_ROOT / "organizations.ttl"
JSON_PATH = REPO_ROOT / "data" / "organizations.json"
AUDIT_PATH = JOBS_ROOT / "audits" / "organization-registry-audit.json"

SCHEMA = Namespace("https://schema.org/")
OKG = Namespace("https://openknowledgegraphs.com/ontology#")
KGJOBS = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
ORGTYPE_BASE = "https://openknowledgegraphs.com/organization-type/"
ORG_BASE = "https://openknowledgegraphs.com/organization/"


class OrganizationRegistryError(RuntimeError):
    """The authoritative organization registry violates its contract."""


def _one(graph: Graph, subject, predicate, label: str, *, required: bool = True):
    values = list(graph.objects(subject, predicate))
    if len(values) > 1 or (required and len(values) != 1):
        raise OrganizationRegistryError(
            f"{subject} requires exactly one {label}; found {len(values)}"
        )
    return values[0] if values else None


def _boolean(value, label: str) -> bool:
    if not isinstance(value, Literal) or value.datatype != XSD.boolean:
        raise OrganizationRegistryError(f"{label} must be an xsd:boolean")
    parsed = value.toPython()
    if not isinstance(parsed, bool):
        raise OrganizationRegistryError(f"{label} must be an xsd:boolean")
    return parsed


def _qid(graph: Graph, subject) -> str | None:
    identity = _one(graph, subject, OWL.sameAs, "external identity", required=False)
    if identity is None:
        return None
    match = re.fullmatch(r"http://www\.wikidata\.org/entity/(Q\d+)", str(identity))
    if not match:
        raise OrganizationRegistryError(f"{subject} has an invalid external identity")
    return match.group(1)


def _class_index(graph: Graph) -> tuple[dict, dict]:
    kinds = {}
    roles = {}
    for subject in graph.subjects(RDF.type, OWL.Class):
        if not str(subject).startswith(ORGTYPE_BASE):
            continue
        if (subject, RDFS.subClassOf, SCHEMA.Organization) not in graph:
            raise OrganizationRegistryError(f"organization class {subject} is not a schema:Organization subclass")
        label = str(_one(graph, subject, RDFS.label, "class label"))
        dimension = str(_one(graph, subject, OKG.classificationDimension, "classification dimension"))
        if dimension == "kind":
            kinds[str(subject)] = label
        elif dimension == "role":
            roles[str(subject)] = label
        else:
            raise OrganizationRegistryError(f"organization class {subject} has an invalid dimension")
    if len(kinds) != 6 or len(roles) != 9:
        raise OrganizationRegistryError(
            f"expected 6 organization kinds and 9 ecosystem roles; found {len(kinds)} and {len(roles)}"
        )
    return kinds, roles


def build_projection(
    organizations_path: Path = ORGANIZATIONS_PATH,
    audit_path: Path = AUDIT_PATH,
) -> dict:
    graph = Graph().parse(organizations_path, format="turtle")
    kinds, roles = _class_index(graph)
    organization_subjects = sorted(
        set(graph.subjects(RDF.type, OKG.Organization)), key=str
    )
    if len(organization_subjects) != 139:
        raise OrganizationRegistryError(
            f"expected 139 accepted organizations, found {len(organization_subjects)}"
        )

    relationships = defaultdict(list)
    for relationship in graph.subjects(RDF.type, KGJOBS.ProducerRelationship):
        organization = _one(
            graph, relationship, KGJOBS.producerOrganization, "producer organization"
        )
        resource = _one(graph, relationship, KGJOBS.catalogResource, "catalog resource")
        relationships[str(organization)].append({
            "resourceUrl": str(resource),
            "producerRoles": sorted(str(value) for value in graph.objects(relationship, KGJOBS.producerRole)),
            "sourceProperties": sorted(str(value) for value in graph.objects(relationship, KGJOBS.sourceProperty)),
            "evidenceUrl": str(_one(graph, relationship, DCTERMS.source, "relationship evidence")),
        })

    records = []
    enabled_count = 0
    for subject in organization_subjects:
        iri = str(subject)
        if not iri.startswith(ORG_BASE) or not iri.endswith("/"):
            raise OrganizationRegistryError(f"invalid organization IRI: {iri}")
        identifier = str(_one(graph, subject, DCTERMS.identifier, "identifier"))
        if iri != f"{ORG_BASE}{identifier}/":
            raise OrganizationRegistryError(f"{subject} identifier does not match its permanent IRI")
        if (subject, RDF.type, SCHEMA.Organization) not in graph:
            raise OrganizationRegistryError(f"{subject} is not a schema:Organization")
        review_status = str(_one(graph, subject, KGJOBS.reviewStatus, "review status"))
        if review_status != "evidence-reviewed":
            raise OrganizationRegistryError(f"accepted organization {subject} is not evidence-reviewed")
        active = _boolean(_one(graph, subject, KGJOBS.active, "active flag"), f"{subject} active flag")
        enabled = _boolean(
            _one(graph, subject, OKG.jobsProductionEnabled, "jobs production flag"),
            f"{subject} jobs production flag",
        )
        if enabled and not active:
            raise OrganizationRegistryError(f"inactive organization {subject} cannot enable production jobs")
        enabled_count += int(enabled)
        type_iris = {str(value) for value in graph.objects(subject, RDF.type)}
        organization_kinds = sorted(type_iris & set(kinds))
        ecosystem_roles = sorted(type_iris & set(roles))
        if not organization_kinds or not ecosystem_roles:
            raise OrganizationRegistryError(f"{subject} requires at least one kind and ecosystem role type")

        evidence = []
        for node in sorted(graph.objects(subject, KGJOBS.inclusionEvidence), key=str):
            if (node, RDF.type, KGJOBS.InclusionEvidence) not in graph:
                raise OrganizationRegistryError(f"{subject} has untyped inclusion evidence")
            evidence.append({
                "url": str(_one(graph, node, DCTERMS.source, "evidence source")),
                "note": str(_one(graph, node, DCTERMS.description, "evidence description")),
                "reviewedOn": str(_one(graph, node, DCTERMS.date, "evidence date")),
            })
        if not evidence:
            raise OrganizationRegistryError(f"{subject} requires cited inclusion evidence")

        homepage = _one(graph, subject, SCHEMA.url, "official homepage", required=False)
        description = _one(graph, subject, DCTERMS.description, "description", required=False)
        review_reason = _one(graph, subject, KGJOBS.reviewReason, "review reason", required=False)
        qid = _qid(graph, subject)
        records.append({
            "active": active,
            "aliases": sorted(str(value) for value in graph.objects(subject, SKOS.altLabel)),
            "catalogRelationships": sorted(
                relationships.get(iri, []),
                key=lambda row: (row["resourceUrl"], row["producerRoles"], row["sourceProperties"]),
            ),
            "description": str(description) if description is not None else None,
            "ecosystemRoles": [{"uri": uri, "label": roles[uri]} for uri in ecosystem_roles],
            "evidence": evidence,
            "identifier": identifier,
            "iri": iri,
            "jobsProductionEnabled": enabled,
            "lastVerified": str(_one(graph, subject, KGJOBS.lastVerified, "last verified date")),
            "name": str(_one(graph, subject, SCHEMA.name, "name")),
            "officialWebsite": str(homepage) if homepage is not None else None,
            "organizationKinds": [{"uri": uri, "label": kinds[uri]} for uri in organization_kinds],
            "qid": qid,
            "reviewReason": str(review_reason) if review_reason is not None else None,
            "reviewStatus": review_status,
        })
    if enabled_count != 12:
        raise OrganizationRegistryError(
            f"expected 12 jobs-enabled organizations, found {enabled_count}"
        )

    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrganizationRegistryError("organization audit is missing or invalid") from exc
    counts = audit.get("counts", {})
    rejected = int(counts.get("rejected", counts.get("catalogRejected", 0)))
    unresolved = int(counts.get("unresolved", counts.get("catalogUnresolved", 0)))
    if rejected != 3 or unresolved != 0:
        raise OrganizationRegistryError(
            f"expected audit-only outcomes rejected=3 unresolved=0; found {rejected} and {unresolved}"
        )
    last_verified = max(record["lastVerified"] for record in records)
    return {
        "schemaVersion": 2,
        "generatedAt": f"{last_verified}T00:00:00Z",
        "lastVerified": last_verified,
        "counts": {
            "accepted": len(records),
            "jobsProductionEnabled": enabled_count,
            "rejectedAuditOnly": rejected,
            "unresolvedAuditOnly": unresolved,
        },
        "organizations": sorted(records, key=lambda row: (row["name"].casefold(), row["iri"])),
    }


def write_projection(payload: dict, path: Path = JSON_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate organizations.ttl and regenerate data/organizations.json."
    )
    parser.add_argument("--check", action="store_true", help="validate without writing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = build_projection()
        if not args.check:
            write_projection(payload)
    except OrganizationRegistryError as exc:
        print(f"Organization registry failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Organization registry valid: "
        f"{payload['counts']['accepted']} accepted, "
        f"{payload['counts']['jobsProductionEnabled']} jobs-enabled"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
