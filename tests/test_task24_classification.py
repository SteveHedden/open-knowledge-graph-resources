import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rdflib import Graph


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fetch_data  # noqa: E402
import semantic_config  # noqa: E402
import wikidata_classification_audit as audit_tool  # noqa: E402


CONFIRMED_EXCLUSIONS = {
    "Q116185807": "black-bisexual-people",
    "Q116185838": "crossdressers",
    "Q124134779": "visual",
    "Q124134874": "auditory",
    "Q124134880": "chartonvisual",
    "Q124134885": "chemonvisual",
    "Q124134971": "mathonvisual",
    "Q124134976": "textual",
    "Q124134989": "textonvisual",
    "Q124134995": "tactile",
    "Q124134998": "musiconvisual",
    "Q124135003": "diagramonvisual",
    "Q124135010": "colordependent",
}

REVIEWED_LOCAL_EXCLUSIONS = {
    "Q16511225": "glossary-of-german-nautical-terms-nz",
    "Q17005183": "glossary-of-education-terms-d-f",
    "Q24024806": "abbreviations-in-appendix",
    "Q2957606": "charadriidae",
    "Q2972287": "ciconiiformes-according-to-sibley",
    "Q3546270": "tyrannidae-according-to-sibley",
    "Q5571732": "glossary-of-baseball-a",
    "Q5571794": "glossary-of-education-terms-ac",
    "Q5571796": "glossary-of-education-terms-g-l",
    "Q5571798": "glossary-of-education-terms-m-o",
    "Q5571799": "glossary-of-education-terms-p-r",
    "Q5571800": "glossary-of-education-terms-s",
    "Q5571802": "glossary-of-education-terms-tz",
    "Q63254594": "list-of-music-terms-of-french-and-italian-origin",
    "Q63254622": "list-of-music-terms-of-german-origin",
    "Q70789890": "glossary-of-german-nautical-terms-am",
}

ALL_SOURCE_EXCLUSIONS = {**CONFIRMED_EXCLUSIONS, **REVIEWED_LOCAL_EXCLUSIONS}


def binding(value):
    return {"type": "uri", "value": value}


def candidate_row(qid, direct_type, parent=None, homepage=None):
    row = {
        "item": binding(f"http://www.wikidata.org/entity/{qid}"),
        "directType": binding(f"http://www.wikidata.org/entity/{direct_type}"),
        "matchedTypeQid": {"type": "literal", "value": "Q100"},
    }
    if parent:
        row["partOfEntity"] = binding(f"http://www.wikidata.org/entity/{parent}")
    if homepage:
        row["officialWebsite"] = {"type": "uri", "value": homepage}
    return row


def item_intent(qid, operations, evidence_url="https://example.org/evidence"):
    return {
        "schemaVersion": 1,
        "taskId": "24",
        "intents": {
            qid: {
                "decision": "correct-wikidata-and-exclude-locally",
                "rationale": f"Reviewed correction for {qid}.",
                "evidenceUrls": [evidence_url],
                "operations": operations,
            }
        },
    }


def audit_record(qid, old_revision, operations, evidence_url="https://example.org/evidence"):
    before = [
        {"guid": f"{qid}$old", "property": "P31", "value": "Q2", "rank": "normal"}
    ]
    resolved = audit_tool.resolve_operations(before, operations)
    return {
        "qid": qid,
        "decision": "correct-wikidata-and-exclude-locally",
        "rationale": f"Reviewed correction for {qid}.",
        "evidenceUrls": [evidence_url],
        "status": "approved",
        "beforeClaims": before,
        "proposedClaims": audit_tool.apply_operations_to_claims(before, resolved),
        "afterClaims": None,
        "editOperations": resolved,
        "oldRevisionId": old_revision,
        "newRevisionId": None,
        "diffUrl": None,
    }


