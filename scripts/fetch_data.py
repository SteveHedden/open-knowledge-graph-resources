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
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from category_classifier import (
    CATEGORY_SET,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MODEL,
    SOFTWARE_TYPE_DEFINITIONS,
    SOFTWARE_TYPE_OPTIONS,
    SOFTWARE_TYPE_SET,
    classify_items,
    load_categories,
    write_categories_atomic,
)

WDQS_URL = "https://query.wikidata.org/sparql"
OKG = Namespace("https://openknowledgegraphs.com/ontology#")
BASE_URL = "https://openknowledgegraphs.com"

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
ONTOLOGIES_OUT = DATA_DIR / "ontologies.ttl"
SOFTWARE_OUT = DATA_DIR / "software.ttl"
ONTOLOGIES_JSON_OUT = DATA_DIR / "ontologies.json"
SOFTWARE_JSON_OUT = DATA_DIR / "software.json"
CATEGORIES_JSON_OUT = DATA_DIR / "categories.json"
SOFTWARE_TYPES_JSON_OUT = DATA_DIR / "software_types.json"
URI_REGISTRY_OUT = DATA_DIR / "uri_registry.json"
PAGE_QIDS_LEGACY = DATA_DIR / "page_qids.json"

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
HOMEPAGE_COVERAGE_WARN_THRESHOLD = float(os.getenv("HOMEPAGE_COVERAGE_WARN_THRESHOLD", "0.30"))
ITEM_COUNT_DROP_WARN_THRESHOLD = float(os.getenv("ITEM_COUNT_DROP_WARN_THRESHOLD", "0.50"))
CATEGORY_CLASSIFICATION_BATCH_SIZE = int(
    os.getenv("CATEGORY_CLASSIFICATION_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))
)
CATEGORY_CLASSIFICATION_MODEL = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)

QID_TO_OSC_CLASS = {
    "Q324254": OKG.Ontology,
    "Q1469824": OKG.ControlledVocabulary,
    "Q8269924": OKG.Taxonomy,
    "Q33002955": OKG.KnowledgeGraph,
    "Q7095059": OKG.OntologyLanguage,
    "Q7247749": OKG.ControlledVocabulary,  # product classification
}
RESOURCE_TYPE_LABELS = {
    OKG.Ontology: "Ontology",
    OKG.ControlledVocabulary: "ControlledVocabulary",
    OKG.Taxonomy: "Taxonomy",
    OKG.KnowledgeGraph: "KnowledgeGraph",
    OKG.OntologyLanguage: "OntologyLanguage",
    OKG.Software: "Software",
}

CATEGORY_LABEL_TO_IRI: dict[str, URIRef] = {
    "Life Sciences & Healthcare": OKG.LifeSciencesHealthcare,
    "Geospatial": OKG.Geospatial,
    "Government & Public Sector": OKG.GovernmentPublicSector,
    "International Development": OKG.InternationalDevelopment,
    "Finance & Business": OKG.FinanceBusiness,
    "Library & Cultural Heritage": OKG.LibraryCulturalHeritage,
    "Technology & Web": OKG.TechnologyWeb,
    "Environment & Agriculture": OKG.EnvironmentAgriculture,
    "General / Cross-domain": OKG.GeneralCrossDomain,
}

SOFTWARE_TYPE_LABEL_TO_IRI: dict[str, URIRef] = {
    "Graph Database": OKG.GraphDatabase,
    "SPARQL Tooling": OKG.SparqlTooling,
    "Ontology Engineering": OKG.OntologyEngineering,
    "Reasoning & Inference": OKG.ReasoningInference,
    "RDF Data Mapping / ETL": OKG.DataMappingETL,
    "Developer Library": OKG.DeveloperLibrary,
    "Knowledge Graph Construction": OKG.KnowledgeGraphConstruction,
    "AI Agent Tooling": OKG.AIAgentTooling,
    "Visualization": OKG.Visualization,
    "Stream Processing": OKG.StreamProcessing,
}

