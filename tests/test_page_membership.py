import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

import sys

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "jobs" / "scripts"))

import generate_pages  # noqa: E402
import validate_catalog  # noqa: E402
from catalog_mentions import add_catalog_mentions, load_match_index  # noqa: E402


def item(qid, slug, homepage, *, description="A complete catalog resource for testing."):
    return {
        "canonicalUrl": f"https://openknowledgegraphs.com/resource/{slug}/",
        "title": slug.replace("-", " ").title(),
        "description": description,
        "homepage": homepage,
        "wikidataId": f"https://www.wikidata.org/wiki/{qid}",
        "types": ["ControlledVocabulary"],
    }


def software_item(qid, slug, title, homepage, *, description=None):
    return {
        "canonicalUrl": f"https://openknowledgegraphs.com/software/{slug}/",
        "title": title,
        "description": description or f"{title} is graph software with a complete test description.",
        "homepage": homepage,
        "wikidataId": f"https://www.wikidata.org/wiki/{qid}",
        "types": ["Software"],
        "softwareType": "Graph Database",
        "aliases": [],
    }


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class PageMembershipBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.candidate = self.root / "candidate"
        self.baseline = self.root / "baseline"
        (self.candidate / "data").mkdir(parents=True)
        (self.candidate / "site").mkdir()
        self.coverage_policy = self.candidate / "coverage-policy.json"
        write_json(
            self.coverage_policy,
            {
                "maximumEmptyShare": 1.0,
                "maximumUnreviewedCoverageDecline": 1.0,
                "acceptedDeclines": [],
                "acceptedShortfalls": [],
            },
        )

    def tearDown(self):
        self.temporary.cleanup()

    def write_candidate(self, resources):
        write_json(self.candidate / "data/ontologies.json", {"items": resources})
        write_json(self.candidate / "data/software.json", {"items": []})

    def write_baseline(self, resources, registry):
        write_json(self.baseline / "data/ontologies.json", {"items": resources})
        write_json(self.baseline / "data/software.json", {"items": []})
        write_json(
            self.baseline / "data/page_qids.json",
            {"resource": registry, "software": {}},
        )

    def run_generator(self, *arguments, good_urls=()):
        async def fake_check_links(_items):
            return set(good_urls)

        with (
            mock.patch.object(generate_pages, "DATA_DIR", str(self.candidate / "data")),
            mock.patch.object(generate_pages, "SITE_DIR", str(self.candidate / "site")),
            mock.patch.object(generate_pages, "COVERAGE_POLICY_PATH", self.coverage_policy),
            mock.patch.object(
                generate_pages,
                "RELATED_DIAGNOSTICS_PATH",
                self.candidate / "related-resources.json",
            ),
            mock.patch.object(generate_pages, "check_links", side_effect=fake_check_links) as checker,
        ):
            generate_pages.main(list(arguments))
        return checker

    def page_registry(self):
        return json.loads(
            (self.candidate / "data/page_qids.json").read_text(encoding="utf-8")
        )

    def test_unchanged_verified_page_survives_failed_checks_for_new_candidates(self):
        stable = item("Q1", "stable", "https://example.test/stable")
        new = item("Q2", "new", "https://example.test/new")
        self.write_baseline([stable], {"Q1": "stable"})
        self.write_candidate([stable, new])

        checker = self.run_generator(
            "--membership-baseline",
            str(self.baseline),
            good_urls=(),
        )

        self.assertEqual(self.page_registry()["resource"], {"Q1": "stable"})
        self.assertTrue((self.candidate / "site/resource/stable/index.html").is_file())
        checked_items = checker.call_args.args[0]
        self.assertEqual([entry["wikidataId"] for entry in checked_items], [new["wikidataId"]])

    def test_removed_or_ineligible_baseline_records_disappear(self):
        stable = item("Q1", "stable", "https://example.test/stable")
        removed = item("Q2", "removed", "https://example.test/removed")
        ineligible = item(
            "Q3",
            "ineligible",
            "https://example.test/ineligible",
            description="ontology",
        )
        self.write_baseline(
            [stable, removed, {**ineligible, "description": "Previously eligible description."}],
            {"Q1": "stable", "Q2": "removed", "Q3": "ineligible"},
        )
        self.write_candidate([stable, ineligible])
        stale_page = self.candidate / "site/resource/removed/index.html"
        stale_page.parent.mkdir(parents=True)
        stale_page.write_text("stale", encoding="utf-8")

        self.run_generator(
            "--membership-baseline",
            str(self.baseline),
            "--skip-link-check",
        )

        self.assertEqual(self.page_registry()["resource"], {"Q1": "stable"})
        self.assertFalse(stale_page.exists())
        self.assertFalse((self.candidate / "site/resource/ineligible/index.html").exists())

    def test_new_and_homepage_changed_candidates_require_successful_verification(self):
        former = item("Q1", "changed", "https://example.test/old")
        changed = item("Q1", "changed", "https://example.test/new-homepage")
        new = item("Q2", "new", "https://example.test/new")
        self.write_baseline([former], {"Q1": "changed"})
        self.write_candidate([changed, new])

        self.run_generator(
            "--membership-baseline",
            str(self.baseline),
            good_urls={new["homepage"]},
        )

        self.assertEqual(self.page_registry()["resource"], {"Q2": "new"})

    def test_baseline_skip_mode_admits_only_unchanged_verified_pages(self):
        stable = item("Q1", "stable", "https://example.test/stable")
        former = item("Q2", "changed", "https://example.test/old")
        changed = item("Q2", "changed", "https://example.test/changed")
        new = item("Q3", "new", "https://example.test/new")
        self.write_baseline([stable, former], {"Q1": "stable", "Q2": "changed"})
        self.write_candidate([stable, changed, new])

        checker = self.run_generator(
            "--membership-baseline",
            str(self.baseline),
            "--skip-link-check",
        )

        self.assertEqual(self.page_registry()["resource"], {"Q1": "stable"})
        checker.assert_not_called()

    def test_legacy_skip_mode_without_baseline_still_admits_all_candidates(self):
        first = item("Q1", "first", "https://example.test/first")
        second = item("Q2", "second", "https://example.test/second")
        self.write_candidate([first, second])

        checker = self.run_generator("--skip-link-check")

        self.assertEqual(
            self.page_registry()["resource"],
            {"Q1": "first", "Q2": "second"},
        )
        checker.assert_not_called()

    def test_task46_page_admission_gates_enable_linked_mentions(self):
        anzo = software_item(
            "Q124653370", "anzograph", "AnzoGraph",
            "https://docs.cambridgesemantics.com/anzograph/",
        )
        neptune = software_item(
            "Q48843359", "amazon-neptune", "Amazon Neptune",
            "https://aws.amazon.com/neptune/",
        )
        write_json(self.candidate / "data/ontologies.json", {"items": []})
        write_json(
            self.candidate / "data/software.json",
            {"items": [anzo, neptune]},
        )
        policy_path = self.candidate / "catalog-mention-policy.json"
        write_json(policy_path, {
            "schemaVersion": 1,
            "shortAcronymAllowlist": [],
            "denylist": [],
            "reviewedAliases": {},
            "disambiguationOverrides": {},
            "pageGatedAliases": {
                "AWS Neptune": {"dataset": "software", "qid": "Q48843359"},
                "Neptune": {"dataset": "software", "qid": "Q48843359"},
            },
            "contextRequiredAliases": ["Neptune"],
            "employerGuardAliases": [],
            "exactCaseVariants": {
                "AWS Neptune": ["AWS Neptune"],
                "Neptune": ["Neptune"],
            },
        })

        rejected_checker = self.run_generator(good_urls=set())
        self.assertEqual(self.page_registry()["software"], {})
        self.assertFalse((self.candidate / "site/software/anzograph/index.html").exists())
        self.assertFalse((self.candidate / "site/software/amazon-neptune/index.html").exists())
        rejected_index = load_match_index(self.candidate, policy_path)
        record = {
            "id": "generated-pages", "title": "Engineer",
            "description": "Use AnzoGraph and Amazon Neptune as graph database tools.",
        }
        self.assertEqual(
            add_catalog_mentions([record], rejected_index)[0]["catalogMentions"],
            [],
        )
        rejected_qids = {
            entry["wikidataId"].rsplit("/", 1)[-1]
            for entry in rejected_checker.call_args.args[0]
        }
        self.assertEqual(rejected_qids, {"Q124653370", "Q48843359"})

        checker = self.run_generator(
            good_urls={anzo["homepage"], neptune["homepage"]},
        )
        self.assertEqual(
            self.page_registry()["software"],
            {"Q124653370": "anzograph", "Q48843359": "amazon-neptune"},
        )
        checked_qids = {
            entry["wikidataId"].rsplit("/", 1)[-1]
            for entry in checker.call_args.args[0]
        }
        self.assertEqual(checked_qids, {"Q124653370", "Q48843359"})
        self.assertTrue((self.candidate / "site/software/anzograph/index.html").is_file())
        self.assertTrue((self.candidate / "site/software/amazon-neptune/index.html").is_file())

        report = validate_catalog.ValidationReport()
        validate_catalog.validate_page_contracts(
            self.candidate,
            {"resource": {"items": []}, "software": {"items": [anzo, neptune]}},
            None,
            report,
        )
        self.assertTrue(report.conforms, validate_catalog.render_report(report))

        match_index = load_match_index(self.candidate, policy_path)
        enriched = add_catalog_mentions([record], match_index)[0]
        self.assertEqual(
            [mention["qid"] for mention in enriched["catalogMentions"]],
            ["Q124653370", "Q48843359"],
        )


if __name__ == "__main__":
    unittest.main()
