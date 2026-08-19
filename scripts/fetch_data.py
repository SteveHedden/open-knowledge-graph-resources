#!/usr/bin/env python3
"""Fetch Open Knowledge Graphs data from Wikidata and write RDF Turtle files."""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import unicodedata
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS, XSD

from category_classifier import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MODEL,
    classify_items,
)
from semantic_config import (
    BASE_URL,
    CATEGORIES_VOCAB_PATH,
    CURATION_PATH,
    OKG,
    ONTOLOGIES_DATASET,
    SOFTWARE_DATASET,
    SOFTWARE_TYPES_VOCAB_PATH,
    SOURCES_PATH,
    ControlledVocabulary,
    SemanticConfigError,
    SourceMappings,
    SourceEligibilityPolicy,
    classification_label_projection,
    controlled_vocabulary_projection,
    load_controlled_vocabulary,
    load_curated_assignments,
    load_source_mappings,
    write_curated_assignments_atomic,
    write_json_atomic as write_projection_json_atomic,
)
from related_resources import (
    DEFAULT_CONFIG as RELATED_SIMILARITY_CONFIG,
    SimilarityContext,
    SimilarityConfig,
    add_related_resources,
    build_similarity_context,
    diagnostics_document,
    write_diagnostics_atomic,
)
from wikidata_relationship_audit import DirectIriEdge, audit_document, truthy_item_edges

WDQS_URL = "https://query.wikidata.org/sparql"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"

ROOT_DIR = Path(
    os.environ.get("OKG_CATALOG_ROOT", Path(__file__).resolve().parent.parent)
).resolve()
DATA_DIR = ROOT_DIR / "data"
ONTOLOGIES_OUT = DATA_DIR / "ontologies.ttl"
SOFTWARE_OUT = DATA_DIR / "software.ttl"
ONTOLOGIES_JSON_OUT = DATA_DIR / "ontologies.json"
SOFTWARE_JSON_OUT = DATA_DIR / "software.json"
CATEGORIES_JSON_OUT = DATA_DIR / "categories.json"
SOFTWARE_TYPES_JSON_OUT = DATA_DIR / "software_types.json"
CONTROLLED_VOCABULARIES_JSON_OUT = DATA_DIR / "controlled_vocabularies.json"
URI_REGISTRY_OUT = DATA_DIR / "uri_registry.json"
PAGE_QIDS_LEGACY = DATA_DIR / "page_qids.json"
RELATED_DIAGNOSTICS_OUT = Path(
    os.environ.get(
        "OKG_RELATED_DIAGNOSTICS_PATH",
        ROOT_DIR / "build" / "related-resources.json",
    )
).resolve()

USER_AGENT = os.getenv(
    "WDQS_USER_AGENT",
    (
        "OpenKnowledgeGraphsBot/0.1 "
        "(https://github.com/SteveHedden/open-knowledge-graphs; "
        "contact: stevehedden@users.noreply.github.com)"
    ),
)
REQUEST_TIMEOUT_SECONDS = int(os.getenv("WDQS_REQUEST_TIMEOUT_SECONDS", "180"))
MAX_REQUEST_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 5
QUERY_PAUSE_SECONDS = float(os.getenv("WDQS_QUERY_PAUSE_SECONDS", "1.0"))
LABEL_QUERY_BATCH_SIZE = int(os.getenv("WDQS_LABEL_QUERY_BATCH_SIZE", "100"))
CATEGORY_CLASSIFICATION_BATCH_SIZE = int(
    os.getenv("CATEGORY_CLASSIFICATION_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))
)
CATEGORY_CLASSIFICATION_MODEL = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)

LOCAL_NAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9]+")
QID_RE = re.compile(r"(Q\d+)$")


class WDQSError(RuntimeError):
    """Raised when WDQS data cannot be fetched reliably."""


def wikidata_property(
    mappings: SourceMappings,
    normalized_field: str,
    catalog: URIRef | None = None,
    value_kind: str | None = None,
) -> str:
    """Return a validated Wikidata property ID from the RDF source registry."""
    return mappings.property_id_for(normalized_field, catalog, value_kind)


def wikidata_class_path(mappings: SourceMappings, catalog: URIRef) -> str:
    instance_of = wikidata_property(mappings, "instanceOf", catalog, "iri")
    subclass_of = wikidata_property(mappings, "subclassOf", catalog, "iri")
    return f"wdt:{instance_of}/wdt:{subclass_of}*"


def optional_direct_clause(
    mappings: SourceMappings,
    normalized_field: str,
    variable: str,
    catalog: URIRef,
    value_kind: str,
) -> str:
    property_id = wikidata_property(mappings, normalized_field, catalog, value_kind)
    return f"  OPTIONAL {{ ?item wdt:{property_id} ?{variable} . }}"


def optional_union_clause(
    mappings: SourceMappings,
    normalized_field: str,
    variable: str,
    catalog: URIRef,
    value_kind: str,
) -> str:
    property_ids = mappings.property_ids_for(normalized_field, catalog, value_kind)
    branches = " UNION\n    ".join(
        f"{{ ?item wdt:{property_id} ?{variable} . }}" for property_id in property_ids
    )
    return f"  OPTIONAL {{\n    {branches}\n  }}"


def class_union_clause(mappings: SourceMappings, catalog: URIRef) -> str:
    path = wikidata_class_path(mappings, catalog)
    return "\n  UNION\n".join(
        f"  {{ ?item {path} wd:{class_id} . }}"
        for class_id in mappings.class_ids_for(catalog)
    )


def build_type_base_query(type_qid: str, mappings: SourceMappings) -> str:
    path = wikidata_class_path(mappings, ONTOLOGIES_DATASET)
    direct_type_property = wikidata_property(mappings, "instanceOf", ONTOLOGIES_DATASET, "iri")
    clauses = [
        optional_direct_clause(mappings, "officialWebsite", "officialWebsite", ONTOLOGIES_DATASET, "iri"),
        optional_direct_clause(mappings, "sourceCodeRepo", "sourceCodeRepo", ONTOLOGIES_DATASET, "iri"),
        optional_direct_clause(mappings, "namespaceURI", "namespaceURI", ONTOLOGIES_DATASET, "iri"),
        optional_direct_clause(mappings, "license", "license", ONTOLOGIES_DATASET, "iri"),
        optional_direct_clause(mappings, "partOfEntity", "partOfEntity", ONTOLOGIES_DATASET, "iri"),
        optional_union_clause(mappings, "creator", "creator", ONTOLOGIES_DATASET, "iri"),
    ]
    return f"""
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT DISTINCT ?item ?directType ?officialWebsite ?sourceCodeRepo ?namespaceURI ?license ?partOfEntity ?creator
WHERE {{
  ?item {path} wd:{type_qid} .
  OPTIONAL {{ ?item wdt:{direct_type_property} ?directType . }}
{chr(10).join(clauses)}
}}
"""


def build_software_base_query(mappings: SourceMappings) -> str:
    clauses = [
        optional_direct_clause(mappings, "officialWebsite", "officialWebsite", SOFTWARE_DATASET, "iri"),
        optional_direct_clause(mappings, "sourceCodeRepo", "sourceCodeRepo", SOFTWARE_DATASET, "iri"),
        optional_direct_clause(mappings, "license", "license", SOFTWARE_DATASET, "iri"),
        optional_direct_clause(mappings, "partOfEntity", "partOfEntity", SOFTWARE_DATASET, "iri"),
        optional_union_clause(mappings, "creator", "creator", SOFTWARE_DATASET, "iri"),
        optional_direct_clause(mappings, "programmingLanguage", "programmingLanguage", SOFTWARE_DATASET, "iri"),
    ]
    return f"""
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT DISTINCT ?item ?officialWebsite ?sourceCodeRepo ?license ?partOfEntity ?creator ?programmingLanguage
WHERE {{
{class_union_clause(mappings, SOFTWARE_DATASET)}
{chr(10).join(clauses)}
}}
"""


