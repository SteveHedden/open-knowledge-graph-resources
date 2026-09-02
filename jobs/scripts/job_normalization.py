"""Small deterministic normalizers shared by jobs ingestion paths."""

from __future__ import annotations

import re


WORKPLACE_MODES = {"remote", "hybrid", "onsite", "unknown"}
URL_RE = re.compile(
    r"(?i)(?:\b(?:https?://|www\.)[^\s<>]+|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"\b(?:[A-Z0-9-]+\.)+[A-Z]{2,}(?:"
    r":\d{1,5}(?:/[^\s<>?#]*)?(?:\?[^\s<>#]*)?(?:#[^\s<>]*)?|"
    r"/[^\s<>]*|\?[^\s<>]+|#[^\s<>]+))"
)


def normalize_workplace(record: dict) -> dict:
    """Return a copy with an authoritative mode and compatible tri-state flag."""
    output = dict(record)
    raw_mode = str(output.get("workplaceMode") or "").strip().casefold()
    if raw_mode in {"on_site", "on-site", "on site"}:
        raw_mode = "onsite"
    if raw_mode not in WORKPLACE_MODES:
        raw_mode = "remote" if output.get("remote") is True else (
            "onsite" if output.get("remote") is False and "remote" in output else "unknown"
        )
    output["workplaceMode"] = raw_mode
    if raw_mode in {"remote", "hybrid"}:
        output["remote"] = True
    elif raw_mode == "onsite":
        output["remote"] = False
    else:
        output.pop("remote", None)
    return output


def add_job_tags(records: list[dict]) -> list[dict]:
    """Rebuild exact, case-sensitive language tags from displayed descriptions."""
    patterns = (
        ("Cypher", re.compile(r"(?<![A-Za-z0-9])Cypher(?![A-Za-z0-9])"),
         "https://openknowledgegraphs.com/software/neo4j/"),
        ("GQL", re.compile(r"(?<![A-Za-z0-9])GQL(?![A-Za-z0-9])"), None),
        (
            "SPARQL",
            re.compile(r"(?<![A-Za-z0-9])SPARQL(?![A-Za-z0-9]|\.(?i:js))"),
            None,
        ),
    )
    output = []
    for source_record in records:
        record = dict(source_record)
        description = str(record.get("description") or "")
        description = URL_RE.sub(lambda match: " " * len(match.group(0)), description)
        visible_mentions = {
            str(value).strip().casefold()
            for mention in record.get("catalogMentions", [])
            if isinstance(mention, dict)
            for value in (mention.get("title"), mention.get("matchedPhrase"))
            if value
        }
        tags = []
        existing_labels: set[str] = set()
        for label, pattern, related_page in patterns:
            match = pattern.search(description)
            if not match:
                continue
            if label.casefold() in visible_mentions:
                continue
            if label.casefold() in existing_labels:
                continue
            tag = {"label": label, "matchedPhrase": match.group(0)}
            if related_page:
                tag["relatedCatalogPage"] = related_page
            tags.append(tag)
            existing_labels.add(label.casefold())
        if tags:
            record["jobTags"] = tags
        else:
            record.pop("jobTags", None)
        output.append(record)
    return output
