"""Small deterministic normalizers shared by jobs ingestion paths."""

from __future__ import annotations

import re


WORKPLACE_MODES = {"remote", "hybrid", "onsite", "unknown"}


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
    """Add exact, case-sensitive description-only language tags."""
    patterns = (
        ("Cypher", re.compile(r"(?<![A-Za-z0-9])Cypher(?![A-Za-z0-9])"),
         "https://openknowledgegraphs.com/software/neo4j/"),
        ("GQL", re.compile(r"(?<![A-Za-z0-9])GQL(?![A-Za-z0-9])"), None),
    )
    output = []
    for source_record in records:
        record = dict(source_record)
        description = str(record.get("description") or "")
        tags = []
        for label, pattern, related_page in patterns:
            match = pattern.search(description)
            if not match:
                continue
            tag = {"label": label, "matchedPhrase": match.group(0)}
            if related_page:
                tag["relatedCatalogPage"] = related_page
            tags.append(tag)
        if tags:
            record["jobTags"] = tags
        else:
            record.pop("jobTags", None)
        output.append(record)
    return output
