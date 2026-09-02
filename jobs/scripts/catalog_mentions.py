"""Deterministic, page-backed catalog mentions for job postings."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
    requires_context: bool = False
    employer_guard: bool = False


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
    for field in ("reviewedAliases", "disambiguationOverrides", "pageGatedAliases"):
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
    for field in ("contextRequiredAliases", "employerGuardAliases"):
        values = policy.get(field, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not _normalized(value) for value in values
        ):
            raise CatalogMentionError(f"catalog mention policy {field} must be a string array")
    variants = policy.get("exactCaseVariants", {})
    if not isinstance(variants, dict):
        raise CatalogMentionError("catalog mention policy exactCaseVariants must be an object")
    for phrase, values in variants.items():
        if (
            not isinstance(phrase, str)
            or not _normalized(phrase)
            or not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not _normalized(value) for value in values)
            or any(_policy_key(value) != _policy_key(phrase) for value in values)
        ):
            raise CatalogMentionError(f"invalid exactCaseVariants entry: {phrase!r}")
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
    page_gated_aliases = {
        _normalized(phrase): (target["dataset"], target["qid"])
        for phrase, target in policy.get("pageGatedAliases", {}).items()
    }
    context_required = {
        _policy_key(value) for value in policy.get("contextRequiredAliases", [])
    }
    employer_guard = {
        _policy_key(value) for value in policy.get("employerGuardAliases", [])
    }
    exact_case_variants = {
        _policy_key(phrase): tuple(_normalized(value) for value in values)
        for phrase, values in policy.get("exactCaseVariants", {}).items()
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

    for surface, target_key in page_gated_aliases.items():
        target = targets.get(target_key)
        if target is None:
            continue
        short_acronym = _is_short_acronym(surface)
        if short_acronym and surface not in acronym_allowlist:
            raise CatalogMentionError(
                f"page-gated short alias {surface!r} is absent from the acronym allowlist"
            )
        surface_targets.setdefault(_policy_key(surface), {})[target.key] = (
            surface,
            target,
            short_acronym,
        )

    terms: list[MatchTerm] = []
    for key, target_map in surface_targets.items():
        selected = target_map
        override = overrides.get(key)
        if override is not None:
            if override not in target_map:
                raise CatalogMentionError(
                    f"disambiguation override {key!r} does not select a matching target"
                )
            selected = {override: target_map[override]}
        elif len(target_map) > 1:
            if override is None:
                continue
        for surface, target, case_sensitive in selected.values():
            variants = exact_case_variants.get(key)
            if variants:
                escaped = "|".join(
                    re.escape(value).replace(r"\ ", r"\s+") for value in variants
                )
                dotted = any("." in value for value in variants)
                left_boundary = (
                    r"(?<![A-Za-z0-9.])" if dotted else r"(?<![A-Za-z0-9])"
                )
                right_boundary = (
                    r"(?![A-Za-z0-9]|\.[A-Za-z0-9])"
                    if dotted
                    else r"(?![A-Za-z0-9])"
                )
                pattern = re.compile(rf"{left_boundary}(?:{escaped}){right_boundary}")
                case_sensitive = True
            else:
                pattern = _phrase_pattern(surface, case_sensitive=case_sensitive)
            terms.append(
                MatchTerm(
                    text=surface,
                    target=target,
                    case_sensitive=case_sensitive,
                    pattern=pattern,
                    requires_context=key in context_required,
                    employer_guard=key in employer_guard,
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

    page_qids = read_json(catalog_root / "data" / "page_qids.json")
    for dataset in DATASET_ORDER:
        slugs = page_qids.get(dataset)
        if not isinstance(slugs, dict):
            continue
        page_qids[dataset] = {
            qid: slug
            for qid, slug in slugs.items()
            if isinstance(slug, str)
            and (catalog_root / "site" / dataset / slug.strip("/") / "index.html").is_file()
        }
    return build_match_index(
        read_json(catalog_root / "data" / "ontologies.json"),
        read_json(catalog_root / "data" / "software.json"),
        page_qids,
        load_policy(policy_path),
    )


def _overlap(left: _Candidate, right: _Candidate) -> bool:
    return left.start < right.end and right.start < left.end


URL_EMAIL_RE = re.compile(
    r"(?i)(?:\b(?:https?://|www\.)[^\s<>]+|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"\b(?:[A-Z0-9-]+\.)+[A-Z]{2,}/[^\s<>]*)"
)
CONTEXT_SEPARATOR_RE = re.compile(r"(?:[.!?](?=\s|$)|[;\n\r•])")
CONTEXT_RE = re.compile(
    r"(?i)(?:\b(?:tools?|technolog(?:y|ies)|skills?|experience|experienced|"
    r"proficien(?:t|cy)|knowledge|familiar(?:ity)?|qualification|requirements?|"
    r"using|use|work(?:ing)? with)\b|\b(?:RDF|RDFS|OWL|SKOS|SHACL|SPARQL|"
    r"Cypher|GQL|ontology|ontologies|knowledge graph|linked data|semantic(?: web)?|"
    r"graph database)\b)"
)
COMPANY_BOILERPLATE_RE = re.compile(
    r"(?i)\b(?:about us|our company|our mission|we are|who we are|company overview|"
    r"equal opportunity employer)\b"
)
NEPTUNE_FALSE_CONTEXT_RE = re.compile(
    r"(?i)(?:\bNeptune\s+Energy\b|\bplanet\s+Neptune\b|"
    r"\b(?:project|initiative|codename|code\s+name|person|colleague|candidate|"
    r"customer|client|employee|engineer|developer|team)\s+"
    r"(?:called\s+|named\s+)?Neptune\b|"
    r"\bNeptune\s+is\s+(?:our|an?|the)\s+"
    r"(?:project|initiative|codename|person|colleague|team|name)\b)"
)


def _mask_urls_and_emails(text: str) -> str:
    return URL_EMAIL_RE.sub(lambda match: " " * (match.end() - match.start()), text)


def _context_segment(text: str, start: int, end: int) -> str:
    left = 0
    right = len(text)
    for separator in CONTEXT_SEPARATOR_RE.finditer(text):
        if separator.end() <= start:
            left = separator.end()
        elif separator.start() >= end:
            right = separator.start()
            break
    return text[left:right]


def _context_allows(
    record: dict[str, Any], term: MatchTerm, text: str, start: int, end: int
) -> bool:
    if not term.requires_context and not term.employer_guard:
        return True
    segment = _context_segment(text, start, end)
    if term.requires_context and not CONTEXT_RE.search(segment):
        return False
    if _policy_key(term.text) == "neptune" and NEPTUNE_FALSE_CONTEXT_RE.search(segment):
        return False
    employer = _policy_key(str(record.get("hiringOrganization") or ""))
    alias = _policy_key(term.text)
    if term.employer_guard and employer and (alias in employer or employer in alias):
        if record.get("firstParty"):
            return False
        self_employer = re.search(
            rf"(?i)(?:\b(?:at|join)\s+{re.escape(term.text)}\b|"
            rf"\b{re.escape(term.text)}\s+is\b)",
            segment,
        )
        if COMPANY_BOILERPLATE_RE.search(segment) or self_employer:
            return False
    return True


def catalog_mentions(
    record: dict[str, Any],
    index: CatalogMentionIndex,
    projected_record: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    selected: list[_Candidate] = []
    for field, field_rank in FIELD_ORDER:
        text = record.get(field)
        if not isinstance(text, str) or not text:
            continue
        projected_text = projected_record.get(field) if projected_record else text
        if not isinstance(projected_text, str) or not projected_text:
            continue
        match_text = _mask_urls_and_emails(projected_text)
        candidates = [
            _Candidate(
                target=term.target,
                matched_phrase=match.group(0),
                field_rank=field_rank,
                start=match.start(),
                end=match.end(),
            )
            for term in index.terms
            for match in term.pattern.finditer(match_text)
            if _context_allows(record, term, match_text, match.start(), match.end())
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
    records: list[dict[str, Any]],
    index: CatalogMentionIndex,
    *,
    text_projection: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    output = []
    for record in records:
        enriched = dict(record)
        matching_record = text_projection(record) if text_projection else None
        enriched["catalogMentions"] = catalog_mentions(record, index, matching_record)
        output.append(enriched)
    return sorted(output, key=lambda record: record["id"])