TYPE_BASE_QUERY_TEMPLATE = """
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT DISTINCT ?item ?officialWebsite ?sourceCodeRepo ?namespaceURI ?license ?partOfEntity ?creator
WHERE {
  ?item wdt:P31/wdt:P279* wd:__TYPE_QID__ .
  OPTIONAL { ?item wdt:P856 ?officialWebsite . }
  OPTIONAL { ?item wdt:P1324 ?sourceCodeRepo . }
  OPTIONAL { ?item wdt:P7510 ?namespaceURI . }
  OPTIONAL { ?item wdt:P275 ?license . }
  OPTIONAL { ?item wdt:P361 ?partOfEntity . }
  OPTIONAL {
    { ?item wdt:P170 ?creator . } UNION
    { ?item wdt:P50 ?creator . }
  }
}
"""

SOFTWARE_BASE_QUERY = """
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT DISTINCT ?item ?officialWebsite ?sourceCodeRepo ?license ?partOfEntity ?creator ?programmingLanguage
WHERE {
  {
    ?item wdt:P31/wdt:P279* wd:Q124653107 .  # semantic web software
  }
  UNION
  {
    ?item wdt:P31/wdt:P279* wd:Q140639670 .  # AI agent memory software
  }
  UNION
  {
    ?item wdt:P31/wdt:P279* wd:Q595971 .  # graph database
  }
  UNION
  {
    ?item wdt:P31/wdt:P279* wd:Q137916409 .  # graph database management system (current preferred class for most graph DB engines, incl. Neo4j/Wikibase; not a subclass of Q595971, so needs its own branch)
  }
  OPTIONAL { ?item wdt:P856 ?officialWebsite . }
  OPTIONAL { ?item wdt:P1324 ?sourceCodeRepo . }
  OPTIONAL { ?item wdt:P275 ?license . }
  OPTIONAL { ?item wdt:P361 ?partOfEntity . }
  OPTIONAL {
    { ?item wdt:P178 ?creator . } UNION
    { ?item wdt:P170 ?creator . } UNION
    { ?item wdt:P50 ?creator . }
  }
  OPTIONAL { ?item wdt:P277 ?programmingLanguage . }
}
"""

SOFTWARE_VERSION_QUERY = """
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>

SELECT ?item ?version ?pubDate
WHERE {
  {
    ?item wdt:P31/wdt:P279* wd:Q124653107 .  # semantic web software
  }
  UNION
  {
    ?item wdt:P31/wdt:P279* wd:Q140639670 .  # AI agent memory software
  }
  UNION
  {
    ?item wdt:P31/wdt:P279* wd:Q595971 .  # graph database
  }
  UNION
  {
    ?item wdt:P31/wdt:P279* wd:Q137916409 .  # graph database management system (current preferred class for most graph DB engines, incl. Neo4j/Wikibase; not a subclass of Q595971, so needs its own branch)
  }
  ?item p:P348 ?verStmt .
  ?verStmt ps:P348 ?version .
  OPTIONAL { ?verStmt pq:P577 ?pubDate . }
}
"""

LOCAL_NAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9]+")
QID_RE = re.compile(r"(Q\d+)$")
REPO_HOST_RE = re.compile(r"^https?://(github\.com|gitlab\.com|bitbucket\.org|codeberg\.org)/", re.IGNORECASE)


class WDQSError(RuntimeError):
    """Raised when WDQS data cannot be fetched reliably."""


@dataclass
class ResourceRecord:
    item_iri: str
    label: str
    description: str | None = None
    category: str | None = None
    software_type: str | None = None
    types: set[URIRef] = field(default_factory=set)
    homepages: set[str] = field(default_factory=set)
    source_repos: set[str] = field(default_factory=set)
    namespace_uris: set[str] = field(default_factory=set)
    licenses: set[str] = field(default_factory=set)
    part_of_labels: set[str] = field(default_factory=set)
    creators: set[str] = field(default_factory=set)
    programming_languages: set[str] = field(default_factory=set)
    latest_version: str | None = None
    release_date: date | None = None


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


