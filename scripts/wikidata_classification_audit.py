#!/usr/bin/env python3
"""Capture, validate, and optionally execute Task 24's reviewed Wikidata plan."""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import jsonschema
import requests

import fetch_data
from semantic_config import ONTOLOGIES_DATASET, SourceMappings, load_source_mappings


ROOT = Path(os.environ.get("OKG_CATALOG_ROOT", Path(__file__).resolve().parent.parent)).resolve()
SNAPSHOT_PATH = ROOT / "audits" / "wikidata" / "task-24-source-snapshot.json"
AUDIT_PATH = ROOT / "audits" / "wikidata" / "task-24-classification-audit.json"
INTENT_PATH = ROOT / "audits" / "wikidata" / "task-24-edit-intent.json"
SCHEMA_PATH = ROOT / "validation" / "task-24-classification-audit.schema.json"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_MAXLAG = int(os.environ.get("WIKIDATA_MAXLAG", "5"))
WIKIDATA_MAXLAG_ATTEMPTS = 8
WIKIDATA_MAXLAG_FALLBACK_SECONDS = 10.0
WIKIDATA_EDIT_PAUSE_SECONDS = float(os.environ.get("WIKIDATA_EDIT_PAUSE_SECONDS", "1.0"))
REFERENCE_URL_PROPERTY = "P854"
HOMOSAURUS_QID = "Q26936735"
EDITABLE_STATUSES = frozenset({"planned", "approved", "executed"})
QID_RE = re.compile(r"^Q[0-9]+$")
PROPERTY_ID_RE = re.compile(r"^P[0-9]+$")
TERM_LIKE_RE = re.compile(
    r"\b(?:individual|controlled|vocabulary|ontology|thesaurus)\s+(?:term|component)\b"
    r"|\bterm\s+(?:for|in|of|from|used)\b"
    r"|\b(?:property|value)\s+(?:that|for|of)\b",
    re.IGNORECASE,
)
TERM_LIKE_LABEL_RE = re.compile(r"\b(?:term|property|component)\b", re.IGNORECASE)
CAMEL_CASE_RE = re.compile(r"[a-z][A-Z]")


class AuditError(RuntimeError):
    """Raised when capture, validation, or execution cannot proceed safely."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AuditError(f"{path} must contain a JSON object.")
    return payload


def validate_edit_intent(intent: dict) -> None:
    if set(intent) != {"schemaVersion", "taskId", "intents"}:
        raise AuditError("Edit intent must contain only schemaVersion, taskId, and intents.")
    if intent["schemaVersion"] != 1 or intent["taskId"] != "24":
        raise AuditError("Edit intent is not the canonical Task 24 version.")
    intents = intent["intents"]
    if not isinstance(intents, dict) or not intents:
        raise AuditError("Edit intent must contain at least one QID.")
    for qid, item_intent in intents.items():
        if not isinstance(qid, str) or not QID_RE.fullmatch(qid):
            raise AuditError(f"Invalid edit-intent QID: {qid!r}")
        if not isinstance(item_intent, dict) or set(item_intent) != {
            "decision",
            "rationale",
            "evidenceUrls",
            "operations",
        }:
            raise AuditError(f"{qid}: malformed canonical edit intent.")
        if item_intent["decision"] != "correct-wikidata-and-exclude-locally":
            raise AuditError(f"{qid}: unsupported edit-intent decision.")
        if not isinstance(item_intent["rationale"], str) or not item_intent["rationale"]:
            raise AuditError(f"{qid}: edit intent requires a rationale.")
        evidence_urls = item_intent["evidenceUrls"]
        if not isinstance(evidence_urls, list) or not evidence_urls:
            raise AuditError(f"{qid}: edit intent requires evidence URLs.")
        for evidence_url in evidence_urls:
            parsed = urlparse(evidence_url) if isinstance(evidence_url, str) else None
            if not parsed or parsed.scheme != "https" or not parsed.netloc:
                raise AuditError(f"{qid}: invalid edit-intent evidence URL.")
        operations = item_intent["operations"]
        if not isinstance(operations, list) or not operations:
            raise AuditError(f"{qid}: edit intent requires operations.")
        for operation in operations:
            if not isinstance(operation, dict) or set(operation) != {"action", "property", "value"}:
                raise AuditError(f"{qid}: malformed edit-intent operation.")
            if operation["action"] not in {"add", "remove"}:
                raise AuditError(f"{qid}: unsupported edit-intent action.")
            if not isinstance(operation["property"], str) or not PROPERTY_ID_RE.fullmatch(
                operation["property"]
            ):
                raise AuditError(f"{qid}: invalid edit-intent property.")
            if not isinstance(operation["value"], str) or not QID_RE.fullmatch(
                operation["value"]
            ):
                raise AuditError(f"{qid}: invalid edit-intent item value.")


def homosaurus_reference_url(item_intent: dict) -> str | None:
    belongs_to_homosaurus = any(
        operation == {"action": "add", "property": "P361", "value": HOMOSAURUS_QID}
        for operation in item_intent["operations"]
    )
    if not belongs_to_homosaurus:
        return None
    matches = [
        url
        for url in item_intent["evidenceUrls"]
        if (urlparse(url).hostname or "").lower() in {"homosaurus.org", "www.homosaurus.org"}
    ]
    if len(matches) != 1:
        raise AuditError("Homosaurus additions require one official Homosaurus evidence URL.")
    return matches[0]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_audit_query(source_class_qid: str, mappings: SourceMappings) -> str:
    path = fetch_data.wikidata_class_path(mappings, ONTOLOGIES_DATASET)
    direct_type = fetch_data.wikidata_property(mappings, "instanceOf", ONTOLOGIES_DATASET, "iri")
    part_of = fetch_data.wikidata_property(mappings, "partOfEntity", ONTOLOGIES_DATASET, "iri")
    homepage = fetch_data.wikidata_property(mappings, "officialWebsite", ONTOLOGIES_DATASET, "iri")
    return f"""
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>

