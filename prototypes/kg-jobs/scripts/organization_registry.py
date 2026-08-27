#!/usr/bin/env python3
"""Build the reviewed KG ecosystem organization registry.

The checked-in source snapshot is the network boundary. Normal generation and
all tests are deterministic and network-free. ``--refresh-source-snapshot`` is
the only mode that contacts Wikidata, and it records the exact claims used by
the audit before rebuilding the projections.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import requests
from pyshacl import validate
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCAT, DCTERMS, OWL, PROV, RDF, RDFS, SKOS, XSD

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parents[1]
SNAPSHOT_PATH = ROOT / "audits" / "organization-source-snapshot.json"
AUDIT_PATH = ROOT / "audits" / "organization-registry-audit.json"
CURATION_PATH = ROOT / "curation" / "organizations.ttl"
URI_REGISTRY_PATH = ROOT / "data" / "organization_uri_registry.json"
JSON_PATH = ROOT / "data" / "organizations.json"
RDF_PATH = ROOT / "data" / "organizations.ttl"

SCHEMA = Namespace("https://schema.org/")
KGJOBS = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/ontology#")
ORGV = Namespace("https://openknowledgegraphs.com/prototypes/kg-jobs/organization-vocabulary/")
ORG_BASE = "https://openknowledgegraphs.com/organization/"
WIKIDATA_ENTITY = "http://www.wikidata.org/entity/"
WIKIDATA_PAGE = "https://www.wikidata.org/wiki/"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
REVIEW_STATUSES = {"evidence-reviewed", "unresolved", "rejected"}

KIND_LABELS = {
    str(ORGV["kind-commercial-enterprise"]): "Commercial enterprise",
    str(ORGV["kind-nonprofit-foundation"]): "Nonprofit or foundation",
    str(ORGV["kind-public-intergovernmental"]): "Public-sector or intergovernmental body",
    str(ORGV["kind-academic-research"]): "Academic or research organization",
    str(ORGV["kind-consortium-association"]): "Consortium or membership association",
    str(ORGV["kind-informal-open-source-community"]): "Informal or open-source community",
}
ROLE_LABELS = {
    str(ORGV["role-graph-platform-provider"]): "Graph database or KG platform provider",
    str(ORGV["role-semantic-tooling-provider"]): "Semantic-modeling or tooling provider",
    str(ORGV["role-kg-consultancy-integrator"]): "KG consultancy or systems integrator",
    str(ORGV["role-standards-steward"]): "Standards or specification steward",
    str(ORGV["role-vocabulary-maintainer"]): "Ontology or vocabulary maintainer or publisher",
    str(ORGV["role-kg-dataset-operator"]): "Knowledge-graph or dataset publisher or operator",
    str(ORGV["role-kg-research-education"]): "KG research or education organization",
    str(ORGV["role-open-source-steward"]): "Open-source KG software steward",
    str(ORGV["role-ecosystem-convener"]): "Ecosystem convener or professional community",
}
PRODUCER_ROLE_BY_PROPERTY = {
    "P170": "creator",
    "P50": "author",
    "P178": "developer",
    "P123": "publisher",
    "P126": "maintainer",
    "P137": "operator",
    "P127": "owner",
}


class OrganizationRegistryError(RuntimeError):
    """The registry cannot be generated without violating its contract."""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrganizationRegistryError(f"cannot read valid JSON from {path}") from exc


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _qid(value: str | None) -> str | None:
    match = re.search(r"Q\d+$", str(value or ""))
    return match.group(0) if match else None


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-") or "organization"


def _identity_key(qid: str | None, curation_key: str | None, name: str) -> str:
    if qid:
        return f"wikidata:{qid}"
    if curation_key:
        return f"curation:{curation_key}"
    return f"name:{unicodedata.normalize('NFKC', name).strip().casefold()}"


def _literal(graph: Graph, subject, predicate, *, required: bool = False) -> str | None:
    values = list(graph.objects(subject, predicate))
    if required and len(values) != 1:
        raise OrganizationRegistryError(
            f"{subject} requires exactly one {predicate}; found {len(values)}"
        )
    if not values:
        return None
    if len(values) > 1:
        raise OrganizationRegistryError(f"{subject} has multiple values for {predicate}")
    return str(values[0])


def _bool_literal(graph: Graph, subject, predicate, default: bool = False) -> bool:
    value = _literal(graph, subject, predicate)
    if value is None:
        return default
    if value not in {"true", "false"}:
        raise OrganizationRegistryError(f"{subject} {predicate} must be boolean")
    return value == "true"


def catalog_candidates(repo_root: Path = REPO_ROOT) -> tuple[dict, list[dict]]:
    """Extract every apparent organization link from the committed catalogs."""
    candidates: dict[str, dict] = {}
    generated_values = []
    record_count = 0
    linked_record_count = 0
    relationship_count = 0
    for catalog in ("ontologies", "software"):
        payload = _read_json(repo_root / "data" / f"{catalog}.json")
        generated_values.append(payload.get("generatedAt"))
        items = payload.get("items")
        if not isinstance(items, list):
            raise OrganizationRegistryError(f"data/{catalog}.json items must be an array")
        record_count += len(items)
        for item in items:
            org_creators = [
                creator for creator in item.get("creators", [])
                if creator.get("type") == "Organization"
            ]
            if org_creators:
                linked_record_count += 1
            for creator in org_creators:
                qid = _qid(creator.get("wikidataId"))
                if not qid:
                    # An apparent organization without a stable external key is
                    # retained as unresolved rather than silently discarded.
                    qid = "UNRESOLVED-" + hashlib.sha256(
                        creator.get("name", "").encode("utf-8")
                    ).hexdigest()[:12]
                candidate = candidates.setdefault(
                    qid,
                    {
                        "qid": qid if qid.startswith("Q") else None,
                        "name": creator.get("name") or qid,
                        "wikidataUrl": creator.get("wikidataId"),
                        "catalogRelationships": [],
                    },
                )
                candidate["catalogRelationships"].append(
                    {
                        "catalog": catalog,
                        "resourceTitle": item.get("title"),
                        "resourceUrl": item.get("canonicalUrl"),
                        "resourceWikidataId": _qid(item.get("wikidataId")),
                        "sourceProperties": [],
                    }
                )
                relationship_count += 1
    if len({value for value in generated_values if value}) > 1:
        raise OrganizationRegistryError("catalog inputs have different generation timestamps")
    generated_at = next((value for value in generated_values if value), None)
    counts = {
        "catalogRecords": record_count,
        "linkedCatalogRecords": linked_record_count,
        "apparentOrganizationCandidates": len(candidates),
        "catalogRelationships": relationship_count,
    }
    ordered = []
    for candidate in candidates.values():
        candidate["catalogRelationships"] = sorted(
            candidate["catalogRelationships"],
            key=lambda row: (
                row["catalog"], row.get("resourceUrl") or "", row.get("resourceTitle") or ""
            ),
        )
        ordered.append(candidate)
    return {"catalogGeneratedAt": generated_at, "counts": counts}, sorted(
        ordered, key=lambda row: (row["name"].casefold(), row.get("qid") or "")
    )


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _entity_claim_qids(entity: dict, prop: str) -> list[str]:
    output = []
    for claim in entity.get("claims", {}).get(prop, []):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, dict) and value.get("id"):
            output.append(value["id"])
    return sorted(set(output))


def _entity_urls(entity: dict, prop: str) -> list[str]:
    output = []
    for claim in entity.get("claims", {}).get(prop, []):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, str) and value.startswith("https://"):
            output.append(value)
    return sorted(set(output))


def _fetch_entities(session: requests.Session, qids: list[str]) -> dict[str, dict]:
    output: dict[str, dict] = {}
    for batch in _chunks(sorted(set(qids)), 50):
        response = session.get(
            WIKIDATA_API,
            params={
                "action": "wbgetentities",
                "format": "json",
                "ids": "|".join(batch),
                "props": "labels|descriptions|claims",
                "languages": "en",
            },
            timeout=30,
            headers={"User-Agent": "OKG-organization-audit/1.0 (https://openknowledgegraphs.com/)"},
        )
        response.raise_for_status()
        payload = response.json()
        output.update(payload.get("entities", {}))
    return output


def _type_verdict(type_labels: list[str], name: str) -> tuple[str, str]:
    text = " | ".join(type_labels).casefold()
    rejected = (
        "scholarly article", "academic article", "research paper", "software", "ontology",
        "controlled vocabulary", "dataset", "database", "website", "human",
    )
    organization = (
        "organization", "company", "business", "enterprise", "university", "institute",
        "library", "museum", "association", "foundation", "consortium", "agency",
        "authority", "government", "ministry", "commission", "committee", "centre",
        "center", "laboratory", "publisher", "community", "school", "archive",
        "corporation", "institution", "society", "public service", "administration",
        "working group", "research group", "department", "division", "district",
        "group of humans", "wikimedia chapter", "executive branch",
    )
    if any(token in text for token in organization):
        return "evidence-reviewed", f"Wikidata organization typing reviewed: {', '.join(type_labels)}"
    if any(token in text for token in rejected) or text == "chapter":
        return "rejected", f"Wikidata type is not an organization: {', '.join(type_labels)}"
    if not type_labels or name.startswith("Q"):
        return "unresolved", "Organization identity or type is unlabeled or missing"
    return "unresolved", f"Wikidata type requires manual organization review: {', '.join(type_labels)}"


def _suggest_kind(type_labels: list[str], name: str = "") -> str | None:
    text = " | ".join(type_labels).casefold()
    name_text = name.casefold()
    if any(value in text for value in ("community", "informal group", "group of humans")):
        return str(ORGV["kind-informal-open-source-community"])
    if any(value in text for value in ("consortium", "association", "standards organization", "committee", "working group", "learned society", "wikimedia chapter")):
        return str(ORGV["kind-consortium-association"])
    if any(value in text for value in ("university", "research", "laboratory", "school", "department", "division")):
        return str(ORGV["kind-academic-research"])
    if any(value in text for value in ("government", "public", "national library", "ministry", "commission", "agency", "intergovernmental", "executive branch", "administration", "district")):
        return str(ORGV["kind-public-intergovernmental"])
    if any(value in text for value in ("nonprofit", "non-profit", "foundation", "charitable")):
        return str(ORGV["kind-nonprofit-foundation"])
    if any(value in text for value in ("company", "business", "enterprise", "publisher", "corporation")):
        return str(ORGV["kind-commercial-enterprise"])
    if "library" in text or "museum" in text or "archive" in text:
        if "university" in name_text:
            return str(ORGV["kind-academic-research"])
        return str(ORGV["kind-public-intergovernmental"])
    if any(value in text for value in ("organization", "institution", "authority", "centre", "center")):
        return str(ORGV["kind-nonprofit-foundation"])
    return None


def refresh_source_snapshot(
    *, repo_root: Path = REPO_ROOT, destination: Path = SNAPSHOT_PATH,
    verified_on: str | None = None,
) -> dict:
    """Refresh Wikidata type/site/relationship claims for the current catalog."""
    metadata, candidates = catalog_candidates(repo_root)
    qids = [candidate["qid"] for candidate in candidates if candidate.get("qid")]
    resource_qids = sorted({
        rel["resourceWikidataId"] for candidate in candidates
        for rel in candidate["catalogRelationships"] if rel.get("resourceWikidataId")
    })
    session = requests.Session()
    candidate_entities = _fetch_entities(session, qids)
    resource_entities = _fetch_entities(session, resource_qids)
    type_qids = sorted({
        type_qid for entity in candidate_entities.values()
        for type_qid in _entity_claim_qids(entity, "P31")
    })
    type_entities = _fetch_entities(session, type_qids)
    type_labels = {
        qid: entity.get("labels", {}).get("en", {}).get("value", qid)
        for qid, entity in type_entities.items()
    }
    for candidate in candidates:
        qid = candidate.get("qid")
        entity = candidate_entities.get(qid or "", {})
        api_label = entity.get("labels", {}).get("en", {}).get("value")
        if api_label:
            candidate["name"] = api_label
        candidate["description"] = entity.get("descriptions", {}).get("en", {}).get("value")
        direct_types = _entity_claim_qids(entity, "P31")
        candidate["wikidataTypes"] = [
            {"qid": type_qid, "label": type_labels.get(type_qid, type_qid)}
            for type_qid in direct_types
        ]
        labels = [row["label"] for row in candidate["wikidataTypes"]]
        verdict, reason = _type_verdict(labels, candidate["name"])
        candidate["suggestedReviewStatus"] = verdict
        candidate["reviewReason"] = reason
        candidate["suggestedKind"] = _suggest_kind(labels, candidate["name"])
        candidate["officialWebsites"] = _entity_urls(entity, "P856")
        for relationship in candidate["catalogRelationships"]:
            resource = resource_entities.get(relationship.get("resourceWikidataId") or "", {})
            matched = []
            for prop in PRODUCER_ROLE_BY_PROPERTY:
                if qid in _entity_claim_qids(resource, prop):
                    matched.append(prop)
            relationship["sourceProperties"] = matched
            relationship["producerRoles"] = [PRODUCER_ROLE_BY_PROPERTY[prop] for prop in matched]
            relationship["evidenceUrl"] = (
                f"{WIKIDATA_PAGE}{relationship['resourceWikidataId']}"
                if relationship.get("resourceWikidataId") else relationship.get("resourceUrl")
            )
    snapshot = {
        "schemaVersion": 1,
        "generatedAt": metadata["catalogGeneratedAt"],
        "verifiedOn": verified_on or date.today().isoformat(),
        "catalogCounts": metadata["counts"],
        "candidates": candidates,
    }
    _write_json(destination, snapshot)
    return snapshot


def create_local_source_snapshot(
    *, repo_root: Path = REPO_ROOT, destination: Path = SNAPSHOT_PATH,
) -> dict:
    """Create an unresolved, network-free snapshot for bootstrap/testing."""
    metadata, candidates = catalog_candidates(repo_root)
    for candidate in candidates:
        candidate.update({
            "description": None,
            "wikidataTypes": [],
            "officialWebsites": [],
            "suggestedReviewStatus": "unresolved",
            "reviewReason": "Wikidata type evidence has not been refreshed",
            "suggestedKind": None,
        })
        for relationship in candidate["catalogRelationships"]:
            relationship["producerRoles"] = []
            relationship["evidenceUrl"] = relationship.get("resourceUrl")
    snapshot = {
        "schemaVersion": 1,
        "generatedAt": metadata["catalogGeneratedAt"],
        "verifiedOn": (metadata["catalogGeneratedAt"] or "")[:10],
        "catalogCounts": metadata["counts"],
        "candidates": candidates,
    }
    _write_json(destination, snapshot)
    return snapshot


def load_curation(path: Path = CURATION_PATH) -> list[dict]:
    graph = Graph().parse(path, format="turtle")
    organizations = []
    for subject in sorted(set(graph.subjects(RDF.type, SCHEMA.Organization)), key=str):
        name = _literal(graph, subject, SCHEMA.name, required=True)
        status = _literal(graph, subject, KGJOBS.reviewStatus, required=True)
        if status not in REVIEW_STATUSES:
            raise OrganizationRegistryError(f"{subject} has invalid review status {status!r}")
        qid_values = sorted({_qid(str(value)) for value in graph.objects(subject, OWL.sameAs)})
        qid_values = [value for value in qid_values if value]
        if len(qid_values) > 1:
            raise OrganizationRegistryError(f"{subject} has multiple Wikidata identities")
        kinds = sorted(str(value) for value in graph.objects(subject, KGJOBS.organizationKind))
        roles = sorted(str(value) for value in graph.objects(subject, KGJOBS.ecosystemRole))
        if status == "evidence-reviewed" and (not kinds or not roles):
            raise OrganizationRegistryError(f"accepted organization {subject} requires kind and role")
        if any(kind not in KIND_LABELS for kind in kinds):
            raise OrganizationRegistryError(f"{subject} uses an unknown organization kind")
        if any(role not in ROLE_LABELS for role in roles):
            raise OrganizationRegistryError(f"{subject} uses an unknown ecosystem role")
        evidence = []
        for node in graph.objects(subject, KGJOBS.inclusionEvidence):
            source = _literal(graph, node, DCTERMS.source, required=True)
            note = _literal(graph, node, DCTERMS.description, required=True)
            reviewed = _literal(graph, node, DCTERMS.date, required=True)
            if not str(source).startswith("https://"):
                raise OrganizationRegistryError(f"{subject} evidence must use HTTPS")
            evidence.append({"url": source, "note": note, "reviewedOn": reviewed})
        if status == "evidence-reviewed" and not evidence:
            raise OrganizationRegistryError(f"accepted organization {subject} requires evidence")
        organizations.append({
            "curationKey": str(subject).rstrip("/").rsplit("/", 1)[-1],
            "curationIri": str(subject),
            "name": name,
            "qid": qid_values[0] if qid_values else None,
            "aliases": sorted(str(value) for value in graph.objects(subject, SKOS.altLabel)),
            "description": _literal(graph, subject, DCTERMS.description),
            "officialWebsite": _literal(graph, subject, SCHEMA.url),
            "careersPage": _literal(graph, subject, KGJOBS.careersPage),
            "active": _bool_literal(graph, subject, KGJOBS.active, True),
            "reviewStatus": status,
            "reviewReason": _literal(graph, subject, KGJOBS.reviewReason) or "Curated review decision",
            "lastVerified": _literal(graph, subject, KGJOBS.lastVerified, required=True),
            "organizationKinds": kinds,
            "ecosystemRoles": roles,
            "evidence": sorted(evidence, key=lambda row: (row["url"], row["note"])),
            "pilotSelected": _bool_literal(graph, subject, KGJOBS.pilotSelected, False),
            "productionApproved": _bool_literal(graph, subject, KGJOBS.productionApproved, False),
        })
    return organizations


def _load_uri_registry(path: Path) -> dict:
    if not path.exists():
        return {"schemaVersion": 1, "organizations": {}}
    registry = _read_json(path)
    if registry.get("schemaVersion") != 1 or not isinstance(registry.get("organizations"), dict):
        raise OrganizationRegistryError("organization URI registry has an unsupported shape")
    slugs = [row.get("slug") for row in registry["organizations"].values()]
    if any(not slug for slug in slugs) or len(slugs) != len(set(slugs)):
        raise OrganizationRegistryError("organization URI registry has missing or duplicate slugs")
    return registry


def _reserve_uri(registry: dict, key: str, name: str) -> str:
    existing = registry["organizations"].get(key)
    if existing:
        return f"{ORG_BASE}{existing['slug']}/"
    base = _slug(name)
    used = {row["slug"] for row in registry["organizations"].values()}
    slug = base
    suffix = 2
    while slug in used:
        slug = f"{base}-{suffix}"
        suffix += 1
    registry["organizations"][key] = {"slug": slug, "reservedName": name}
    return f"{ORG_BASE}{slug}/"


def _catalog_role(relationships: list[dict]) -> list[str]:
    catalogs = {row.get("catalog") for row in relationships}
    producer_roles = {
        role for row in relationships for role in row.get("producerRoles", [])
    }
    roles = set()
    if "ontologies" in catalogs or producer_roles & {"creator", "author", "publisher", "maintainer"}:
        roles.add(str(ORGV["role-vocabulary-maintainer"]))
    if "software" in catalogs or "developer" in producer_roles:
        roles.add(str(ORGV["role-open-source-steward"]))
    return sorted(roles)


def _catalog_organization(candidate: dict, verified_on: str) -> dict:
    labels = [row.get("label", "") for row in candidate.get("wikidataTypes", [])]
    if labels:
        status, reason = _type_verdict(labels, candidate["name"])
        kind = _suggest_kind(labels, candidate["name"])
    else:
        status = candidate.get("suggestedReviewStatus") or "unresolved"
        reason = candidate.get("reviewReason") or "Catalog relationship review"
        kind = candidate.get("suggestedKind")
    roles = _catalog_role(candidate.get("catalogRelationships", []))
    if status == "evidence-reviewed" and (not kind or not roles):
        status = "unresolved"
        reason = "Organization identity is plausible but kind or ecosystem role is incomplete"
    evidence = []
    for relationship in candidate.get("catalogRelationships", []):
        if relationship.get("evidenceUrl"):
            properties = ", ".join(relationship.get("sourceProperties", [])) or "collapsed creator field"
            evidence.append({
                "url": relationship["evidenceUrl"],
                "note": (
                    f"{relationship.get('resourceTitle')} credits this candidate through {properties}."
                ),
                "reviewedOn": verified_on,
            })
    return {
        "name": candidate["name"],
        "qid": candidate.get("qid"),
        "aliases": [],
        "description": candidate.get("description"),
        "officialWebsite": (candidate.get("officialWebsites") or [None])[0],
        "careersPage": None,
        "active": status == "evidence-reviewed",
        "reviewStatus": status,
        "reviewReason": reason,
        "lastVerified": verified_on,
        "organizationKinds": [kind] if kind and status == "evidence-reviewed" else [],
        "ecosystemRoles": roles if status == "evidence-reviewed" else [],
        "evidence": sorted(evidence, key=lambda row: (row["url"], row["note"])),
        "catalogRelationships": candidate.get("catalogRelationships", []),
        "pilotSelected": False,
        "productionApproved": False,
        "supplemental": False,
    }


def _merge_curated(base: dict | None, curated: dict) -> dict:
    output = dict(base or {})
    for key, value in curated.items():
        if value is not None and key not in {"curationIri", "curationKey"}:
            output[key] = value
    output["supplemental"] = base is None
    output.setdefault("catalogRelationships", [])
    return output


def build_registry(
    *, snapshot_path: Path = SNAPSHOT_PATH, curation_path: Path = CURATION_PATH,
    uri_registry_path: Path = URI_REGISTRY_PATH, write: bool = True,
) -> tuple[dict, dict, Graph, dict]:
    snapshot = _read_json(snapshot_path)
    candidates = snapshot.get("candidates", [])
    verified_on = snapshot.get("verifiedOn") or str(snapshot.get("generatedAt", ""))[:10]
    by_identity: dict[str, dict] = {}
    for candidate in candidates:
        record = _catalog_organization(candidate, verified_on)
        key = _identity_key(record.get("qid"), None, record["name"])
        by_identity[key] = record
    for curated in load_curation(curation_path):
        key = _identity_key(curated.get("qid"), curated.get("curationKey"), curated["name"])
        by_identity[key] = _merge_curated(by_identity.get(key), curated)

    registry = _load_uri_registry(uri_registry_path)
    organizations = []
    for key, record in sorted(by_identity.items(), key=lambda item: (item[1]["name"].casefold(), item[0])):
        if record["reviewStatus"] == "evidence-reviewed":
            record["iri"] = _reserve_uri(registry, key, record["name"])
        else:
            record["iri"] = None
        record["identityKey"] = key
        record["organizationKinds"] = [
            {"uri": value, "label": KIND_LABELS[value]}
            for value in sorted(record.get("organizationKinds", []))
        ]
        record["ecosystemRoles"] = [
            {"uri": value, "label": ROLE_LABELS[value]}
            for value in sorted(record.get("ecosystemRoles", []))
        ]
        organizations.append(record)

    organizations.sort(key=lambda row: (row["name"].casefold(), row["identityKey"]))
    status_counts = Counter(row["reviewStatus"] for row in organizations)
    missing = [row for row in organizations if row.get("supplemental")]
    apparent_keys = {_identity_key(row.get("qid"), None, row["name"]) for row in candidates}
    catalog_status_counts = Counter(
        row["reviewStatus"] for row in organizations if row["identityKey"] in apparent_keys
    )
    audit = {
        "schemaVersion": 1,
        "generatedAt": snapshot.get("generatedAt"),
        "lastVerified": verified_on,
        "inputCounts": snapshot.get("catalogCounts", {}),
        "counts": {
            "accepted": status_counts["evidence-reviewed"],
            "rejected": status_counts["rejected"],
            "unresolved": status_counts["unresolved"],
            "missingRelationship": len(missing),
            "catalogAccepted": catalog_status_counts["evidence-reviewed"],
            "catalogRejected": catalog_status_counts["rejected"],
            "catalogUnresolved": catalog_status_counts["unresolved"],
            "pilotSelected": sum(row.get("pilotSelected", False) for row in organizations),
        },
        "accepted": [
            {"identityKey": row["identityKey"], "name": row["name"], "iri": row["iri"], "reason": row["reviewReason"]}
            for row in organizations if row["reviewStatus"] == "evidence-reviewed"
        ],
        "rejected": [
            {"identityKey": row["identityKey"], "name": row["name"], "reason": row["reviewReason"]}
            for row in organizations if row["reviewStatus"] == "rejected"
        ],
        "unresolved": [
            {"identityKey": row["identityKey"], "name": row["name"], "reason": row["reviewReason"]}
            for row in organizations if row["reviewStatus"] == "unresolved"
        ],
        "missingRelationships": [
            {"identityKey": row["identityKey"], "name": row["name"], "status": row["reviewStatus"]}
            for row in missing
        ],
    }
    payload = {
        "schemaVersion": 1,
        "generatedAt": snapshot.get("generatedAt"),
        "lastVerified": verified_on,
        "counts": audit["counts"],
        "organizations": organizations,
    }
    graph = build_registry_graph(payload)
    validate_registry_graph(graph)
    if write:
        _write_json(uri_registry_path, registry)
        _write_json(JSON_PATH, payload)
        _write_json(AUDIT_PATH, audit)
        RDF_PATH.parent.mkdir(parents=True, exist_ok=True)
        graph.serialize(destination=str(RDF_PATH), format="turtle")
    return payload, audit, graph, registry


def build_registry_graph(payload: dict) -> Graph:
    graph = Graph()
    for prefix, namespace in (
        ("schema", SCHEMA), ("kgjobs", KGJOBS), ("orgv", ORGV),
        ("dcterms", DCTERMS), ("prov", PROV), ("dcat", DCAT),
        ("skos", SKOS), ("owl", OWL),
    ):
        graph.bind(prefix, namespace)
    dataset = URIRef("https://openknowledgegraphs.com/prototypes/kg-jobs/organizations")
    graph.add((dataset, RDF.type, DCAT.Dataset))
    graph.add((dataset, DCTERMS.title, Literal("Reviewed KG ecosystem organization registry", lang="en")))
    if payload.get("generatedAt"):
        graph.add((dataset, DCTERMS.modified, Literal(payload["generatedAt"], datatype=XSD.dateTime)))
    for organization in payload["organizations"]:
        if organization["reviewStatus"] != "evidence-reviewed":
            continue
        subject = URIRef(organization["iri"])
        graph.add((subject, RDF.type, SCHEMA.Organization))
        graph.add((subject, RDF.type, PROV.Entity))
        graph.add((subject, SCHEMA.name, Literal(organization["name"])))
        graph.add((subject, KGJOBS.reviewStatus, Literal(organization["reviewStatus"])))
        graph.add((subject, KGJOBS.active, Literal(organization["active"], datatype=XSD.boolean)))
        graph.add((subject, KGJOBS.lastVerified, Literal(organization["lastVerified"], datatype=XSD.date)))
        graph.add((subject, KGJOBS.pilotSelected, Literal(organization["pilotSelected"], datatype=XSD.boolean)))
        graph.add((subject, KGJOBS.productionApproved, Literal(organization["productionApproved"], datatype=XSD.boolean)))
        if organization.get("description"):
            graph.add((subject, DCTERMS.description, Literal(organization["description"])))
        if organization.get("officialWebsite"):
            graph.add((subject, SCHEMA.url, URIRef(organization["officialWebsite"])))
        if organization.get("careersPage"):
            graph.add((subject, KGJOBS.careersPage, URIRef(organization["careersPage"])))
        if organization.get("qid"):
            graph.add((subject, OWL.sameAs, URIRef(f"{WIKIDATA_ENTITY}{organization['qid']}")))
        for alias in organization.get("aliases", []):
            graph.add((subject, SKOS.altLabel, Literal(alias)))
        for kind in organization["organizationKinds"]:
            graph.add((subject, KGJOBS.organizationKind, URIRef(kind["uri"])))
        for role in organization["ecosystemRoles"]:
            graph.add((subject, KGJOBS.ecosystemRole, URIRef(role["uri"])))
        for evidence in organization.get("evidence", []):
            digest = hashlib.sha256(
                f"{organization['iri']}|{evidence['url']}|{evidence['note']}|{evidence['reviewedOn']}".encode("utf-8")
            ).hexdigest()[:16]
            node = URIRef(f"{organization['iri']}evidence/{digest}")
            graph.add((node, RDF.type, KGJOBS.InclusionEvidence))
            graph.add((node, DCTERMS.source, URIRef(evidence["url"])))
            graph.add((node, DCTERMS.description, Literal(evidence["note"])))
            graph.add((node, DCTERMS.date, Literal(evidence["reviewedOn"], datatype=XSD.date)))
            graph.add((subject, KGJOBS.inclusionEvidence, node))
        for index, relationship in enumerate(organization.get("catalogRelationships", []), start=1):
            digest = hashlib.sha256(
                f"{organization['iri']}|{relationship.get('resourceUrl')}|{index}".encode("utf-8")
            ).hexdigest()[:16]
            node = URIRef(f"{organization['iri']}relationship/{digest}")
            graph.add((node, RDF.type, KGJOBS.ProducerRelationship))
            graph.add((node, KGJOBS.producerOrganization, subject))
            if relationship.get("resourceUrl"):
                graph.add((node, KGJOBS.catalogResource, URIRef(relationship["resourceUrl"])))
            for prop in relationship.get("sourceProperties", []):
                graph.add((node, KGJOBS.sourceProperty, Literal(prop)))
            for role in relationship.get("producerRoles", []):
                graph.add((node, KGJOBS.producerRole, Literal(role)))
            if relationship.get("evidenceUrl"):
                graph.add((node, DCTERMS.source, URIRef(relationship["evidenceUrl"])))
            graph.add((dataset, URIRef(f"{DCAT}resource"), subject))
    return graph


def validate_registry_graph(graph: Graph) -> None:
    data = Graph()
    for triple in graph:
        data.add(triple)
    data.parse(ROOT / "vocabularies" / "organizations.ttl", format="turtle")
    shapes = Graph().parse(ROOT / "ontology.ttl", format="turtle")
    conforms, _, report = validate(
        data, shacl_graph=shapes, ont_graph=shapes,
        inference="none", abort_on_first=False,
    )
    if not conforms:
        raise OrganizationRegistryError(f"organization registry failed SHACL:\n{report}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-source-snapshot", action="store_true",
        help="explicitly contact Wikidata and replace the checked-in audit snapshot",
    )
    parser.add_argument(
        "--bootstrap-source-snapshot", action="store_true",
        help="create a network-free unresolved snapshot from committed catalogs",
    )
    parser.add_argument("--verified-on", help="ISO review date for a live snapshot")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.refresh_source_snapshot and args.bootstrap_source_snapshot:
        raise OrganizationRegistryError("choose only one snapshot mode")
    if args.refresh_source_snapshot:
        refresh_source_snapshot(verified_on=args.verified_on)
    elif args.bootstrap_source_snapshot:
        create_local_source_snapshot()
    if not SNAPSHOT_PATH.exists():
        raise OrganizationRegistryError(
            "organization source snapshot is absent; bootstrap or refresh it explicitly"
        )
    payload, audit, _, _ = build_registry()
    print(
        "Organization registry built: "
        f"{audit['counts']['accepted']} accepted, "
        f"{audit['counts']['unresolved']} unresolved, "
        f"{audit['counts']['rejected']} rejected; "
        f"{audit['counts']['pilotSelected']} pilot organizations"
    )
    print(f"RDF/JSON organizations: {len(payload['organizations'])}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OrganizationRegistryError as exc:
        print(f"Organization registry failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
