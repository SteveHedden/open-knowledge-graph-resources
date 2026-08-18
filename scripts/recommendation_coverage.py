#!/usr/bin/env python3
"""Release gating for recommendations on the exact final page set."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping


class RecommendationCoverageError(RuntimeError):
    def __init__(self, report: dict[str, object]):
        self.report = report
        messages = report.get("gate", {}).get("errors", [])
        super().__init__("; ".join(str(message) for message in messages))


def coverage_by_catalog(
    survivors: Iterable[tuple[str, Mapping[str, object], str, str]],
) -> dict[str, dict[str, object]]:
    rows = list(survivors)
    survivor_urls = {
        str(item.get("canonicalUrl"))
        for _, item, _, _ in rows
        if item.get("canonicalUrl")
    }
    result: dict[str, dict[str, object]] = {}
    for dataset in ("resource", "software"):
        dataset_rows = [item for row_dataset, item, _, _ in rows if row_dataset == dataset]
        with_recommendations = 0
        selected_links = 0
        for item in dataset_rows:
            links = {
                str(related.get("canonicalUrl"))
                for related in item.get("relatedTools", [])
                if isinstance(related, dict)
                and related.get("canonicalUrl") in survivor_urls
            }
            if links:
                with_recommendations += 1
                selected_links += len(links)
        total = len(dataset_rows)
        coverage = with_recommendations / total if total else 0.0
        result[dataset] = {
            "finalPageCount": total,
            "pagesWithRecommendations": with_recommendations,
            "pagesWithoutRecommendations": total - with_recommendations,
            "coverageShare": round(coverage, 6),
            "emptyShare": round(1.0 - coverage, 6) if total else 1.0,
            "selectedLinkCount": selected_links,
        }
    return result


def qualifying_reasons_by_catalog(
    survivors: Iterable[tuple[str, Mapping[str, object], str, str]],
    diagnostics: Mapping[str, object],
) -> dict[str, dict[str, int]]:
    """Count selected reasons restricted to links renderable on final pages."""
    survivor_urls = {
        str(item.get("canonicalUrl"))
        for _, item, _, _ in survivors
        if item.get("canonicalUrl")
    }
    result: dict[str, dict[str, int]] = {}
    catalogs = diagnostics.get("catalogs", [])
    for dataset in ("resource", "software"):
        counter: Counter[str] = Counter()
        catalog = next(
            (
                row for row in catalogs
                if isinstance(row, dict) and row.get("dataset") == dataset
            ),
            {},
        )
        for relationship in catalog.get("relationships", []):
            if not isinstance(relationship, dict):
                continue
            if (
                relationship.get("subject") not in survivor_urls
                or relationship.get("candidate") not in survivor_urls
            ):
                continue
            counter.update(str(reason) for reason in relationship.get("qualifyingReasons", []))
        result[dataset] = dict(sorted(counter.items()))
    return result


def baseline_survivors(baseline_root: Path) -> list[tuple[str, dict[str, object], str, str]]:
    registry_path = baseline_root / "data" / "page_qids.json"
    if not registry_path.is_file():
        return []
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    rows = []
    for dataset, filename in (("resource", "ontologies.json"), ("software", "software.json")):
        payload = json.loads((baseline_root / "data" / filename).read_text(encoding="utf-8"))
        by_qid = {
            str(item.get("wikidataId", "")).rstrip("/").split("/")[-1]: item
            for item in payload.get("items", [])
            if isinstance(item, dict)
        }
        for qid, slug in registry.get(dataset, {}).items():
            if qid in by_qid:
                rows.append((dataset, by_qid[qid], qid, str(slug)))
    return rows


def baseline_generation_id(baseline_root: Path) -> str | None:
    manifest = baseline_root / "data" / "manifest.json"
    if not manifest.is_file():
        return None
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    value = payload.get("generationId")
    return value if isinstance(value, str) else None


def evaluate_coverage(
    candidate: dict[str, dict[str, object]],
    baseline: dict[str, dict[str, object]] | None,
    policy: Mapping[str, object],
    baseline_id: str | None,
) -> dict[str, object]:
    maximum_empty = float(policy["maximumEmptyShare"])
    maximum_decline = float(policy["maximumUnreviewedCoverageDecline"])
    accepted = {
        (entry.get("baselineGenerationId"), entry.get("catalog"))
        for entry in policy.get("acceptedDeclines", [])
        if isinstance(entry, dict) and str(entry.get("rationale", "")).strip()
    }
    accepted_shortfalls = {
        (entry.get("baselineGenerationId"), entry.get("catalog")): entry
        for entry in policy.get("acceptedShortfalls", [])
        if isinstance(entry, dict)
        and str(entry.get("rationale", "")).strip()
        and isinstance(entry.get("minimumCoverageShare"), (int, float))
    }
    errors = []
    comparisons: dict[str, dict[str, object]] = {}
    normal_floor = 1.0 - maximum_empty
    effective_floors: dict[str, float] = {}
    for dataset in ("resource", "software"):
        current = float(candidate[dataset]["coverageShare"])
        reviewed_shortfall = accepted_shortfalls.get((baseline_id, dataset))
        reviewed_floor = (
            float(reviewed_shortfall["minimumCoverageShare"])
            if reviewed_shortfall is not None
            else None
        )
        effective_floor = normal_floor
        shortfall_applied = False
        if current < normal_floor and reviewed_floor is not None:
            effective_floor = reviewed_floor
            shortfall_applied = current >= reviewed_floor
        effective_floors[dataset] = effective_floor
        if current + 1e-12 < effective_floor:
            errors.append(
                f"{dataset} recommendation coverage {current:.1%} is below "
                f"the required {effective_floor:.1%} floor"
            )
        comparisons[dataset] = {
            "normalMinimumCoverageShare": normal_floor,
            "effectiveMinimumCoverageShare": effective_floor,
            "reviewedShortfallApplied": shortfall_applied,
        }
        if baseline is not None:
            prior = float(baseline[dataset]["coverageShare"])
            decline = prior - current
            reviewed = (baseline_id, dataset) in accepted
            comparisons[dataset].update({
                "baselineCoverageShare": prior,
                "decline": round(decline, 6),
                "reviewedException": reviewed,
            })
            if decline > maximum_decline + 1e-12 and not reviewed:
                errors.append(
                    f"{dataset} recommendation coverage declined {decline:.1%}; "
                    f"the unreviewed limit is {maximum_decline:.1%}"
                )
    return {
        "schemaVersion": "1.0.0",
        "candidate": candidate,
        "baselineGenerationId": baseline_id,
        "baseline": baseline,
        "comparisons": comparisons,
        "gate": {
            "passed": not errors,
            "maximumEmptyShare": maximum_empty,
            "effectiveMinimumCoverageShareByCatalog": effective_floors,
            "maximumUnreviewedCoverageDecline": maximum_decline,
            "errors": errors,
        },
    }


def load_policy(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in ("maximumEmptyShare", "maximumUnreviewedCoverageDecline"):
        value = payload.get(key)
        if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
            raise ValueError(f"Invalid recommendation coverage policy value: {key}")
    if not isinstance(payload.get("acceptedDeclines"), list):
        raise ValueError("Recommendation coverage policy requires acceptedDeclines list")
    shortfalls = payload.get("acceptedShortfalls", [])
    if not isinstance(shortfalls, list):
        raise ValueError("Recommendation coverage policy requires acceptedShortfalls list")
    seen = set()
    for entry in shortfalls:
        if not isinstance(entry, dict):
            raise ValueError("Recommendation coverage acceptedShortfalls entries must be objects")
        baseline_id = entry.get("baselineGenerationId")
        catalog = entry.get("catalog")
        floor = entry.get("minimumCoverageShare")
        rationale = entry.get("rationale")
        if not isinstance(baseline_id, str) or not baseline_id.strip():
            raise ValueError("Recommendation coverage shortfall requires baselineGenerationId")
        if catalog not in {"resource", "software"}:
            raise ValueError("Recommendation coverage shortfall has invalid catalog")
        if not isinstance(floor, (int, float)) or not 0.0 <= float(floor) <= 1.0:
            raise ValueError("Recommendation coverage shortfall has invalid minimumCoverageShare")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("Recommendation coverage shortfall requires rationale")
        key = (baseline_id, catalog)
        if key in seen:
            raise ValueError("Recommendation coverage shortfall entries must be unique")
        seen.add(key)
    return payload


def append_diagnostics(path: Path, report: dict[str, object]) -> None:
    payload = {}
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    payload["finalPageRecommendationCoverage"] = report
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