SELECT DISTINCT ?item ?directType ?partOfEntity ?officialWebsite
WHERE {{
  ?item {path} wd:{source_class_qid} .
  OPTIONAL {{ ?item wdt:{direct_type} ?directType . }}
  OPTIONAL {{ ?item wdt:{part_of} ?partOfEntity . }}
  OPTIONAL {{ ?item wdt:{homepage} ?officialWebsite . }}
}}
"""


def capture_source_snapshot(session: requests.Session, mappings: SourceMappings) -> dict:
    aggregated: dict[str, dict[str, object]] = {}
    source_classes = mappings.class_ids_for(ONTOLOGIES_DATASET)
    for source_class in source_classes:
        rows = fetch_data.run_wdqs_query(
            session,
            build_audit_query(source_class, mappings),
            f"Task 24 source class {source_class}",
        )
        for row in rows:
            item = fetch_data.binding_value(row, "item")
            if not item:
                continue
            qid = fetch_data.qid_from_wikidata_iri(item)
            record = aggregated.setdefault(
                qid,
                {
                    "qid": qid,
                    "directTypes": set(),
                    "parentQids": set(),
                    "officialWebsites": set(),
                    "matchedSourceClasses": set(),
                },
            )
            record["matchedSourceClasses"].add(source_class)
            direct_type = fetch_data.binding_value(row, "directType")
            if direct_type:
                record["directTypes"].add(fetch_data.qid_from_wikidata_iri(direct_type))
            parent = fetch_data.binding_value(row, "partOfEntity")
            if parent:
                record["parentQids"].add(fetch_data.qid_from_wikidata_iri(parent))
            homepage = fetch_data.binding_value(row, "officialWebsite")
            if homepage:
                record["officialWebsites"].add(homepage)

    item_iris = {f"http://www.wikidata.org/entity/{qid}" for qid in aggregated}
    labels, descriptions = fetch_data.fetch_entity_labels(session, item_iris)
    records = []
    for qid, raw in sorted(aggregated.items()):
        iri = f"http://www.wikidata.org/entity/{qid}"
        records.append(
            {
                "qid": qid,
                "label": labels.get(iri, qid),
                "description": descriptions.get(iri),
                "directTypes": sorted(raw["directTypes"]),
                "parentQids": sorted(raw["parentQids"]),
                "officialWebsites": sorted(raw["officialWebsites"]),
                "matchedSourceClasses": sorted(raw["matchedSourceClasses"]),
            }
        )

    property_ids = {
        "instanceOf": fetch_data.wikidata_property(mappings, "instanceOf", value_kind="iri"),
        "subclassOf": fetch_data.wikidata_property(mappings, "subclassOf", value_kind="iri"),
        "partOf": fetch_data.wikidata_property(
            mappings, "partOfEntity", ONTOLOGIES_DATASET, "iri"
        ),
        "officialWebsite": fetch_data.wikidata_property(
            mappings, "officialWebsite", ONTOLOGIES_DATASET, "iri"
        ),
    }
    return {
        "schemaVersion": 1,
        "capturedAt": utc_now(),
        "endpoint": fetch_data.WDQS_URL,
        "sourceClasses": list(source_classes),
        "queryContract": {
            "positivePath": "direct instance-of followed by zero or more subclass-of edges",
            "properties": property_ids,
        },
        "cohortSize": len(records),
        "records": records,
    }


def candidate_signals(record: dict, marker_qids: set[str], exclusions: set[str]) -> list[str]:
    signals: list[str] = []
    if record["qid"] in exclusions:
        signals.append("confirmed-exclusion")
    if marker_qids & set(record["directTypes"]):
        signals.append("direct-term-component-type")
    if record["parentQids"]:
        signals.append("direct-part-of")
    if any(urlparse(url).fragment for url in record["officialWebsites"]):
        signals.append("fragment-homepage")
    description = record.get("description") or ""
    if TERM_LIKE_RE.search(description):
        signals.append("term-like-description")
    label = record.get("label") or ""
    if TERM_LIKE_LABEL_RE.search(label):
        signals.append("term-like-label")
    if CAMEL_CASE_RE.search(label):
        signals.append("camel-case-label")
    return signals


def request_json(session: requests.Session, params: dict, label: str) -> dict:
    for attempt in range(1, 5):
        try:
            response = session.get(WIKIDATA_API, params=params, timeout=60)
        except requests.RequestException as exc:
            if attempt == 4:
                raise AuditError(f"{label}: request failed") from exc
            time.sleep(attempt * 2)
            continue
        if response.status_code == 429 or 500 <= response.status_code < 600:
            if attempt == 4:
                raise AuditError(f"{label}: HTTP {response.status_code}")
            time.sleep(attempt * 2)
            continue
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise AuditError(f"{label}: Wikidata API error {payload['error'].get('code')}")
        return payload
    raise AuditError(f"{label}: request attempts exhausted")


def fetch_entities(session: requests.Session, qids: list[str]) -> dict[str, dict]:
    entities: dict[str, dict] = {}
    for batch in fetch_data.chunked(sorted(qids), 50):
        payload = request_json(
            session,
            {
                "action": "wbgetentities",
                "format": "json",
                "formatversion": "2",
                "ids": "|".join(batch),
                "props": "info|labels|descriptions|claims",
                "languages": "en",
            },
            "Wikidata entity batch",
        )
        raw_entities = payload.get("entities", {})
        entity_values = raw_entities.values() if isinstance(raw_entities, dict) else raw_entities
        for entity in entity_values:
            if isinstance(entity, dict) and entity.get("id"):
                entities[entity["id"]] = entity
    missing = set(qids) - set(entities)
    if missing:
        raise AuditError("Wikidata entity response omitted: " + ", ".join(sorted(missing)))
    return entities


def claim_qid(statement: dict) -> str | None:
    try:
        return statement["mainsnak"]["datavalue"]["value"]["id"]
    except (KeyError, TypeError):
        return None


def relevant_claims(entity: dict, property_ids: set[str]) -> list[dict]:
    claims = []
    for property_id in sorted(entity.get("claims", {})):
        for statement in entity["claims"][property_id]:
            if "remove" in statement or statement.get("removed"):
                continue
            value = claim_qid(statement)
            if property_id not in property_ids or value is None:
                continue
            claims.append(
                {
                    "guid": statement.get("id"),
                    "property": property_id,
                    "value": value,
                    "rank": statement.get("rank", "normal"),
                }
            )
    return sorted(claims, key=lambda claim: (claim["property"], claim["value"], claim["guid"] or ""))


def resolve_operations(before_claims: list[dict], raw_operations: list[dict]) -> list[dict]:
    operations = []
    for raw in raw_operations:
        operation = {
            "action": raw["action"],
            "property": raw["property"],
            "value": raw["value"],
            "claimGuid": None,
        }
        matches = [
            claim
            for claim in before_claims
            if claim["property"] == raw["property"] and claim["value"] == raw["value"]
        ]
        if raw["action"] == "remove":
            if len(matches) != 1 or not matches[0]["guid"]:
                raise AuditError(
                    f"Planned removal must resolve to one exact claim: {raw['property']} {raw['value']}"
                )
            operation["claimGuid"] = matches[0]["guid"]
        elif raw["action"] == "add":
            if matches:
                raise AuditError(
                    f"Planned addition already exists: {raw['property']} {raw['value']}"
                )
        else:
            raise AuditError(f"Unsupported edit action: {raw['action']}")
        operations.append(operation)
    return operations


def apply_operations_to_claims(before_claims: list[dict], operations: list[dict]) -> list[dict]:
    proposed = copy.deepcopy(before_claims)
    for operation in operations:
        if operation["action"] == "remove":
            proposed = [claim for claim in proposed if claim["guid"] != operation["claimGuid"]]
        else:
            proposed.append(
                {
                    "guid": None,
                    "property": operation["property"],
                    "value": operation["value"],
                    "rank": "normal",
                }
            )
    return sorted(proposed, key=lambda claim: (claim["property"], claim["value"], claim["guid"] or ""))


def validate_audit_intent_binding(audit: dict, intent: dict) -> None:
    """Require every executable audit delta to be the resolved canonical intent delta."""
    validate_edit_intent(intent)
    records = audit.get("records")
    if not isinstance(records, list):
        raise AuditError("Audit records must be a list.")
    records_by_qid = {
        record.get("qid"): record for record in records if isinstance(record, dict)
    }
    if len(records_by_qid) != len(records):
        raise AuditError("Audit contains malformed or duplicate QID records.")

    intent_qids = set(intent["intents"])
    editable_qids = {
        record["qid"] for record in records if record.get("status") in EDITABLE_STATUSES
    }
    if editable_qids != intent_qids:
        missing = sorted(intent_qids - editable_qids)
        extra = sorted(editable_qids - intent_qids)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unauthorized " + ", ".join(extra))
        raise AuditError("Audit executable QIDs do not match canonical intent: " + "; ".join(details))

    for qid, item_intent in intent["intents"].items():
        record = records_by_qid.get(qid)
        if record is None:
            raise AuditError(f"{qid}: canonical edit intent is absent from the audit.")
        if record.get("decision") != item_intent["decision"]:
            raise AuditError(f"{qid}: audit decision differs from canonical edit intent.")
        if record.get("rationale") != item_intent["rationale"]:
            raise AuditError(f"{qid}: audit rationale differs from canonical edit intent.")
        if not set(item_intent["evidenceUrls"]).issubset(record.get("evidenceUrls", [])):
            raise AuditError(f"{qid}: audit omits canonical edit-intent evidence.")
        expected_operations = resolve_operations(
            record.get("beforeClaims", []), item_intent["operations"]
        )
        if record.get("editOperations") != expected_operations:
            raise AuditError(f"{qid}: audit operations differ from canonical edit intent.")
        homosaurus_reference_url(item_intent)


def expected_audit_mode(audit: dict, intent: dict) -> str:
    statuses = {
        record["qid"]: record["status"]
        for record in audit["records"]
        if record["qid"] in intent["intents"]
    }
    executed = sum(status == "executed" for status in statuses.values())
    if executed == 0:
        return "review-first"
    if executed == len(intent["intents"]):
        return "executed"
    return "partially-executed"


def checkpoint_audit_state(audit: dict, intent: dict) -> None:
    mode = expected_audit_mode(audit, intent)
    audit["mode"] = mode
    audit["liveEditsPerformed"] = mode != "review-first"
    audit["auditTimestamp"] = utc_now()


def after_claims_match_proposal(after_claims: list[dict], proposed_claims: list[dict]) -> bool:
    unmatched = copy.deepcopy(after_claims)
    for proposed in proposed_claims:
        matches = [
            index
            for index, actual in enumerate(unmatched)
            if actual["property"] == proposed["property"]
            and actual["value"] == proposed["value"]
            and actual["rank"] == proposed["rank"]
            and (
                actual["guid"] == proposed["guid"]
                if proposed["guid"] is not None
                else bool(actual["guid"])
            )
        ]
        if len(matches) != 1:
            return False
        unmatched.pop(matches[0])
    return not unmatched


def snapshot_facts(snapshot: dict) -> dict[str, fetch_data.OntologyCandidateFacts]:
    return {
        record["qid"]: fetch_data.OntologyCandidateFacts(
            qid=record["qid"],
            direct_type_qids=frozenset(record["directTypes"]),
            direct_parent_qids=frozenset(record["parentQids"]),
        )
        for record in snapshot["records"]
    }


def retention_rationale(source_record: dict, signals: list[str]) -> str:
    observations = []
    if "direct-part-of" in signals:
        observations.append(
            "the direct part-of relationship describes a nested or collection membership, "
            "and the item has no direct term/component marker"
        )
    if "fragment-homepage" in signals:
        observations.append("the fragment-style official URL is only a documentation locator")
    if "term-like-description" in signals or "term-like-label" in signals:
        observations.append("term-like text is not a source identity assertion")
    if "camel-case-label" in signals:
        observations.append("the camel-case label is a naming convention rather than eligibility evidence")
    if not observations:
        observations.append("the reviewed source facts do not identify an individual term or component")
    source_classes = ", ".join(source_record["matchedSourceClasses"])
    return (
        f"Retain after review against source class match(es) {source_classes}: "
        + "; ".join(observations)
        + ". No Wikidata claim change is proposed."
    )


def local_exclusion_rationale(source_record: dict) -> str:
    source_classes = ", ".join(source_record["matchedSourceClasses"])
    return (
        f"Exclude locally after Task 24 source review against source class match(es) "
        f"{source_classes}. The reviewed record is not a standalone OKG ontology, "
        "controlled vocabulary, taxonomy, knowledge graph, or ontology language. "
        "No additional Wikidata claim change is proposed."
    )


def build_audit(
    snapshot: dict,
    snapshot_path: Path,
    mappings: SourceMappings,
    intent: dict,
    entities: dict[str, dict],
) -> dict:
    policy = mappings.eligibility_policy_for(ONTOLOGIES_DATASET)
    relevant_property_ids = {
        fetch_data.wikidata_property(mappings, "instanceOf", value_kind="iri"),
        fetch_data.wikidata_property(mappings, "subclassOf", value_kind="iri"),
        fetch_data.wikidata_property(
            mappings, "partOfEntity", ONTOLOGIES_DATASET, "iri"
        ),
    }
    records_by_qid = {record["qid"]: record for record in snapshot["records"]}
    signals_by_qid = {
        qid: candidate_signals(record, set(policy.term_component_markers), set(policy.exclusions))
        for qid, record in records_by_qid.items()
    }
    candidate_qids = sorted(qid for qid, signals in signals_by_qid.items() if signals)
    missing_intents = set(intent["intents"]) - set(candidate_qids)
    if missing_intents:
        raise AuditError("Edit intent QIDs are absent from the captured candidate union: " + ", ".join(sorted(missing_intents)))
    if set(policy.exclusions) - set(candidate_qids):
        raise AuditError("A confirmed exclusion is absent from the captured candidate union.")

    audit_records = []
    for qid in candidate_qids:
        source_record = records_by_qid[qid]
        entity = entities[qid]
        before_claims = relevant_claims(entity, relevant_property_ids)
        edit_intent = intent["intents"].get(qid)
        raw_operations = edit_intent["operations"] if edit_intent else []
        operations = resolve_operations(before_claims, raw_operations)
        proposed_claims = apply_operations_to_claims(before_claims, operations)
        evidence = {
            f"https://www.wikidata.org/wiki/{qid}",
            *source_record["officialWebsites"],
        }
        if edit_intent:
            evidence.update(edit_intent["evidenceUrls"])
            decision = edit_intent["decision"]
            rationale = edit_intent["rationale"]
            status = "planned"
        elif qid in policy.exclusions:
            decision = "exclude-locally"
            status = "no-edit"
            rationale = local_exclusion_rationale(source_record)
        else:
            decision = "retain"
            status = "no-edit"
            rationale = retention_rationale(source_record, signals_by_qid[qid])
        audit_records.append(
            {
                "qid": qid,
                "label": source_record["label"],
                "description": source_record["description"],
                "matchedSourceClasses": source_record["matchedSourceClasses"],
                "triggeringSignals": signals_by_qid[qid],
                "parentQids": source_record["parentQids"],
                "decision": decision,
                "status": status,
                "beforeClaims": before_claims,
                "proposedClaims": proposed_claims,
                "afterClaims": None,
                "editOperations": operations,
                "evidenceUrls": sorted(evidence),
                "rationale": rationale,
                "oldRevisionId": entity.get("lastrevid"),
                "newRevisionId": None,
                "diffUrl": None,
            }
        )

    eligibility = fetch_data.evaluate_ontology_eligibility(snapshot_facts(snapshot), policy)
    return {
        "schemaVersion": 1,
        "taskId": "24",
        "mode": "review-first",
        "auditTimestamp": snapshot["capturedAt"],
        "sourceSnapshot": {
            "path": "audits/wikidata/task-24-source-snapshot.json",
            "sha256": sha256_file(snapshot_path),
            "capturedAt": snapshot["capturedAt"],
            "endpoint": snapshot["endpoint"],
            "sourceClasses": snapshot["sourceClasses"],
        },
        "cohortSize": snapshot["cohortSize"],
        "markerQids": sorted(policy.term_component_markers),
        "confirmedExclusionQids": sorted(policy.exclusions),
        "ruleExcludedQids": sorted(eligibility.rule_exclusion_qids),
        "liveEditsPerformed": False,
        "records": audit_records,
    }


def validate_audit(
    audit: dict,
    snapshot: dict,
    mappings: SourceMappings,
    intent: dict | None = None,
) -> None:
    intent = read_json(INTENT_PATH) if intent is None else intent
    schema = read_json(SCHEMA_PATH)
    errors = sorted(
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).iter_errors(audit),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        rendered = "; ".join(
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise AuditError("Audit schema validation failed: " + rendered)

    if audit["cohortSize"] != snapshot["cohortSize"]:
        raise AuditError("Audit cohort size does not match the captured source snapshot.")
    if audit["sourceSnapshot"]["sha256"] != sha256_file(SNAPSHOT_PATH):
        raise AuditError("Audit source snapshot checksum does not match the committed snapshot.")

    policy = mappings.eligibility_policy_for(ONTOLOGIES_DATASET)
    records_by_qid = {record["qid"]: record for record in snapshot["records"]}
    expected_candidates = {
        qid
        for qid, record in records_by_qid.items()
        if candidate_signals(record, set(policy.term_component_markers), set(policy.exclusions))
    }
    actual_qids = [record["qid"] for record in audit["records"]]
    if len(actual_qids) != len(set(actual_qids)):
        raise AuditError("Audit contains duplicate QID records.")
    if set(actual_qids) != expected_candidates:
        raise AuditError("Audit records do not exactly cover the reproducible candidate union.")
    if set(policy.exclusions) - set(actual_qids):
        raise AuditError("Audit omits a confirmed exclusion.")
    if set(audit["confirmedExclusionQids"]) != set(policy.exclusions):
        raise AuditError("Audit confirmed exclusions do not match sources.ttl.")

    intent_qids = set(intent["intents"])
    for record in audit["records"]:
        qid = record["qid"]
        if qid in intent_qids:
            continue
        expected_decision = "exclude-locally" if qid in policy.exclusions else "retain"
        if record["decision"] != expected_decision:
            raise AuditError(
                f"{qid}: audit decision must be {expected_decision!r} for the declared source policy."
            )

    manual_review = audit.get("manualReview")
    if manual_review and manual_review["status"] == "complete":
        if manual_review["recordCount"] != len(audit["records"]):
            raise AuditError("Completed manual review count does not cover every audit record.")

    validate_audit_intent_binding(audit, intent)
    expected_mode = expected_audit_mode(audit, intent)
    if audit["mode"] != expected_mode:
        raise AuditError(
            f"Audit mode {audit['mode']!r} does not truthfully reflect record states "
            f"({expected_mode!r} required)."
        )
    if audit["liveEditsPerformed"] != (expected_mode != "review-first"):
        raise AuditError("liveEditsPerformed does not truthfully reflect executed records.")

    for record in audit["records"]:
        status = record["status"]
        operations = record["editOperations"]
        if status in {"planned", "approved"}:
            if not operations or record["afterClaims"] is not None or record["newRevisionId"] is not None or record["diffUrl"] is not None:
                raise AuditError(f"{record['qid']}: planned/approved edits require operations and null after fields.")
        elif status == "no-edit":
            if operations or record["proposedClaims"] != record["beforeClaims"]:
                raise AuditError(f"{record['qid']}: no-edit record must preserve claims exactly.")
        elif status == "executed":
            if record["afterClaims"] is None or not record["oldRevisionId"] or not record["newRevisionId"] or not record["diffUrl"]:
                raise AuditError(f"{record['qid']}: executed record lacks revision evidence.")
            if record["newRevisionId"] <= record["oldRevisionId"]:
                raise AuditError(f"{record['qid']}: executed record did not advance the revision.")
            expected_diff_url = (
                f"https://www.wikidata.org/w/index.php?title={record['qid']}"
                f"&diff={record['newRevisionId']}&oldid={record['oldRevisionId']}"
            )
            if record["diffUrl"] != expected_diff_url:
                raise AuditError(f"{record['qid']}: executed diff URL does not match its revisions.")
            if not after_claims_match_proposal(record["afterClaims"], record["proposedClaims"]):
                raise AuditError(f"{record['qid']}: executed claims do not match the reviewed proposal.")
        expected_proposed = apply_operations_to_claims(record["beforeClaims"], operations)
        if expected_proposed != record["proposedClaims"]:
            raise AuditError(f"{record['qid']}: proposed claims do not match exact edit operations.")


def item_value_snak(property_id: str, value_qid: str) -> dict:
    return {
        "snaktype": "value",
        "property": property_id,
        "datatype": "wikibase-item",
        "datavalue": {
            "value": {
                "entity-type": "item",
                "numeric-id": int(value_qid[1:]),
                "id": value_qid,
            },
            "type": "wikibase-entityid",
        },
    }


def reference_url(reference_url: str) -> dict:
    return {
        "snaks": {
            REFERENCE_URL_PROPERTY: [
                {
                    "snaktype": "value",
                    "property": REFERENCE_URL_PROPERTY,
                    "datatype": "url",
                    "datavalue": {"value": reference_url, "type": "string"},
                }
            ]
        },
        "snaks-order": [REFERENCE_URL_PROPERTY],
    }


def atomic_entity_data(operations: list[dict], evidence_url: str | None = None) -> dict:
    claims: dict[str, list[dict]] = {}
    for operation in operations:
        if operation["action"] == "remove":
            statement = {"id": operation["claimGuid"], "remove": ""}
        elif operation["action"] == "add":
            statement = {
                "mainsnak": item_value_snak(operation["property"], operation["value"]),
                "type": "statement",
                "rank": "normal",
            }
            if evidence_url and operation["property"] in {"P31", "P361"}:
                statement["references"] = [reference_url(evidence_url)]
        else:
            raise AuditError(f"Unsupported edit action: {operation['action']}")
        claims.setdefault(operation["property"], []).append(statement)
    return {"claims": claims}


class WikidataClient:
    def __init__(self, session: requests.Session):
        self.session = session
        self.csrf_token: str | None = None

    def _post(self, data: dict, label: str) -> dict:
        for attempt in range(1, WIKIDATA_MAXLAG_ATTEMPTS + 1):
            try:
                response = self.session.post(
                    WIKIDATA_API,
                    data={"format": "json", "formatversion": "2", **data},
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise AuditError(
                    f"{label}: write response failed or was indeterminate; "
                    "inspect the QID and revision history before retrying"
                ) from exc

            error = payload.get("error")
            if not error:
                return payload
            if error.get("code") != "maxlag" or attempt == WIKIDATA_MAXLAG_ATTEMPTS:
                raise AuditError(f"{label}: Wikidata API error {error.get('code')}")

            delay = WIKIDATA_MAXLAG_FALLBACK_SECONDS
            retry_after = response.headers.get("Retry-After")
            try:
                if retry_after is not None:
                    delay = max(delay, float(retry_after))
                if error.get("lag") is not None:
                    delay = max(delay, float(error["lag"]))
            except (TypeError, ValueError):
                pass
            delay = min(delay, 30.0)
            print(
                f"{label}: Wikidata maxlag; retrying in {delay:.0f}s "
                f"({attempt}/{WIKIDATA_MAXLAG_ATTEMPTS}).",
                file=sys.stderr,
            )
            time.sleep(delay)

        raise AuditError(f"{label}: Wikidata maxlag retry loop exhausted.")

    def login(self, username: str, password: str) -> None:
        token_payload = request_json(
            self.session,
            {"action": "query", "meta": "tokens", "type": "login", "format": "json"},
            "Wikidata login token",
        )
        login_token = token_payload["query"]["tokens"]["logintoken"]
        login = self._post(
            {"action": "login", "lgname": username, "lgpassword": password, "lgtoken": login_token},
            "Wikidata login",
        )
        if login.get("login", {}).get("result") != "Success":
            raise AuditError("Wikidata login was not successful.")
        token = request_json(
            self.session,
            {"action": "query", "meta": "tokens", "format": "json"},
            "Wikidata edit token",
        )
        self.csrf_token = token["query"]["tokens"]["csrftoken"]

    def get_entity(self, qid: str) -> dict:
        return fetch_entities(self.session, [qid])[qid]

    def edit_entity(
        self,
        qid: str,
        operations: list[dict],
        base_revision_id: int,
        summary: str,
        evidence_url: str | None = None,
    ) -> dict:
        payload = self._post(
            {
                "action": "wbeditentity",
                "id": qid,
                "data": json.dumps(
                    atomic_entity_data(operations, evidence_url),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "baserevid": base_revision_id,
                "maxlag": WIKIDATA_MAXLAG,
                "assert": "user",
                "token": self.csrf_token,
                "summary": summary,
            },
            f"atomic edit of {qid}",
        )
        entity = payload.get("entity")
        if payload.get("success") != 1:
            raise AuditError(f"{qid}: atomic edit response did not report success.")
        if (
            not isinstance(entity, dict)
            or entity.get("id") != qid
            or not isinstance(entity.get("lastrevid"), int)
            or entity["lastrevid"] <= base_revision_id
        ):
            raise AuditError(f"{qid}: atomic edit response omitted the updated entity.")
        return entity


def execute_approved_records(audit: dict, client: WikidataClient, on_record=None) -> int:
    intent = read_json(INTENT_PATH)
    validate_audit_intent_binding(audit, intent)
    approved = [record for record in audit["records"] if record["status"] == "approved"]
    if not approved:
        raise AuditError("No audit records have separately approved status.")
    if any(record["status"] == "planned" for record in audit["records"]):
        raise AuditError("Planned records remain; separate approval must cover the complete edit plan.")

    for index, record in enumerate(approved):
        current = client.get_entity(record["qid"])
        relevant_property_ids = {
            claim["property"] for claim in record["beforeClaims"]
        } | {operation["property"] for operation in record["editOperations"]}
        current_claims = relevant_claims(current, relevant_property_ids)
        if current.get("lastrevid") != record["oldRevisionId"]:
            if after_claims_match_proposal(current_claims, record["proposedClaims"]):
                record["afterClaims"] = current_claims
                record["newRevisionId"] = current["lastrevid"]
                record["diffUrl"] = (
                    f"https://www.wikidata.org/w/index.php?title={record['qid']}"
                    f"&diff={record['newRevisionId']}&oldid={record['oldRevisionId']}"
                )
                record["status"] = "executed"
                checkpoint_audit_state(audit, intent)
                if on_record:
                    on_record(audit)
                if index + 1 < len(approved):
                    time.sleep(WIKIDATA_EDIT_PAUSE_SECONDS)
                continue
            raise AuditError(
                f"{record['qid']}: source revision changed after review and does not "
                "match the reviewed result; recapture the plan."
            )
        if current_claims != record["beforeClaims"]:
            raise AuditError(f"{record['qid']}: reviewed claims no longer match Wikidata.")
        summary = "Correct term classification per reviewed OKG Task 24 audit"
        after = client.edit_entity(
            record["qid"],
            record["editOperations"],
            record["oldRevisionId"],
            summary,
            homosaurus_reference_url(intent["intents"][record["qid"]]),
        )
        after_claims = relevant_claims(after, relevant_property_ids)
        new_revision_id = after.get("lastrevid")
        if not new_revision_id or new_revision_id <= record["oldRevisionId"]:
            raise AuditError(f"{record['qid']}: Wikidata did not return a new revision.")
        if not after_claims_match_proposal(after_claims, record["proposedClaims"]):
            raise AuditError(f"{record['qid']}: atomic edit result differs from reviewed proposal.")
        record["afterClaims"] = after_claims
        record["newRevisionId"] = new_revision_id
        record["diffUrl"] = (
            f"https://www.wikidata.org/w/index.php?title={record['qid']}"
            f"&diff={record['newRevisionId']}&oldid={record['oldRevisionId']}"
        )
        record["status"] = "executed"
        checkpoint_audit_state(audit, intent)
        if on_record:
            on_record(audit)
        if index + 1 < len(approved):
            time.sleep(WIKIDATA_EDIT_PAUSE_SECONDS)

    return len(approved)


def configured_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": fetch_data.USER_AGENT, "Accept": "application/json"})
    return session


def audit_contains_protected_evidence(audit: dict) -> bool:
    if audit.get("liveEditsPerformed") or audit.get("mode") != "review-first":
        return True
    for record in audit.get("records", []):
        if record.get("status") in {"approved", "executed"}:
            return True
        if any(record.get(field) is not None for field in ("afterClaims", "newRevisionId", "diffUrl")):
            return True
    return False


def protect_capture_evidence(archive_and_recapture: bool) -> Path | None:
    if not AUDIT_PATH.exists():
        return None
    existing_audit = read_json(AUDIT_PATH)
    if not audit_contains_protected_evidence(existing_audit):
        return None
    if not archive_and_recapture:
        raise AuditError(
            "Capture refused: the current audit contains approval or execution evidence. "
            "Use --archive-and-recapture to preserve it before creating a new capture."
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    archive_path = AUDIT_PATH.parent / "archive" / f"task-24-{timestamp}"
    archive_path.mkdir(parents=True, exist_ok=False)
    for source_path in (AUDIT_PATH, SNAPSHOT_PATH, INTENT_PATH):
        if source_path.exists():
            shutil.copy2(source_path, archive_path / source_path.name)
    return archive_path


def wikidata_credentials() -> tuple[str, str]:
    username = os.environ.get("WIKI_USER", "").strip()
    password = os.environ.get("WIKI_PASS", "")
    try:
        if not username:
            username = input("Wikidata username: ").strip()
        if not password:
            password = getpass.getpass("Wikidata bot password: ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise AuditError("Wikidata credentials were not provided.") from exc
    if not username or not password:
        raise AuditError("Live execution requires a Wikidata username and bot password.")
    return username, password


def command_capture(args: argparse.Namespace) -> int:
    protect_capture_evidence(args.archive_and_recapture)
    mappings = load_source_mappings(ROOT / "sources.ttl")
    session = configured_session()
    snapshot = capture_source_snapshot(session, mappings)
    write_json_atomic(SNAPSHOT_PATH, snapshot)
    policy = mappings.eligibility_policy_for(ONTOLOGIES_DATASET)
    candidate_qids = [
        record["qid"]
        for record in snapshot["records"]
        if candidate_signals(record, set(policy.term_component_markers), set(policy.exclusions))
    ]
    entities = fetch_entities(session, candidate_qids)
    audit = build_audit(snapshot, SNAPSHOT_PATH, mappings, read_json(INTENT_PATH), entities)
    write_json_atomic(AUDIT_PATH, audit)
    validate_audit(audit, snapshot, mappings)
    print(
        f"Captured {snapshot['cohortSize']} raw ontology candidates and "
        f"{len(audit['records'])} reviewed-signal records; no live edits were made."
    )
    return 0


def command_validate(args: argparse.Namespace) -> int:
    mappings = load_source_mappings(ROOT / "sources.ttl")
    snapshot = read_json(SNAPSHOT_PATH)
    audit = read_json(AUDIT_PATH)
    validate_audit(audit, snapshot, mappings)
    print(f"Validated Task 24 audit: {len(audit['records'])} reviewed records.")
    return 0


def command_execute(args: argparse.Namespace) -> int:
    mappings = load_source_mappings(ROOT / "sources.ttl")
    snapshot = read_json(SNAPSHOT_PATH)
    audit = read_json(AUDIT_PATH)
    validate_audit(audit, snapshot, mappings)
    planned = sum(record["status"] in {"planned", "approved"} for record in audit["records"])
    if not args.apply_approved:
        print(f"Dry run only: {planned} reviewed records contain proposed edits; no live writes made.")
        return 0

    username, password = wikidata_credentials()
    client = WikidataClient(configured_session())
    client.login(username, password)
    count = execute_approved_records(audit, client, lambda payload: write_json_atomic(AUDIT_PATH, payload))
    write_json_atomic(AUDIT_PATH, audit)
    print(f"Executed {count} separately approved Wikidata edit records.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture", help="capture the public source snapshot and dry-run plan")
    capture.add_argument(
        "--archive-and-recapture",
        action="store_true",
        help="archive an approved or executed audit and its evidence before replacing it",
    )
    capture.set_defaults(func=command_capture)
    validate = subparsers.add_parser("validate", help="validate the committed snapshot and audit")
    validate.set_defaults(func=command_validate)
    execute = subparsers.add_parser("execute", help="show or execute separately approved edits")
    execute.add_argument(
        "--apply-approved",
        action="store_true",
        help="perform only records already marked approved after separate user review",
    )
    execute.set_defaults(func=command_execute)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except (AuditError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Task 24 audit error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