def build_software_version_query(mappings: SourceMappings) -> str:
    version_property = wikidata_property(mappings, "version", SOFTWARE_DATASET, "string")
    publication_date_property = wikidata_property(
        mappings, "publicationDate", SOFTWARE_DATASET, "date-time"
    )
    return f"""
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>

SELECT ?item ?version ?pubDate
WHERE {{
{class_union_clause(mappings, SOFTWARE_DATASET)}
  ?item p:{version_property} ?verStmt .
  ?verStmt ps:{version_property} ?version .
  OPTIONAL {{ ?verStmt pq:{publication_date_property} ?pubDate . }}
}}
"""


@dataclass
class ResourceRecord:
    item_iri: str
    label: str
    description: str | None = None
    aliases: set[str] = field(default_factory=set)
    category: URIRef | None = None
    software_type: URIRef | None = None
    types: set[URIRef] = field(default_factory=set)
    homepages: set[str] = field(default_factory=set)
    source_repos: set[str] = field(default_factory=set)
    namespace_uris: set[str] = field(default_factory=set)
    licenses: set[str] = field(default_factory=set)
    part_of_entities: set[str] = field(default_factory=set)
    part_of_labels: set[str] = field(default_factory=set)
    source_types: set[str] = field(default_factory=set)
    uses_entities: set[str] = field(default_factory=set)
    creators: set[str] = field(default_factory=set)
    programming_languages: set[str] = field(default_factory=set)
    latest_version: str | None = None
    release_date: date | None = None


@dataclass(frozen=True)
class OntologyCandidateFacts:
    qid: str
    direct_type_qids: frozenset[str]
    direct_parent_qids: frozenset[str]


@dataclass(frozen=True)
class OntologyEligibilityResult:
    eligible_qids: frozenset[str]
    declared_exclusion_qids: frozenset[str]
    rule_exclusion_qids: frozenset[str]


def ontology_candidate_facts(rows: list[dict]) -> dict[str, OntologyCandidateFacts]:
    collected: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        item_iri = binding_value(row, "item")
        if not item_iri:
            continue
        qid = qid_from_wikidata_iri(item_iri)
        entry = collected.setdefault(qid, {"types": set(), "parents": set()})
        direct_type = binding_value(row, "directType")
        if direct_type:
            entry["types"].add(qid_from_wikidata_iri(direct_type))
        parent = binding_value(row, "partOfEntity")
        if parent:
            entry["parents"].add(qid_from_wikidata_iri(parent))
    return {
        qid: OntologyCandidateFacts(
            qid=qid,
            direct_type_qids=frozenset(values["types"]),
            direct_parent_qids=frozenset(values["parents"]),
        )
        for qid, values in collected.items()
    }


def evaluate_ontology_eligibility(
    facts: dict[str, OntologyCandidateFacts],
    policy: SourceEligibilityPolicy,
) -> OntologyEligibilityResult:
    """Evaluate the narrow RDF-declared term/component-with-parent rule.

    Confirmed exclusions take precedence over reviewed exceptions. A parent is
    cataloged only when it is itself a raw candidate and recursively eligible
    in this same snapshot. Cycles in policy-relevant direct part-of facts are rejected
    rather than producing traversal-order-dependent publication decisions.
    """

    memo: dict[str, bool] = {}
    rule_exclusions: set[str] = set()

    def is_eligible(qid: str, visiting: tuple[str, ...] = ()) -> bool:
        if qid in memo:
            return memo[qid]
        if qid in policy.exclusions:
            memo[qid] = False
            return False
        if qid in policy.exceptions:
            memo[qid] = True
            return True
        if qid in visiting:
            cycle = " -> ".join((*visiting, qid))
            raise SemanticConfigError(
                f"Policy-relevant Wikidata part-of cycle requires review: {cycle}"
            )

        candidate = facts[qid]
        has_marker = bool(candidate.direct_type_qids & policy.term_component_markers)
        has_cataloged_parent = has_marker and any(
            parent_qid in facts and is_eligible(parent_qid, (*visiting, qid))
            for parent_qid in sorted(candidate.direct_parent_qids)
        )
        eligible = not has_cataloged_parent
        memo[qid] = eligible
        if not eligible:
            rule_exclusions.add(qid)
        return eligible

    eligible = frozenset(qid for qid in sorted(facts) if is_eligible(qid))
    return OntologyEligibilityResult(
        eligible_qids=eligible,
        declared_exclusion_qids=frozenset(set(facts) & set(policy.exclusions)),
        rule_exclusion_qids=frozenset(rule_exclusions),
    )


def filter_ontology_rows(
    rows: list[dict],
    policy: SourceEligibilityPolicy,
) -> tuple[list[dict], OntologyEligibilityResult]:
    facts = ontology_candidate_facts(rows)
    result = evaluate_ontology_eligibility(facts, policy)
    filtered = [
        row
        for row in rows
        if (item := binding_value(row, "item"))
        and qid_from_wikidata_iri(item) in result.eligible_qids
    ]
    return filtered, result


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def binding_value(binding: dict, key: str) -> str | None:
    value = binding.get(key, {}).get("value")
    if value is None:
        return None
    value = value.strip()
    return value or None


def qid_from_wikidata_iri(iri: str) -> str:
    match = QID_RE.search(iri)
    if not match:
        raise ValueError(f"Could not parse QID from IRI: {iri}")
    return match.group(1)


def canonical_entity_iri(iri: str) -> str:
    qid = qid_from_wikidata_iri(iri)
    return f"http://www.wikidata.org/entity/{qid}"


def wikidata_page_iri(iri: str) -> str:
    qid = qid_from_wikidata_iri(iri)
    return f"https://www.wikidata.org/wiki/{qid}"


def sanitize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = LOCAL_NAME_CLEAN_RE.sub("_", normalized).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        cleaned = "Resource"
    if cleaned[0].isdigit():
        cleaned = f"Resource_{cleaned}"
    return cleaned


def slugify(text: str) -> str:
    """Convert a title to a URL-friendly slug. Mirrors generate_pages.py's slugify —
    kept in sync intentionally so a slug computed here matches the page path
    generate_pages.py would build for the same title.
    """
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or "item"


def load_uri_registry() -> dict[str, dict[str, str]]:
    """Load the persistent QID -> slug registry. Once a slug is assigned to a
    QID it is never reassigned, so a resource's URI never changes even before
    it has a live page (see mint_resource_iri).
    """
    if URI_REGISTRY_OUT.exists():
        with open(URI_REGISTRY_OUT, encoding="utf-8") as f:
            registry = json.load(f)
    elif PAGE_QIDS_LEGACY.exists():
        # First run after the registry was introduced: seed from the existing
        # published-page slug map so no live URL changes.
        with open(PAGE_QIDS_LEGACY, encoding="utf-8") as f:
            registry = json.load(f)
    else:
        registry = {}
    registry.setdefault("resource", {})
    registry.setdefault("software", {})
    return registry