def is_repo_url(url: str) -> bool:
    return bool(REPO_HOST_RE.search(url))


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


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def fetch_entity_labels(
    session: requests.Session,
    entity_iris: set[str],
) -> tuple[dict[str, str], dict[str, str]]:
    if not entity_iris:
        return {}, {}

    labels: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    description_lang: dict[str, str] = {}
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

        time.sleep(QUERY_PAUSE_SECONDS)

    return labels, descriptions


def fetch_human_creators(session: requests.Session, creator_iris: set[str]) -> set[str]:
    """Return the subset of creator_iris that are instance-of human (Q5) on Wikidata.

    schema.org's `creator` property only accepts Person or Organization
    (https://schema.org/creator); everything not identified as human here
    is treated as an Organization when the catalog is rendered.
    """
    if not creator_iris:
        return set()

    humans: set[str] = set()
    sorted_entities = sorted(canonical_entity_iri(iri) for iri in creator_iris)

    for chunk in chunked(sorted_entities, LABEL_QUERY_BATCH_SIZE):
        values = " ".join(f"<{entity}>" for entity in chunk)
        human_query = f"""
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT DISTINCT ?entity
WHERE {{
  VALUES ?entity {{ {values} }}
  ?entity wdt:P31 wd:Q5 .
}}
"""
        human_rows = run_wdqs_query(session, human_query, "creator human-check query")
        for row in human_rows:
            entity_iri = binding_value(row, "entity")
            if entity_iri:
                humans.add(canonical_entity_iri(entity_iri))
        time.sleep(QUERY_PAUSE_SECONDS)

    return humans


