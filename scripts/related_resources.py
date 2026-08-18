#!/usr/bin/env python3
"""Deterministic, explainable related-resource scoring for one OKG catalog."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from semantic_config import OKG


QID_RE = re.compile(r"(Q\d+)$")
TOKEN_RE = re.compile(r"[a-z0-9]+")
MULTI_TENANT_REPOSITORY_HOSTS = frozenset(
    {
        "bitbucket.org",
        "github.com",
        "gitlab.com",
    }
)
TEXT_STOP_WORDS = frozenset(
    {
        "about",
        "and",
        "data",
        "for",
        "from",
        "graph",
        "knowledge",
        "ontology",
        "resource",
        "semantic",
        "software",
        "that",
        "the",
        "this",
        "tool",
        "using",
        "vocabulary",
        "with",
    }
)

# The 2026-08-14 full-cohort audit found that degree <= 6 retains narrow exact
# source types while the next large software class spans 154 comparable records.
# This is intentionally a fixed corpus policy, not a per-run percentile.
MAX_SHARED_SOURCE_TYPE_DEGREE = 6
SOURCE_TYPE_DEGREE_RATIONALE = (
    "The reviewed 2026-08-14 cohort audit retained exact source types shared by "
    "at most six comparable records; broader classes were non-discriminating."
)


@dataclass(frozen=True)
class SimilarityConfig:
    """Public scoring contract. Structural evidence is always mandatory."""

    direct_relationship: int = 120
    direct_uses: int = 120
    shared_parent: int = 100
    shared_source_type: int = 65
    same_repository: int = 90
    same_namespace_family: int = 70
    shared_creator: int = 55
    shared_category: int = 4
    shared_software_type: int = 4
    shared_rdf_type: int = 3
    shared_programming_language: int = 4
    shared_license: int = 2
    text_similarity: int = 3
    text_similarity_floor: float = 0.25
    score_threshold: int = 60
    max_related: int = 5
    max_shared_parent_degree: int = 6
    max_shared_source_type_degree: int = MAX_SHARED_SOURCE_TYPE_DEGREE

    def __post_init__(self) -> None:
        if self.score_threshold <= 0:
            raise ValueError("score_threshold must be positive")
        if not 1 <= self.max_related <= 5:
            raise ValueError("max_related must be between one and five")
        if self.max_shared_parent_degree < 2:
            raise ValueError("max_shared_parent_degree must be at least two")
        if self.max_shared_source_type_degree < 2:
            raise ValueError("max_shared_source_type_degree must be at least two")
        if not 0.0 <= self.text_similarity_floor <= 1.0:
            raise ValueError("text_similarity_floor must be between zero and one")


DEFAULT_CONFIG = SimilarityConfig()


@dataclass(frozen=True)
class ResourceFeatures:
    subject: URIRef
    source_entities: frozenset[str]
    parents: frozenset[str]
    uses: frozenset[str]
    source_types: frozenset[str]
    creators: frozenset[str]
    repositories: frozenset[str]
    namespace_families: frozenset[str]
    categories: frozenset[str]
    software_types: frozenset[str]
    rdf_types: frozenset[str]
    programming_languages: frozenset[str]
    licenses: frozenset[str]
    text_tokens: frozenset[str]


@dataclass(frozen=True)
class SimilarityContext:
    """Corpus facts used to decide whether shared identity is discriminating."""

    parent_degrees: Mapping[str, int]
    catalog_source_entities: frozenset[str]
    source_type_degrees: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreComponent:
    feature: str
    score: int
    evidence: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "score": self.score,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class PairScore:
    subject: str
    candidate: str
    score: int
    qualifies: bool
    qualifying_reasons: tuple[str, ...]
    components: tuple[ScoreComponent, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "candidate": self.candidate,
            "score": self.score,
            "qualifies": self.qualifies,
            "qualifyingReasons": list(self.qualifying_reasons),
            "components": [component.as_dict() for component in self.components],
        }


def _iri_values(graph: Graph, subject: URIRef, predicate: URIRef) -> frozenset[str]:
    return frozenset(
        str(value)
        for value in graph.objects(subject, predicate)
        if isinstance(value, URIRef)
    )


def _literal_values(graph: Graph, subject: URIRef, predicate: URIRef) -> frozenset[str]:
    return frozenset(
        str(value).strip().casefold()
        for value in graph.objects(subject, predicate)
        if isinstance(value, Literal) and str(value).strip()
    )


def _wikidata_entity(value: str) -> str | None:
    match = QID_RE.search(value.rstrip("/"))
    if not match:
        return None
    return f"http://www.wikidata.org/entity/{match.group(1)}"


def canonical_repository(value: str) -> str | None:
    """Normalize one repository URL without equating repositories by owner."""
    parsed = urlsplit(value.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.hostname.casefold() if parsed.hostname else ""
    if not host:
        return None
    port = parsed.port
    authority = host if port is None else f"{host}:{port}"
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
    if not path:
        return None
    path_segments = tuple(segment for segment in path.split("/") if segment)
    if host in MULTI_TENANT_REPOSITORY_HOSTS and len(path_segments) < 2:
        # A single path segment on these hosts identifies an account, workspace,
        # or group—not a repository. Treating it as structural evidence would
        # relate every resource that merely cites the same hosting organization.
        return None
    return urlunsplit(("https", authority, path, "", ""))


def namespace_family(value: str) -> str | None:
    """Return a conservative family key: scheme, authority, and full base path.

    A host by itself is deliberately not a family. Query strings, fragments,
    and a trailing namespace delimiter are presentation differences only.
    """
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.casefold()
    if not scheme:
        return None
    if parsed.netloc:
        host = parsed.hostname.casefold() if parsed.hostname else ""
        if not host:
            return None
        port = parsed.port
        authority = host if port is None else f"{host}:{port}"
        path = re.sub(r"/+", "/", parsed.path).rstrip("/#")
        if not path:
            return None
        return urlunsplit((scheme, authority, path, "", ""))
    path = parsed.path.rstrip("/#")
    return f"{scheme}:{path}" if path else None


def _text_tokens(graph: Graph, subject: URIRef) -> frozenset[str]:
    content = " ".join(
        str(value)
        for predicate in (OKG.title, OKG.description)
        for value in graph.objects(subject, predicate)
        if isinstance(value, Literal)
    ).casefold()
    return frozenset(
        token
        for token in TOKEN_RE.findall(content)
        if len(token) >= 4 and token not in TEXT_STOP_WORDS
    )


def features_for_resource(graph: Graph, subject: URIRef) -> ResourceFeatures:
    source_entities = frozenset(
        entity
        for value in _iri_values(graph, subject, OKG.wikidataId)
        if (entity := _wikidata_entity(value)) is not None
    )
    repositories = frozenset(
        normalized
        for value in _iri_values(graph, subject, OKG.sourceRepo)
        if (normalized := canonical_repository(value)) is not None
    )
    namespace_families = frozenset(
        family
        for value in _iri_values(graph, subject, OKG.namespaceURI)
        if (family := namespace_family(value)) is not None
    )
    return ResourceFeatures(
        subject=subject,
        source_entities=source_entities,
        parents=_iri_values(graph, subject, DCTERMS.isPartOf),
        uses=_iri_values(graph, subject, OKG.uses),
        source_types=_iri_values(graph, subject, OKG.sourceType),
        creators=_iri_values(graph, subject, OKG.creator),
        repositories=repositories,
        namespace_families=namespace_families,
        categories=_iri_values(graph, subject, OKG.category),
        software_types=_iri_values(graph, subject, OKG.softwareType),
        rdf_types=_iri_values(graph, subject, RDF.type),
        programming_languages=_literal_values(graph, subject, OKG.programmingLanguage),
        licenses=_iri_values(graph, subject, OKG.hasLicense),
        text_tokens=_text_tokens(graph, subject),
    )


def _comparable_subjects(graph: Graph) -> list[URIRef]:
    return sorted(
        {
            subject
            for subject in graph.subjects(OKG.wikidataId, None)
            if isinstance(subject, URIRef) and (subject, OKG.homepage, None) in graph
        },
        key=str,
    )


def build_similarity_context(graphs: Iterable[Graph]) -> SimilarityContext:
    """Build global parent degree and catalog-identity context across catalogs."""
    parent_members: dict[str, set[str]] = {}
    source_type_members: dict[str, set[str]] = {}
    catalog_source_entities: set[str] = set()
    for graph in graphs:
        for subject in _comparable_subjects(graph):
            features = features_for_resource(graph, subject)
            catalog_source_entities.update(features.source_entities)
            for parent in features.parents:
                parent_members.setdefault(parent, set()).add(str(subject))
            for source_type in features.source_types:
                source_type_members.setdefault(source_type, set()).add(str(subject))
    return SimilarityContext(
        parent_degrees={parent: len(members) for parent, members in parent_members.items()},
        catalog_source_entities=frozenset(catalog_source_entities),
        source_type_degrees={
            source_type: len(members) for source_type, members in source_type_members.items()
        },
    )


def _intersection(left: frozenset[str], right: frozenset[str]) -> tuple[str, ...]:
    return tuple(sorted(left & right))


def score_pair(
    left: ResourceFeatures,
    right: ResourceFeatures,
    config: SimilarityConfig = DEFAULT_CONFIG,
    context: SimilarityContext | None = None,
) -> PairScore:
    if left.subject == right.subject:
        raise ValueError("A resource cannot be scored against itself")

    components: list[ScoreComponent] = []
    qualifying_reasons: list[str] = []

    direct_evidence = []
    if left.parents & right.source_entities:
        direct_evidence.append(f"{left.subject} dcterms:isPartOf {right.subject}")
    if right.parents & left.source_entities:
        direct_evidence.append(f"{right.subject} dcterms:isPartOf {left.subject}")
    if direct_evidence:
        components.append(
            ScoreComponent(
                "direct_relationship",
                config.direct_relationship,
                tuple(sorted(direct_evidence)),
            )
        )
        qualifying_reasons.append("direct_relationship")

    uses_evidence = []
    if left.uses & right.source_entities:
        uses_evidence.append(f"{left.subject} okg:uses {right.subject}")
    if right.uses & left.source_entities:
        uses_evidence.append(f"{right.subject} okg:uses {left.subject}")
    if uses_evidence:
        components.append(
            ScoreComponent("direct_uses", config.direct_uses, tuple(sorted(uses_evidence)))
        )
        qualifying_reasons.append("direct_uses")

    shared_parents = _intersection(left.parents, right.parents)
    if context is not None:
        shared_parents = tuple(
            parent
            for parent in shared_parents
            if parent in context.catalog_source_entities
            and context.parent_degrees.get(parent, 2) <= config.max_shared_parent_degree
        )
    if shared_parents:
        components.append(
            ScoreComponent("shared_parent", config.shared_parent, shared_parents)
        )
        qualifying_reasons.append("shared_parent")

    shared_source_types: tuple[str, ...] = ()
    if context is not None:
        shared_source_types = tuple(
            source_type
            for source_type in _intersection(left.source_types, right.source_types)
            if 2 <= context.source_type_degrees.get(source_type, 0)
            <= config.max_shared_source_type_degree
        )
    if shared_source_types:
        components.append(
            ScoreComponent(
                "shared_source_type", config.shared_source_type, shared_source_types
            )
        )
        qualifying_reasons.append("shared_source_type")

    structural_specs = (
        ("same_repository", config.same_repository, left.repositories, right.repositories),
        (
            "same_namespace_family",
            config.same_namespace_family,
            left.namespace_families,
            right.namespace_families,
        ),
        ("shared_creator", config.shared_creator, left.creators, right.creators),
    )
    for feature, weight, left_values, right_values in structural_specs:
        evidence = _intersection(left_values, right_values)
        if evidence:
            components.append(ScoreComponent(feature, weight, evidence))
            qualifying_reasons.append(feature)

    weak_specs = (
        ("shared_category", config.shared_category, left.categories, right.categories),
        (
            "shared_software_type",
            config.shared_software_type,
            left.software_types,
            right.software_types,
        ),
        ("shared_rdf_type", config.shared_rdf_type, left.rdf_types, right.rdf_types),
        (
            "shared_programming_language",
            config.shared_programming_language,
            left.programming_languages,
            right.programming_languages,
        ),
        ("shared_license", config.shared_license, left.licenses, right.licenses),
    )
    for feature, weight, left_values, right_values in weak_specs:
        evidence = _intersection(left_values, right_values)
        if evidence:
            components.append(ScoreComponent(feature, weight, evidence))

    shared_tokens = left.text_tokens & right.text_tokens
    token_union = left.text_tokens | right.text_tokens
    if len(shared_tokens) >= 2 and token_union:
        similarity = len(shared_tokens) / len(token_union)
        if similarity >= config.text_similarity_floor:
            evidence = (*sorted(shared_tokens), f"jaccard={similarity:.6f}")
            components.append(ScoreComponent("text_similarity", config.text_similarity, evidence))

    score = sum(component.score for component in components)
    qualifies = bool(qualifying_reasons) and score >= config.score_threshold
    return PairScore(
        subject=str(left.subject),
        candidate=str(right.subject),
        score=score,
        qualifies=qualifies,
        qualifying_reasons=tuple(qualifying_reasons),
        components=tuple(components),
    )


def _index(
    features: Iterable[ResourceFeatures],
    attribute: str,
) -> dict[str, set[URIRef]]:
    result: dict[str, set[URIRef]] = {}
    for feature_set in features:
        for value in getattr(feature_set, attribute):
            result.setdefault(value, set()).add(feature_set.subject)
    return result


def add_related_resources(
    graph: Graph,
    dataset: str,
    config: SimilarityConfig = DEFAULT_CONFIG,
    context: SimilarityContext | None = None,
) -> dict[str, object]:
    """Replace related links in one graph and return deterministic diagnostics."""
    graph.remove((None, OKG.relatedTo, None))
    subjects = _comparable_subjects(graph)
    feature_map = {subject: features_for_resource(graph, subject) for subject in subjects}
    feature_values = list(feature_map.values())
    indexes = {
        attribute: _index(feature_values, attribute)
        for attribute in (
            "source_entities",
            "parents",
            "uses",
            "source_types",
            "creators",
            "repositories",
            "namespace_families",
        )
    }
    context = context or build_similarity_context((graph,))
    suppressed_shared_parents = []
    for parent in sorted(indexes["parents"]):
        degree = context.parent_degrees.get(parent, len(indexes["parents"][parent]))
        reasons = []
        if parent not in context.catalog_source_entities:
            reasons.append("parent_not_cataloged")
        if degree > config.max_shared_parent_degree:
            reasons.append("degree_exceeds_limit")
        if reasons:
            suppressed_shared_parents.append(
                {"parent": parent, "memberCount": degree, "reasons": reasons}
            )
    suppressed_shared_source_types = []
    for source_type in sorted(indexes["source_types"]):
        degree = context.source_type_degrees.get(
            source_type, len(indexes["source_types"][source_type])
        )
        if degree > config.max_shared_source_type_degree:
            suppressed_shared_source_types.append(
                {
                    "sourceType": source_type,
                    "memberCount": degree,
                    "reasons": ["degree_exceeds_limit"],
                }
            )

    selected_scores: list[PairScore] = []
    structural_candidate_count = 0
    below_threshold_count = 0
    for subject in subjects:
        features = feature_map[subject]
        candidates: set[URIRef] = set()
        for attribute in ("creators", "repositories", "namespace_families"):
            for value in getattr(features, attribute):
                candidates.update(indexes[attribute].get(value, set()))
        for parent in features.parents:
            if (
                parent in context.catalog_source_entities
                and context.parent_degrees.get(parent, 0) <= config.max_shared_parent_degree
            ):
                candidates.update(indexes["parents"].get(parent, set()))
        for parent in features.parents:
            candidates.update(indexes["source_entities"].get(parent, set()))
        for source_entity in features.source_entities:
            candidates.update(indexes["parents"].get(source_entity, set()))
            candidates.update(indexes["uses"].get(source_entity, set()))
        for used_entity in features.uses:
            candidates.update(indexes["source_entities"].get(used_entity, set()))
        for source_type in features.source_types:
            if (
                2 <= context.source_type_degrees.get(source_type, 0)
                <= config.max_shared_source_type_degree
            ):
                candidates.update(indexes["source_types"].get(source_type, set()))
        candidates.discard(subject)

        scored: list[PairScore] = []
        for candidate in sorted(candidates, key=str):
            result = score_pair(
                features,
                feature_map[candidate],
                config,
                context,
            )
            structural_candidate_count += 1
            if result.qualifies:
                scored.append(result)
            else:
                below_threshold_count += 1
        scored.sort(key=lambda result: (-result.score, result.candidate))
        for result in scored[: config.max_related]:
            graph.add((subject, OKG.relatedTo, URIRef(result.candidate)))
            selected_scores.append(result)

    selected_scores.sort(key=lambda result: (result.subject, -result.score, result.candidate))
    reason_distribution = Counter(
        reason for result in selected_scores for reason in result.qualifying_reasons
    )
    return {
        "dataset": dataset,
        "candidateResourceCount": len(subjects),
        "structuralCandidateCount": structural_candidate_count,
        "belowThresholdCount": below_threshold_count,
        "suppressedSharedParentCount": len(suppressed_shared_parents),
        "suppressedSharedParents": suppressed_shared_parents,
        "suppressedSharedSourceTypeCount": len(suppressed_shared_source_types),
        "suppressedSharedSourceTypes": suppressed_shared_source_types,
        "selectedRelationshipCount": len(selected_scores),
        "qualifyingReasonDistribution": dict(sorted(reason_distribution.items())),
        "relationships": [result.as_dict() for result in selected_scores],
    }


def diagnostics_document(
    catalogs: Iterable[dict[str, object]],
    config: SimilarityConfig = DEFAULT_CONFIG,
    source_audit: dict[str, object] | None = None,
) -> dict[str, object]:
    document = {
        "schemaVersion": "2.0.0",
        "scoringConfig": asdict(config),
        "scoringPolicyRationale": {
            "maxSharedSourceTypeDegree": SOURCE_TYPE_DEGREE_RATIONALE,
        },
        "catalogs": sorted(catalogs, key=lambda catalog: str(catalog["dataset"])),
    }
    if source_audit is not None:
        document["sourceRelationshipAudit"] = source_audit
    return document


def write_diagnostics_atomic(payload: dict[str, object], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
