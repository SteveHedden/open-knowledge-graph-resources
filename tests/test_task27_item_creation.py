import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import create_intentional_arrangement_skos_item as task27  # noqa: E402


AUDIT_PATH = ROOT / "audits" / "wikidata" / "task-27-item-creation.json"


class Task27AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit = task27.load_audit(AUDIT_PATH)
        cls.payload = task27.compile_entity_payload(cls.audit)

    def test_reproducible_duplicate_record_covers_both_names_and_urls(self):
        checks = self.audit["duplicateGate"]["checks"]
        self.assertEqual(
            [check["kind"] for check in checks],
            ["exact-name", "short-name", "official-website", "source-repository"],
        )
        self.assertTrue(all(check["resultCount"] == 0 for check in checks))
        self.assertTrue(all(check["searchUrl"].startswith("https://www.wikidata.org/") for check in checks))

    def test_comparable_notability_and_version_decisions_are_recorded(self):
        gate = self.audit["notabilityGate"]
        self.assertEqual(gate["decision"], "passed-comparable-evidence")
        self.assertFalse(gate["directIndependentProductCoverageFound"])
        self.assertGreaterEqual(len(gate["evidence"]), 3)
        version = self.audit["versionGate"]
        self.assertEqual(version["decision"], "passed-use-v0.3.0")
        self.assertIn("Pre-1.0", version["rationale"])

    def test_payload_has_only_the_approved_claims_and_no_developer(self):
        claims = self.audit["proposedEntity"]["statements"]
        self.assertEqual(
            [(claim["property"], claim["value"]) for claim in claims],
            [
                ("P31", "Q124653107"),
                ("P4428", "Q2288360"),
                ("P856", "https://jesstalisman-ia.github.io/intentional-arrangement-skos/"),
                ("P1324", "https://github.com/jesstalisman-ia/intentional-arrangement-skos"),
                ("P275", "Q36795408"),
                ("P277", "Q2005"),
                ("P277", "Q28865"),
                ("P571", "+2026-00-00T00:00:00Z"),
                ("P348", "0.3.0"),
            ],
        )
        self.assertNotIn("P178", {claim["property"] for claim in claims})

    def test_version_rank_qualifiers_and_reference_are_exact(self):
        version = next(
            claim
            for claim in self.audit["proposedEntity"]["statements"]
            if claim["property"] == "P348"
        )
        self.assertEqual(version["rank"], "preferred")
        self.assertEqual(
            [(qualifier["property"], qualifier["value"]) for qualifier in version["qualifiers"]],
            [
                ("P577", "+2026-08-15T00:00:00Z"),
                ("P548", "Q2804309"),
            ],
        )
        reference = self.audit["references"][version["reference"]]["snaks"]
        self.assertEqual(
            next(snak["value"] for snak in reference if snak["property"] == "P1476"),
            "v0.3.0 — Glossary, SKOS-XL build mode, per-section colors",
        )

    def test_all_claims_have_property_specific_references_and_execution_date(self):
        statements = self.audit["proposedEntity"]["statements"]
        for statement in statements:
            reference = self.audit["references"][statement["reference"]]["snaks"]
            self.assertTrue(any(snak["property"] == "P854" for snak in reference))
            self.assertEqual(
                [snak["value"] for snak in reference if snak["property"] == "P813"],
                ["+2026-08-16T00:00:00Z"],
            )

    def test_payload_hash_is_deterministic(self):
        first = task27.payload_sha256(self.payload)
        second = task27.payload_sha256(task27.compile_entity_payload(self.audit))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_live_execution_is_blocked_until_separate_approval(self):
        pending = copy.deepcopy(self.audit)
        pending["approval"]["status"] = "awaiting-user"
        with mock.patch.object(task27, "configured_session") as session:
            with self.assertRaisesRegex(task27.Task27Error, "separate user approval"):
                task27.execute(pending, self.payload, "not-approved")
        session.assert_not_called()

    def test_payload_compiles_to_wikibase_time_and_reference_shapes(self):
        inception = next(
            claim for claim in self.payload["claims"]
            if claim["mainsnak"]["property"] == "P571"
        )
        self.assertEqual(inception["mainsnak"]["datavalue"]["value"]["precision"], 9)
        version = next(
            claim for claim in self.payload["claims"]
            if claim["mainsnak"]["property"] == "P348"
        )
        self.assertEqual(version["rank"], "preferred")
        self.assertEqual(version["qualifiers-order"], ["P577", "P548"])
        self.assertEqual(version["references"][0]["snaks-order"], ["P854", "P1476", "P813"])

    def test_validation_rejects_an_inferred_developer(self):
        changed = copy.deepcopy(self.audit)
        changed["proposedEntity"]["statements"].append(
            {
                "property": "P178",
                "datatype": "wikibase-item",
                "value": "Q1",
                "rank": "normal",
                "qualifiers": [],
                "reference": "readme",
            }
        )
        with self.assertRaises(task27.Task27Error):
            task27.validate_audit(changed)


if __name__ == "__main__":
    unittest.main()
