#!/usr/bin/env python3
"""Create Wikidata item for Matdata YASGUI fork with all necessary properties.

The item Q140834866 was pre-created; this script adds the properties:
- P31 (instance of): Q124653107 (semantic web software)
- P856 (official website): https://yasgui.matdata.eu/
- P1324 (source code repository): https://github.com/Matdata-eu/yasgui
- P275 (license): Q334661 (MIT License)
- P348 (software version): 5.20.2

Usage:
  WIKI_USER="Lemoncheddar@okg-updater" WIKI_PASS="botpassword" python3 scripts/create_yasgui_matdata_item.py
  WIKI_USER="Lemoncheddar@okg-updater" WIKI_PASS="botpassword" python3 scripts/create_yasgui_matdata_item.py --dry-run
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests

API_URL = "https://www.wikidata.org/w/api.php"
EDIT_SUMMARY = "Add properties to Matdata YASGUI fork (semantic web software) for OKG catalog"
PAUSE = 1.0

# QID of the pre-created item
ITEM_QID = "Q140834866"

# Properties to add: (property_id, value, value_type)
# value_type: "item" for Q-values, "string" for URLs/text
PROPERTIES = [
    ("P31", "Q124653107", "item"),  # instance of: semantic web software
    ("P856", "https://yasgui.matdata.eu/", "string"),  # official website
    ("P1324", "https://github.com/Matdata-eu/yasgui", "string"),  # source code repository
    ("P275", "Q334661", "item"),  # license: MIT License
    ("P348", "5.20.2", "string"),  # software version
]


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
    print(f"Adding properties to {ITEM_QID} (Matdata YASGUI fork)\n")
    if dry_run:
        print("DRY RUN — no edits will be made\n")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "OKG-ItemCreator/1.0 (https://openknowledgegraphs.com)"
    })

    token = "" if dry_run else login(session)

    errors = 0
    for prop, value, value_type in PROPERTIES:
        if dry_run:
            print(f"  {prop}: {value} ({value_type})")
            continue

        if value_type == "item":
            status = add_item_claim(session, token, ITEM_QID, prop, value)
        else:
            status = add_string_claim(session, token, ITEM_QID, prop, value)

        print(f"  {prop}: {status}")
        if "✗" in status:
            errors += 1
        time.sleep(PAUSE)

    if not dry_run:
        print(f"\n{'✓ Done — all properties added!' if errors == 0 else f'✗ {errors} error(s) occurred.'}")
        print(f"Item: https://www.wikidata.org/wiki/{ITEM_QID}")
        print(f"OKG will pick it up on the next daily refresh (or run: python scripts/fetch_data.py)")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
