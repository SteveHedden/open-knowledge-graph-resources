#!/usr/bin/env python3
"""Propose (never auto-apply) a Wikidata match for a kg-jobs employer.

Read-only against Wikidata's public search API. For each candidate, fetches
its P31 (instance of) values and checks them against a fixed list of real
organization/company/business types -- a top-ranked search hit is not
enough on its own, since Wikidata often only has an item for a product
(e.g. Neo4j the graph database) with no separate item for the company behind
it. Candidates whose P31 values don't include an organization-like type are
flagged NOT ORG-TYPED rather than silently proposed.

This script only prints a reviewed proposal. Nothing is written anywhere.
To record a confirmed match, add one line to employers.ttl by hand (or via
scripts/apply_employer_match.py) -- never edit data/jobs.ttl or
runtime/jobs.ttl directly, they are regenerated outputs.

Usage:
    python3 scripts/match_employer_wikidata.py "Neo4j"
    python3 scripts/match_employer_wikidata.py "OpenAI" "Accenture"
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entities import employer_uri  # noqa: E402

API_URL = "https://www.wikidata.org/w/api.php"
USER_AGENT = "OKG-KG-Jobs-Identity-Review/1.0 (+https://openknowledgegraphs.com/)"

# Real organization/company/business P31 types -- not exhaustive, but covers
# the common cases. A candidate must match one of these directly; this
# script does not attempt transitive subclass reasoning, so an unusual org
# type may need a manual look even when this comes back NOT ORG-TYPED.
ORGANIZATION_TYPES = {
    "Q4830453": "business",
    "Q6881511": "enterprise",
    "Q783794": "company",
    "Q891723": "public company",
    "Q43229": "organization",
    "Q167037": "corporation",
    "Q431289": "brand",
    "Q1058914": "startup company",
    "Q10689397": "business enterprise",
    "Q2000699": "software company",
    "Q4539": "cooperative",
    "Q163740": "nonprofit organization",
    "Q484652": "international organization",
    "Q157031": "foundation",
}


def search_candidates(name: str, limit: int = 5) -> list[dict]:
    resp = requests.get(
        API_URL,
        params={
            "action": "wbsearchentities",
            "search": name,
            "language": "en",
            "type": "item",
            "format": "json",
            "limit": limit,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("search", [])


def fetch_entity(qid: str) -> dict:
    resp = requests.get(
        f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json",
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["entities"][qid]


def p31_labels(entity: dict) -> list[str]:
    return [
        claim["mainsnak"]["datavalue"]["value"]["id"]
        for claim in entity.get("claims", {}).get("P31", [])
        if claim.get("mainsnak", {}).get("datavalue")
    ]


def official_website(entity: dict) -> str | None:
    claims = entity.get("claims", {}).get("P856", [])
    if not claims:
        return None
    value = claims[0].get("mainsnak", {}).get("datavalue", {}).get("value")
    return value if isinstance(value, str) else None


def propose(name: str) -> None:
    uri = employer_uri(name)
    print(f"\n=== {name}  ({uri}) ===")
    candidates = search_candidates(name)
    if not candidates:
        print("  No Wikidata candidates found at all.")
        return

    any_org_typed = False
    for candidate in candidates:
        qid = candidate["id"]
        label = candidate.get("label", "")
        description = candidate.get("description", "")
        entity = fetch_entity(qid)
        type_ids = p31_labels(entity)
        org_matches = [ORGANIZATION_TYPES[t] for t in type_ids if t in ORGANIZATION_TYPES]
        website = official_website(entity)
        tag = "ORG-TYPED" if org_matches else "NOT ORG-TYPED"
        if org_matches:
            any_org_typed = True
        print(f"  [{tag}] {qid} — {label} — {description}")
        if org_matches:
            print(f"           instance of: {', '.join(org_matches)}")
        if website:
            print(f"           official website: {website}")
        time.sleep(0.3)

    if not any_org_typed:
        print(
            "  No candidate is typed as an organization/company/business on Wikidata --"
        )
        print(
            "  most likely Wikidata only has an item for the product, not the company."
        )
        print("  Do not record an owl:sameAs match from this list.")


def main() -> None:
    names = sys.argv[1:]
    if not names:
        print(__doc__)
        raise SystemExit(1)
    for name in names:
        propose(name)


if __name__ == "__main__":
    main()