def entity_from_claims(qid, revision, claims):
    serialized = {}
    for claim in claims:
        serialized.setdefault(claim["property"], []).append(
            {
                "id": claim["guid"],
                "rank": claim["rank"],
                "mainsnak": {
                    "datavalue": {"value": {"id": claim["value"]}}
                },
            }
        )
    return {"id": qid, "lastrevid": revision, "claims": serialized}


def policy_fixture(path):
    path.write_text(
        """@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix okg: <https://openknowledgegraphs.com/ontology#> .
@prefix src: <https://openknowledgegraphs.com/sources#> .
@prefix wd: <http://www.wikidata.org/entity/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

src:class a okg:SourceClassMapping ; okg:sourceClassId "Q100" ; okg:targetTerm okg:Ontology ; okg:projectionValue "Ontology" ; okg:catalogDataset <https://openknowledgegraphs.com/datasets/ontologies> ; okg:sortOrder 1 .
src:property a okg:SourcePropertyMapping ; okg:sourcePropertyId "P31" ; okg:normalizedField "instanceOf" ; okg:targetTerm rdf:type ; okg:valueKind "iri" ; okg:cardinality "many" ; okg:sortOrder 1 .

src:policy a okg:SourceEligibilityPolicy ;
  okg:catalogDataset <https://openknowledgegraphs.com/datasets/ontologies> ;
  okg:termComponentMarker wd:Q1969448, wd:Q137426747, wd:Q7095057 ;
  okg:sourceExclusion src:excluded ;
  okg:eligibilityException src:exception .
src:excluded a okg:SourceExclusion ; okg:sourceEntity wd:Q10 ; dcterms:description "confirmed bad child" ; dcterms:source <https://example.org/evidence/excluded> .
src:exception a okg:SourceEligibilityException ; okg:sourceEntity wd:Q9 ; dcterms:description "reviewed valid nested vocabulary" ; dcterms:source <https://example.org/evidence/exception> .
""",
        encoding="utf-8",
    )


class EligibilityRuleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        source_path = Path(self.tempdir.name) / "sources.ttl"
        policy_fixture(source_path)
        self.mappings = semantic_config.load_source_mappings(source_path)
        self.policy = self.mappings.eligibility_policy_for(
            semantic_config.ONTOLOGIES_DATASET
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_markers_exclusions_and_exceptions_are_loaded_from_rdf(self):
        self.assertEqual(
            self.policy.term_component_markers,
            frozenset({"Q1969448", "Q137426747", "Q7095057"}),
        )
        self.assertEqual(set(self.policy.exclusions), {"Q10"})
        self.assertEqual(set(self.policy.exceptions), {"Q9"})
        self.assertIn("example.org/evidence", self.policy.exclusions["Q10"].evidence_urls[0])

    def test_narrow_rule_preserves_legitimate_and_signal_only_records(self):
        rows = [
            candidate_row("Q1", "Q100"),
            candidate_row("Q2", "Q100", parent="Q1"),
            candidate_row("Q3", "Q1969448", parent="Q1"),
            candidate_row("Q4", "Q137426747", parent="Q1"),
            candidate_row("Q5", "Q7095057", parent="Q1"),
            candidate_row("Q6", "Q1310239", parent="Q1"),
            candidate_row("Q7", "Q1969448", parent="Q999"),
            candidate_row("Q8", "Q100", homepage="https://example.org/vocab#term"),
            candidate_row("Q9", "Q1969448", parent="Q1"),
            candidate_row("Q10", "Q100"),
        ]

        filtered, result = fetch_data.filter_ontology_rows(rows, self.policy)
        retained = {
            fetch_data.qid_from_wikidata_iri(fetch_data.binding_value(row, "item"))
            for row in filtered
        }

        self.assertEqual(retained, {"Q1", "Q2", "Q6", "Q7", "Q8", "Q9"})
        self.assertEqual(result.rule_exclusion_qids, frozenset({"Q3", "Q4", "Q5"}))
        self.assertEqual(result.declared_exclusion_qids, frozenset({"Q10"}))

    def test_filtering_removes_ineligible_rows_before_record_or_slug_work(self):
        rows = [
            candidate_row("Q1", "Q100"),
            candidate_row("Q3", "Q1969448", parent="Q1"),
        ]
        filtered, _ = fetch_data.filter_ontology_rows(rows, self.policy)
        records, _, _ = fetch_data.parse_ontology_rows(
            filtered,
            {
                "http://www.wikidata.org/entity/Q1": "Parent vocabulary",
                "http://www.wikidata.org/entity/Q3": "Child term",
            },
            {},
            {},
            {"Q100": semantic_config.OKG.ControlledVocabulary},
        )
        self.assertEqual(
            {fetch_data.qid_from_wikidata_iri(iri) for iri in records},
            {"Q1"},
        )


class AuditContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = audit_tool.read_json(audit_tool.SNAPSHOT_PATH)
        cls.audit = audit_tool.read_json(audit_tool.AUDIT_PATH)
        cls.mappings = semantic_config.load_source_mappings(ROOT / "sources.ttl")

    def test_committed_audit_is_strict_and_covers_reproducible_candidate_union(self):
        audit_tool.validate_audit(self.audit, self.snapshot, self.mappings)
        self.assertEqual(self.audit["cohortSize"], len(self.snapshot["records"]))
        self.assertEqual(
            {
                record["qid"]
                for record in self.audit["records"]
                if record["status"] in audit_tool.EDITABLE_STATUSES
            },
            set(CONFIRMED_EXCLUSIONS),
        )

    def test_reviewed_local_exclusions_are_explicit_policy_decisions(self):
        policy = self.mappings.eligibility_policy_for(
            semantic_config.ONTOLOGIES_DATASET
        )
        self.assertEqual(set(policy.exclusions), set(ALL_SOURCE_EXCLUSIONS))
        records = {record["qid"]: record for record in self.audit["records"]}
        for qid in REVIEWED_LOCAL_EXCLUSIONS:
            self.assertEqual(records[qid]["decision"], "exclude-locally")
            self.assertEqual(records[qid]["status"], "no-edit")
            self.assertFalse(records[qid]["editOperations"])

        tampered = copy.deepcopy(self.audit)
        record = next(
            item for item in tampered["records"] if item["qid"] in REVIEWED_LOCAL_EXCLUSIONS
        )
        record["decision"] = "retain"
        with self.assertRaises(audit_tool.AuditError):
            audit_tool.validate_audit(tampered, self.snapshot, self.mappings)

    def test_manual_review_covers_every_flagged_record(self):
        review = self.audit["manualReview"]
        self.assertEqual(review["status"], "complete")
        self.assertEqual(review["recordCount"], len(self.audit["records"]))
        self.assertEqual(
            review["reviewedRanges"],
            ["0-349", "350-799", "800-1249", "1250-1691"],
        )

        tampered = copy.deepcopy(self.audit)
        tampered["manualReview"]["recordCount"] -= 1
        with self.assertRaises(audit_tool.AuditError):
            audit_tool.validate_audit(tampered, self.snapshot, self.mappings)

    def test_committed_audit_lifecycle_fields_are_truthful(self):
        for record in self.audit["records"]:
            if record["qid"] not in CONFIRMED_EXCLUSIONS:
                continue
            self.assertTrue(record["editOperations"])
            if record["status"] in {"planned", "approved"}:
                self.assertIsNone(record["afterClaims"])
                self.assertIsNone(record["newRevisionId"])
                self.assertIsNone(record["diffUrl"])
            else:
                self.assertEqual(record["status"], "executed")
                self.assertIsNotNone(record["afterClaims"])
                self.assertNotEqual(record["newRevisionId"], record["oldRevisionId"])
                self.assertIsNotNone(record["diffUrl"])

        intent = audit_tool.read_json(audit_tool.INTENT_PATH)
        expected_mode = audit_tool.expected_audit_mode(self.audit, intent)
        self.assertEqual(self.audit["mode"], expected_mode)
        self.assertEqual(
            self.audit["liveEditsPerformed"], expected_mode != "review-first"
        )

    def planned_audit_fixture(self):
        fixture = copy.deepcopy(self.audit)
        for record in fixture["records"]:
            if record["qid"] not in CONFIRMED_EXCLUSIONS:
                continue
            record["status"] = "planned"
            record["afterClaims"] = None
            record["newRevisionId"] = None
            record["diffUrl"] = None
        fixture["mode"] = "review-first"
        fixture["liveEditsPerformed"] = False
        return fixture

    def test_planned_approved_partial_and_executed_audits_validate(self):
        intent = audit_tool.read_json(audit_tool.INTENT_PATH)
        approved = self.planned_audit_fixture()
        for record in approved["records"]:
            if record["status"] == "planned":
                record["status"] = "approved"
        audit_tool.validate_audit(approved, self.snapshot, self.mappings, intent)

        def mark_executed(record):
            after = copy.deepcopy(record["proposedClaims"])
            for index, claim in enumerate(after):
                if claim["guid"] is None:
                    claim["guid"] = f"{record['qid']}$test-{index}"
            record["afterClaims"] = after
            record["newRevisionId"] = record["oldRevisionId"] + 1
            record["diffUrl"] = (
                f"https://www.wikidata.org/w/index.php?title={record['qid']}"
                f"&diff={record['newRevisionId']}&oldid={record['oldRevisionId']}"
            )
            record["status"] = "executed"

        partial = copy.deepcopy(approved)
        first_edit = next(
            record for record in partial["records"] if record["qid"] in CONFIRMED_EXCLUSIONS
        )
        mark_executed(first_edit)
        partial["mode"] = "partially-executed"
        partial["liveEditsPerformed"] = True
        audit_tool.validate_audit(partial, self.snapshot, self.mappings, intent)

        executed = copy.deepcopy(approved)
        for record in executed["records"]:
            if record["qid"] in CONFIRMED_EXCLUSIONS:
                mark_executed(record)
        executed["mode"] = "executed"
        executed["liveEditsPerformed"] = True
        audit_tool.validate_audit(executed, self.snapshot, self.mappings, intent)

    def test_executor_refuses_unapproved_plan(self):
        class NoWriteClient:
            def get_entity(self, qid):
                raise AssertionError("unapproved dry-run plan must not read or write Wikidata")

        with self.assertRaises(audit_tool.AuditError):
            audit_tool.execute_approved_records(self.planned_audit_fixture(), NoWriteClient())

    def test_mocked_executor_applies_only_reviewed_operations(self):
        operations = [
            {"action": "remove", "property": "P31", "value": "Q2"},
            {"action": "add", "property": "P31", "value": "Q3"},
        ]
        intent = item_intent("Q1", operations)
        planned = audit_record("Q1", 10, operations)
        fixture_audit = {
            "records": [planned],
            "liveEditsPerformed": False,
            "mode": "review-first",
            "auditTimestamp": "2026-08-14T00:00:00Z",
        }

        class FakeClient:
            def __init__(self):
                self.writes = []

            def get_entity(self, qid):
                return entity_from_claims(qid, 10, planned["beforeClaims"])

            def edit_entity(self, qid, resolved, base_revision, summary, evidence_url):
                self.writes.append((qid, copy.deepcopy(resolved), base_revision, evidence_url))
                after = copy.deepcopy(planned["proposedClaims"])
                for claim in after:
                    if claim["guid"] is None:
                        claim["guid"] = f"{qid}$new"
                return entity_from_claims(qid, 11, after)

        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            intent_path = Path(directory) / "intent.json"
            audit_tool.write_json_atomic(intent_path, intent)
            with mock.patch.object(audit_tool, "INTENT_PATH", intent_path):
                self.assertEqual(audit_tool.execute_approved_records(fixture_audit, client), 1)
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0][0:3], ("Q1", planned["editOperations"], 10))
        self.assertEqual(planned["status"], "executed")
        self.assertEqual(planned["newRevisionId"], 11)
        self.assertEqual(fixture_audit["mode"], "executed")
        self.assertTrue(fixture_audit["liveEditsPerformed"])

    def test_executor_reconciles_an_already_applied_reviewed_result(self):
        operations = [{"action": "remove", "property": "P31", "value": "Q2"}]
        intent = item_intent("Q1", operations)
        record = audit_record("Q1", 10, operations)
        record["status"] = "approved"
        fixture_audit = {
            "records": [record],
            "liveEditsPerformed": False,
            "mode": "review-first",
            "auditTimestamp": "2026-08-14T00:00:00Z",
        }

        class AlreadyAppliedClient:
            def get_entity(self, qid):
                return entity_from_claims(qid, 11, record["proposedClaims"])

            def edit_entity(self, *args, **kwargs):
                raise AssertionError("an already-applied reviewed result must not be rewritten")

        with tempfile.TemporaryDirectory() as directory:
            intent_path = Path(directory) / "intent.json"
            audit_tool.write_json_atomic(intent_path, intent)
            with mock.patch.object(audit_tool, "INTENT_PATH", intent_path):
                self.assertEqual(
                    audit_tool.execute_approved_records(
                        fixture_audit,
                        AlreadyAppliedClient(),
                    ),
                    1,
                )

        self.assertEqual(record["status"], "executed")
        self.assertEqual(record["newRevisionId"], 11)
        self.assertEqual(record["afterClaims"], record["proposedClaims"])
        self.assertEqual(fixture_audit["mode"], "executed")

    def test_executor_rejects_audit_operation_tampering_before_client_access(self):
        tampered = self.planned_audit_fixture()
        for record in tampered["records"]:
            if record["status"] == "planned":
                record["status"] = "approved"
        edited = next(record for record in tampered["records"] if record["editOperations"])
        edited["editOperations"][0]["value"] = "Q999"

        class NoAccessClient:
            def get_entity(self, qid):
                raise AssertionError("tampered audit must be rejected before client access")

        with self.assertRaises(audit_tool.AuditError):
            audit_tool.execute_approved_records(tampered, NoAccessClient())

    def test_atomic_wbeditentity_payload_has_conflict_guards_and_homosaurus_references(self):
        evidence_url = "https://homosaurus.org/v3/homoit0000135"
        operations = [
            {
                "action": "remove",
                "claimGuid": "Q1$old",
                "property": "P31",
                "value": "Q1469824",
            },
            {
                "action": "add",
                "claimGuid": None,
                "property": "P31",
                "value": "Q1969448",
            },
            {
                "action": "add",
                "claimGuid": None,
                "property": "P361",
                "value": "Q26936735",
            },
        ]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "success": 1,
                    "entity": {"id": "Q1", "lastrevid": 11, "claims": {}},
                }

        class FakeSession:
            def __init__(self):
                self.posts = []

            def post(self, url, data, timeout):
                self.posts.append((url, copy.deepcopy(data), timeout))
                return FakeResponse()

        session = FakeSession()
        client = audit_tool.WikidataClient(session)
        client.csrf_token = "csrf-token"
        client.edit_entity("Q1", operations, 10, "reviewed summary", evidence_url)

        self.assertEqual(len(session.posts), 1)
        _, post_data, _ = session.posts[0]
        self.assertEqual(post_data["action"], "wbeditentity")
        self.assertEqual(post_data["id"], "Q1")
        self.assertEqual(post_data["baserevid"], 10)
        self.assertEqual(post_data["maxlag"], audit_tool.WIKIDATA_MAXLAG)
        self.assertEqual(post_data["assert"], "user")
        self.assertEqual(post_data["token"], "csrf-token")

        entity_data = json.loads(post_data["data"])
        self.assertNotIn("clear", entity_data)
        self.assertEqual(set(entity_data["claims"]), {"P31", "P361"})
        self.assertEqual(entity_data["claims"]["P31"][0], {"id": "Q1$old", "remove": ""})
        for statement in (
            entity_data["claims"]["P31"][1],
            entity_data["claims"]["P361"][0],
        ):
            references = statement["references"]
            self.assertEqual(len(references), 1)
            self.assertEqual(
                references[0]["snaks"]["P854"][0]["datavalue"]["value"],
                evidence_url,
            )
        self.assertNotIn("P50", entity_data["claims"])

        removal_only = audit_tool.atomic_entity_data([operations[0]])
        self.assertEqual(
            removal_only,
            {"claims": {"P31": [{"id": "Q1$old", "remove": ""}]}},
        )

    def test_post_waits_and_retries_safe_maxlag_rejections(self):
        class FakeResponse:
            def __init__(self, payload, retry_after=None):
                self.payload = payload
                self.headers = {}
                if retry_after is not None:
                    self.headers["Retry-After"] = retry_after

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class FakeSession:
            def __init__(self):
                self.responses = [
                    FakeResponse(
                        {"error": {"code": "maxlag", "lag": 7}},
                        retry_after="3",
                    ),
                    FakeResponse({"success": 1}),
                ]
                self.posts = 0

            def post(self, url, data, timeout):
                self.posts += 1
                return self.responses.pop(0)

        session = FakeSession()
        with mock.patch.object(audit_tool.time, "sleep") as sleep:
            payload = audit_tool.WikidataClient(session)._post(
                {"action": "wbeditentity"},
                "atomic edit",
            )

        self.assertEqual(payload, {"success": 1})
        self.assertEqual(session.posts, 2)
        sleep.assert_called_once_with(
            audit_tool.WIKIDATA_MAXLAG_FALLBACK_SECONDS
        )

    def test_later_qid_failure_leaves_truthful_partial_checkpoint(self):
        raw_operations = [{"action": "remove", "property": "P31", "value": "Q2"}]
        intent = {
            "schemaVersion": 1,
            "taskId": "24",
            "intents": {},
        }
        records = []
        for qid, revision in (("Q1", 10), ("Q2", 20)):
            single = item_intent(qid, raw_operations)["intents"][qid]
            intent["intents"][qid] = single
            records.append(audit_record(qid, revision, raw_operations))
        fixture_audit = {
            "records": records,
            "liveEditsPerformed": False,
            "mode": "review-first",
            "auditTimestamp": "2026-08-14T00:00:00Z",
        }
        checkpoints = []

        class FailingSecondClient:
            def get_entity(self, qid):
                record = next(record for record in records if record["qid"] == qid)
                return entity_from_claims(qid, record["oldRevisionId"], record["beforeClaims"])

            def edit_entity(self, qid, operations, base_revision, summary, evidence_url):
                if qid == "Q2":
                    raise audit_tool.AuditError("simulated second-item failure")
                return entity_from_claims(qid, 11, [])

        with tempfile.TemporaryDirectory() as directory:
            intent_path = Path(directory) / "intent.json"
            audit_tool.write_json_atomic(intent_path, intent)
            with mock.patch.object(audit_tool, "INTENT_PATH", intent_path):
                with self.assertRaises(audit_tool.AuditError):
                    audit_tool.execute_approved_records(
                        fixture_audit,
                        FailingSecondClient(),
                        lambda payload: checkpoints.append(copy.deepcopy(payload)),
                    )

        self.assertEqual(records[0]["status"], "executed")
        self.assertEqual(records[1]["status"], "approved")
        self.assertEqual(fixture_audit["mode"], "partially-executed")
        self.assertTrue(fixture_audit["liveEditsPerformed"])
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["mode"], "partially-executed")
        self.assertTrue(checkpoints[0]["liveEditsPerformed"])

    def test_capture_requires_archival_before_replacing_execution_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_path = root / "task-24-classification-audit.json"
            snapshot_path = root / "task-24-source-snapshot.json"
            intent_path = root / "task-24-edit-intent.json"
            protected = {
                "mode": "partially-executed",
                "liveEditsPerformed": True,
                "records": [{"status": "executed"}],
            }
            audit_tool.write_json_atomic(audit_path, protected)
            snapshot_path.write_text("snapshot evidence\n", encoding="utf-8")
            intent_path.write_text("intent evidence\n", encoding="utf-8")
            original = audit_path.read_bytes()

            with (
                mock.patch.object(audit_tool, "AUDIT_PATH", audit_path),
                mock.patch.object(audit_tool, "SNAPSHOT_PATH", snapshot_path),
                mock.patch.object(audit_tool, "INTENT_PATH", intent_path),
            ):
                with self.assertRaises(audit_tool.AuditError):
                    audit_tool.protect_capture_evidence(False)
                self.assertEqual(audit_path.read_bytes(), original)
                archive = audit_tool.protect_capture_evidence(True)

            self.assertIsNotNone(archive)
            self.assertEqual((archive / audit_path.name).read_bytes(), original)
            self.assertEqual(
                (archive / snapshot_path.name).read_text(encoding="utf-8"),
                "snapshot evidence\n",
            )

    def test_credentials_fall_back_to_non_echoing_prompt(self):
        with (
            mock.patch.dict(os.environ, {"WIKI_USER": "", "WIKI_PASS": ""}),
            mock.patch("builtins.input", return_value="BotUser") as username_prompt,
            mock.patch.object(audit_tool.getpass, "getpass", return_value="secret") as password_prompt,
        ):
            self.assertEqual(audit_tool.wikidata_credentials(), ("BotUser", "secret"))
        username_prompt.assert_called_once()
        password_prompt.assert_called_once()