def fetch_person_identifiers(session: requests.Session, human_iris: set[str]) -> dict[str, dict[str, str]]:
    """Fetch person-specific identifiers (GitHub username, Google Scholar author ID)
    for creators already confirmed human. Organizations are deliberately excluded —
    these identifiers only make sense as claims about an individual.
    """
    if not human_iris:
        return {}

    identifiers: dict[str, dict[str, str]] = {}
    sorted_entities = sorted(canonical_entity_iri(iri) for iri in human_iris)

    for chunk in chunked(sorted_entities, LABEL_QUERY_BATCH_SIZE):
        values = " ".join(f"<{entity}>" for entity in chunk)
        identifier_query = f"""
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT DISTINCT ?entity ?github ?scholar
WHERE {{
  VALUES ?entity {{ {values} }}
  OPTIONAL {{ ?entity wdt:P2037 ?github . }}
  OPTIONAL {{ ?entity wdt:P1960 ?scholar . }}
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

        type_qid = binding_value(row, "matchedTypeQid")
        if type_qid:
            osc_type = QID_TO_OSC_CLASS.get(type_qid)
            if osc_type is not None:
                record.types.add(osc_type)

        homepage = binding_value(row, "officialWebsite")
        if homepage:
            if is_repo_url(homepage):
                record.source_repos.add(homepage)
            else:
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
        record.types.add(OKG.Software)

        homepage = binding_value(row, "officialWebsite")
        if homepage:
            if is_repo_url(homepage):
                record.source_repos.add(homepage)
            else:
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

        # Fallback when no P577 qualifier exists on any P348 statement.
        version = sorted((candidate[0] for candidate in candidates), reverse=True)[0]
        results[item_iri] = (version, None)

    return results


def apply_existing_categories(
    ontology_records: dict[str, ResourceRecord],
    category_mapping: dict[str, str],
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for item_iri, record in ontology_records.items():
        qid = qid_from_wikidata_iri(item_iri)
        existing_category = category_mapping.get(qid)
        if existing_category in CATEGORY_SET:
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
    category_mapping: dict[str, str],
) -> tuple[int, int]:
    missing_items = apply_existing_categories(ontology_records, category_mapping)
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
    )

    for qid, category in classified.items():
        category_mapping[qid] = category

    if classified:
        try:
            write_categories_atomic(CATEGORIES_JSON_OUT, category_mapping)
        except OSError as exc:
            logging.warning("Unable to write category mapping %s: %s", CATEGORIES_JSON_OUT, exc)

    for item_iri, record in ontology_records.items():
        qid = qid_from_wikidata_iri(item_iri)
        category = category_mapping.get(qid)
        if category in CATEGORY_SET:
            record.category = category

    if failed_qids:
        logging.warning(
            "Category classification failed for %d ontology items; leaving them uncategorized.",
            len(failed_qids),
        )

    return len(classified), len(failed_qids)


def apply_existing_software_types(
    software_records: dict[str, ResourceRecord],
    software_type_mapping: dict[str, str],
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for item_iri, record in software_records.items():
        qid = qid_from_wikidata_iri(item_iri)
        existing_type = software_type_mapping.get(qid)
        if existing_type in SOFTWARE_TYPE_SET:
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
    software_type_mapping: dict[str, str],
) -> tuple[int, int]:
    missing_items = apply_existing_software_types(software_records, software_type_mapping)
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
        category_options=SOFTWARE_TYPE_OPTIONS,
        category_set=SOFTWARE_TYPE_SET,
        definitions=SOFTWARE_TYPE_DEFINITIONS,
        entity_label="knowledge graph or AI agent memory software resource",
        fallback_instruction="Pick the single closest match when unsure.",
    )

    for qid, software_type in classified.items():
        software_type_mapping[qid] = software_type

    if classified:
        try:
            write_categories_atomic(SOFTWARE_TYPES_JSON_OUT, software_type_mapping)
        except OSError as exc:
            logging.warning("Unable to write software type mapping %s: %s", SOFTWARE_TYPES_JSON_OUT, exc)

    for item_iri, record in software_records.items():
        qid = qid_from_wikidata_iri(item_iri)
        software_type = software_type_mapping.get(qid)
        if software_type in SOFTWARE_TYPE_SET:
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


def extract_items_from_graph(
    graph: Graph,
    allowed_types: set[URIRef],
    include_software_fields: bool,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    subjects = {subject for subject in graph.subjects(predicate=OKG.wikidataId)}

    for subject in subjects:
        if not isinstance(subject, URIRef):
            continue

        type_labels = sorted(
            {
                RESOURCE_TYPE_LABELS[rdf_type]
                for rdf_type in graph.objects(subject, RDF.type)
                if rdf_type in allowed_types
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

        items.append(item)

    items.sort(key=lambda value: (str(value["title"]).casefold(), str(value["wikidataId"])))
    return items


def build_json_payload(
    graph: Graph,
    allowed_types: set[URIRef],
    include_software_fields: bool,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generatedAt": generated_at,
        "items": extract_items_from_graph(graph, allowed_types, include_software_fields),
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
) -> Graph:
    graph = Graph()
    graph.bind("okg", OKG)
    graph.bind("rdf", RDF)
    graph.bind("rdfs", RDFS)
    graph.bind("xsd", XSD)

    for record in sorted(records.values(), key=lambda row: row.label.casefold()):
        qid = qid_from_wikidata_iri(record.item_iri)
        resource_iri = mint_resource_iri(dataset_path, slug_registry[qid])

        for rdf_type in sorted(record.types, key=str):
            graph.add((resource_iri, RDF.type, rdf_type))

        graph.add((resource_iri, RDFS.label, Literal(record.label)))
        graph.add((resource_iri, OKG.title, Literal(record.label)))
        graph.add((resource_iri, OKG.wikidataId, URIRef(wikidata_page_iri(record.item_iri))))
        if record.description:
            graph.add((resource_iri, OKG.description, Literal(record.description)))
        if record.category and record.category in CATEGORY_LABEL_TO_IRI:
            category_iri = CATEGORY_LABEL_TO_IRI[record.category]
            graph.add((resource_iri, OKG.category, category_iri))
            graph.add((category_iri, RDF.type, OKG.Category))
            graph.add((category_iri, RDFS.label, Literal(record.category)))

        if record.homepages:
            homepage = sorted(record.homepages)[0]
            graph.add((resource_iri, OKG.homepage, URIRef(homepage)))

        if record.source_repos:
            source_repo = sorted(record.source_repos)[0]
            graph.add((resource_iri, OKG.sourceRepo, URIRef(source_repo)))

        if record.namespace_uris:
            namespace_uri = sorted(record.namespace_uris)[0]
            graph.add((resource_iri, OKG.namespaceURI, URIRef(namespace_uri)))

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
            if record.software_type and record.software_type in SOFTWARE_TYPE_LABEL_TO_IRI:
                software_type_iri = SOFTWARE_TYPE_LABEL_TO_IRI[record.software_type]
                graph.add((resource_iri, OKG.softwareType, software_type_iri))
                graph.add((software_type_iri, RDF.type, OKG.SoftwareType))
                graph.add((software_type_iri, RDFS.label, Literal(record.software_type)))

            for language_iri in sorted(record.programming_languages):
                language_label = (programming_language_labels or {}).get(language_iri)
                if language_label:
                    graph.add((resource_iri, OKG.programmingLanguage, Literal(language_label)))

    return graph


def load_existing_payload(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        loaded = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Could not read previous payload %s: %s", path, exc)
        return None
    if not isinstance(loaded, dict):
        logging.warning("Previous payload %s has unexpected format and will be ignored.", path)
        return None
    return loaded


def payload_item_list(payload: dict[str, object] | None) -> list[dict[str, object]]:
    if not payload:
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def item_count(items: list[dict[str, object]]) -> int:
    return len(items)


def homepage_coverage_ratio(items: list[dict[str, object]]) -> float:
    total = len(items)
    if total == 0:
        return 0.0
    with_homepage = 0
    for item in items:
        homepage = item.get("homepage")
        if isinstance(homepage, str) and homepage.strip():
            with_homepage += 1
    return with_homepage / total


def warn_on_quality_drift(
    dataset_name: str,
    new_payload: dict[str, object],
    previous_payload: dict[str, object] | None,
) -> None:
    new_items = payload_item_list(new_payload)
    new_count = item_count(new_items)
    new_coverage = homepage_coverage_ratio(new_items)

    if new_count > 0 and new_coverage < HOMEPAGE_COVERAGE_WARN_THRESHOLD:
        logging.warning(
            "%s: homepage coverage is low at %.1f%% (threshold %.1f%%).",
            dataset_name,
            new_coverage * 100.0,
            HOMEPAGE_COVERAGE_WARN_THRESHOLD * 100.0,
        )

    previous_items = payload_item_list(previous_payload)
    previous_count = item_count(previous_items)
    if previous_count == 0:
        return

    drop_ratio = (previous_count - new_count) / previous_count
    if drop_ratio > ITEM_COUNT_DROP_WARN_THRESHOLD:
        logging.warning(
            "%s: item count dropped by %.1f%% (%d -> %d), above %.1f%% warning threshold.",
            dataset_name,
            drop_ratio * 100.0,
            previous_count,
            new_count,
            ITEM_COUNT_DROP_WARN_THRESHOLD * 100.0,
        )


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


def run() -> int:
    configure_logging()

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/sparql-results+json",
            "User-Agent": USER_AGENT,
        }
    )

    try:
        ontology_rows: list[dict] = []
        for type_qid in QID_TO_OSC_CLASS:
            logging.info("Querying Wikidata for class %s", type_qid)
            query = TYPE_BASE_QUERY_TEMPLATE.replace("__TYPE_QID__", type_qid)
            typed_rows = run_wdqs_query(session, query, f"class {type_qid} query")
            for row in typed_rows:
                row["matchedTypeQid"] = {"type": "literal", "value": type_qid}
            ontology_rows.extend(typed_rows)
            time.sleep(QUERY_PAUSE_SECONDS)

        logging.info("Querying Wikidata for software base fields")
        software_base_rows = run_wdqs_query(session, SOFTWARE_BASE_QUERY, "software base query")

        time.sleep(QUERY_PAUSE_SECONDS)
        logging.info("Querying Wikidata for software versions and release dates")
        software_version_rows = run_wdqs_query(session, SOFTWARE_VERSION_QUERY, "software version query")

        label_entities = set()
        label_entities.update(collect_entity_iris(ontology_rows, "item"))
        label_entities.update(collect_entity_iris(ontology_rows, "license"))
        label_entities.update(collect_entity_iris(ontology_rows, "partOfEntity"))
        label_entities.update(collect_entity_iris(ontology_rows, "creator"))
        label_entities.update(collect_entity_iris(software_base_rows, "item"))
        label_entities.update(collect_entity_iris(software_base_rows, "license"))
        label_entities.update(collect_entity_iris(software_base_rows, "partOfEntity"))
        label_entities.update(collect_entity_iris(software_base_rows, "creator"))

        time.sleep(QUERY_PAUSE_SECONDS)
        logging.info("Querying Wikidata for labels of %d referenced entities", len(label_entities))
        labels, descriptions = fetch_entity_labels(session, label_entities)

        creator_entities = set()
        creator_entities.update(collect_entity_iris(ontology_rows, "creator"))
        creator_entities.update(collect_entity_iris(software_base_rows, "creator"))

        time.sleep(QUERY_PAUSE_SECONDS)
        logging.info("Checking creator type (Person vs Organization) for %d entities", len(creator_entities))
        human_creators = fetch_human_creators(session, creator_entities)

        time.sleep(QUERY_PAUSE_SECONDS)
        logging.info("Fetching person identifiers (GitHub, Google Scholar) for %d human creators", len(human_creators))
        person_identifiers = fetch_person_identifiers(session, human_creators)

    except WDQSError as exc:
        logging.warning("Wikidata fetch failed: %s", exc)
        logging.warning("No data files were modified.")
        return 1

    ontology_records, ontology_license_labels, ontology_creator_labels = parse_ontology_rows(
        ontology_rows, labels, descriptions
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
    )
    latest_versions = pick_latest_version_rows(software_version_rows)

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

        category_mapping = load_categories(CATEGORIES_JSON_OUT)
        newly_classified_count, failed_classification_count = classify_missing_ontology_categories(
            ontology_records=ontology_records,
            category_mapping=category_mapping,
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

        software_type_mapping = load_categories(SOFTWARE_TYPES_JSON_OUT, valid_set=SOFTWARE_TYPE_SET)
        newly_typed_count, failed_typing_count = classify_missing_software_types(
            software_records=software_records,
            software_type_mapping=software_type_mapping,
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
        )

        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        ontologies_json = build_json_payload(
            graph=ontology_graph,
            allowed_types={OKG.Ontology, OKG.ControlledVocabulary, OKG.Taxonomy, OKG.KnowledgeGraph, OKG.OntologyLanguage},
            include_software_fields=False,
            generated_at=generated_at,
        )
        software_json = build_json_payload(
            graph=software_graph,
            allowed_types={OKG.Software},
            include_software_fields=True,
            generated_at=generated_at,
        )

        previous_ontologies_json = load_existing_payload(ONTOLOGIES_JSON_OUT)
        previous_software_json = load_existing_payload(SOFTWARE_JSON_OUT)
        warn_on_quality_drift(
            dataset_name="ontologies",
            new_payload=ontologies_json,
            previous_payload=previous_ontologies_json,
        )
        warn_on_quality_drift(
            dataset_name="software",
            new_payload=software_json,
            previous_payload=previous_software_json,
        )

        write_graph_atomic(ontology_graph, ONTOLOGIES_OUT)
        write_graph_atomic(software_graph, SOFTWARE_OUT)
        write_json_atomic(ontologies_json, ONTOLOGIES_JSON_OUT)
        write_json_atomic(software_json, SOFTWARE_JSON_OUT)
        save_uri_registry(uri_registry)

        logging.info("Wrote %s (%d triples)", ONTOLOGIES_OUT, len(ontology_graph))
        logging.info("Wrote %s (%d triples)", SOFTWARE_OUT, len(software_graph))
        logging.info("Wrote %s (%d items)", ONTOLOGIES_JSON_OUT, len(ontologies_json["items"]))
        logging.info("Wrote %s (%d items)", SOFTWARE_JSON_OUT, len(software_json["items"]))
        return 0
    except WDQSError as exc:
        logging.warning("Data integrity guard triggered: %s", exc)
        logging.warning("No data files were modified.")
        return 1


if __name__ == "__main__":
    sys.exit(run())
