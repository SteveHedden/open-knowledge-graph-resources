#!/usr/bin/env python3
"""Update Matdata YASGUI fork (Q140834866) to clarify it's a fork of official YASGUI (Q114893193).

Changes:
- Add more specific label: "Yasgui (Matdata)"
- Add P361 (part of) relationship to Q114893193 (original YASGUI)
- Update description to reference upstream

Usage:
  WIKI_USER="Lemoncheddar@okg-updater" WIKI_PASS="botpassword" python3 scripts/update_yasgui_matdata_fork_relationship.py
  WIKI_USER="Lemoncheddar@okg-updater" WIKI_PASS="botpassword" python3 scripts/update_yasgui_matdata_fork_relationship.py --dry-run
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

API_URL = "https://www.wikidata.org/w/api.php"
EDIT_SUMMARY = "Clarify Matdata YASGUI as fork of official YASGUI; add part-of relationship (via OKG catalog bot)"
PAUSE = 1.0

ITEM_QID = "Q140834866"  # Matdata YASGUI fork
ORIGINAL_QID = "Q114893193"  # Original YASGUI (Triply)


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

    r = session.get(API_URL, params={"action": "query", "meta": "tokens", "format": "json"})
    return r.json()["query"]["tokens"]["csrftoken"]


def update_label(session: requests.Session, token: str) -> str:
    """Update the label to be more specific."""
    r = session.post(API_URL, data={
        "action": "wbsetlabel",
        "entity": ITEM_QID,
        "language": "en",
        "value": "Yasgui (Matdata)",
        "token": token,
        "format": "json",
        "summary": EDIT_SUMMARY,
    })
    resp = r.json()
    if "error" in resp:
        return f"✗ {resp['error'].get('code', 'error')}"
    return f"✓ Updated label to 'Yasgui (Matdata)'"


def add_part_of_claim(session: requests.Session, token: str) -> str:
    """Add P361 (part of) relationship to original YASGUI."""
    numeric_id = int(ORIGINAL_QID.lstrip("Q"))
    r = session.post(API_URL, data={
        "action": "wbcreateclaim",
        "entity": ITEM_QID,
        "property": "P361",
        "snaktype": "value",
        "value": json.dumps({"entity-type": "item", "numeric-id": numeric_id}),
        "token": token,
        "format": "json",
        "summary": EDIT_SUMMARY,
    })
    resp = r.json()
    if "error" in resp:
        return f"✗ {resp['error'].get('code', 'error')}"
    return f"✓ Added P361 (part of) → {ORIGINAL_QID}"


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    print(f"Updating Matdata YASGUI fork ({ITEM_QID}) relationship to original YASGUI ({ORIGINAL_QID})\n")
    if dry_run:
        print("DRY RUN — no edits will be made\n")
        print("  - Would update label: 'Yasgui' → 'Yasgui (Matdata)'")
        print("  - Would add P361 (part of) → Q114893193")
        return 0

    session = requests.Session()
    session.headers.update({
        "User-Agent": "OKG-ItemCreator/1.0 (https://openknowledgegraphs.com)"
    })

    token = login(session)

    # Update label
    status = update_label(session, token)
    print(f"  {status}")
    time.sleep(PAUSE)

    # Add part-of relationship
    status = add_part_of_claim(session, token)
    print(f"  {status}")

    print(f"\n✓ Done!")
    print(f"Item: https://www.wikidata.org/wiki/{ITEM_QID}")
    print(f"Related to: https://www.wikidata.org/wiki/{ORIGINAL_QID}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