def save_uri_registry(registry: dict[str, dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = URI_REGISTRY_OUT.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(URI_REGISTRY_OUT)


def assign_slugs(records: dict[str, ResourceRecord], dataset_key: str, registry: dict[str, dict[str, str]]) -> None:
    """Assign a stable slug to every record that doesn't already have one in
    the registry. Existing assignments are never changed. New collisions
    (two records wanting the same slug) are resolved deterministically by
    processing records in a fixed sort order and appending the QID.
    """
    dataset_registry = registry[dataset_key]
    used_slugs = set(dataset_registry.values())

    pending = [
        record
        for record in records.values()
        if qid_from_wikidata_iri(record.item_iri) not in dataset_registry
    ]
    pending.sort(key=lambda row: (row.label.casefold(), row.item_iri))

    for record in pending:
        qid = qid_from_wikidata_iri(record.item_iri)
        slug = slugify(record.label)
        if slug in used_slugs:
            slug = f"{slug}-{qid.lower()}"
        used_slugs.add(slug)
        dataset_registry[qid] = slug


def mint_resource_iri(dataset_path: str, slug: str) -> URIRef:
    return URIRef(f"{BASE_URL}/{dataset_path}/{slug}/")


def mint_license_iri(label: str | None, wikidata_iri: str) -> URIRef:
    qid = qid_from_wikidata_iri(wikidata_iri)
    base = sanitize_label(label or qid)
    return OKG[f"License_{base}_{qid}"]


def mint_creator_iri(label: str | None, wikidata_iri: str) -> URIRef:
    qid = qid_from_wikidata_iri(wikidata_iri)
    base = sanitize_label(label or qid)
    return OKG[f"Creator_{base}_{qid}"]


def parse_retry_after_seconds(raw_header: str | None, attempt: int) -> float:
    if raw_header:
        try:
            return max(float(raw_header), 0.0)
        except ValueError:
            pass
    return float(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))


def run_wdqs_query(session: requests.Session, query: str, label: str) -> list[dict]:
    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        try:
            response = session.post(
                WDQS_URL,
                data={"query": query, "format": "json"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            if attempt == MAX_REQUEST_ATTEMPTS:
                raise WDQSError(f"{label}: request failed after retries: {exc}") from exc
            delay = float(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            logging.warning("%s: request error (%s); retrying in %.1fs", label, exc, delay)
            time.sleep(delay)
            continue

        if response.status_code == 429:
            if attempt == MAX_REQUEST_ATTEMPTS:
                raise WDQSError(f"{label}: rate-limited repeatedly (HTTP 429)")
            delay = parse_retry_after_seconds(response.headers.get("Retry-After"), attempt)
            logging.warning("%s: HTTP 429; retrying in %.1fs", label, delay)
            time.sleep(delay)
            continue

        if 500 <= response.status_code < 600:
            if attempt == MAX_REQUEST_ATTEMPTS:
                raise WDQSError(f"{label}: server error HTTP {response.status_code}")
            delay = float(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            logging.warning(
                "%s: server error HTTP %s; retrying in %.1fs",
                label,
                response.status_code,
                delay,
            )
            time.sleep(delay)
            continue

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise WDQSError(f"{label}: request failed HTTP {response.status_code}") from exc

        try:
            payload = response.json()
            return payload["results"]["bindings"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WDQSError(f"{label}: malformed JSON response") from exc

    raise WDQSError(f"{label}: request attempts exhausted")


def fetch_direct_iri_edges(
    session: requests.Session,
    qids: set[str],
) -> tuple[DirectIriEdge, ...]:
    """Fetch all direct/truthy item-valued claims for the captured cohort."""
    edges: set[DirectIriEdge] = set()
    for batch in chunked(sorted(qids), 50):
        for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
            try:
                response = session.get(
                    WIKIDATA_API_URL,
                    params={
                        "action": "wbgetentities",
                        "format": "json",
                        "ids": "|".join(batch),
                        "props": "claims",
                    },
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                if attempt == MAX_REQUEST_ATTEMPTS:
                    raise WDQSError(f"direct relationship audit failed: {exc}") from exc
                time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_REQUEST_ATTEMPTS:
                    raise WDQSError(
                        f"direct relationship audit failed HTTP {response.status_code}"
                    )
                time.sleep(parse_retry_after_seconds(response.headers.get("Retry-After"), attempt))
                continue
            try:
                response.raise_for_status()
                entities = response.json()["entities"]
            except (requests.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise WDQSError("direct relationship audit returned malformed data") from exc
            edges.update(truthy_item_edges(entities))
            break
        time.sleep(QUERY_PAUSE_SECONDS)
    return tuple(sorted(edges))


def apply_declared_relationships(
    records: dict[str, ResourceRecord],
    edges: tuple[DirectIriEdge, ...],
    mappings: SourceMappings,
) -> None:
    """Ingest only relationship predicates explicitly reviewed in sources.ttl."""
    source_type_property = wikidata_property(mappings, "sourceType", value_kind="iri")
    uses_property = wikidata_property(mappings, "usesEntity", value_kind="iri")
    for edge in edges:
        record = records.get(canonical_entity_iri(edge.subject_qid))
        if record is None:
            continue
        target = canonical_entity_iri(edge.object_qid)
        if edge.property_id == source_type_property:
            record.source_types.add(target)
        elif edge.property_id == uses_property:
            record.uses_entities.add(target)


def verify_recommendation_exemplars(
    graph: Graph,
    records: dict[str, ResourceRecord],
    slug_registry: dict[str, str],
    edges: tuple[DirectIriEdge, ...],
    mappings: SourceMappings,
) -> list[dict[str, str]]:
    """Enforce declarative exemplars whenever their live source facts are eligible."""
    source_claims = {
        (edge.subject_qid, edge.property_id, edge.object_qid)
        for edge in edges
    }
    verified: list[dict[str, str]] = []
    for exemplar in mappings.recommendation_exemplars:
        if exemplar.catalog != ONTOLOGIES_DATASET:
            continue
        subject_record = records.get(canonical_entity_iri(exemplar.subject_qid))
        object_record = records.get(canonical_entity_iri(exemplar.object_qid))
        if (
            subject_record is None
            or object_record is None
            or not subject_record.homepages
            or not object_record.homepages
            or (
                exemplar.subject_qid,
                exemplar.source_property_id,
                exemplar.object_qid,
            ) not in source_claims
        ):
            continue
        subject = mint_resource_iri("resource", slug_registry[exemplar.subject_qid])
        target = mint_resource_iri("resource", slug_registry[exemplar.object_qid])
        if (subject, OKG.relatedTo, target) not in graph:
            raise WDQSError(
                f"Pinned recommendation is missing: {exemplar.label}"
            )
        verified.append(
            {
                "subjectQid": exemplar.subject_qid,
                "subjectLabel": exemplar.label,
                "sourcePropertyId": exemplar.source_property_id,
                "objectQid": exemplar.object_qid,
                "selectedTarget": str(target),
            }
        )
    return verified


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def fetch_entity_labels(
    session: requests.Session,
    entity_iris: set[str],
) -> tuple[dict[str, str], dict[str, str], dict[str, set[str]]]:
    if not entity_iris:
        return {}, {}, {}

    labels: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    description_lang: dict[str, str] = {}
    aliases: dict[str, set[str]] = {}
    sorted_entities = sorted(canonical_entity_iri(iri) for iri in entity_iris)

    for chunk in chunked(sorted_entities, LABEL_QUERY_BATCH_SIZE):
        values = " ".join(f"<{entity}>" for entity in chunk)
        label_query = f"""
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX bd: <http://www.bigdata.com/rdf#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?entity ?entityLabel
WHERE {{
  VALUES ?entity {{ {values} }}
  SERVICE wikibase:label {{
    bd:serviceParam wikibase:language "en,mul,[AUTO_LANGUAGE]" .
    ?entity rdfs:label ?entityLabel .
  }}
}}
"""
        label_rows = run_wdqs_query(session, label_query, "entity label query")
        for row in label_rows:
            entity_iri = binding_value(row, "entity")
            entity_label = binding_value(row, "entityLabel")
            if entity_iri and entity_label:
                canonical = canonical_entity_iri(entity_iri)
                labels[canonical] = entity_label

        description_query = f"""
PREFIX schema: <http://schema.org/>

SELECT ?entity ?entityDescription
WHERE {{
  VALUES ?entity {{ {values} }}
  ?entity schema:description ?entityDescription .
  FILTER(LANG(?entityDescription) = "en" || LANG(?entityDescription) = "mul")
}}
"""
        description_rows = run_wdqs_query(session, description_query, "entity description query")
        for row in description_rows:
            entity_iri = binding_value(row, "entity")
            description_text = binding_value(row, "entityDescription")
            if not entity_iri or not description_text:
                continue
            canonical = canonical_entity_iri(entity_iri)
            description_meta = row.get("entityDescription", {})
            lang = str(description_meta.get("xml:lang", "")).lower()
            current_lang = description_lang.get(canonical, "")

            if canonical not in descriptions:
                descriptions[canonical] = description_text
                description_lang[canonical] = lang
                continue

            if current_lang != "en" and lang == "en":
                descriptions[canonical] = description_text
                description_lang[canonical] = lang

        alias_query = f"""
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

SELECT ?entity ?altLabel
WHERE {{
  VALUES ?entity {{ {values} }}
  ?entity skos:altLabel ?altLabel .
  FILTER(LANG(?altLabel) = "en")
}}
"""
        alias_rows = run_wdqs_query(session, alias_query, "entity alias query")
        for row in alias_rows:
            entity_iri = binding_value(row, "entity")
            alt_label = binding_value(row, "altLabel")
            if not entity_iri or not alt_label:
                continue
            canonical = canonical_entity_iri(entity_iri)
            aliases.setdefault(canonical, set()).add(alt_label)

        time.sleep(QUERY_PAUSE_SECONDS)

    return labels, descriptions, aliases


def fetch_human_creators(
    session: requests.Session,
    creator_iris: set[str],
    mappings: SourceMappings,
) -> set[str]:
    """Return the subset of creator IRIs mapped to OKG Person by Wikidata.

    schema.org's `creator` property only accepts Person or Organization
    (https://schema.org/creator); everything not identified as human here
    is treated as an Organization when the catalog is rendered.
    """
    if not creator_iris:
        return set()

    humans: set[str] = set()
    sorted_entities = sorted(canonical_entity_iri(iri) for iri in creator_iris)
    instance_of = wikidata_property(mappings, "instanceOf", value_kind="iri")
    human_class = mappings.class_id_for_target(OKG.Person)

    for chunk in chunked(sorted_entities, LABEL_QUERY_BATCH_SIZE):
        values = " ".join(f"<{entity}>" for entity in chunk)
        human_query = f"""
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT DISTINCT ?entity
WHERE {{
  VALUES ?entity {{ {values} }}
  ?entity wdt:{instance_of} wd:{human_class} .
}}
"""
        human_rows = run_wdqs_query(session, human_query, "creator human-check query")
        for row in human_rows:
            entity_iri = binding_value(row, "entity")
            if entity_iri:
                humans.add(canonical_entity_iri(entity_iri))
        time.sleep(QUERY_PAUSE_SECONDS)

    return humans


def fetch_person_identifiers(
    session: requests.Session,
    human_iris: set[str],
    mappings: SourceMappings,
) -> dict[str, dict[str, str]]:
    """Fetch person-specific identifiers (GitHub username, Google Scholar author ID)
    for creators already confirmed human. Organizations are deliberately excluded —
    these identifiers only make sense as claims about an individual.
    """
    if not human_iris:
        return {}

    identifiers: dict[str, dict[str, str]] = {}
    sorted_entities = sorted(canonical_entity_iri(iri) for iri in human_iris)
    github_property = wikidata_property(mappings, "github", value_kind="external-identifier")
    scholar_property = wikidata_property(mappings, "scholar", value_kind="external-identifier")

    for chunk in chunked(sorted_entities, LABEL_QUERY_BATCH_SIZE):
        values = " ".join(f"<{entity}>" for entity in chunk)
        identifier_query = f"""
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT DISTINCT ?entity ?github ?scholar
WHERE {{
  VALUES ?entity {{ {values} }}
  OPTIONAL {{ ?entity wdt:{github_property} ?github . }}
  OPTIONAL {{ ?entity wdt:{scholar_property} ?scholar . }}
}}
"""
        rows = run_wdqs_query(session, identifier_query, "creator person-identifier query")
        for row in rows:
            entity_iri = binding_value(row, "entity")
            if not entity_iri:
                continue
            entity = canonical_entity_iri(entity_iri)
            github = binding_value(row, "github")
            scholar = binding_value(row, "scholar")
            if not github and not scholar:
                continue
            entry = identifiers.setdefault(entity, {})
            if github:
                entry["github"] = f"https://github.com/{github}"
            if scholar:
                entry["scholar"] = f"https://scholar.google.com/citations?user={scholar}"
        time.sleep(QUERY_PAUSE_SECONDS)

    return identifiers


def get_or_create_record(records: dict[str, ResourceRecord], item_iri: str, label: str) -> ResourceRecord:
    record = records.get(item_iri)
    if record is None:
        record = ResourceRecord(item_iri=item_iri, label=label)
        records[item_iri] = record
    elif not record.label and label:
        record.label = label
    return record


def label_for_entity(iri: str, labels: dict[str, str]) -> str:
    canonical = canonical_entity_iri(iri)
    return labels.get(canonical, qid_from_wikidata_iri(iri))


def parse_ontology_rows(
    rows: list[dict],
    labels: dict[str, str],
    descriptions: dict[str, str],
    aliases: dict[str, set[str]],
    qid_to_okg_class: dict[str, URIRef],
) -> tuple[dict[str, ResourceRecord], dict[str, str], dict[str, str]]:
    records: dict[str, ResourceRecord] = {}
    license_labels: dict[str, str] = {}
    creator_labels: dict[str, str] = {}

    for row in rows:
        item_iri_raw = binding_value(row, "item")
        if not item_iri_raw:
            continue
        item_iri = canonical_entity_iri(item_iri_raw)
        label = label_for_entity(item_iri, labels)
        record = get_or_create_record(records, item_iri, label)
        if record.description is None:
            record.description = descriptions.get(item_iri)
        record.aliases.update(aliases.get(item_iri, ()))

        type_qid = binding_value(row, "matchedTypeQid")
        if type_qid:
            osc_type = qid_to_okg_class.get(type_qid)
            if osc_type is not None:
                record.types.add(osc_type)

        homepage = binding_value(row, "officialWebsite")
        if homepage:
            record.homepages.add(homepage)

        source_repo = binding_value(row, "sourceCodeRepo")
        if source_repo:
            record.source_repos.add(source_repo)

        namespace_uri = binding_value(row, "namespaceURI")
        if namespace_uri:
            record.namespace_uris.add(namespace_uri)

        license_iri_raw = binding_value(row, "license")
        if license_iri_raw:
            license_iri = canonical_entity_iri(license_iri_raw)
            record.licenses.add(license_iri)
            license_labels[license_iri] = label_for_entity(license_iri, labels)

        part_of_iri_raw = binding_value(row, "partOfEntity")
        if part_of_iri_raw:
            record.part_of_entities.add(canonical_entity_iri(part_of_iri_raw))
            part_of_label = label_for_entity(part_of_iri_raw, labels)
            record.part_of_labels.add(part_of_label)

        creator_iri_raw = binding_value(row, "creator")
        if creator_iri_raw:
            creator_iri = canonical_entity_iri(creator_iri_raw)
            record.creators.add(creator_iri)
            creator_labels[creator_iri] = label_for_entity(creator_iri, labels)

    return records, license_labels, creator_labels


def parse_software_rows(
    rows: list[dict],
    labels: dict[str, str],
    descriptions: dict[str, str],
    aliases: dict[str, set[str]],
) -> tuple[dict[str, ResourceRecord], dict[str, str], dict[str, str], dict[str, str]]:
    records: dict[str, ResourceRecord] = {}
    license_labels: dict[str, str] = {}
    creator_labels: dict[str, str] = {}
    programming_language_labels: dict[str, str] = {}

    for row in rows:
        item_iri_raw = binding_value(row, "item")
        if not item_iri_raw:
            continue
        item_iri = canonical_entity_iri(item_iri_raw)
        label = label_for_entity(item_iri, labels)
        record = get_or_create_record(records, item_iri, label)
        if record.description is None:
            record.description = descriptions.get(item_iri)
        record.aliases.update(aliases.get(item_iri, ()))
        record.types.add(OKG.Software)

        homepage = binding_value(row, "officialWebsite")
        if homepage:
            record.homepages.add(homepage)

        source_repo = binding_value(row, "sourceCodeRepo")
        if source_repo:
            record.source_repos.add(source_repo)

        license_iri_raw = binding_value(row, "license")
        if license_iri_raw:
            license_iri = canonical_entity_iri(license_iri_raw)
            record.licenses.add(license_iri)
            license_labels[license_iri] = label_for_entity(license_iri, labels)

        part_of_iri_raw = binding_value(row, "partOfEntity")
        if part_of_iri_raw:
            record.part_of_entities.add(canonical_entity_iri(part_of_iri_raw))
            part_of_label = label_for_entity(part_of_iri_raw, labels)
            record.part_of_labels.add(part_of_label)

        creator_iri_raw = binding_value(row, "creator")
        if creator_iri_raw:
            creator_iri = canonical_entity_iri(creator_iri_raw)
            record.creators.add(creator_iri)
            creator_labels[creator_iri] = label_for_entity(creator_iri, labels)

        programming_language_iri_raw = binding_value(row, "programmingLanguage")
        if programming_language_iri_raw:
            programming_language_iri = canonical_entity_iri(programming_language_iri_raw)
            record.programming_languages.add(programming_language_iri)
            programming_language_labels[programming_language_iri] = label_for_entity(
                programming_language_iri, labels
            )

    return records, license_labels, creator_labels, programming_language_labels


def parse_wikidata_datetime(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    value = raw_value.strip().lstrip("+")
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value[:10])
    except ValueError:
        return None


def pick_latest_version_rows(rows: list[dict]) -> dict[str, tuple[str, date | None]]:
    by_item: dict[str, list[tuple[str, datetime | None]]] = {}

    for row in rows:
        item_iri_raw = binding_value(row, "item")
        version = binding_value(row, "version")
        if not item_iri_raw or not version:
            continue
        item_iri = canonical_entity_iri(item_iri_raw)
        pub_date = parse_wikidata_datetime(binding_value(row, "pubDate"))
        by_item.setdefault(item_iri, []).append((version, pub_date))

    results: dict[str, tuple[str, date | None]] = {}
    for item_iri, candidates in by_item.items():
        with_dates = [candidate for candidate in candidates if candidate[1] is not None]
        if with_dates:
            # Keep version and release date from the same statement row.
            version, dt_value = max(with_dates, key=lambda item: (item[1], item[0]))  # type: ignore[arg-type]
            results[item_iri] = (version, dt_value.date() if dt_value else None)
            continue

        # Fallback when no publication-date qualifier exists on any version statement.
        version = sorted((candidate[0] for candidate in candidates), reverse=True)[0]
        results[item_iri] = (version, None)

    return results


def apply_existing_categories(
    ontology_records: dict[str, ResourceRecord],
    category_mapping: dict[str, URIRef],
    vocabulary: ControlledVocabulary,
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for item_iri, record in ontology_records.items():
        qid = qid_from_wikidata_iri(item_iri)
        existing_category = category_mapping.get(qid)
        if existing_category in vocabulary.by_iri:
            record.category = existing_category
            continue

        if existing_category:
            logging.warning(
                "Ignoring invalid category value for %s (%s): %s",
                record.label,
                qid,
                existing_category,
            )
        missing.append(
            {
                "qid": qid,
                "title": record.label,
                "description": record.description or "",
            }
        )
    return missing


def classify_missing_ontology_categories(
    ontology_records: dict[str, ResourceRecord],
    category_mapping: dict[str, URIRef],
    vocabulary: ControlledVocabulary,
) -> tuple[int, int]:
    missing_items = apply_existing_categories(ontology_records, category_mapping, vocabulary)
    if not missing_items:
        return 0, 0

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logging.warning(
            "ANTHROPIC_API_KEY is not set; leaving %d ontology items uncategorized.",
            len(missing_items),
        )
        return 0, len(missing_items)

    classified, failed_qids = classify_items(
        items=missing_items,
        api_key=api_key,
        model=CATEGORY_CLASSIFICATION_MODEL,
        batch_size=CATEGORY_CLASSIFICATION_BATCH_SIZE,
        category_options=vocabulary.labels,
        category_set=vocabulary.label_set,
        definitions=vocabulary.prompt_definitions,
    )

    for qid, category in classified.items():
        category_mapping[qid] = vocabulary.by_label[category].iri

    for item_iri, record in ontology_records.items():
        qid = qid_from_wikidata_iri(item_iri)
        category = category_mapping.get(qid)
        if category in vocabulary.by_iri:
            record.category = category

    if failed_qids:
        logging.warning(
            "Category classification failed for %d ontology items; leaving them uncategorized.",
            len(failed_qids),
        )

    return len(classified), len(failed_qids)


def apply_existing_software_types(
    software_records: dict[str, ResourceRecord],
    software_type_mapping: dict[str, URIRef],
    vocabulary: ControlledVocabulary,
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for item_iri, record in software_records.items():
        qid = qid_from_wikidata_iri(item_iri)
        existing_type = software_type_mapping.get(qid)
        if existing_type in vocabulary.by_iri:
            record.software_type = existing_type
            continue

        if existing_type:
            logging.warning(
                "Ignoring invalid software type value for %s (%s): %s",
                record.label,
                qid,
                existing_type,
            )
        missing.append(
            {
                "qid": qid,
                "title": record.label,
                "description": record.description or "",
            }
        )
    return missing


def classify_missing_software_types(
    software_records: dict[str, ResourceRecord],
    software_type_mapping: dict[str, URIRef],
    vocabulary: ControlledVocabulary,
) -> tuple[int, int]:
    missing_items = apply_existing_software_types(software_records, software_type_mapping, vocabulary)
    if not missing_items:
        return 0, 0

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logging.warning(
            "ANTHROPIC_API_KEY is not set; leaving %d software items untyped.",
            len(missing_items),
        )
        return 0, len(missing_items)

    classified, failed_qids = classify_items(
        items=missing_items,
        api_key=api_key,
        model=CATEGORY_CLASSIFICATION_MODEL,
        batch_size=CATEGORY_CLASSIFICATION_BATCH_SIZE,
        category_options=vocabulary.labels,
        category_set=vocabulary.label_set,
        definitions=vocabulary.prompt_definitions,
        entity_label="knowledge graph or AI agent memory software resource",
        fallback_instruction="Pick the single closest match when unsure.",
    )

    for qid, software_type in classified.items():
        software_type_mapping[qid] = vocabulary.by_label[software_type].iri

    for item_iri, record in software_records.items():
        qid = qid_from_wikidata_iri(item_iri)
        software_type = software_type_mapping.get(qid)
        if software_type in vocabulary.by_iri:
            record.software_type = software_type

    if failed_qids:
        logging.warning(
            "Software type classification failed for %d items; leaving them untyped.",
            len(failed_qids),
        )

    return len(classified), len(failed_qids)


def collect_entity_iris(rows: list[dict], key: str) -> set[str]:
    values: set[str] = set()
    for row in rows:
        iri = binding_value(row, key)
        if iri:
            values.add(canonical_entity_iri(iri))
    return values


def first_literal_value(graph: Graph, subject: URIRef, predicate: URIRef) -> str | None:
    for value in graph.objects(subject, predicate):
        if isinstance(value, Literal):
            return str(value)
    return None


def first_iri_value(graph: Graph, subject: URIRef, predicate: URIRef) -> str | None:
    for value in graph.objects(subject, predicate):
        if isinstance(value, URIRef):
            return str(value)
    return None


def all_literal_values(graph: Graph, subject: URIRef, predicate: URIRef) -> list[str]:
    return sorted({str(value) for value in graph.objects(subject, predicate) if isinstance(value, Literal)})


def license_labels_for_resource(graph: Graph, subject: URIRef) -> list[str]:
    labels: set[str] = set()
    for license_node in graph.objects(subject, OKG.hasLicense):
        if not isinstance(license_node, URIRef):
            continue
        label = first_literal_value(graph, license_node, OKG.licenseName)
        if not label:
            label = first_literal_value(graph, license_node, RDFS.label)
        if label:
            labels.add(label)
    return sorted(labels, key=str.casefold)


def creators_for_resource(graph: Graph, subject: URIRef) -> list[dict[str, str]]:
    creators: list[dict[str, str]] = []
    for creator_node in graph.objects(subject, OKG.creator):
        if not isinstance(creator_node, URIRef):
            continue
        name = first_literal_value(graph, creator_node, RDFS.label)
        if not name:
            continue
        schema_type = "Person" if (creator_node, RDF.type, OKG.Person) in graph else "Organization"
        entry = {"name": name, "type": schema_type}
        wikidata_id = first_iri_value(graph, creator_node, OKG.wikidataId)
        if wikidata_id:
            entry["wikidataId"] = wikidata_id
        github_profile = first_iri_value(graph, creator_node, OKG.githubProfile)
        if github_profile:
            entry["githubProfile"] = github_profile
        scholar_profile = first_iri_value(graph, creator_node, OKG.googleScholarProfile)
        if scholar_profile:
            entry["googleScholarProfile"] = scholar_profile
        creators.append(entry)
    creators.sort(key=lambda entry: entry["name"].casefold())
    return creators


def add_related_tools(
    graph: Graph,
    dataset: str,
    config: SimilarityConfig = RELATED_SIMILARITY_CONFIG,
    context: SimilarityContext | None = None,
) -> dict[str, object]:
    """Hardened compatibility entry point for KG-grounded similarity."""
    return add_related_resources(graph, dataset=dataset, config=config, context=context)


def related_tools_for_resource(graph: Graph, subject: URIRef) -> list[dict[str, str]]:
    """Return a deterministic projection of the resource's RDF related links.

    RDF graphs do not retain insertion or ranking order, so the authoritative
    relation set is projected alphabetically with the canonical URL as a stable
    tie-breaker.
    """
    related: list[dict[str, str]] = []
    for node in graph.objects(subject, OKG.relatedTo):
        if not isinstance(node, URIRef):
            continue
        title = first_literal_value(graph, node, OKG.title)
        if not title:
            continue
        related.append({"title": title, "canonicalUrl": str(node)})
    related.sort(key=lambda entry: (entry["title"].casefold(), entry["canonicalUrl"]))
    return related


def extract_items_from_graph(
    graph: Graph,
    allowed_types: set[URIRef],
    include_software_fields: bool,
    resource_type_labels: dict[URIRef, str],
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    subjects = {subject for subject in graph.subjects(predicate=OKG.wikidataId)}

    for subject in subjects:
        if not isinstance(subject, URIRef):
            continue

        type_labels = sorted(
            {
                resource_type_labels[rdf_type]
                for rdf_type in graph.objects(subject, RDF.type)
                if rdf_type in allowed_types and rdf_type in resource_type_labels
            }
        )
        if not type_labels:
            continue

        title = first_literal_value(graph, subject, OKG.title) or first_literal_value(graph, subject, RDFS.label)
        wikidata_id = first_iri_value(graph, subject, OKG.wikidataId)
        if not title or not wikidata_id:
            continue
        # Exclude items whose only Wikidata label is a bare language code placeholder (e.g. "en")
        # These are bulk-imported items awaiting cleanup — tracked in issue #15
        if title.strip().lower() == "en":
            continue

        item: dict[str, object] = {
            "title": title,
            "wikidataId": wikidata_id,
            "types": type_labels,
            "canonicalUrl": str(subject),
        }
        description = first_literal_value(graph, subject, OKG.description)
        if description:
            item["description"] = description

        item["aliases"] = all_literal_values(graph, subject, OKG.alias)

        category_iri = first_iri_value(graph, subject, OKG.category)
        if category_iri:
            category_label = first_literal_value(graph, URIRef(category_iri), RDFS.label)
            if category_label:
                item["category"] = category_label

        homepage = first_iri_value(graph, subject, OKG.homepage)
        if homepage:
            item["homepage"] = homepage

        source_repo = first_iri_value(graph, subject, OKG.sourceRepo)
        if source_repo:
            item["sourceRepo"] = source_repo

        namespace_uri = first_iri_value(graph, subject, OKG.namespaceURI)
        if namespace_uri:
            item["namespaceURI"] = namespace_uri

        part_of = first_literal_value(graph, subject, OKG.partOf)
        if part_of:
            item["partOf"] = part_of

        creators = creators_for_resource(graph, subject)
        if creators:
            item["creators"] = creators

        licenses = license_labels_for_resource(graph, subject)
        if licenses:
            item["licenses"] = licenses

        if include_software_fields:
            latest_version = first_literal_value(graph, subject, OKG.latestVersion)
            if latest_version:
                item["latestVersion"] = latest_version
            release_date = first_literal_value(graph, subject, OKG.releaseDate)
            if release_date:
                item["releaseDate"] = release_date
            software_type_iri = first_iri_value(graph, subject, OKG.softwareType)
            if software_type_iri:
                software_type_label = first_literal_value(graph, URIRef(software_type_iri), RDFS.label)
                if software_type_label:
                    item["softwareType"] = software_type_label
            programming_languages = all_literal_values(graph, subject, OKG.programmingLanguage)
            if programming_languages:
                item["programmingLanguages"] = programming_languages

        related_tools = related_tools_for_resource(graph, subject)
        if related_tools:
            item["relatedTools"] = related_tools

        items.append(item)

    items.sort(key=lambda value: (str(value["title"]).casefold(), str(value["wikidataId"])))
    return items


def build_json_payload(
    graph: Graph,
    allowed_types: set[URIRef],
    include_software_fields: bool,
    generated_at: str,
    resource_type_labels: dict[URIRef, str],
) -> dict[str, object]:
    return {
        "generatedAt": generated_at,
        "items": extract_items_from_graph(
            graph,
            allowed_types,
            include_software_fields,
            resource_type_labels,
        ),
    }


def build_graph(
    records: dict[str, ResourceRecord],
    license_labels: dict[str, str],
    creator_labels: dict[str, str],
    human_creators: set[str],
    person_identifiers: dict[str, dict[str, str]],
    include_software_fields: bool,
    dataset_path: str,
    slug_registry: dict[str, str],
    programming_language_labels: dict[str, str] | None = None,
    category_vocabulary: ControlledVocabulary | None = None,
    software_type_vocabulary: ControlledVocabulary | None = None,
) -> Graph:
    graph = Graph()
    graph.bind("okg", OKG)
    graph.bind("rdf", RDF)
    graph.bind("rdfs", RDFS)
    graph.bind("xsd", XSD)

    for record in sorted(records.values(), key=lambda row: row.label.casefold()):
        if not record.label or not record.label.strip():
            raise ValueError(f"Cannot build RDF without a label for {record.item_iri}")
        qid = qid_from_wikidata_iri(record.item_iri)
        resource_iri = mint_resource_iri(dataset_path, slug_registry[qid])

        for rdf_type in sorted(record.types, key=str):
            graph.add((resource_iri, RDF.type, rdf_type))

        graph.add((resource_iri, RDFS.label, Literal(record.label)))
        graph.add((resource_iri, OKG.title, Literal(record.label)))
        graph.add((resource_iri, OKG.wikidataId, URIRef(wikidata_page_iri(record.item_iri))))
        if record.description:
            graph.add((resource_iri, OKG.description, Literal(record.description)))
        for alias in sorted(record.aliases):
            graph.add((resource_iri, OKG.alias, Literal(alias)))
        if record.category:
            if category_vocabulary is None or record.category not in category_vocabulary.by_iri:
                raise ValueError(f"Unknown curated category for {record.item_iri}: {record.category}")
            category = category_vocabulary.by_iri[record.category]
            graph.add((resource_iri, OKG.category, category.iri))
            graph.add((category.iri, RDF.type, OKG.Category))
            graph.add((category.iri, RDFS.label, Literal(category.label)))

        if record.homepages:
            homepage = sorted(record.homepages)[0]
            graph.add((resource_iri, OKG.homepage, URIRef(homepage)))

        if record.source_repos:
            source_repo = sorted(record.source_repos)[0]
            graph.add((resource_iri, OKG.sourceRepo, URIRef(source_repo)))

        if record.namespace_uris:
            namespace_uri = sorted(record.namespace_uris)[0]
            graph.add((resource_iri, OKG.namespaceURI, URIRef(namespace_uri)))

        for parent_entity in sorted(record.part_of_entities):
            graph.add((resource_iri, DCTERMS.isPartOf, URIRef(parent_entity)))

        for source_type in sorted(record.source_types):
            graph.add((resource_iri, OKG.sourceType, URIRef(source_type)))

        for used_entity in sorted(record.uses_entities):
            graph.add((resource_iri, OKG.uses, URIRef(used_entity)))

        if record.part_of_labels:
            part_of = sorted(record.part_of_labels)[0]
            graph.add((resource_iri, OKG.partOf, Literal(part_of)))

        for creator_iri in sorted(record.creators):
            creator_label = creator_labels.get(creator_iri)
            local_creator_iri = mint_creator_iri(creator_label, creator_iri)
            graph.add((resource_iri, OKG.creator, local_creator_iri))
            if creator_label:
                graph.add((local_creator_iri, RDFS.label, Literal(creator_label)))
            is_human = creator_iri in human_creators
            creator_type = OKG.Person if is_human else OKG.Organization
            graph.add((local_creator_iri, RDF.type, creator_type))
            graph.add((local_creator_iri, OKG.wikidataId, URIRef(wikidata_page_iri(creator_iri))))
            if is_human:
                identifiers = person_identifiers.get(canonical_entity_iri(creator_iri), {})
                if identifiers.get("github"):
                    graph.add((local_creator_iri, OKG.githubProfile, URIRef(identifiers["github"])))
                if identifiers.get("scholar"):
                    graph.add((local_creator_iri, OKG.googleScholarProfile, URIRef(identifiers["scholar"])))

        for license_iri in sorted(record.licenses):
            license_label = license_labels.get(license_iri)
            local_license_iri = mint_license_iri(license_label, license_iri)
            graph.add((resource_iri, OKG.hasLicense, local_license_iri))
            if license_label:
                graph.add((local_license_iri, RDFS.label, Literal(license_label)))
                graph.add((local_license_iri, OKG.licenseName, Literal(license_label)))
            graph.add((local_license_iri, RDF.type, OKG.License))

        if include_software_fields:
            if record.latest_version:
                graph.add((resource_iri, OKG.latestVersion, Literal(record.latest_version)))
            if record.release_date:
                graph.add(
                    (
                        resource_iri,
                        OKG.releaseDate,
                        Literal(record.release_date.isoformat(), datatype=XSD.date),
                    )
                )
            if record.software_type:
                if (
                    software_type_vocabulary is None
                    or record.software_type not in software_type_vocabulary.by_iri
                ):
                    raise ValueError(
                        f"Unknown curated software type for {record.item_iri}: {record.software_type}"
                    )
                software_type = software_type_vocabulary.by_iri[record.software_type]
                graph.add((resource_iri, OKG.softwareType, software_type.iri))
                graph.add((software_type.iri, RDF.type, OKG.SoftwareType))
                graph.add((software_type.iri, RDFS.label, Literal(software_type.label)))

            for language_iri in sorted(record.programming_languages):
                language_label = (programming_language_labels or {}).get(language_iri)
                if language_label:
                    graph.add((resource_iri, OKG.programmingLanguage, Literal(language_label)))

    return graph


def write_graph_atomic(graph: Graph, destination: Path) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_suffix(destination.suffix + ".tmp")
        serialized = graph.serialize(format="turtle")

        # Hard-fail if emitted TTL is not parseable by rdflib.
        validation_graph = Graph()
        validation_graph.parse(data=serialized, format="turtle")

        temp_path.write_text(serialized, encoding="utf-8")
        temp_path.replace(destination)
    except WDQSError:
        raise
    except Exception as exc:
        raise WDQSError(f"Failed to write/validate Turtle output {destination}: {exc}") from exc


def write_json_atomic(payload: dict[str, object], destination: Path) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_suffix(destination.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(destination)
    except Exception as exc:
        raise WDQSError(f"Failed to write JSON output {destination}: {exc}") from exc


def ensure_non_empty_results(
    ontology_records: dict[str, ResourceRecord],
    software_records: dict[str, ResourceRecord],
) -> None:
    if not ontology_records:
        raise WDQSError("Ontology query returned zero resources; refusing to overwrite data files.")
    if not software_records:
        raise WDQSError("Software query returned zero resources; refusing to overwrite data files.")


def record_source_retrieved_at() -> None:
    """Record when the complete Wikidata extraction finished, when requested."""
    destination_value = os.getenv("OKG_SOURCE_RETRIEVED_AT_FILE")
    if not destination_value:
        return
    destination = Path(destination_value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    timestamp = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(timestamp + "\n", encoding="utf-8")
    temporary.replace(destination)


def run() -> int:
    configure_logging()

    try:
        category_vocabulary = load_controlled_vocabulary(CATEGORIES_VOCAB_PATH)
        software_type_vocabulary = load_controlled_vocabulary(SOFTWARE_TYPES_VOCAB_PATH)
        source_mappings = load_source_mappings(SOURCES_PATH)
        curated_assignments = load_curated_assignments(
            CURATION_PATH,
            category_vocabulary,
            software_type_vocabulary,
        )
    except (OSError, SemanticConfigError) as exc:
        logging.warning("Semantic configuration is invalid: %s", exc)
        logging.warning("No data files were modified.")
        return 1

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/sparql-results+json",
            "User-Agent": USER_AGENT,
        }
    )

    try:
        ontology_rows: list[dict] = []
        for type_qid in source_mappings.class_ids_for(ONTOLOGIES_DATASET):
            logging.info("Querying Wikidata for class %s", type_qid)
            query = build_type_base_query(type_qid, source_mappings)
            typed_rows = run_wdqs_query(session, query, f"class {type_qid} query")
            for row in typed_rows:
                row["matchedTypeQid"] = {"type": "literal", "value": type_qid}
            ontology_rows.extend(typed_rows)
            time.sleep(QUERY_PAUSE_SECONDS)

        raw_ontology_qids = set(ontology_candidate_facts(ontology_rows))
        raw_ontology_count = len(raw_ontology_qids)
        ontology_rows, eligibility = filter_ontology_rows(
            ontology_rows,
            source_mappings.eligibility_policy_for(ONTOLOGIES_DATASET),
        )
        logging.info(
            "Ontology eligibility retained %d of %d raw candidates; excluded %d declared and %d by rule",
            len(eligibility.eligible_qids),
            raw_ontology_count,
            len(eligibility.declared_exclusion_qids),
            len(eligibility.rule_exclusion_qids),
        )

        logging.info("Querying Wikidata for software base fields")
        software_base_rows = run_wdqs_query(
            session,
            build_software_base_query(source_mappings),
            "software base query",
        )
        raw_software_qids = {
            qid_from_wikidata_iri(item)
            for row in software_base_rows
            if (item := binding_value(row, "item"))
        }

        captured_cohort = raw_ontology_qids | raw_software_qids
        logging.info(
            "Auditing direct IRI-valued Wikidata claims for %d captured records",
            len(captured_cohort),
        )
        direct_iri_edges = fetch_direct_iri_edges(session, captured_cohort)

        time.sleep(QUERY_PAUSE_SECONDS)
        logging.info("Querying Wikidata for software versions and release dates")
        software_version_rows = run_wdqs_query(
            session,
            build_software_version_query(source_mappings),
            "software version query",
        )

        label_entities = set()
        label_entities.update(collect_entity_iris(ontology_rows, "item"))
        label_entities.update(collect_entity_iris(ontology_rows, "license"))
        label_entities.update(collect_entity_iris(ontology_rows, "partOfEntity"))
        label_entities.update(collect_entity_iris(ontology_rows, "creator"))
        label_entities.update(collect_entity_iris(software_base_rows, "item"))
        label_entities.update(collect_entity_iris(software_base_rows, "license"))
        label_entities.update(collect_entity_iris(software_base_rows, "partOfEntity"))
        label_entities.update(collect_entity_iris(software_base_rows, "creator"))
        label_entities.update(collect_entity_iris(software_base_rows, "programmingLanguage"))

        time.sleep(QUERY_PAUSE_SECONDS)
        logging.info("Querying Wikidata for labels of %d referenced entities", len(label_entities))
        labels, descriptions, entity_aliases = fetch_entity_labels(session, label_entities)

        creator_entities = set()
        creator_entities.update(collect_entity_iris(ontology_rows, "creator"))
        creator_entities.update(collect_entity_iris(software_base_rows, "creator"))

        time.sleep(QUERY_PAUSE_SECONDS)
        logging.info("Checking creator type (Person vs Organization) for %d entities", len(creator_entities))
        human_creators = fetch_human_creators(session, creator_entities, source_mappings)

        time.sleep(QUERY_PAUSE_SECONDS)
        logging.info("Fetching person identifiers (GitHub, Google Scholar) for %d human creators", len(human_creators))
        person_identifiers = fetch_person_identifiers(session, human_creators, source_mappings)

    except WDQSError as exc:
        logging.warning("Wikidata fetch failed: %s", exc)
        logging.warning("No data files were modified.")
        return 1

    record_source_retrieved_at()

    ontology_records, ontology_license_labels, ontology_creator_labels = parse_ontology_rows(
        ontology_rows,
        labels,
        descriptions,
        entity_aliases,
        source_mappings.class_target_map(ONTOLOGIES_DATASET),
    )
    (
        software_records,
        software_license_labels,
        software_creator_labels,
        software_programming_language_labels,
    ) = parse_software_rows(
        software_base_rows,
        labels,
        descriptions,
        entity_aliases,
    )
    latest_versions = pick_latest_version_rows(software_version_rows)
    apply_declared_relationships(ontology_records, direct_iri_edges, source_mappings)
    apply_declared_relationships(software_records, direct_iri_edges, source_mappings)

    for item_iri, (version, release_dt) in latest_versions.items():
        record = software_records.get(item_iri)
        if record is None:
            continue
        record.latest_version = version
        record.release_date = release_dt

    try:
        ensure_non_empty_results(ontology_records, software_records)

        uri_registry = load_uri_registry()
        assign_slugs(ontology_records, "resource", uri_registry)
        assign_slugs(software_records, "software", uri_registry)

        category_mapping = curated_assignments.categories
        newly_classified_count, failed_classification_count = classify_missing_ontology_categories(
            ontology_records=ontology_records,
            category_mapping=category_mapping,
            vocabulary=category_vocabulary,
        )
        if newly_classified_count:
            logging.info(
                "Classified %d newly discovered ontology items into categories.",
                newly_classified_count,
            )
        if failed_classification_count:
            logging.warning(
                "%d ontology items remain uncategorized after this run.",
                failed_classification_count,
            )

        software_type_mapping = curated_assignments.software_types
        newly_typed_count, failed_typing_count = classify_missing_software_types(
            software_records=software_records,
            software_type_mapping=software_type_mapping,
            vocabulary=software_type_vocabulary,
        )
        if newly_typed_count:
            logging.info(
                "Classified %d newly discovered software items into software types.",
                newly_typed_count,
            )
        if failed_typing_count:
            logging.warning(
                "%d software items remain untyped after this run.",
                failed_typing_count,
            )

        ontology_graph = build_graph(
            records=ontology_records,
            license_labels=ontology_license_labels,
            creator_labels=ontology_creator_labels,
            human_creators=human_creators,
            person_identifiers=person_identifiers,
            include_software_fields=False,
            dataset_path="resource",
            slug_registry=uri_registry["resource"],
            category_vocabulary=category_vocabulary,
        )
        software_graph = build_graph(
            records=software_records,
            license_labels=software_license_labels,
            creator_labels=software_creator_labels,
            human_creators=human_creators,
            person_identifiers=person_identifiers,
            include_software_fields=True,
            dataset_path="software",
            slug_registry=uri_registry["software"],
            programming_language_labels=software_programming_language_labels,
            software_type_vocabulary=software_type_vocabulary,
        )
        similarity_context = build_similarity_context((ontology_graph, software_graph))
        ontology_related_diagnostics = add_related_tools(
            ontology_graph,
            dataset="resource",
            context=similarity_context,
        )
        software_related_diagnostics = add_related_tools(
            software_graph,
            dataset="software",
            context=similarity_context,
        )
        ontology_related_diagnostics["pinnedExemplars"] = verify_recommendation_exemplars(
            ontology_graph,
            ontology_records,
            uri_registry["resource"],
            direct_iri_edges,
            source_mappings,
        )
        cohort_catalogs: dict[str, frozenset[str]] = {
            qid: frozenset(
                catalog
                for catalog, members in (
                    ("resource", raw_ontology_qids),
                    ("software", raw_software_qids),
                )
                if qid in members
            )
            for qid in sorted(captured_cohort)
        }
        source_audit = audit_document(
            direct_iri_edges,
            cohort_catalogs,
            reviewed_property_ids={
                wikidata_property(source_mappings, "sourceType", value_kind="iri"),
                wikidata_property(source_mappings, "usesEntity", value_kind="iri"),
                wikidata_property(source_mappings, "partOfEntity", value_kind="iri"),
            },
            labels={qid_from_wikidata_iri(iri): label for iri, label in labels.items()},
        )
        write_diagnostics_atomic(
            diagnostics_document(
                (ontology_related_diagnostics, software_related_diagnostics),
                RELATED_SIMILARITY_CONFIG,
                source_audit=source_audit,
            ),
            RELATED_DIAGNOSTICS_OUT,
        )
        logging.info(
            "Related-resource scoring selected %d resource and %d software links; diagnostics: %s",
            ontology_related_diagnostics["selectedRelationshipCount"],
            software_related_diagnostics["selectedRelationshipCount"],
            RELATED_DIAGNOSTICS_OUT,
        )

        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        resource_type_labels = source_mappings.projection_type_labels
        ontologies_json = build_json_payload(
            graph=ontology_graph,
            allowed_types={OKG.Ontology, OKG.ControlledVocabulary, OKG.Taxonomy, OKG.KnowledgeGraph, OKG.OntologyLanguage},
            include_software_fields=False,
            generated_at=generated_at,
            resource_type_labels=resource_type_labels,
        )
        software_json = build_json_payload(
            graph=software_graph,
            allowed_types={OKG.Software},
            include_software_fields=True,
            generated_at=generated_at,
            resource_type_labels=resource_type_labels,
        )

        write_graph_atomic(ontology_graph, ONTOLOGIES_OUT)
        write_graph_atomic(software_graph, SOFTWARE_OUT)
        write_json_atomic(ontologies_json, ONTOLOGIES_JSON_OUT)
        write_json_atomic(software_json, SOFTWARE_JSON_OUT)
        write_curated_assignments_atomic(
            CURATION_PATH,
            curated_assignments,
            uri_registry,
            category_vocabulary,
            software_type_vocabulary,
        )
        write_projection_json_atomic(
            classification_label_projection(category_mapping, category_vocabulary),
            CATEGORIES_JSON_OUT,
        )
        write_projection_json_atomic(
            classification_label_projection(software_type_mapping, software_type_vocabulary),
            SOFTWARE_TYPES_JSON_OUT,
        )
        write_projection_json_atomic(
            controlled_vocabulary_projection(category_vocabulary, software_type_vocabulary),
            CONTROLLED_VOCABULARIES_JSON_OUT,
        )
        save_uri_registry(uri_registry)

        logging.info("Wrote %s (%d triples)", ONTOLOGIES_OUT, len(ontology_graph))
        logging.info("Wrote %s (%d triples)", SOFTWARE_OUT, len(software_graph))
        logging.info("Wrote %s (%d items)", ONTOLOGIES_JSON_OUT, len(ontologies_json["items"]))
        logging.info("Wrote %s (%d items)", SOFTWARE_JSON_OUT, len(software_json["items"]))
        return 0
    except (WDQSError, SemanticConfigError, OSError, ValueError) as exc:
        logging.warning("Data integrity guard triggered: %s", exc)
        logging.warning("No data files were modified.")
        return 1


if __name__ == "__main__":
    sys.exit(run())
