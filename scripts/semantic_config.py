#!/usr/bin/env python3
"""Load OKG's RDF configuration and write its derived compatibility projections."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS, SKOS


BASE_URL = "https://openknowledgegraphs.com"
OKG = Namespace(f"{BASE_URL}/ontology#")

ROOT_DIR = Path(
    os.environ.get("OKG_CATALOG_ROOT", Path(__file__).resolve().parent.parent)
).resolve()
CATEGORIES_VOCAB_PATH = ROOT_DIR / "vocabularies" / "categories.ttl"
SOFTWARE_TYPES_VOCAB_PATH = ROOT_DIR / "vocabularies" / "software-types.ttl"
SOURCES_PATH = ROOT_DIR / "sources.ttl"
CURATION_PATH = ROOT_DIR / "curation" / "classifications.ttl"

ONTOLOGIES_DATASET = URIRef(f"{BASE_URL}/datasets/ontologies")
SOFTWARE_DATASET = URIRef(f"{BASE_URL}/datasets/software")

QID_RE = re.compile(r"^Q\d+$")
PID_RE = re.compile(r"^P\d+$")


class SemanticConfigError(RuntimeError):
    """Raised when an authoritative RDF configuration artifact is invalid."""


def _single_iri(graph: Graph, subject: URIRef, predicate: URIRef) -> URIRef:
    values = [value for value in graph.objects(subject, predicate) if isinstance(value, URIRef)]
    if len(values) != 1:
        raise SemanticConfigError(
            f"Expected one IRI for {subject} {predicate}, found {len(values)}."
        )
    return values[0]


def _single_literal(graph: Graph, subject: URIRef, predicate: URIRef) -> str:
    values = [str(value).strip() for value in graph.objects(subject, predicate) if isinstance(value, Literal)]
    values = [value for value in values if value]
    if len(values) != 1:
        raise SemanticConfigError(
            f"Expected one non-empty literal for {subject} {predicate}, found {len(values)}."
        )
    return values[0]


def _optional_literal(graph: Graph, subject: URIRef, predicate: URIRef) -> str | None:
    values = [str(value).strip() for value in graph.objects(subject, predicate) if isinstance(value, Literal)]
    values = [value for value in values if value]
    if len(values) > 1:
        raise SemanticConfigError(f"Expected at most one literal for {subject} {predicate}.")
    return values[0] if values else None


@dataclass(frozen=True)
class ControlledConcept:
    iri: URIRef
    label: str
    definition: str
    scope_note: str
    slug: str
    sort_order: int

    @property
    def prompt_definition(self) -> str:
        return f"{self.definition} Scope: {self.scope_note}"


@dataclass(frozen=True)
class ControlledVocabulary:
    path: Path
    graph: Graph
    scheme: URIRef
    concept_class: URIRef
    classification_predicate: URIRef
    concepts: tuple[ControlledConcept, ...]

    @property
    def by_iri(self) -> dict[URIRef, ControlledConcept]:
        return {concept.iri: concept for concept in self.concepts}

    @property
    def by_label(self) -> dict[str, ControlledConcept]:
        return {concept.label: concept for concept in self.concepts}

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(concept.label for concept in self.concepts)

    @property
    def label_set(self) -> set[str]:
        return set(self.labels)

    @property
    def prompt_definitions(self) -> dict[str, str]:
        return {concept.label: concept.prompt_definition for concept in self.concepts}


def load_controlled_vocabulary(path: Path) -> ControlledVocabulary:
    graph = Graph().parse(path, format="turtle")
    schemes = {subject for subject in graph.subjects(RDF.type, SKOS.ConceptScheme)}
    if len(schemes) != 1:
        raise SemanticConfigError(f"{path} must define exactly one skos:ConceptScheme.")
    scheme = next(iter(schemes))
    if not isinstance(scheme, URIRef):
        raise SemanticConfigError(f"{path} concept scheme must have a public IRI.")

    for predicate in (
        DCTERMS.title,
        DCTERMS.description,
        DCTERMS.issued,
        DCTERMS.modified,
    ):
        _single_literal(graph, scheme, predicate)

    concept_class = _single_iri(graph, scheme, OKG.conceptClass)
    classification_predicate = _single_iri(graph, scheme, OKG.classificationPredicate)

    concepts: list[ControlledConcept] = []
    for subject in graph.subjects(RDF.type, SKOS.Concept):
        if not isinstance(subject, URIRef) or (subject, SKOS.inScheme, scheme) not in graph:
            continue
        label = _single_literal(graph, subject, SKOS.prefLabel)
        definition = _single_literal(graph, subject, SKOS.definition)
        scope_note = _single_literal(graph, subject, SKOS.scopeNote)
        slug = _single_literal(graph, subject, OKG.urlSlug)
        raw_order = _single_literal(graph, subject, OKG.sortOrder)
        try:
            sort_order = int(raw_order)
        except ValueError as exc:
            raise SemanticConfigError(f"Invalid sort order for {subject}: {raw_order}") from exc
        if (subject, RDF.type, concept_class) not in graph:
            raise SemanticConfigError(f"{subject} must also be typed as {concept_class}.")
        concepts.append(
            ControlledConcept(
                iri=subject,
                label=label,
                definition=definition,
                scope_note=scope_note,
                slug=slug,
                sort_order=sort_order,
            )
        )

    concepts.sort(key=lambda concept: (concept.sort_order, concept.label.casefold()))
    if not concepts:
        raise SemanticConfigError(f"{path} contains no controlled concepts.")
    if len({concept.iri for concept in concepts}) != len(concepts):
        raise SemanticConfigError(f"{path} contains duplicate concept IRIs.")
    if len({concept.label for concept in concepts}) != len(concepts):
        raise SemanticConfigError(f"{path} contains duplicate preferred labels.")
    if len({concept.slug for concept in concepts}) != len(concepts):
        raise SemanticConfigError(f"{path} contains duplicate URL slugs.")

    return ControlledVocabulary(
        path=path,
        graph=graph,
        scheme=scheme,
        concept_class=concept_class,
        classification_predicate=classification_predicate,
        concepts=tuple(concepts),
    )


@dataclass(frozen=True)
class SourceClassMapping:
    iri: URIRef
    source_class_id: str
    target_class: URIRef
    projection_value: str
    catalogs: frozenset[URIRef]
    sort_order: int


@dataclass(frozen=True)
class SourcePropertyMapping:
    iri: URIRef
    source_property_id: str
    normalized_field: str
    target_term: URIRef
    value_kind: str
    cardinality: str
    catalogs: frozenset[URIRef]
    sort_order: int


@dataclass(frozen=True)
class SourceEligibilityDecision:
    iri: URIRef
    source_qid: str
    rationale: str
    evidence_urls: tuple[str, ...]


@dataclass(frozen=True)
class SourceEligibilityPolicy:
    iri: URIRef
    catalog: URIRef
    term_component_markers: frozenset[str]
    exclusions: dict[str, SourceEligibilityDecision]
    exceptions: dict[str, SourceEligibilityDecision]


@dataclass(frozen=True)
class SourceInclusion:
    iri: URIRef
    catalog: URIRef
    source_qid: str
    target_class: URIRef
    projection_value: str
    rationale: str
    evidence_urls: tuple[str, ...]


@dataclass(frozen=True)
class RecommendationExemplar:
    iri: URIRef
    catalog: URIRef
    subject_qid: str
    source_property_id: str
    object_qid: str
    label: str


@dataclass(frozen=True)
class SourceMappings:
    graph: Graph
    class_mappings: tuple[SourceClassMapping, ...]
    property_mappings: tuple[SourcePropertyMapping, ...]
    eligibility_policies: tuple[SourceEligibilityPolicy, ...]
    source_inclusions: tuple[SourceInclusion, ...]
    recommendation_exemplars: tuple[RecommendationExemplar, ...]

    def class_mappings_for(self, catalog: URIRef) -> tuple[SourceClassMapping, ...]:
        return tuple(mapping for mapping in self.class_mappings if catalog in mapping.catalogs)

    def class_target_map(self, catalog: URIRef) -> dict[str, URIRef]:
        return {
            mapping.source_class_id: mapping.target_class
            for mapping in self.class_mappings_for(catalog)
        }

    def class_ids_for(self, catalog: URIRef) -> tuple[str, ...]:
        return tuple(mapping.source_class_id for mapping in self.class_mappings_for(catalog))

    def inclusions_for(self, catalog: URIRef) -> tuple[SourceInclusion, ...]:
        return tuple(
            inclusion for inclusion in self.source_inclusions if inclusion.catalog == catalog
        )

    def inclusion_target_map(self, catalog: URIRef) -> dict[str, URIRef]:
        return {
            inclusion.source_qid: inclusion.target_class
            for inclusion in self.inclusions_for(catalog)
        }

    def target_classes_for(self, catalog: URIRef) -> set[URIRef]:
        return {
            *(mapping.target_class for mapping in self.class_mappings_for(catalog)),
            *(inclusion.target_class for inclusion in self.inclusions_for(catalog)),
        }

    def class_id_for_target(self, target_class: URIRef) -> str:
        matches = [
            mapping.source_class_id
            for mapping in self.class_mappings
            if mapping.target_class == target_class and not mapping.catalogs
        ]
        if len(matches) != 1:
            raise SemanticConfigError(
                f"Expected one non-catalog source class mapping for {target_class}, found {len(matches)}."
            )
        return matches[0]

    @property
    def projection_type_labels(self) -> dict[URIRef, str]:
        labels: dict[URIRef, str] = {}
        for mapping in (*self.class_mappings, *self.source_inclusions):
            previous = labels.setdefault(mapping.target_class, mapping.projection_value)
            if previous != mapping.projection_value:
                raise SemanticConfigError(
                    f"Conflicting projection values for {mapping.target_class}."
                )
        return labels

    def property_ids_for(
        self,
        normalized_field: str,
        catalog: URIRef | None = None,
        value_kind: str | None = None,
    ) -> tuple[str, ...]:
        matches = [
            mapping
            for mapping in self.property_mappings
            if mapping.normalized_field == normalized_field
            and (catalog is None or not mapping.catalogs or catalog in mapping.catalogs)
            and (value_kind is None or mapping.value_kind == value_kind)
        ]
        if not matches:
            qualifier = f" for {catalog}" if catalog else ""
            raise SemanticConfigError(
                f"No source property mapping for normalized field {normalized_field!r}{qualifier}."
            )
        return tuple(mapping.source_property_id for mapping in matches)

    def property_id_for(
        self,
        normalized_field: str,
        catalog: URIRef | None = None,
        value_kind: str | None = None,
    ) -> str:
        matches = self.property_ids_for(normalized_field, catalog, value_kind)
        if len(matches) != 1:
            raise SemanticConfigError(
                f"Expected one source property mapping for {normalized_field!r}, found {len(matches)}."
            )
        return matches[0]

    def eligibility_policy_for(self, catalog: URIRef) -> SourceEligibilityPolicy:
        matches = [policy for policy in self.eligibility_policies if policy.catalog == catalog]
        if len(matches) != 1:
            raise SemanticConfigError(
                f"Expected one source eligibility policy for {catalog}, found {len(matches)}."
            )
        return matches[0]


def _qid_from_source_entity(value: URIRef) -> str:
    match = re.search(r"/(Q\d+)$", str(value))
    if not match:
        raise SemanticConfigError(f"Eligibility source entity is not a Wikidata QID IRI: {value}")
    return match.group(1)


def _load_eligibility_decision(
    graph: Graph,
    subject: URIRef,
    expected_type: URIRef,
) -> SourceEligibilityDecision:
    if (subject, RDF.type, expected_type) not in graph:
        raise SemanticConfigError(f"Eligibility decision {subject} must be typed as {expected_type}.")
    source_qid = _qid_from_source_entity(_single_iri(graph, subject, OKG.sourceEntity))
    rationale = _single_literal(graph, subject, DCTERMS.description)
    evidence_urls = tuple(
        sorted(str(value) for value in graph.objects(subject, DCTERMS.source) if isinstance(value, URIRef))
    )
    if not evidence_urls:
        raise SemanticConfigError(f"Eligibility decision {subject} requires public evidence.")
    return SourceEligibilityDecision(
        iri=subject,
        source_qid=source_qid,
        rationale=rationale,
        evidence_urls=evidence_urls,
    )


def load_source_mappings(path: Path = SOURCES_PATH) -> SourceMappings:
    graph = Graph().parse(path, format="turtle")
    class_mappings: list[SourceClassMapping] = []
    property_mappings: list[SourcePropertyMapping] = []
    eligibility_policies: list[SourceEligibilityPolicy] = []
    source_inclusions: list[SourceInclusion] = []
    recommendation_exemplars: list[RecommendationExemplar] = []

    for subject in graph.subjects(RDF.type, OKG.SourceClassMapping):
        if not isinstance(subject, URIRef):
            raise SemanticConfigError("Source class mappings must have stable IRIs.")
        source_class_id = _single_literal(graph, subject, OKG.sourceClassId)
        if not QID_RE.fullmatch(source_class_id):
            raise SemanticConfigError(f"Invalid Wikidata class identifier: {source_class_id}")
        catalogs = frozenset(
            value for value in graph.objects(subject, OKG.catalogDataset) if isinstance(value, URIRef)
        )
        class_mappings.append(
            SourceClassMapping(
                iri=subject,
                source_class_id=source_class_id,
                target_class=_single_iri(graph, subject, OKG.targetTerm),
                projection_value=_single_literal(graph, subject, OKG.projectionValue),
                catalogs=catalogs,
                sort_order=int(_single_literal(graph, subject, OKG.sortOrder)),
            )
        )

    for subject in graph.subjects(RDF.type, OKG.SourcePropertyMapping):
        if not isinstance(subject, URIRef):
            raise SemanticConfigError("Source property mappings must have stable IRIs.")
        source_property_id = _single_literal(graph, subject, OKG.sourcePropertyId)
        if not PID_RE.fullmatch(source_property_id):
            raise SemanticConfigError(f"Invalid Wikidata property identifier: {source_property_id}")
        catalogs = frozenset(
            value for value in graph.objects(subject, OKG.catalogDataset) if isinstance(value, URIRef)
        )
        property_mappings.append(
            SourcePropertyMapping(
                iri=subject,
                source_property_id=source_property_id,
                normalized_field=_single_literal(graph, subject, OKG.normalizedField),
                target_term=_single_iri(graph, subject, OKG.targetTerm),
                value_kind=_single_literal(graph, subject, OKG.valueKind),
                cardinality=_single_literal(graph, subject, OKG.cardinality),
                catalogs=catalogs,
                sort_order=int(_single_literal(graph, subject, OKG.sortOrder)),
            )
        )

    for subject in graph.subjects(RDF.type, OKG.SourceInclusion):
        if not isinstance(subject, URIRef):
            raise SemanticConfigError("Source inclusions must have stable IRIs.")
        evidence_urls = tuple(
            sorted(
                str(value)
                for value in graph.objects(subject, DCTERMS.source)
                if isinstance(value, URIRef)
            )
        )
        if not evidence_urls:
            raise SemanticConfigError(f"Source inclusion {subject} requires public evidence.")
        source_inclusions.append(
            SourceInclusion(
                iri=subject,
                catalog=_single_iri(graph, subject, OKG.catalogDataset),
                source_qid=_qid_from_source_entity(
                    _single_iri(graph, subject, OKG.sourceEntity)
                ),
                target_class=_single_iri(graph, subject, OKG.targetTerm),
                projection_value=_single_literal(graph, subject, OKG.projectionValue),
                rationale=_single_literal(graph, subject, DCTERMS.description),
                evidence_urls=evidence_urls,
            )
        )

    inclusion_keys = [
        (inclusion.catalog, inclusion.source_qid) for inclusion in source_inclusions
    ]
    if len(inclusion_keys) != len(set(inclusion_keys)):
        raise SemanticConfigError("Source inclusions must be unique by catalog and QID.")

    for subject in graph.subjects(RDF.type, OKG.SourceEligibilityPolicy):
        if not isinstance(subject, URIRef):
            raise SemanticConfigError("Source eligibility policies must have stable IRIs.")
        catalog = _single_iri(graph, subject, OKG.catalogDataset)
        markers = frozenset(
            _qid_from_source_entity(value)
            for value in graph.objects(subject, OKG.termComponentMarker)
            if isinstance(value, URIRef)
        )
        if not markers:
            raise SemanticConfigError(f"Eligibility policy {subject} has no term/component markers.")

        exclusions: dict[str, SourceEligibilityDecision] = {}
        for decision_iri in graph.objects(subject, OKG.sourceExclusion):
            if not isinstance(decision_iri, URIRef):
                raise SemanticConfigError("Source exclusions must have stable IRIs.")
            decision = _load_eligibility_decision(graph, decision_iri, OKG.SourceExclusion)
            if decision.source_qid in exclusions:
                raise SemanticConfigError(f"Duplicate source exclusion for {decision.source_qid}.")
            exclusions[decision.source_qid] = decision

        exceptions: dict[str, SourceEligibilityDecision] = {}
        for decision_iri in graph.objects(subject, OKG.eligibilityException):
            if not isinstance(decision_iri, URIRef):
                raise SemanticConfigError("Eligibility exceptions must have stable IRIs.")
            decision = _load_eligibility_decision(
                graph, decision_iri, OKG.SourceEligibilityException
            )
            if decision.source_qid in exceptions:
                raise SemanticConfigError(
                    f"Duplicate source eligibility exception for {decision.source_qid}."
                )
            exceptions[decision.source_qid] = decision

        overlap = set(exclusions) & set(exceptions)
        if overlap:
            raise SemanticConfigError(
                "Confirmed exclusions cannot also be eligibility exceptions: "
                + ", ".join(sorted(overlap))
            )
        eligibility_policies.append(
            SourceEligibilityPolicy(
                iri=subject,
                catalog=catalog,
                term_component_markers=markers,
                exclusions=exclusions,
                exceptions=exceptions,
            )
        )

    property_ids_by_iri = {
        mapping.iri: mapping.source_property_id for mapping in property_mappings
    }
    for subject in graph.subjects(RDF.type, OKG.RecommendationExemplar):
        if not isinstance(subject, URIRef):
            raise SemanticConfigError("Recommendation exemplars must have stable IRIs.")
        property_mapping = _single_iri(graph, subject, OKG.sourcePropertyMapping)
        if property_mapping not in property_ids_by_iri:
            raise SemanticConfigError(
                f"Recommendation exemplar {subject} uses an undeclared source mapping."
            )
        recommendation_exemplars.append(
            RecommendationExemplar(
                iri=subject,
                catalog=_single_iri(graph, subject, OKG.catalogDataset),
                subject_qid=_qid_from_source_entity(
                    _single_iri(graph, subject, OKG.sourceEntity)
                ),
                source_property_id=property_ids_by_iri[property_mapping],
                object_qid=_qid_from_source_entity(
                    _single_iri(graph, subject, OKG.sourceObject)
                ),
                label=_single_literal(graph, subject, RDFS.label),
            )
        )

    class_mappings.sort(key=lambda mapping: (mapping.sort_order, mapping.source_class_id))
    property_mappings.sort(key=lambda mapping: (mapping.sort_order, mapping.source_property_id))
    if not class_mappings or not property_mappings:
        raise SemanticConfigError(f"{path} must contain class and property mappings.")

    return SourceMappings(
        graph=graph,
        class_mappings=tuple(class_mappings),
        property_mappings=tuple(property_mappings),
        eligibility_policies=tuple(eligibility_policies),
        source_inclusions=tuple(
            sorted(source_inclusions, key=lambda inclusion: (str(inclusion.catalog), inclusion.source_qid))
        ),
        recommendation_exemplars=tuple(
            sorted(recommendation_exemplars, key=lambda exemplar: str(exemplar.iri))
        ),
    )


@dataclass
class CurationAssignments:
    categories: dict[str, URIRef]
    software_types: dict[str, URIRef]


def qid_from_wikidata_value(value: str) -> str:
    match = re.search(r"(Q\d+)$", value.strip())
    if not match:
        raise SemanticConfigError(f"Could not parse Wikidata QID from {value!r}.")
    return match.group(1)


def load_curated_assignments(
    path: Path,
    category_vocab: ControlledVocabulary,
    software_type_vocab: ControlledVocabulary,
) -> CurationAssignments:
    graph = Graph().parse(path, format="turtle")
    vocabularies = (
        (category_vocab, {}),
        (software_type_vocab, {}),
    )
    results: list[dict[str, URIRef]] = []

    for vocabulary, mapping in vocabularies:
        valid_concepts = vocabulary.by_iri
        for subject, concept in graph.subject_objects(vocabulary.classification_predicate):
            if not isinstance(subject, URIRef) or not isinstance(concept, URIRef):
                raise SemanticConfigError("Curated assignments must connect public IRIs.")
            if concept not in valid_concepts:
                raise SemanticConfigError(
                    f"Curated assignment uses concept outside {vocabulary.scheme}: {concept}"
                )
            wikidata_values = [
                value
                for value in graph.objects(subject, OKG.wikidataId)
                if isinstance(value, URIRef)
            ]
            if len(wikidata_values) != 1:
                raise SemanticConfigError(f"Curated resource {subject} must have one okg:wikidataId.")
            qid = qid_from_wikidata_value(str(wikidata_values[0]))
            previous = mapping.setdefault(qid, concept)
            if previous != concept:
                raise SemanticConfigError(f"Multiple curated assignments for {qid}.")
        results.append(mapping)

    return CurationAssignments(categories=results[0], software_types=results[1])


def resource_iri_by_qid(
    uri_registry: dict[str, dict[str, str]],
    registry_key: str,
) -> dict[str, URIRef]:
    return {
        qid: URIRef(f"{BASE_URL}/{registry_key}/{slug}/")
        for qid, slug in uri_registry.get(registry_key, {}).items()
    }


def curated_subject_for_qid(
    qid: str,
    registered_resources: dict[str, URIRef],
) -> URIRef:
    """Use an existing OKG resource IRI, or retain Wikidata as the subject authority."""
    return registered_resources.get(qid, URIRef(f"http://www.wikidata.org/entity/{qid}"))


def write_curated_assignments_atomic(
    path: Path,
    assignments: CurationAssignments,
    uri_registry: dict[str, dict[str, str]],
    category_vocab: ControlledVocabulary,
    software_type_vocab: ControlledVocabulary,
) -> None:
    graph = Graph()
    graph.bind("okg", OKG)

    for mapping, vocabulary, registry_key in (
        (assignments.categories, category_vocab, "resource"),
        (assignments.software_types, software_type_vocab, "software"),
    ):
        resources = resource_iri_by_qid(uri_registry, registry_key)
        for qid, concept in sorted(mapping.items()):
            resource = curated_subject_for_qid(qid, resources)
            graph.add((resource, OKG.wikidataId, URIRef(f"https://www.wikidata.org/wiki/{qid}")))
            graph.add((resource, vocabulary.classification_predicate, concept))

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    serialized = graph.serialize(format="turtle")
    Graph().parse(data=serialized, format="turtle")
    temp_path.write_text(serialized.rstrip() + "\n", encoding="utf-8")
    temp_path.replace(path)


def classification_label_projection(
    mapping: dict[str, URIRef],
    vocabulary: ControlledVocabulary,
) -> dict[str, str]:
    concepts = vocabulary.by_iri
    return {qid: concepts[concept].label for qid, concept in sorted(mapping.items())}


def write_json_atomic(payload: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def controlled_vocabulary_projection(
    category_vocab: ControlledVocabulary,
    software_type_vocab: ControlledVocabulary,
) -> dict[str, list[dict[str, str]]]:
    def entries(vocabulary: ControlledVocabulary) -> list[dict[str, str]]:
        return [
            {
                "id": concept.slug,
                "label": concept.label,
                "iri": str(concept.iri),
                "definition": concept.definition,
                "scopeNote": concept.scope_note,
            }
            for concept in vocabulary.concepts
        ]

    return {
        "categories": entries(category_vocab),
        "softwareTypes": entries(software_type_vocab),
    }
