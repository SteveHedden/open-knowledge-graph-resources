"""Deterministic, page-backed catalog mentions for job postings."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_URL = "https://openknowledgegraphs.com"
QID_RE = re.compile(r"Q\d+$")
SHORT_ACRONYM_RE = re.compile(r"[A-Za-z0-9]{2,5}")
DATASET_ORDER = {"resource": 0, "software": 1}
FIELD_ORDER = (("title", 0), ("description", 1))


class CatalogMentionError(ValueError):
    """Raised when catalog mention configuration is structurally invalid."""


@dataclass(frozen=True)
class CatalogTarget:
    dataset: str
    qid: str
    title: str
    canonical_url: str

    @property
    def key(self) -> tuple[str, str]:
        return self.dataset, self.qid


@dataclass(frozen=True)
class MatchTerm:
    text: str
    target: CatalogTarget
    case_sensitive: bool
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class CatalogMentionIndex:
    terms: tuple[MatchTerm, ...]


@dataclass(frozen=True)
class _Candidate:
    target: CatalogTarget
    matched_phrase: str
    field_rank: int
    start: int
    end: int


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _policy_key(value: str) -> str:
    return _normalized(value).casefold()


def _qid(value: Any) -> str | None:
    candidate = str(value or "").rstrip("/").rsplit("/", 1)[-1]
    return candidate if QID_RE.fullmatch(candidate) else None


def _is_short_acronym(value: str) -> bool:
    return bool(
        value.isascii()
        and SHORT_ACRONYM_RE.fullmatch(value)
        and any(character.isalpha() for character in value)
    )


def _phrase_pattern(value: str, *, case_sensitive: bool) -> re.Pattern[str]:
    # Catalog labels are normalized, but source descriptions may separate
    # phrase tokens with newlines or repeated whitespace.
    escaped = re.escape(value).replace(r"\ ", r"\s+")
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", flags)


def load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogMentionError(f"invalid catalog mention policy: {path}") from exc
    if not isinstance(policy, dict) or policy.get("schemaVersion") != 1:
        raise CatalogMentionError("catalog mention policy must use schemaVersion 1")
    for field in ("shortAcronymAllowlist", "denylist"):
        values = policy.get(field)
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not _normalized(value) for value in values
        ):
            raise CatalogMentionError(f"catalog mention policy {field} must be a string array")
    for field in ("reviewedAliases", "disambiguationOverrides"):
        mappings = policy.get(field)
        if not isinstance(mappings, dict):
            raise CatalogMentionError(f"catalog mention policy {field} must be an object")
        for phrase, target in mappings.items():
            if (
                not isinstance(phrase, str)
                or not _normalized(phrase)
                or not isinstance(target, dict)
                or target.get("dataset") not in DATASET_ORDER
                or not QID_RE.fullmatch(str(target.get("qid", "")))
            ):
                raise CatalogMentionError(f"invalid {field} entry: {phrase!r}")
    return policy


def build_match_index(
    ontologies: dict[str, Any],
    software: dict[str, Any],
    page_qids: dict[str, Any],
    policy: dict[str, Any],
) -> CatalogMentionIndex:
    acronym_allowlist = set(policy["shortAcronymAllowlist"])
    denylist = {_policy_key(value) for value in policy["denylist"]}
    overrides = {
        _policy_key(phrase): (target["dataset"], target["qid"])
        for phrase, target in policy["disambiguationOverrides"].items()
    }
    reviewed_aliases = {
        _normalized(phrase): (target["dataset"], target["qid"])
        for phrase, target in policy["reviewedAliases"].items()
    }

    surface_targets: dict[str, dict[tuple[str, str], tuple[str, CatalogTarget, bool]]] = {}
    targets: dict[tuple[str, str], CatalogTarget] = {}
    for dataset, payload in (("resource", ontologies), ("software", software)):
        items = payload.get("items") if isinstance(payload, dict) else None
        slugs = page_qids.get(dataset) if isinstance(page_qids, dict) else None
        if not isinstance(items, list) or not isinstance(slugs, dict):
            raise CatalogMentionError(f"invalid {dataset} catalog mention inputs")
        for item in items:
            if not isinstance(item, dict):
                continue
            qid = _qid(item.get("wikidataId"))
            title = _normalized(str(item.get("title") or ""))
            slug = slugs.get(qid) if qid else None
            if not qid or not title or not isinstance(slug, str) or not slug.strip():
                continue
            target = CatalogTarget(
                dataset=dataset,
                qid=qid,
                title=title,
                canonical_url=f"{BASE_URL}/{dataset}/{slug.strip('/')}/",
            )
            targets[target.key] = target
            aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
            for raw_surface in (title, *aliases):
                if not isinstance(raw_surface, str):
                    continue
                surface = _normalized(raw_surface)
                if not surface or _policy_key(surface) in denylist:
                    continue
                short_acronym = _is_short_acronym(surface)
                if short_acronym and surface not in acronym_allowlist:
                    continue
                key = _policy_key(surface)
                surface_targets.setdefault(key, {})[target.key] = (
                    surface,
                    target,
                    short_acronym,
                )

    for surface, target_key in reviewed_aliases.items():
        target = targets.get(target_key)
        if target is None:
            raise CatalogMentionError(
                f"reviewed alias {surface!r} targets a catalog item without a current page"
            )
        short_acronym = _is_short_acronym(surface)
        if short_acronym and surface not in acronym_allowlist:
            raise CatalogMentionError(
                f"reviewed short alias {surface!r} is absent from the acronym allowlist"
            )
        surface_targets.setdefault(_policy_key(surface), {})[target.key] = (
            surface,
            target,
            short_acronym,
        )

    terms: list[MatchTerm] = []
    for key, target_map in surface_targets.items():
        selected = target_map
        if len(target_map) > 1:
            override = overrides.get(key)
            if override is None or override not in target_map:
                continue
            selected = {override: target_map[override]}
        for surface, target, case_sensitive in selected.values():
            terms.append(
                MatchTerm(
                    text=surface,
                    target=target,
                    case_sensitive=case_sensitive,
                    pattern=_phrase_pattern(surface, case_sensitive=case_sensitive),
                )
            )
    terms.sort(
        key=lambda term: (
            -len(term.text),
            DATASET_ORDER[term.target.dataset],
            term.target.title.casefold(),
            term.target.qid,
            term.text.casefold(),
            term.text,
        )
    )
    return CatalogMentionIndex(tuple(terms))


def load_match_index(catalog_root: Path, policy_path: Path) -> CatalogMentionIndex:
    def read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogMentionError(f"invalid catalog mention input: {path}") from exc
        if not isinstance(payload, dict):
            raise CatalogMentionError(f"catalog mention input must be an object: {path}")
        return payload

    return build_match_index(
        read_json(catalog_root / "data" / "ontologies.json"),
        read_json(catalog_root / "data" / "software.json"),
        read_json(catalog_root / "data" / "page_qids.json"),
        load_policy(policy_path),
    )


def _overlap(left: _Candidate, right: _Candidate) -> bool:
    return left.start < right.end and right.start < left.end


def catalog_mentions(record: dict[str, Any], index: CatalogMentionIndex) -> list[dict[str, str]]:
    selected: list[_Candidate] = []
    for field, field_rank in FIELD_ORDER:
        text = record.get(field)
        if not isinstance(text, str) or not text:
            continue
        candidates = [
            _Candidate(
                target=term.target,
                matched_phrase=match.group(0),
                field_rank=field_rank,
                start=match.start(),
                end=match.end(),
            )
            for term in index.terms
            for match in term.pattern.finditer(text)
        ]
        # Prefer the longest phrase only when spans overlap. Non-overlapping
        # mentions remain independent even when one surface is globally longer.
        candidates.sort(
            key=lambda candidate: (
                -(candidate.end - candidate.start),
                candidate.start,
                DATASET_ORDER[candidate.target.dataset],
                candidate.target.title.casefold(),
                candidate.target.qid,
            )
        )
        field_selected: list[_Candidate] = []
        for candidate in candidates:
            if not any(_overlap(candidate, existing) for existing in field_selected):
                field_selected.append(candidate)
        selected.extend(field_selected)

    selected.sort(
        key=lambda candidate: (
            candidate.field_rank,
            candidate.start,
            DATASET_ORDER[candidate.target.dataset],
            candidate.target.title.casefold(),
            candidate.target.qid,
        )
    )
    earliest_by_target: dict[tuple[str, str], _Candidate] = {}
    for candidate in selected:
        earliest_by_target.setdefault(candidate.target.key, candidate)
    ordered = sorted(
        earliest_by_target.values(),
        key=lambda candidate: (
            candidate.field_rank,
            candidate.start,
            DATASET_ORDER[candidate.target.dataset],
            candidate.target.title.casefold(),
            candidate.target.qid,
        ),
    )
    return [
        {
            "title": candidate.target.title,
            "dataset": candidate.target.dataset,
            "qid": candidate.target.qid,
            "canonicalUrl": candidate.target.canonical_url,
            "matchedPhrase": candidate.matched_phrase,
        }
        for candidate in ordered
    ]


def add_catalog_mentions(
    records: list[dict[str, Any]], index: CatalogMentionIndex
) -> list[dict[str, Any]]:
    output = []
    for record in records:
        enriched = dict(record)
        enriched["catalogMentions"] = catalog_mentions(record, index)
        output.append(enriched)
    return sorted(output, key=lambda record: record["id"])
