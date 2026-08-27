"""Reviewed first-party qualification without changing aggregator classification.

The base classifier remains the sole strong-vocabulary route. This module
adds a narrowly scoped contextual route for explicitly approved first-party
career sources after source-specific boilerplate removal. Every policy term,
role family, exclusion, and strip marker is loaded from RDF.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph, Namespace, RDF
from rdflib.namespace import DCTERMS, RDFS, SKOS

from classifier import Evidence, MatchTerm, classify, find_evidence, normalize
from live_records import evidence_json

KGJOBS = Namespace("https://openknowledgegraphs.com/jobs/ontology#")
KGJV = Namespace("https://openknowledgegraphs.com/jobs/vocab/")
FIELD_ORDER = ("title", "description", "qualifications", "responsibilities")


class FirstPartyPolicyError(RuntimeError):
    """The declarative qualification policy is incomplete or ambiguous."""


@dataclass(frozen=True)
class RoleFamily:
    uri: str
    label: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class SourceStripPolicy:
    prefix_markers: tuple[str, ...]
    suffix_markers: tuple[str, ...]
    identity_concepts: frozenset[str]


@dataclass(frozen=True)
class FirstPartyPolicy:
    role_families: tuple[RoleFamily, ...]
    excluded_title_terms: tuple[str, ...]
    placeholder_title_terms: tuple[str, ...]
    contextual_terms: tuple[MatchTerm, ...]
    source_policies: dict[str, SourceStripPolicy]


def _one(graph: Graph, subject, predicate, label: str):
    values = list(graph.objects(subject, predicate))
    if len(values) != 1:
        raise FirstPartyPolicyError(
            f"{subject} requires exactly one {label}; found {len(values)}"
        )
    return values[0]


def load_first_party_policy(vocab_path: Path) -> FirstPartyPolicy:
    graph = Graph().parse(vocab_path, format="turtle")
    policy = KGJV["first-party-contextual-policy"]
    if (policy, RDF.type, KGJOBS.FirstPartyQualificationPolicy) not in graph:
        raise FirstPartyPolicyError("first-party contextual policy is missing")

    families = []
    for family in sorted(graph.objects(policy, KGJOBS.approvedRoleFamily), key=str):
        terms = tuple(sorted({str(value).casefold() for value in graph.objects(family, KGJOBS.roleFamilyTerm)}))
        if not terms:
            raise FirstPartyPolicyError(f"role family {family} has no terms")
        families.append(RoleFamily(
            uri=str(family),
            label=str(_one(graph, family, RDFS.label, "label")),
            terms=terms,
        ))

    contextual_terms = []
    for term_node in sorted(graph.objects(policy, KGJOBS.contextualPolicyTerm), key=str):
        concept = _one(graph, term_node, KGJOBS.policyConcept, "policy concept")
        case_sensitive = _one(graph, term_node, KGJOBS.caseSensitive, "case sensitivity")
        if not isinstance(case_sensitive.toPython(), bool):
            raise FirstPartyPolicyError(f"{term_node} case sensitivity is not boolean")
        contextual_terms.append(MatchTerm(
            text=str(_one(graph, term_node, KGJOBS.termText, "term text")),
            case_sensitive=case_sensitive.toPython(),
            concept_uri=str(concept),
            concept_label=str(_one(graph, concept, SKOS.prefLabel, "concept label")),
            concept_scheme="First-party contextual product concepts",
            strength="contextual",
        ))

    source_policies = {}
    for node in graph.subjects(RDF.type, KGJOBS.FirstPartyQualificationPolicy):
        if node == policy:
            continue
        sources = list(graph.objects(node, KGJOBS.appliesToSource))
        if len(sources) != 1:
            raise FirstPartyPolicyError(f"source policy {node} requires exactly one source")
        source_uri = str(sources[0])
        if source_uri in source_policies:
            raise FirstPartyPolicyError(f"duplicate strip policy for {source_uri}")
        source_policies[source_uri] = SourceStripPolicy(
            prefix_markers=tuple(sorted({str(value) for value in graph.objects(node, KGJOBS.stripPrefixThrough)})),
            suffix_markers=tuple(sorted({str(value) for value in graph.objects(node, KGJOBS.stripSuffixFrom)})),
            identity_concepts=frozenset(str(value) for value in graph.objects(node, KGJOBS.identityConcept)),
        )

    return FirstPartyPolicy(
        role_families=tuple(families),
        excluded_title_terms=tuple(sorted(str(value).casefold() for value in graph.objects(policy, KGJOBS.excludedTitleTerm))),
        placeholder_title_terms=tuple(sorted(str(value).casefold() for value in graph.objects(policy, KGJOBS.placeholderTitleTerm))),
        contextual_terms=tuple(sorted(contextual_terms, key=lambda term: (term.concept_uri, term.text))),
        source_policies=source_policies,
    )


def _contains_term(text: str, term: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.IGNORECASE) is not None


def _strip_description(description: str, source_policy: SourceStripPolicy) -> tuple[str, list[dict]]:
    text = normalize(html.unescape(description or "").replace("\xa0", " "))
    removed = []
    folded = text.casefold()
    prefix_hits = [
        (folded.find(marker.casefold()), marker)
        for marker in source_policy.prefix_markers
        if folded.find(marker.casefold()) >= 0
    ]
    if prefix_hits:
        index, marker = min(prefix_hits, key=lambda item: item[0])
        end = index + len(marker)
        removed.append({"kind": "prefix", "marker": marker, "characters": end})
        text = text[end:].lstrip(" :-–—")
        folded = text.casefold()
    suffix_hits = [
        (folded.find(marker.casefold()), marker)
        for marker in source_policy.suffix_markers
        if folded.find(marker.casefold()) >= 0
    ]
    if suffix_hits:
        index, marker = min(suffix_hits, key=lambda item: item[0])
        removed.append({"kind": "suffix", "marker": marker, "characters": len(text) - index})
        text = text[:index].rstrip()
    return text, removed


def classify_first_party_record(
    record: dict,
    base_terms: list[MatchTerm],
    policy: FirstPartyPolicy,
) -> dict:
    """Classify one first-party record and retain its unmodified source text."""
    if not record.get("firstParty"):
        raise FirstPartyPolicyError("first-party classifier received a non-first-party record")
    source_uri = str(record.get("sourceDataset") or "")
    source_policy = policy.source_policies.get(
        source_uri, SourceStripPolicy((), (), frozenset())
    )
    working = {field: record.get(field) for field in FIELD_ORDER}
    stripped_description, stripped_spans = _strip_description(
        str(record.get("description") or ""), source_policy
    )
    working["description"] = stripped_description

    base_evidence = find_evidence(working, base_terms)
    policy_evidence = find_evidence(working, policy.contextual_terms)
    all_evidence = sorted(
        [*base_evidence, *policy_evidence],
        key=lambda item: (FIELD_ORDER.index(item.source_field), item.concept_uri, item.matched_phrase),
    )
    base_result = classify(base_evidence)
    title = normalize(record.get("title")).casefold()
    placeholder = next((term for term in policy.placeholder_title_terms if _contains_term(title, term)), None)
    excluded = next((term for term in policy.excluded_title_terms if _contains_term(title, term)), None)
    family = next(
        (
            role_family for role_family in policy.role_families
            if any(_contains_term(title, term) for term in role_family.terms)
        ),
        None,
    )
    contextual_concepts = sorted({
        evidence.concept_uri
        for evidence in all_evidence
        if not evidence.negated
        and evidence.strength == "contextual"
        and evidence.concept_uri not in source_policy.identity_concepts
    })

    if placeholder:
        result = "not_match"
        route = "placeholder-exclusion"
        reason = f"title matches reviewed placeholder term {placeholder!r}"
    elif base_result == "qualified":
        result = "qualified"
        route = "strong-vocabulary"
        reason = "job-specific text contains an unnegated strong vocabulary concept"
    elif excluded:
        result = "not_match"
        route = "generic-role-exclusion"
        reason = f"title matches excluded generic role term {excluded!r} without strong evidence"
    elif family and len(contextual_concepts) >= 2:
        result = "qualified"
        route = "first-party-contextual"
        reason = (
            f"concrete {family.label} opening has {len(contextual_concepts)} distinct "
            "unnegated contextual KG/product concepts"
        )
    elif all_evidence:
        result = "review"
        route = "insufficient-contextual-evidence"
        reason = "job-specific text does not satisfy the bounded contextual qualification gate"
    else:
        result = "not_match"
        route = "no-job-specific-evidence"
        reason = "job-specific text contains no controlled KG evidence"

    enriched = dict(record)
    enriched["classification"] = result
    enriched["evidence"] = evidence_json(all_evidence)
    enriched["qualificationAudit"] = {
        "baseClassification": base_result,
        "contextualConcepts": contextual_concepts,
        "excludedTitleTerm": excluded,
        "placeholderTitleTerm": placeholder,
        "reason": reason,
        "roleFamily": family.label if family else None,
        "route": route,
        "strippedBoilerplate": stripped_spans,
    }
    return enriched


def classify_first_party_records(
    records: list[dict], base_terms: list[MatchTerm], policy: FirstPartyPolicy
) -> list[dict]:
    return sorted(
        (classify_first_party_record(record, base_terms, policy) for record in records),
        key=lambda record: record["id"],
    )