class GeneratedArtifactTests(unittest.TestCase):
    def test_confirmed_children_are_absent_and_parent_vocabularies_remain(self):
        payload = json.loads((ROOT / "data" / "ontologies.json").read_text())
        qids = {
            item["wikidataId"].rsplit("/", 1)[-1]
            for item in payload["items"]
        }
        self.assertFalse(set(CONFIRMED_EXCLUSIONS) & qids)
        self.assertTrue({"Q124134540", "Q26936735"} <= qids)
        self.assertFalse(set(REVIEWED_LOCAL_EXCLUSIONS) & qids)
        self.assertTrue({"Q2626877", "Q3255006", "Q5571793", "Q839752"} <= qids)

        graph_text = (ROOT / "data" / "ontologies.ttl").read_text()
        page_qids = json.loads((ROOT / "data" / "page_qids.json").read_text())["resource"]
        sitemap = (ROOT / "site" / "sitemap.xml").read_text()
        for qid, slug in CONFIRMED_EXCLUSIONS.items():
            self.assertNotIn(qid, graph_text)
            self.assertNotIn(qid, page_qids)
            self.assertNotIn(f"/resource/{slug}/", sitemap)
            self.assertFalse((ROOT / "site" / "resource" / slug / "index.html").exists())
        for qid, slug in REVIEWED_LOCAL_EXCLUSIONS.items():
            self.assertNotIn(qid, graph_text)
            self.assertNotIn(qid, page_qids)
            self.assertNotIn(f"/resource/{slug}/", sitemap)
            self.assertFalse((ROOT / "site" / "resource" / slug / "index.html").exists())

    def test_uri_reservations_and_curation_history_are_preserved(self):
        registry = json.loads((ROOT / "data" / "uri_registry.json").read_text())["resource"]
        self.assertEqual(
            {qid: registry[qid] for qid in CONFIRMED_EXCLUSIONS},
            CONFIRMED_EXCLUSIONS,
        )
        self.assertEqual(
            {qid: registry[qid] for qid in REVIEWED_LOCAL_EXCLUSIONS},
            REVIEWED_LOCAL_EXCLUSIONS,
        )
        curation = Graph().parse(ROOT / "curation" / "classifications.ttl", format="turtle")
        curated_qids = {
            str(value).rsplit("/", 1)[-1]
            for value in curation.objects(None, semantic_config.OKG.wikidataId)
        }
        self.assertTrue(set(ALL_SOURCE_EXCLUSIONS) <= curated_qids)


if __name__ == "__main__":
    unittest.main()
