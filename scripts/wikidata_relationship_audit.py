#!/usr/bin/env python3
"""Deterministic audit of direct Wikidata item-valued relationships."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True, order=True)
class DirectIriEdge:
    subject_qid: str
    property_id: str
    object_qid: str


def truthy_item_edges(entities: Mapping[str, object]) -> tuple[DirectIriEdge, ...]:
    """Extract direct/truthy item claims from an Action API entity response."""
    edges: set[DirectIriEdge] = set()
    for subject_qid, raw_entity in entities.items():
        if not isinstance(raw_entity, dict):
            continue
        claims = raw_entity.get("claims", {})
        if not isinstance(claims, dict):
            continue
        for property_id, raw_statements in claims.items():
            if not isinstance(raw_statements, list):
                continue
            statements = [row for row in raw_statements if isinstance(row, dict)]
            preferred = [row for row in statements if row.get("rank") == "preferred"]
            truthy = preferred or [row for row in statements if row.get("rank") == "normal"]
            for statement in truthy:
                snak = statement.get("mainsnak", {})
                if not isinstance(snak, dict) or snak.get("datatype") != "wikibase-item":
                    continue
                value = snak.get("datavalue", {}).get("value", {})
                if not isinstance(value, dict):
                    continue
                object_qid = value.get("id")
                if isinstance(object_qid, str):
                    edges.add(DirectIriEdge(subject_qid, property_id, object_qid))
    return tuple(sorted(edges))


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[math.ceil(percentile * len(ordered)) - 1]


def _degree_summary(counter: Counter[str]) -> dict[str, object]:
    values = list(counter.values())
    return {
        "minimum": min(values, default=0),
        "median": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "maximum": max(values, default=0),
        "mean": round(sum(values) / len(values), 6) if values else 0.0,
    }


def audit_document(
    edges: Iterable[DirectIriEdge],
    cohort_catalogs: Mapping[str, frozenset[str]],
    reviewed_property_ids: Iterable[str],
    labels: Mapping[str, str] | None = None,
    representative_limit: int = 5,
) -> dict[str, object]:
    """Describe every in-cohort direct IRI edge without making it scoreable."""
    labels = labels or {}
    reviewed = frozenset(reviewed_property_ids)
    cohort = frozenset(cohort_catalogs)
    intercohort = sorted(
        edge for edge in set(edges)
        if edge.subject_qid in cohort and edge.object_qid in cohort
    )
    by_property: dict[str, list[DirectIriEdge]] = {}
    for edge in intercohort:
        by_property.setdefault(edge.property_id, []).append(edge)

    outgoing_by_property: dict[str, list[DirectIriEdge]] = {
        property_id: [] for property_id in reviewed
    }
    for edge in sorted(set(edges)):
        if edge.subject_qid in cohort and edge.property_id in reviewed:
            outgoing_by_property[edge.property_id].append(edge)

    reviewed_profiles = []
    for property_id, rows in sorted(outgoing_by_property.items()):
        object_degree = Counter(row.object_qid for row in rows)
        subject_catalogs: dict[str, set[str]] = {}
        for row in rows:
            for catalog in cohort_catalogs[row.subject_qid]:
                subject_catalogs.setdefault(catalog, set()).add(row.subject_qid)
        reviewed_profiles.append(
            {
                "sourcePropertyId": property_id,
                "edgeCount": len(rows),
                "uniqueSubjectCount": len({row.subject_qid for row in rows}),
                "uniqueObjectCount": len(object_degree),
                "subjectCoverageByCatalog": {
                    catalog: len(subjects)
                    for catalog, subjects in sorted(subject_catalogs.items())
                },
                "objectDegree": _degree_summary(object_degree),
                "highestDegreeObjects": [
                    {
                        "objectQid": qid,
                        "objectLabel": labels.get(qid, qid),
                        "memberCount": count,
                    }
                    for qid, count in sorted(
                        object_degree.items(), key=lambda row: (-row[1], row[0])
                    )[:20]
                ],
            }
        )

    predicates = []
    for property_id, rows in sorted(by_property.items()):
        subjects = {row.subject_qid for row in rows}
        objects = {row.object_qid for row in rows}
        records = subjects | objects
        out_degree = Counter(row.subject_qid for row in rows)
        in_degree = Counter(row.object_qid for row in rows)
        same_catalog = 0
        catalog_subjects: dict[str, set[str]] = {}
        for row in rows:
            shared = cohort_catalogs[row.subject_qid] & cohort_catalogs[row.object_qid]
            if shared:
                same_catalog += 1
            for catalog in cohort_catalogs[row.subject_qid]:
                catalog_subjects.setdefault(catalog, set()).add(row.subject_qid)
        representatives = [
            {
                "subjectQid": row.subject_qid,
                "subjectLabel": labels.get(row.subject_qid, row.subject_qid),
                "objectQid": row.object_qid,
                "objectLabel": labels.get(row.object_qid, row.object_qid),
                "sameCatalog": bool(
                    cohort_catalogs[row.subject_qid] & cohort_catalogs[row.object_qid]
                ),
            }
            for row in rows[:representative_limit]
        ]
        predicates.append(
            {
                "sourcePropertyId": property_id,
                "reviewedForScoring": property_id in reviewed,
                "edgeCount": len(rows),
                "sameCatalogEdgeCount": same_catalog,
                "crossCatalogEdgeCount": len(rows) - same_catalog,
                "uniqueSubjectCount": len(subjects),
                "uniqueObjectCount": len(objects),
                "uniqueRecordCount": len(records),
                "subjectCoverageByCatalog": {
                    catalog: len(subject_qids)
                    for catalog, subject_qids in sorted(catalog_subjects.items())
                },
                "outDegree": _degree_summary(out_degree),
                "inDegree": _degree_summary(in_degree),
                "representativeEdges": representatives,
            }
        )

    digest_input = "\n".join(
        f"{qid}:{','.join(sorted(cohort_catalogs[qid]))}" for qid in sorted(cohort)
    )
    return {
        "schemaVersion": "1.0.0",
        "cohort": {
            "recordCount": len(cohort),
            "catalogRecordCounts": {
                catalog: sum(catalog in memberships for memberships in cohort_catalogs.values())
                for catalog in sorted({value for values in cohort_catalogs.values() for value in values})
            },
            "sha256": hashlib.sha256(digest_input.encode("utf-8")).hexdigest(),
        },
        "intercohortEdgeCount": len(intercohort),
        "predicateCount": len(predicates),
        "predicates": predicates,
        "reviewedPredicateAvailability": reviewed_profiles,
    }


def dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
