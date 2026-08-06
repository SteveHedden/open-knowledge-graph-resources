#!/usr/bin/env python3
"""Create Wikidata item for Karamarie Fecho.

Biomedical researcher, knowledge graph expert, and founder of Copperline
Professional Solutions. Research Affiliate at RENCI.

Usage:
  WIKI_USER="Lemoncheddar@okg-updater" WIKI_PASS="botpassword" python3 scripts/create_karamarie_fecho_item.py
  WIKI_USER="Lemoncheddar@okg-updater" WIKI_PASS="botpassword" python3 scripts/create_karamarie_fecho_item.py --dry-run
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests

API_URL = "https://www.wikidata.org/w/api.php"
EDIT_SUMMARY = "Create Karamarie Fecho entry for biomedical knowledge graph researcher"
PAUSE = 1.0


def login(session: requests.Session) -> str:
    """Login to Wikidata and return CSRF token."""
    user = os.environ.get("WIKI_USER")
    password = os.environ.get("WIKI_PASS")
    if not user or not password:
        print("Error: Set WIKI_USER and WIKI_PASS environment variables.")
        sys.exit(1)

    r = session.get(API_URL, params={
        "action": "query", "meta": "tokens", "type": "login", "format": "json"
    })
    login_token = r.json()["query"]["tokens"]["logintoken"]

    r = session.post(API_URL, data={
        "action": "login", "lgname": user, "lgpassword": password,
        "lgtoken": login_token, "format": "json"
    })
    result = r.json()["login"]["result"]
    if result != "Success":
        print(f"Login failed: {result}")
        sys.exit(1)
    print(f"✓ Logged in as {user}\n")

    r = session.get(API_URL, params={"action": "query", "meta": "tokens", "type": "csrf", "format": "json"})
    return r.json()["query"]["tokens"]["csrftoken"]


def create_item(session: requests.Session, token: str, label: str, description: str) -> str:
    """Create a new Wikidata item with label and description."""
    r = session.post(API_URL, data={
        "action": "wbeditentity",
        "new": "item",
        "data": json.dumps({
            "labels": {"en": {"language": "en", "value": label}},
            "descriptions": {"en": {"language": "en", "value": description}}
        }),
        "token": token,
        "format": "json",
        "summary": EDIT_SUMMARY,
    })
    resp = r.json()
    if "error" in resp:
        print(f"❌ ERROR creating item: {resp['error']}")
        return None
    qid = resp["entity"]["id"]
    print(f"✓ Created item: {qid}")
    print(f"  Label: {label}")
    print(f"  Description: {description}\n")
    return qid


def add_item_claim(session: requests.Session, token: str, qid: str, prop: str, target_qid: str) -> str:
    """Add an item-type claim (Q-value)."""
    numeric_id = int(target_qid.lstrip("Q"))
    r = session.post(API_URL, data={
        "action": "wbcreateclaim",
        "entity": qid,
        "property": prop,
        "snaktype": "value",
        "value": json.dumps({"entity-type": "item", "numeric-id": numeric_id}),
        "token": token,
        "format": "json",
        "summary": EDIT_SUMMARY,
    })
    resp = r.json()
    if "error" in resp:
        return f"✗ {resp['error'].get('code', 'error')}: {resp['error'].get('info', '')}"
    return f"✓ {prop} → {target_qid}"


def add_string_claim(session: requests.Session, token: str, qid: str, prop: str, value: str) -> str:
    """Add a string-type claim (URL or text)."""
    r = session.post(API_URL, data={
        "action": "wbcreateclaim",
        "entity": qid,
        "property": prop,
        "snaktype": "value",
        "value": json.dumps(value),
        "token": token,
        "format": "json",
        "summary": EDIT_SUMMARY,
    })
    resp = r.json()
    if "error" in resp:
        return f"✗ {resp['error'].get('code', 'error')}: {resp['error'].get('info', '')}"
    return f"✓ {prop} → {value}"


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    print("Creating Wikidata entry for Karamarie Fecho\n")
    if dry_run:
        print("DRY RUN — no edits will be made\n")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "OKG-ItemCreator/1.0 (https://openknowledgegraphs.com)"
    })

    if dry_run:
        print("Would create item with:")
        print("  Label: Karamarie Fecho")
        print("  Description: Biomedical researcher and knowledge graph expert\n")
        print("Properties to add:")
        print("  P31: Q5 (instance of: human)")
        print("  P106: Q1650915 (occupation: researcher)")
        print("  P108: Q7312413 (employer: RENCI)")
        print("  P937: Q625946 (work location: Chapel Hill, North Carolina)")
        return 0

    token = login(session)

    # Create item
    qid = create_item(
        session,
        token,
        "Karamarie Fecho",
        "Biomedical researcher and knowledge graph expert"
    )
    if not qid:
        return 1

    time.sleep(PAUSE)

    # Add properties: (property_id, value, value_type)
    # value_type: "item" for Q-values, "string" for URLs/text
    properties = [
        ("P31", "Q5", "item"),  # instance of: human
        ("P106", "Q1650915", "item"),  # occupation: researcher
        ("P108", "Q7312413", "item"),  # employer: RENCI
        ("P937", "Q625946", "item"),  # work location: Chapel Hill
    ]

    errors = 0
    for prop, value, value_type in properties:
        if value_type == "item":
            status = add_item_claim(session, token, qid, prop, value)
        else:
            status = add_string_claim(session, token, qid, prop, value)

        print(f"  {prop}: {status}")
        if "✗" in status:
            errors += 1
        time.sleep(PAUSE)

    print(f"\n{'✓ Done — item created!' if errors == 0 else f'✗ {errors} error(s) occurred.'}")
    print(f"Item: https://www.wikidata.org/wiki/{qid}")
    print(f"Now add as P170 (creator) to ROBOKOP: Q140876601")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
