#!/usr/bin/env python3
"""Validate an OKG catalog candidate before it can be committed or published."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from pyshacl import validate as shacl_validate
from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS, SKOS

import fetch_data
import semantic_config


ROOT_DIR = Path(__file__).resolve().parent.parent
BASE_URL = "https://openknowledgegraphs.com"
OKG = semantic_config.OKG

LOSS_WARNING_THRESHOLD = 0.02
LOSS_FAILURE_THRESHOLD = 0.10
METADATA_COVERAGE_DROP_WARNING = 0.02
HOMEPAGE_COVERAGE_WARNING = 0.30

QID_RE = re.compile(r"^Q\d+$")
PID_RE = re.compile(r"^P\d+$")
WIKIDATA_PAGE_RE = re.compile(r"^https://www\.wikidata\.org/wiki/(Q\d+)$")
GENERATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HARDCODED_SOURCE_ID_RE = re.compile(r"\b[QP]\d{2,}\b")

GRAPH_PATHS = (
    "ontology.ttl",
    "vocabularies/categories.ttl",
    "vocabularies/software-types.ttl",
    "sources.ttl",
    "curation/classifications.ttl",
    "data/ontologies.ttl",
    "data/software.ttl",
)

DATASET_SPECS = {
    "resource": {
        "graph": "data/ontologies.ttl",
        "json": "data/ontologies.json",
        "registry": "resource",
        "allowed_types": {
            OKG.Ontology,
            OKG.ControlledVocabulary,
            OKG.Taxonomy,
            OKG.KnowledgeGraph,
            OKG.OntologyLanguage,
        },
        "include_software_fields": False,
        "classification_predicate": OKG.category,
        "metadata_fields": ("description", "homepage", "category", "sourceRepo"),
    },
    "software": {
        "graph": "data/software.ttl",
        "json": "data/software.json",
        "registry": "software",
        "allowed_types": {OKG.Software},
        "include_software_fields": True,
        "classification_predicate": OKG.softwareType,
        "metadata_fields": (
            "description",
            "homepage",
            "softwareType",
            "sourceRepo",
            "licenses",
            "programmingLanguages",
        ),
    },
}

JSON_IRI_FIELDS = {
    "canonicalUrl",
    "homepage",
    "sourceRepo",
    "namespaceURI",
    "wikidataId",
    "githubProfile",
    "googleScholarProfile",
}

RDF_IRI_PREDICATES = {
    DCTERMS.isPartOf,
    OKG.category,
    OKG.softwareType,
    OKG.homepage,
    OKG.hasLicense,
    OKG.wikidataId,
    OKG.creator,
    OKG.githubProfile,
    OKG.googleScholarProfile,
    OKG.namespaceURI,
    OKG.relatedTo,
    OKG.uses,
    OKG.sourceType,
    OKG.sourceRepo,
    OKG.conceptClass,
    OKG.classificationPredicate,
    OKG.sourceDataset,
    OKG.catalogDataset,
    OKG.targetTerm,
    OKG.sourceObject,
    OKG.sourcePropertyMapping,
    SKOS.inScheme,
}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str


@dataclass
class ValidationReport:
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def conforms(self) -> bool:
        return not self.errors

    def error(self, code: str, message: str) -> None:
        self.errors.append(ValidationIssue(code, message))

    def warning(self, code: str, message: str) -> None:
        self.warnings.append(ValidationIssue(code, message))

    def note(self, message: str) -> None:
        self.notes.append(message)

    def has_error(self, code: str) -> bool:
        return any(issue.code == code for issue in self.errors)


@dataclass
class BaselineSnapshot:
    payloads: dict[str, dict[str, Any]]
    registry: dict[str, dict[str, str]]
    page_qids: dict[str, dict[str, str]]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_graph(path: Path) -> Graph:
    return Graph().parse(path, format="turtle")


def absolute_iri(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(parsed.scheme)


def qid_from_value(value: str) -> str | None:
    match = re.search(r"(Q\d+)$", value.strip())
    return match.group(1) if match else None


def payload_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items")
    return items if isinstance(items, list) else []


def items_by_qid(
    dataset: str,
    payload: dict[str, Any],
    report: ValidationReport,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(payload_items(payload)):
        if not isinstance(item, dict):
            report.error("json-contract", f"{dataset} item {index} is not an object.")
            continue
        wikidata_id = item.get("wikidataId")
        qid = qid_from_value(wikidata_id) if isinstance(wikidata_id, str) else None
        if qid is None:
            report.error(
                "json-identity",
                f"{dataset} item {index} has no complete Wikidata identity IRI.",
            )
            continue
        if qid in result:
            report.error("json-identity", f"{dataset} repeats identity {qid}.")
            continue
        result[qid] = item
    return result


def load_graphs(root: Path, report: ValidationReport) -> dict[str, Graph]:
    graphs: dict[str, Graph] = {}
    for relative_path in GRAPH_PATHS:
        try:
            graphs[relative_path] = parse_graph(root / relative_path)
        except Exception as exc:
            report.error("rdf-parse", f"Could not parse {relative_path}: {exc}")
    return graphs


def validate_shacl(graphs: dict[str, Graph], report: ValidationReport) -> None:
    if any(path not in graphs for path in GRAPH_PATHS):
        return
    merged = Graph()
    for path in GRAPH_PATHS:
        merged += graphs[path]
    ontology = graphs["ontology.ttl"]
    try:
        conforms, _, results_text = shacl_validate(
            data_graph=merged,
            shacl_graph=ontology,
            ont_graph=ontology,
            inference="none",
            abort_on_first=False,
            allow_infos=False,
            allow_warnings=False,
            meta_shacl=True,
            advanced=True,
        )
    except Exception as exc:
        report.error("shacl", f"pySHACL could not execute: {exc}")
        return
    if not conforms:
        compact = " ".join(str(results_text).split())
        report.error("shacl", f"Merged catalog violates SHACL: {compact[:1200]}")


def validate_declared_schema_terms(
    ontology: Graph,
    layer_graphs: Iterable[Graph],
    report: ValidationReport,
) -> None:
    undeclared_properties: set[URIRef] = set()
    undeclared_classes: set[URIRef] = set()
    for graph in layer_graphs:
        for predicate in set(graph.predicates()):
            if str(predicate).startswith(str(OKG)) and (
                predicate,
                RDF.type,
                RDF.Property,
            ) not in ontology:
                undeclared_properties.add(predicate)
        for class_iri in set(graph.objects(None, RDF.type)):
            if (
                isinstance(class_iri, URIRef)
                and str(class_iri).startswith(str(OKG))
                and (class_iri, RDF.type, RDFS.Class) not in ontology
            ):
                undeclared_classes.add(class_iri)
    for term in sorted(undeclared_properties, key=str):
        report.error("schema-term", f"Undeclared OKG property is emitted: {term}")
    for term in sorted(undeclared_classes, key=str):
        report.error("schema-term", f"Undeclared OKG class is emitted: {term}")


def validate_public_iris(
    graphs: Iterable[Graph],
    payloads: dict[str, dict[str, Any]],
    report: ValidationReport,
) -> None:
    for graph in graphs:
        for subject, predicate, value in graph:
            for node in (subject, predicate, value):
                if isinstance(node, URIRef) and (QID_RE.fullmatch(str(node)) or PID_RE.fullmatch(str(node))):
                    report.error("bare-id", f"Bare source identifier is used as an RDF IRI: {node}")
            if predicate in RDF_IRI_PREDICATES and (
                not isinstance(value, URIRef) or not absolute_iri(str(value))
            ):
                report.error(
                    "bare-id",
                    f"RDF reference {predicate} must use a complete IRI, found {value!r}.",
                )

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                nested_path = f"{path}.{key}" if path else key
                if key in JSON_IRI_FIELDS:
                    if not isinstance(nested, str) or not absolute_iri(nested):
                        report.error(
                            "bare-id",
                            f"Public JSON reference {nested_path} must be a complete IRI.",
                        )
                    elif key == "wikidataId" and not WIKIDATA_PAGE_RE.fullmatch(nested):
                        report.error(
                            "json-identity",
                            f"{nested_path} is not a complete Wikidata entity-page IRI: {nested}",
                        )
                walk(nested, nested_path)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                walk(nested, f"{path}[{index}]")

    for dataset, payload in payloads.items():
        walk(payload, dataset)


def validate_vocabularies_and_curation(
    root: Path,
    graphs: dict[str, Graph],
    payloads: dict[str, dict[str, Any]],
    report: ValidationReport,
) -> tuple[
    semantic_config.ControlledVocabulary | None,
    semantic_config.ControlledVocabulary | None,
    semantic_config.SourceMappings | None,
]:
    try:
        categories = semantic_config.load_controlled_vocabulary(
            root / "vocabularies/categories.ttl"
        )
        software_types = semantic_config.load_controlled_vocabulary(
            root / "vocabularies/software-types.ttl"
        )
        mappings = semantic_config.load_source_mappings(root / "sources.ttl")
        curation = semantic_config.load_curated_assignments(
            root / "curation/classifications.ttl",
            categories,
            software_types,
        )
    except Exception as exc:
        report.error("semantic-config", f"Semantic configuration is invalid: {exc}")
        return None, None, None

    projection_specs = (
        (
            "data/categories.json",
            semantic_config.classification_label_projection(curation.categories, categories),
        ),
        (
            "data/software_types.json",
            semantic_config.classification_label_projection(
                curation.software_types,
                software_types,
            ),
        ),
        (
            "data/controlled_vocabularies.json",
            semantic_config.controlled_vocabulary_projection(categories, software_types),
        ),
    )
    for relative_path, expected in projection_specs:
        try:
            actual = read_json(root / relative_path)
        except Exception as exc:
            report.error("output-contract", f"Could not read {relative_path}: {exc}")
            continue
        if actual != expected:
            report.error(
                "output-contract",
                f"{relative_path} is not the deterministic projection of authoritative RDF.",
            )

    for dataset, vocabulary in (("resource", categories), ("software", software_types)):
        graph_path = DATASET_SPECS[dataset]["graph"]
        graph = graphs.get(str(graph_path))
        if graph is None:
            continue
        predicate = DATASET_SPECS[dataset]["classification_predicate"]
        valid = set(vocabulary.by_iri)
        for subject, concept in graph.subject_objects(predicate):
            if concept not in valid:
                report.error(
                    "controlled-value",
                    f"{dataset} resource {subject} uses concept outside {vocabulary.scheme}: {concept}",
                )
    return categories, software_types, mappings


def validate_json_contract_and_projection(
    graphs: dict[str, Graph],
    payloads: dict[str, dict[str, Any]],
    mappings: semantic_config.SourceMappings | None,
    report: ValidationReport,
) -> None:
    if mappings is None:
        return
    type_labels = mappings.projection_type_labels
    for dataset, spec in DATASET_SPECS.items():
        payload = payloads.get(dataset)
        graph = graphs.get(str(spec["graph"]))
        if payload is None or graph is None:
            continue
        if set(payload) != {"generatedAt", "items"}:
            report.error(
                "json-contract",
                f"{dataset} JSON top-level keys must be exactly generatedAt and items.",
            )
        generated_at = payload.get("generatedAt")
        if not isinstance(generated_at, str) or not GENERATED_AT_RE.fullmatch(generated_at):
            report.error(
                "json-contract",
                f"{dataset} generatedAt must be a UTC timestamp in YYYY-MM-DDTHH:MM:SSZ form.",
            )
        else:
            try:
                datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            except ValueError:
                report.error("json-contract", f"{dataset} generatedAt is invalid: {generated_at}")

        expected = fetch_data.extract_items_from_graph(
            graph,
            spec["allowed_types"],
            bool(spec["include_software_fields"]),
            type_labels,
        )
        actual = payload_items(payload)
        if actual != expected:
            expected_ids = {
                qid_from_value(str(item.get("wikidataId", ""))) for item in expected
            }
            actual_ids = {
                qid_from_value(str(item.get("wikidataId", ""))) for item in actual
            }
            report.error(
                "rdf-json-parity",
                f"{dataset} RDF/JSON projection mismatch: RDF={len(expected)}, JSON={len(actual)}, "
                f"missing={len(expected_ids - actual_ids)}, extra={len(actual_ids - expected_ids)}.",
            )


def validate_registry(
    current: dict[str, dict[str, str]],
    baseline: dict[str, dict[str, str]] | None,
    payloads: dict[str, dict[str, Any]],
    report: ValidationReport,
) -> None:
    for dataset, spec in DATASET_SPECS.items():
        registry_key = str(spec["registry"])
        entries = current.get(registry_key)
        if not isinstance(entries, dict):
            report.error("registry", f"Registry section {registry_key} is missing or invalid.")
            continue
        owners: dict[str, str] = {}
        for qid, slug in entries.items():
            if not QID_RE.fullmatch(qid) or not isinstance(slug, str) or not slug.strip():
                report.error("registry", f"Invalid {registry_key} registry entry: {qid} -> {slug!r}")
                continue
            previous_owner = owners.setdefault(slug, qid)
            if previous_owner != qid:
                report.error(
                    "slug-collision",
                    f"{registry_key} slug {slug!r} is assigned to both {previous_owner} and {qid}.",
                )

        indexed = items_by_qid(dataset, payloads.get(dataset, {}), report)
        for qid, item in indexed.items():
            slug = entries.get(qid)
            expected_uri = f"{BASE_URL}/{registry_key}/{slug}/" if slug else None
            if expected_uri is None:
                report.error("registry", f"Published {dataset} identity {qid} has no registry entry.")
            elif item.get("canonicalUrl") != expected_uri:
                report.error(
                    "uri-stability",
                    f"Published {dataset} identity {qid} has URI {item.get('canonicalUrl')!r}; "
                    f"registry requires {expected_uri}.",
                )

        if baseline is None:
            continue
        baseline_entries = baseline.get(registry_key, {})
        for qid, old_slug in baseline_entries.items():
            new_slug = entries.get(qid)
            if new_slug is None:
                report.error(
                    "registry-reservation",
                    f"Reserved {registry_key} identity {qid} was removed from uri_registry.json.",
                )
            elif new_slug != old_slug:
                report.error(
                    "uri-stability",
                    f"Reserved {registry_key} identity {qid} changed slug from {old_slug!r} to {new_slug!r}.",
                )


def coverage(items: list[dict[str, Any]], field_name: str) -> float:
    if not items:
        return 0.0
    present = 0
    for item in items:
        value = item.get(field_name)
        if isinstance(value, str):
            present += bool(value.strip())
        elif isinstance(value, (list, dict)):
            present += bool(value)
        elif value is not None:
            present += 1
    return present / len(items)


def validate_regressions(
    payloads: dict[str, dict[str, Any]],
    baseline: BaselineSnapshot | None,
    report: ValidationReport,
) -> None:
    if baseline is None:
        report.warning("baseline", "No committed baseline was available; regression checks were skipped.")
        return
    for dataset, spec in DATASET_SPECS.items():
        current_index = items_by_qid(dataset, payloads.get(dataset, {}), report)
        baseline_index = items_by_qid(
            f"baseline {dataset}",
            baseline.payloads.get(dataset, {}),
            report,
        )
        baseline_ids = set(baseline_index)
        current_ids = set(current_index)
        lost = baseline_ids - current_ids
        added = current_ids - baseline_ids
        if not baseline_ids:
            report.error("baseline", f"Committed {dataset} baseline contains no identities.")
            continue
        loss_ratio = len(lost) / len(baseline_ids)
        detail = (
            f"{dataset}: +{len(added)} / -{len(lost)} type-qualified QIDs "
            f"against {len(baseline_ids)} committed identities"
        )
        report.note(detail)
        if loss_ratio > LOSS_FAILURE_THRESHOLD:
            report.error(
                "record-loss",
                f"{detail}; {loss_ratio:.1%} loss exceeds the 10% failure threshold. "
                f"Lost: {', '.join(sorted(lost)[:20])}",
            )
        elif loss_ratio > LOSS_WARNING_THRESHOLD:
            report.warning(
                "record-loss",
                f"{detail}; {loss_ratio:.1%} loss exceeds the 2% warning threshold. "
                f"Lost: {', '.join(sorted(lost)[:20])}",
            )

        for qid in sorted(baseline_ids & current_ids):
            old_uri = baseline_index[qid].get("canonicalUrl")
            new_uri = current_index[qid].get("canonicalUrl")
            if old_uri != new_uri:
                report.error(
                    "uri-stability",
                    f"Surviving {dataset} identity {qid} changed URI from {old_uri!r} to {new_uri!r}.",
                )

        current_items = list(current_index.values())
        baseline_items = list(baseline_index.values())
        homepage_coverage = coverage(current_items, "homepage")
        if current_items and homepage_coverage < HOMEPAGE_COVERAGE_WARNING:
            report.warning(
                "metadata-coverage",
                f"{dataset} homepage coverage is low at {homepage_coverage:.1%} "
                f"(warning threshold {HOMEPAGE_COVERAGE_WARNING:.0%}).",
            )
        for field_name in spec["metadata_fields"]:
            before = coverage(baseline_items, field_name)
            after = coverage(current_items, field_name)
            if before - after > METADATA_COVERAGE_DROP_WARNING:
                report.warning(
                    "metadata-coverage",
                    f"{dataset} {field_name} coverage fell from {before:.1%} to {after:.1%}.",
                )


def collect_consumed_mapping_fields(pipeline_source: str) -> set[str]:
    tree = ast.parse(pipeline_source)
    consumed: set[str] = set()
    field_argument_indexes = {
        "wikidata_property": 1,
        "optional_direct_clause": 1,
        "optional_union_clause": 1,
        "property_id_for": 0,
        "property_ids_for": 0,
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name not in field_argument_indexes:
            continue
        argument_index = field_argument_indexes[name]
        if len(node.args) <= argument_index:
            continue
        field_arg = node.args[argument_index]
        if isinstance(field_arg, ast.Constant) and isinstance(field_arg.value, str):
            consumed.add(field_arg.value)
    return consumed


def collect_consumed_unscoped_class_targets(pipeline_source: str) -> set[URIRef]:
    tree = ast.parse(pipeline_source)
    targets: set[URIRef] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "class_id_for_target" or not node.args:
            continue
        arg = node.args[0]
        if (
            isinstance(arg, ast.Attribute)
            and isinstance(arg.value, ast.Name)
            and arg.value.id == "OKG"
        ):
            targets.add(URIRef(f"{OKG}{arg.attr}"))
    return targets


def collect_called_names(pipeline_source: str) -> set[str]:
    tree = ast.parse(pipeline_source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def validate_mapping_coverage_source(
    mappings: semantic_config.SourceMappings,
    pipeline_source: str,
    report: ValidationReport,
) -> None:
    declared_fields = {mapping.normalized_field for mapping in mappings.property_mappings}
    consumed_fields = collect_consumed_mapping_fields(pipeline_source)
    for field_name in sorted(consumed_fields - declared_fields):
        report.error(
            "mapping-coverage",
            f"Executable normalized field {field_name!r} has no sources.ttl declaration.",
        )
    for field_name in sorted(declared_fields - consumed_fields):
        report.error(
            "mapping-coverage",
            f"Declared normalized field {field_name!r} is not consumed by the pipeline.",
        )

    unscoped_targets = collect_consumed_unscoped_class_targets(pipeline_source)
    called_names = collect_called_names(pipeline_source)
    valid_catalogs = {
        semantic_config.ONTOLOGIES_DATASET,
        semantic_config.SOFTWARE_DATASET,
    }
    if any(mapping.catalogs for mapping in mappings.class_mappings) and "class_ids_for" not in called_names:
        report.error(
            "mapping-coverage",
            "Catalog-scoped source class mappings are declared but class_ids_for is not consumed.",
        )
    for mapping in mappings.class_mappings:
        if mapping.catalogs:
            unknown = set(mapping.catalogs) - valid_catalogs
            if unknown:
                report.error(
                    "mapping-coverage",
                    f"Class mapping {mapping.source_class_id} targets unsupported catalogs: {unknown}",
                )
        elif mapping.target_class not in unscoped_targets:
            report.error(
                "mapping-coverage",
                f"Unscoped class mapping {mapping.source_class_id} -> {mapping.target_class} is unused.",
            )

    hardcoded = sorted(set(HARDCODED_SOURCE_ID_RE.findall(pipeline_source)))
    if hardcoded:
        report.error(
            "mapping-drift",
            f"Executable pipeline hard-codes source identifiers instead of using sources.ttl: {hardcoded}",
        )


def validate_mapping_coverage(
    root: Path,
    mappings: semantic_config.SourceMappings | None,
    report: ValidationReport,
) -> None:
    if mappings is None:
        return
    paths = (root / "scripts/fetch_data.py", root / "scripts/category_classifier.py")
    try:
        pipeline_source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    except OSError as exc:
        report.error("mapping-coverage", f"Could not read executable mapping consumers: {exc}")
        return
    try:
        validate_mapping_coverage_source(mappings, pipeline_source, report)
    except SyntaxError as exc:
        report.error("mapping-coverage", f"Could not parse mapping consumer code: {exc}")


def validate_known_records_data(
    fixtures: dict[str, dict[str, dict[str, Any]]],
    payloads: dict[str, dict[str, Any]],
    report: ValidationReport,
) -> None:
    for dataset, records in fixtures.items():
        indexed = items_by_qid(dataset, payloads.get(dataset, {}), report)
        for qid, expected in records.items():
            item = indexed.get(qid)
            if item is None:
                report.error("known-record", f"Known {dataset} fixture {qid} is missing.")
                continue
            for field_name, expected_value in expected.items():
                if item.get(field_name) != expected_value:
                    report.error(
                        "known-record",
                        f"Known {dataset} fixture {qid} changed {field_name}: "
                        f"expected {expected_value!r}, found {item.get(field_name)!r}.",
                    )


def validate_known_records(
    root: Path,
    payloads: dict[str, dict[str, Any]],
    report: ValidationReport,
) -> None:
    try:
        fixtures = read_json(root / "validation/known_records.json")
    except Exception as exc:
        report.error("known-record", f"Could not load known-record fixtures: {exc}")
        return
    validate_known_records_data(fixtures, payloads, report)


def validate_page_contracts(
    root: Path,
    payloads: dict[str, dict[str, Any]],
    baseline: BaselineSnapshot | None,
    report: ValidationReport,
) -> None:
    try:
        page_qids = read_json(root / "data/page_qids.json")
    except Exception as exc:
        report.error("page-contract", f"Could not read data/page_qids.json: {exc}")
        return

    expected_page_urls: set[str] = set()
    for dataset in DATASET_SPECS:
        mapping = page_qids.get(dataset)
        if not isinstance(mapping, dict):
            report.error("page-contract", f"page_qids.json lacks a {dataset} object.")
            continue
        indexed = items_by_qid(dataset, payloads.get(dataset, {}), report)
        if len(set(mapping.values())) != len(mapping):
            report.error("page-contract", f"page_qids.json contains duplicate {dataset} slugs.")
        for qid, slug in mapping.items():
            item = indexed.get(qid)
            expected_uri = f"{BASE_URL}/{dataset}/{slug}/"
            if item is None:
                report.error("page-contract", f"Page mapping {dataset}:{qid} has no catalog record.")
            elif item.get("canonicalUrl") != expected_uri:
                report.error(
                    "page-contract",
                    f"Page mapping {dataset}:{qid} does not match its canonical URL.",
                )
            page_path = root / "site" / dataset / slug / "index.html"
            if not page_path.is_file():
                report.error("page-contract", f"Generated page is missing: {page_path.relative_to(root)}")
            expected_page_urls.add(expected_uri)

        actual_slugs = {
            path.parent.name
            for path in (root / "site" / dataset).glob("*/index.html")
            if path.is_file()
        }
        mapped_slugs = set(mapping.values())
        for slug in sorted(actual_slugs - mapped_slugs):
            report.error("page-contract", f"Unregistered generated page: site/{dataset}/{slug}/")

        if baseline is not None:
            old_pages = set(baseline.page_qids.get(dataset, {}))
            current_pages = set(mapping)
            surviving_records = set(indexed)
            flaky_candidates = (old_pages - current_pages) & surviving_records
            if flaky_candidates:
                report.warning(
                    "external-link",
                    f"{dataset} lost {len(flaky_candidates)} pages for surviving records; "
                    f"check external-link availability: {', '.join(sorted(flaky_candidates)[:20])}",
                )

    try:
        tree = ET.parse(root / "site/sitemap.xml")
        namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locations = {
            element.text.strip()
            for element in tree.findall("sm:url/sm:loc", namespace)
            if element.text
        }
    except Exception as exc:
        report.error("page-contract", f"Could not parse site/sitemap.xml: {exc}")
        return
    actual_page_urls = {
        value
        for value in locations
        if value.startswith(f"{BASE_URL}/resource/")
        or value.startswith(f"{BASE_URL}/software/")
    }
    if actual_page_urls != expected_page_urls:
        report.error(
            "page-contract",
            f"Sitemap/page registry mismatch: missing={len(expected_page_urls - actual_page_urls)}, "
            f"extra={len(actual_page_urls - expected_page_urls)}.",
        )


def git_show_json(root: Path, ref: str, relative_path: str) -> Any:
    completed = subprocess.run(
        ["git", "show", f"{ref}:{relative_path}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def load_baseline_from_git(
    root: Path,
    ref: str,
    report: ValidationReport,
) -> BaselineSnapshot | None:
    try:
        return BaselineSnapshot(
            payloads={
                "resource": git_show_json(root, ref, "data/ontologies.json"),
                "software": git_show_json(root, ref, "data/software.json"),
            },
            registry=git_show_json(root, ref, "data/uri_registry.json"),
            page_qids=git_show_json(root, ref, "data/page_qids.json"),
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        report.error("baseline", f"Could not load committed baseline {ref}: {exc}")
        return None


def load_payloads(root: Path, report: ValidationReport) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    for dataset, spec in DATASET_SPECS.items():
        try:
            payload = read_json(root / str(spec["json"]))
            if not isinstance(payload, dict):
                raise ValueError("top level is not an object")
            payloads[dataset] = payload
        except Exception as exc:
            report.error("json-parse", f"Could not read {spec['json']}: {exc}")
    return payloads


def validate_catalog(
    root: Path = ROOT_DIR,
    baseline_ref: str | None = "HEAD",
    baseline: BaselineSnapshot | None = None,
    repository_root: Path = ROOT_DIR,
) -> ValidationReport:
    report = ValidationReport()
    graphs = load_graphs(root, report)
    payloads = load_payloads(root, report)
    try:
        registry = read_json(root / "data/uri_registry.json")
        if not isinstance(registry, dict):
            raise ValueError("top level is not an object")
    except Exception as exc:
        report.error("registry", f"Could not read data/uri_registry.json: {exc}")
        registry = {}

    if baseline is None and baseline_ref is not None:
        baseline = load_baseline_from_git(repository_root, baseline_ref, report)

    validate_shacl(graphs, report)
    ontology = graphs.get("ontology.ttl")
    if ontology is not None:
        validate_declared_schema_terms(
            ontology,
            (graph for path, graph in graphs.items() if path != "ontology.ttl"),
            report,
        )
    validate_public_iris(graphs.values(), payloads, report)
    _, _, mappings = validate_vocabularies_and_curation(root, graphs, payloads, report)
    validate_json_contract_and_projection(graphs, payloads, mappings, report)
    validate_registry(
        registry,
        baseline.registry if baseline is not None else None,
        payloads,
        report,
    )
    validate_regressions(payloads, baseline, report)
    validate_mapping_coverage(root, mappings, report)
    validate_known_records(root, payloads, report)
    validate_page_contracts(root, payloads, baseline, report)
    return report


def render_report(report: ValidationReport) -> str:
    lines = [
        "Catalog validation PASSED" if report.conforms else "Catalog validation FAILED",
        f"Errors: {len(report.errors)}; warnings: {len(report.warnings)}",
    ]
    lines.extend(f"[INFO] {note}" for note in report.notes)
    lines.extend(f"[WARNING:{issue.code}] {issue.message}" for issue in report.warnings)
    lines.extend(f"[ERROR:{issue.code}] {issue.message}" for issue in report.errors)
    return "\n".join(lines)


def append_github_summary(path: Path, report: ValidationReport) -> None:
    status = "Passed" if report.conforms else "Failed"
    lines = [
        "## OKG catalog validation",
        "",
        f"**{status}** — {len(report.errors)} errors, {len(report.warnings)} warnings.",
        "",
    ]
    if report.notes:
        lines.extend(("### Catalog changes", ""))
        lines.extend(f"- {note}" for note in report.notes)
        lines.append("")
    if report.warnings:
        lines.extend(("### Warnings", ""))
        lines.extend(f"- `{issue.code}`: {issue.message}" for issue in report.warnings)
        lines.append("")
    if report.errors:
        lines.extend(("### Errors", ""))
        lines.extend(f"- `{issue.code}`: {issue.message}" for issue in report.errors)
        lines.append("")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT_DIR)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=ROOT_DIR,
        help="Git checkout used to resolve --baseline-ref (default: repository root).",
    )
    parser.add_argument(
        "--baseline-ref",
        default="HEAD",
        help="Git ref containing the previously successful catalog (default: HEAD).",
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Skip comparison with a committed catalog (structural validation still runs).",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="Append a Markdown result summary; defaults to GITHUB_STEP_SUMMARY when set.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    baseline_ref = None if args.skip_baseline else args.baseline_ref
    report = validate_catalog(
        root=root,
        baseline_ref=baseline_ref,
        repository_root=args.repository_root.resolve(),
    )
    print(render_report(report))
    summary_path = args.summary_file
    if summary_path is None and os.getenv("GITHUB_STEP_SUMMARY"):
        summary_path = Path(os.environ["GITHUB_STEP_SUMMARY"])
    if summary_path is not None:
        append_github_summary(summary_path, report)
    return 0 if report.conforms else 1


if __name__ == "__main__":
    sys.exit(main())
