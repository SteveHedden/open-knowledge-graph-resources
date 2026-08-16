#!/usr/bin/env python3
"""Prepare, guard, create, and verify Task 27's Wikidata item.

Dry-run is the default and performs no network writes. Live execution requires the
separately approved payload hash to be recorded in the audit and supplied on the
command line. Immediately before a live write, duplicate searches are repeated.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = ROOT / "audits" / "wikidata" / "task-27-item-creation.json"
API_URL = "https://www.wikidata.org/w/api.php"
CALENDAR_MODEL = "http://www.wikidata.org/entity/Q1985727"
USER_AGENT = "OKG-Task27-ItemCreator/1.0 (https://openknowledgegraphs.com)"


class Task27Error(RuntimeError):
    """Raised when a Task 27 safety or verification condition fails."""


def load_audit(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        audit = json.load(handle)
    validate_audit(audit)
    return audit


def _time_value(value: str, precision: int) -> dict[str, Any]:
    return {
        "time": value,
        "timezone": 0,
        "before": 0,
        "after": 0,
        "precision": precision,
        "calendarmodel": CALENDAR_MODEL,
    }


def compile_snak(spec: dict[str, Any]) -> dict[str, Any]:
    datatype = spec["datatype"]
    value = spec["value"]
    if datatype == "wikibase-item":
        datavalue = {
            "value": {
                "entity-type": "item",
                "numeric-id": int(value.removeprefix("Q")),
                "id": value,
            },
            "type": "wikibase-entityid",
        }
    elif datatype in {"url", "string"}:
        datavalue = {"value": value, "type": "string"}
    elif datatype == "time":
        datavalue = {
            "value": _time_value(value, int(spec["precision"])),
            "type": "time",
        }
    elif datatype == "monolingualtext":
        datavalue = {
            "value": {"text": value, "language": spec["language"]},
            "type": "monolingualtext",
        }
    else:
        raise Task27Error(f"Unsupported datatype: {datatype}")
    return {
        "snaktype": "value",
        "property": spec["property"],
        "datavalue": datavalue,
        "datatype": datatype,
    }


def compile_reference(audit: dict[str, Any], reference_id: str) -> dict[str, Any]:
    specs = audit["references"][reference_id]["snaks"]
    snaks: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for spec in specs:
        prop = spec["property"]
        if prop not in snaks:
            order.append(prop)
        snaks.setdefault(prop, []).append(compile_snak(spec))
    return {"snaks": snaks, "snaks-order": order}


def compile_statement(audit: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    qualifiers: dict[str, list[dict[str, Any]]] = {}
    qualifier_order: list[str] = []
    for qualifier in spec.get("qualifiers", []):
        prop = qualifier["property"]
        if prop not in qualifiers:
            qualifier_order.append(prop)
        qualifiers.setdefault(prop, []).append(compile_snak(qualifier))

    statement = {
        "mainsnak": compile_snak(spec),
        "type": "statement",
        "rank": spec["rank"],
        "references": [compile_reference(audit, spec["reference"])],
    }
    if qualifiers:
        statement["qualifiers"] = qualifiers
        statement["qualifiers-order"] = qualifier_order
    return statement


def compile_entity_payload(audit: dict[str, Any]) -> dict[str, Any]:
    proposed = audit["proposedEntity"]
    return {
        "labels": {
            language: {"language": language, "value": value}
            for language, value in proposed["labels"].items()
        },
        "descriptions": {
            language: {"language": language, "value": value}
            for language, value in proposed["descriptions"].items()
        },
        "aliases": {
            language: [
                {"language": language, "value": value} for value in values
            ]
            for language, values in proposed["aliases"].items()
        },
        "claims": [
            compile_statement(audit, statement)
            for statement in proposed["statements"]
        ],
    }


def canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def validate_audit(audit: dict[str, Any]) -> None:
    if audit.get("taskId") != 27:
        raise Task27Error("Audit is not for Task 27.")
    for gate in ("duplicateGate", "notabilityGate", "versionGate"):
        if not str(audit.get(gate, {}).get("decision", "")).startswith("passed"):
            raise Task27Error(f"{gate} has not passed.")
    if audit["duplicateGate"].get("existingQid") is not None:
        raise Task27Error("Creation payload cannot target an existing Wikidata item.")
    checks = audit["duplicateGate"].get("checks", [])
    if [check.get("kind") for check in checks] != [
        "exact-name",
        "short-name",
        "official-website",
        "source-repository",
    ]:
        raise Task27Error("Duplicate gate must record both names and both URLs.")
    if any(check.get("resultCount") != 0 for check in checks):
        raise Task27Error("Duplicate gate contains a matching Wikidata result.")

    proposed = audit["proposedEntity"]
    if proposed["labels"] != {"en": "Intentional Arrangement SKOS Editor"}:
        raise Task27Error("Unexpected label payload.")
    if proposed["descriptions"] != {
        "en": "web application for creating, validating, visualizing, querying and exporting SKOS vocabularies"
    }:
        raise Task27Error("Unexpected description payload.")
    if proposed["aliases"] != {"en": ["Intentional Arrangement SKOS"]}:
        raise Task27Error("Unexpected alias payload.")

    statements = proposed["statements"]
    expected = [
        ("P31", "Q124653107"),
        ("P4428", "Q2288360"),
        ("P856", "https://jesstalisman-ia.github.io/intentional-arrangement-skos/"),
        ("P1324", "https://github.com/jesstalisman-ia/intentional-arrangement-skos"),
        ("P275", "Q36795408"),
        ("P277", "Q2005"),
        ("P277", "Q28865"),
        ("P571", "+2026-00-00T00:00:00Z"),
        ("P348", "0.3.0"),
    ]
    actual = [(statement["property"], statement["value"]) for statement in statements]
    if actual != expected:
        raise Task27Error(f"Unexpected statement payload: {actual!r}")
    if any(statement["property"] == "P178" for statement in statements):
        raise Task27Error("P178 is intentionally unsupported and must be omitted.")

    inception = next(statement for statement in statements if statement["property"] == "P571")
    if inception.get("precision") != 9:
        raise Task27Error("P571 must use year precision.")
    version = next(statement for statement in statements if statement["property"] == "P348")
    if version.get("rank") != "preferred":
        raise Task27Error("P348 0.3.0 must use preferred rank.")
    qualifiers = {
        qualifier["property"]: qualifier for qualifier in version.get("qualifiers", [])
    }
    if qualifiers.get("P577", {}).get("value") != "+2026-08-15T00:00:00Z":
        raise Task27Error("P348 must carry the approved P577 date.")
    if qualifiers.get("P548", {}).get("value") != "Q2804309":
        raise Task27Error("P348 must carry the stable-version qualifier.")

    expected_retrieved = f'+{audit["executionDate"]}T00:00:00Z'
    for reference_id, reference in audit["references"].items():
        retrieved = [
            snak
            for snak in reference["snaks"]
            if snak["property"] == "P813"
        ]
        if len(retrieved) != 1 or retrieved[0]["value"] != expected_retrieved:
            raise Task27Error(f"{reference_id} has an incorrect P813 value.")
    release_snaks = audit["references"]["v0.3.0-release"]["snaks"]
    title = [snak for snak in release_snaks if snak["property"] == "P1476"]
    if len(title) != 1 or title[0]["value"] != (
        "v0.3.0 — Glossary, SKOS-XL build mode, per-section colors"
    ):
        raise Task27Error("Release reference title does not match the approved title.")


def configured_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def api_request(
    session: requests.Session,
    method: str,
    *,
    label: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = session.request(method, API_URL, params=params, data=data, timeout=45)
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        error = payload["error"]
        raise Task27Error(
            f"{label}: Wikidata API error {error.get('code')}: {error.get('info')}"
        )
    return payload


def repeat_duplicate_checks(
    session: requests.Session, audit: dict[str, Any]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for check in audit["duplicateGate"]["checks"]:
        payload = api_request(
            session,
            "GET",
            label=f"duplicate search ({check['kind']})",
            params={
                "action": "query",
                "list": "search",
                "srsearch": check["query"],
                "srnamespace": 0,
                "srlimit": 10,
                "format": "json",
                "formatversion": 2,
            },
        )
        hits = [
            {"title": row["title"], "snippet": row.get("snippet", "")}
            for row in payload["query"]["search"]
        ]
        results.append({"kind": check["kind"], "query": check["query"], "hits": hits})
    return results


def login(session: requests.Session) -> str:
    username = os.environ.get("WIKI_USER")
    password = os.environ.get("WIKI_PASS")
    if not username or not password:
        raise Task27Error("Live execution requires WIKI_USER and WIKI_PASS.")
    token_payload = api_request(
        session,
        "GET",
        label="login token",
        params={
            "action": "query",
            "meta": "tokens",
            "type": "login",
            "format": "json",
        },
    )
    login_payload = api_request(
        session,
        "POST",
        label="login",
        data={
            "action": "login",
            "lgname": username,
            "lgpassword": password,
            "lgtoken": token_payload["query"]["tokens"]["logintoken"],
            "format": "json",
        },
    )
    if login_payload.get("login", {}).get("result") != "Success":
        raise Task27Error(f"Wikidata login failed: {login_payload.get('login')}")
    csrf_payload = api_request(
        session,
        "GET",
        label="CSRF token",
        params={"action": "query", "meta": "tokens", "format": "json"},
    )
    return csrf_payload["query"]["tokens"]["csrftoken"]


def _datavalue_key(snak: dict[str, Any]) -> str:
    return json.dumps(snak.get("datavalue"), ensure_ascii=False, sort_keys=True)


def _snak_groups_contain(
    actual: dict[str, list[dict[str, Any]]],
    expected: dict[str, list[dict[str, Any]]],
) -> bool:
    for prop, expected_snaks in expected.items():
        actual_keys = {_datavalue_key(snak) for snak in actual.get(prop, [])}
        if any(_datavalue_key(snak) not in actual_keys for snak in expected_snaks):
            return False
    return True


def statement_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    if actual.get("rank") != expected["rank"]:
        return False
    if _datavalue_key(actual["mainsnak"]) != _datavalue_key(expected["mainsnak"]):
        return False
    if not _snak_groups_contain(actual.get("qualifiers", {}), expected.get("qualifiers", {})):
        return False
    return any(
        _snak_groups_contain(reference.get("snaks", {}), expected["references"][0]["snaks"])
        for reference in actual.get("references", [])
    )


def verify_entity(entity: dict[str, Any], expected: dict[str, Any]) -> None:
    for field in ("labels", "descriptions"):
        if entity.get(field, {}).get("en", {}).get("value") != expected[field]["en"]["value"]:
            raise Task27Error(f"Live readback has an unexpected English {field} value.")
    actual_aliases = {
        alias["value"] for alias in entity.get("aliases", {}).get("en", [])
    }
    expected_aliases = {alias["value"] for alias in expected["aliases"]["en"]}
    if not expected_aliases.issubset(actual_aliases):
        raise Task27Error("Live readback is missing the approved English alias.")

    claims = entity.get("claims", {})
    for statement in expected["claims"]:
        prop = statement["mainsnak"]["property"]
        if not any(statement_matches(actual, statement) for actual in claims.get(prop, [])):
            value = statement["mainsnak"].get("datavalue", {}).get("value")
            raise Task27Error(f"Live readback mismatch for {prop}={value!r}.")


def execute(
    audit: dict[str, Any], payload: dict[str, Any], approved_hash: str | None
) -> dict[str, Any]:
    digest = payload_sha256(payload)
    approval = audit["approval"]
    if approval.get("status") != "approved":
        raise Task27Error("The audit does not record separate user approval.")
    if approval.get("payloadSha256") != digest or approved_hash != digest:
        raise Task27Error("The supplied approval does not match the exact payload hash.")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if audit["executionDate"] != today:
        raise Task27Error(
            "P813 is no longer the actual UTC execution date; regenerate and reapprove the dry run."
        )

    session = configured_session()
    duplicate_results = repeat_duplicate_checks(session, audit)
    matches = [result for result in duplicate_results if result["hits"]]
    if matches:
        raise Task27Error(
            "Duplicate search changed; refusing to create an item: "
            + json.dumps(matches, ensure_ascii=False)
        )

    token = login(session)
    created = api_request(
        session,
        "POST",
        label="item creation",
        data={
            "action": "wbeditentity",
            "new": "item",
            "data": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "token": token,
            "summary": audit["editPlan"]["summary"],
            "bot": "1",
            "assert": "user",
            "maxlag": 5,
            "format": "json",
            "formatversion": 2,
        },
    )
    qid = created["entity"]["id"]
    readback = api_request(
        session,
        "GET",
        label="live readback",
        params={
            "action": "wbgetentities",
            "ids": qid,
            "props": "labels|descriptions|aliases|claims|info",
            "format": "json",
            "formatversion": 2,
        },
    )["entities"][qid]
    verify_entity(readback, payload)
    return {
        "qid": qid,
        "revisionId": readback["lastrevid"],
        "payloadSha256": digest,
        "duplicateChecks": duplicate_results,
        "readbackVerified": True,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the exact payload (default).")
    mode.add_argument("--execute", action="store_true", help="Perform the separately approved edit.")
    parser.add_argument(
        "--approved-payload-sha256",
        help="Exact dry-run hash approved by the user; required with --execute.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        audit = load_audit(args.audit)
        payload = compile_entity_payload(audit)
        digest = payload_sha256(payload)
        if args.execute:
            result = execute(audit, payload, args.approved_payload_sha256)
            print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print("DRY RUN — no public write")
            print(f"Edit summary: {audit['editPlan']['summary']}")
            print(f"Payload SHA-256: {digest}")
            print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (Task27Error, requests.RequestException, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
