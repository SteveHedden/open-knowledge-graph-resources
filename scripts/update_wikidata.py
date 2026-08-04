#!/usr/bin/env python3
"""Update Wikidata items with descriptions and/or properties.

Usage examples:
  # Add/update description only
  WIKI_USER="Lemoncheddar" WIKI_PASS="[botpass]" python3 scripts/update_wikidata.py \
    -q Q130626355 -d "EU infrastructure for European railway network data"
  
  # Add P856 (official website)
  WIKI_USER="Lemoncheddar" WIKI_PASS="[botpass]" python3 scripts/update_wikidata.py \
    -q Q130626355 -p P856 -v "https://www.era.europa.eu/domains/registers/era-knowlege-graph_en"
  
  # Both
  WIKI_USER="Lemoncheddar" WIKI_PASS="[botpass]" python3 scripts/update_wikidata.py \
    -q Q130626355 -d "Description here" -p P856 -v "https://example.com"
  
  # Dry run
  WIKI_USER="Lemoncheddar" WIKI_PASS="[botpass]" python3 scripts/update_wikidata.py \
    -q Q130626355 -d "Description" --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

API_URL = "https://www.wikidata.org/w/api.php"
PAUSE = 1.0


def login(session: requests.Session, user: str, password: str) -> str:
    """Login to Wikidata and return CSRF token."""
    # Get login token
    r = session.get(API_URL, params={
        "action": "query", "meta": "tokens", "type": "login", "format": "json"
    })
    login_token = r.json()["query"]["tokens"]["logintoken"]

    # Login
    r = session.post(API_URL, data={
        "action": "login", "lgname": user, "lgpassword": password,
        "lgtoken": login_token, "format": "json"
    })
    result = r.json()["login"]["result"]
    if result != "Success":
        print(f"❌ Login failed: {result}")
        sys.exit(1)
    print(f"✓ Logged in as {user}\n")

    # Get CSRF token for edits
    r = session.get(API_URL, params={"action": "query", "meta": "tokens", "type": "csrf", "format": "json"})
    return r.json()["query"]["tokens"]["csrftoken"]


def set_description(session: requests.Session, token: str, qid: str, description: str, edit_summary: str) -> bool:
    """Set English description."""
    r = session.post(API_URL, data={
        "action": "wbsetdescription",
        "entity": qid,
        "language": "en",
        "value": description,
        "token": token,
        "format": "json",
        "summary": edit_summary,
    })
    resp = r.json()
    if "error" in resp:
        print(f"❌ ERROR setting description: {resp['error']}")
        return False
    print(f"✓ Added description: \"{description}\"")
    return True


def add_claim(session: requests.Session, token: str, qid: str, prop: str, value: str, edit_summary: str) -> bool:
    """Add a property claim (for item Q-values, URLs, or text)."""
    # Detect if value is a Q-ID (item reference)
    if value.startswith("Q") and value[1:].isdigit():
        # Format as item reference
        numeric_id = int(value.lstrip("Q"))
        formatted_value = json.dumps({"entity-type": "item", "numeric-id": numeric_id})
    else:
        # Format as string value (URL or text) - encode as JSON string
        formatted_value = json.dumps(value)

    r = session.post(API_URL, data={
        "action": "wbcreateclaim",
        "entity": qid,
        "property": prop,
        "snaktype": "value",
        "value": formatted_value,
        "token": token,
        "format": "json",
        "summary": edit_summary,
    })
    resp = r.json()
    if "error" in resp:
        print(f"❌ ERROR creating {prop} claim: {resp['error']}")
        return False
    print(f"✓ Added {prop} → {value[:60]}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-q", "--qid", required=True, help="Wikidata QID (e.g., Q130626355)")
    parser.add_argument("-d", "--description", help="English description to set")
    parser.add_argument("-p", "--property", help="Property to add (e.g., P856)")
    parser.add_argument("-v", "--value", help="Value for property")
    parser.add_argument("-s", "--summary", help="Edit summary (optional)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done, don't edit")
    
    args = parser.parse_args()
    
    user = os.environ.get("WIKI_USER")
    password = os.environ.get("WIKI_PASS")
    if not user or not password:
        print("Error: Set WIKI_USER and WIKI_PASS environment variables.")
        sys.exit(1)
    
    edit_summary = args.summary or "Update via OKG bot (https://openknowledgegraphs.com)"
    
    print(f"Updating {args.qid} on Wikidata\n")
    
    if args.dry_run:
        print("DRY RUN — no edits will be made\n")
        if args.description:
            print(f"  - Would set description: \"{args.description}\"")
        if args.property and args.value:
            print(f"  - Would add {args.property} → {args.value}")
        return 0
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "OKG-Wikidata-Updater/1.0 (https://openknowledgegraphs.com)"
    })
    
    token = login(session, user, password)
    
    # Set description if provided
    if args.description:
        if not set_description(session, token, args.qid, args.description, edit_summary):
            return 1
        time.sleep(PAUSE)
    
    # Add property claim if provided
    if args.property and args.value:
        if not add_claim(session, token, args.qid, args.property, args.value, edit_summary):
            return 1
    
    print(f"\n✓ Done! Updated {args.qid}")
    print(f"Item: https://www.wikidata.org/wiki/{args.qid}")
    print(f"OKG will pick this up on next catalog refresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
