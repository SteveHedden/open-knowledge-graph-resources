import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import semantic_config  # noqa: E402
import validate_catalog  # noqa: E402


OKG = semantic_config.OKG


def catalog_item(dataset, qid, slug=None):
    path = "resource" if dataset == "resource" else "software"
    return {
        "title": qid,
        "wikidataId": f"https://www.wikidata.org/wiki/{qid}",
        "types": ["Ontology" if dataset == "resource" else "Software"],
        "canonicalUrl": f"https://openknowledgegraphs.com/{path}/{slug or qid.lower()}/",
    }


def payload(dataset, qids):
    return {
        "generatedAt": "2026-08-13T00:00:00Z",
        "items": [catalog_item(dataset, qid) for qid in qids],
    }


class CatalogValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graphs = {
            relative: validate_catalog.parse_graph(ROOT / relative)
            for relative in validate_catalog.GRAPH_PATHS
        }
        cls.payloads = {
            "resource": validate_catalog.read_json(ROOT / "data/ontologies.json"),
            "software": validate_catalog.read_json(ROOT / "data/software.json"),
        }
        cls.mappings = semantic_config.load_source_mappings(ROOT / "sources.ttl")

    def test_current_catalog_passes_the_shared_validator(self):
        report = validate_catalog.validate_catalog(ROOT, baseline_ref="HEAD")
        self.assertTrue(report.conforms, validate_catalog.render_report(report))
        self.assertTrue(
            all(issue.code == "external-link" for issue in report.warnings),
            report.warnings,
        )

    def test_pyshacl_rejects_a_resource_without_required_identity(self):
        graphs = dict(self.graphs)
        invalid = Graph()
        invalid += graphs["data/ontologies.ttl"]
        invalid.add((URIRef("https://example.org/invalid"), RDF.type, OKG.Ontology))
        graphs["data/ontologies.ttl"] = invalid
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_shacl(graphs, report)

        self.assertTrue(report.has_error("shacl"))

    def test_undeclared_schema_terms_are_hard_failures(self):
        invalid = Graph()
        invalid.add(
            (
                URIRef("https://example.org/resource"),
                OKG.undeclaredPredicate,
                Literal("value"),
            )
        )
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_declared_schema_terms(
            self.graphs["ontology.ttl"],
            [invalid],
            report,
        )

        self.assertTrue(report.has_error("schema-term"))

    def test_public_bare_qids_fail_but_source_mapping_literals_are_allowed(self):
        graph = Graph()
        graph.add((URIRef("https://example.org/a"), OKG.relatedTo, URIRef("Q123")))
        graph.add(
            (
                URIRef("https://example.org/mapping"),
                OKG.sourceClassId,
                Literal("Q123"),
            )
        )
        payloads = {
            "resource": {
                "generatedAt": "2026-08-13T00:00:00Z",
                "items": [
                    {
                        "wikidataId": "https://www.wikidata.org/wiki/Q123",
                        "canonicalUrl": "Q123",
                    }
                ],
            }
        }
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_public_iris([graph], payloads, report)

        self.assertTrue(report.has_error("bare-id"))
        self.assertFalse(
            any("sourceClassId" in issue.message for issue in report.errors),
            report.errors,
        )

    def test_invalid_controlled_value_is_rejected(self):
        graphs = dict(self.graphs)
        invalid = Graph()
        invalid += graphs["data/ontologies.ttl"]
        subject = next(invalid.subjects(OKG.category, None))
        invalid.remove((subject, OKG.category, None))
        invalid.add((subject, OKG.category, URIRef("https://example.org/not-a-category")))
        graphs["data/ontologies.ttl"] = invalid
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_vocabularies_and_curation(
            ROOT,
            graphs,
            self.payloads,
            report,
        )

        self.assertTrue(report.has_error("controlled-value"))

    def test_rdf_json_identity_and_count_mismatch_is_rejected(self):
        payloads = copy.deepcopy(self.payloads)
        payloads["resource"]["items"].pop()
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_json_contract_and_projection(
            self.graphs,
            payloads,
            self.mappings,
            report,
        )

        self.assertTrue(report.has_error("rdf-json-parity"))

    def test_broken_json_output_contract_is_rejected(self):
        payloads = copy.deepcopy(self.payloads)
        payloads["resource"]["unexpected"] = True
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_json_contract_and_projection(
            self.graphs,
            payloads,
            self.mappings,
            report,
        )

        self.assertTrue(report.has_error("json-contract"))

    def test_registry_removal_uri_change_and_slug_collision_are_rejected(self):
        baseline = {
            "resource": {"Q1": "one", "Q2": "two"},
            "software": {},
        }
        current = {
            "resource": {"Q1": "changed", "Q3": "changed"},
            "software": {},
        }
        payloads = {
            "resource": payload("resource", []),
            "software": payload("software", []),
        }
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_registry(current, baseline, payloads, report)

        self.assertTrue(report.has_error("registry-reservation"))
        self.assertTrue(report.has_error("uri-stability"))
        self.assertTrue(report.has_error("slug-collision"))

    def test_record_loss_warns_above_two_percent_without_failing(self):
        baseline_qids = [f"Q{number}" for number in range(1, 101)]
        current_qids = baseline_qids[:95]
        baseline = validate_catalog.BaselineSnapshot(
            payloads={
                "resource": payload("resource", baseline_qids),
                "software": payload("software", ["Q1001"]),
            },
            registry={},
            page_qids={},
        )
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_regressions(
            {
                "resource": payload("resource", current_qids),
                "software": payload("software", ["Q1001"]),
            },
            baseline,
            report,
        )

        self.assertFalse(report.has_error("record-loss"))
        self.assertTrue(any(issue.code == "record-loss" for issue in report.warnings))

    def test_additions_cannot_mask_threshold_breaking_loss(self):
        baseline_qids = [f"Q{number}" for number in range(1, 101)]
        survivors = baseline_qids[:89]
        replacements = [f"Q{number}" for number in range(1001, 1012)]
        baseline = validate_catalog.BaselineSnapshot(
            payloads={
                "resource": payload("resource", baseline_qids),
                "software": payload("software", ["Q2001"]),
            },
            registry={},
            page_qids={},
        )
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_regressions(
            {
                "resource": payload("resource", survivors + replacements),
                "software": payload("software", ["Q2001"]),
            },
            baseline,
            report,
        )

        self.assertTrue(report.has_error("record-loss"))

    def test_surviving_qid_uri_change_is_rejected(self):
        baseline = validate_catalog.BaselineSnapshot(
            payloads={
                "resource": payload("resource", ["Q1"]),
                "software": payload("software", ["Q2"]),
            },
            registry={},
            page_qids={},
        )
        changed = payload("resource", ["Q1"])
        changed["items"][0]["canonicalUrl"] = (
            "https://openknowledgegraphs.com/resource/a-different-uri/"
        )
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_regressions(
            {"resource": changed, "software": payload("software", ["Q2"])},
            baseline,
            report,
        )

        self.assertTrue(report.has_error("uri-stability"))

    def test_mapping_coverage_is_bidirectional_and_rejects_hardcoded_ids(self):
        source = (ROOT / "scripts/fetch_data.py").read_text() + "\n" + (
            ROOT / "scripts/category_classifier.py"
        ).read_text()
        report = validate_catalog.ValidationReport()
        validate_catalog.validate_mapping_coverage_source(self.mappings, source, report)
        self.assertTrue(report.conforms, validate_catalog.render_report(report))

        invalid_source = source.replace('"officialWebsite"', '"undeclaredField"')
        invalid_source += '\nHARDCODED_MAPPING = "wd:Q999 and wdt:P999"\n'
        invalid_report = validate_catalog.ValidationReport()
        validate_catalog.validate_mapping_coverage_source(
            self.mappings,
            invalid_source,
            invalid_report,
        )
        self.assertTrue(invalid_report.has_error("mapping-coverage"))
        self.assertTrue(invalid_report.has_error("mapping-drift"))

    def test_all_three_workflow_paths_run_validation_before_promotion(self):
        pull_request = (ROOT / ".github/workflows/validate.yml").read_text()
        refresh = (ROOT / ".github/workflows/update-data.yml").read_text()
        deployment = (ROOT / ".github/workflows/deploy.yml").read_text()

        self.assertIn("pull_request:", pull_request)
        self.assertIn("scripts/validate_catalog.py", pull_request)
        self.assertLess(
            refresh.index("scripts/validate_catalog.py"),
            refresh.index("Promote complete generation into checkout"),
        )
        self.assertIn("Validate and verify exact rollback target", deployment)
        self.assertIn("scripts/validate_catalog.py", deployment)
        self.assertIn('catalog_snapshot.py" verify', deployment)
        self.assertIn("fetch-depth: 0", deployment)

    def test_broken_page_and_sitemap_contract_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "site/resource").mkdir(parents=True)
            (root / "site/software").mkdir(parents=True)
            (root / "data/page_qids.json").write_text(
                json.dumps({"resource": {"Q1": "q1"}, "software": {}}),
                encoding="utf-8",
            )
            (root / "site/sitemap.xml").write_text(
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>',
                encoding="utf-8",
            )
            report = validate_catalog.ValidationReport()
            validate_catalog.validate_page_contracts(
                root,
                {
                    "resource": payload("resource", ["Q1"]),
                    "software": payload("software", []),
                },
                None,
                report,
            )

        self.assertTrue(report.has_error("page-contract"))

    def test_known_record_fixture_regression_is_rejected(self):
        fixtures = validate_catalog.read_json(ROOT / "validation/known_records.json")
        self.assertNotIn("auditory", json.dumps(fixtures).casefold())
        payloads = copy.deepcopy(self.payloads)
        qid = "Q7276224"
        for item in payloads["software"]["items"]:
            if item["wikidataId"].endswith(qid):
                item["softwareType"] = "Graph Database"
                break
        report = validate_catalog.ValidationReport()

        validate_catalog.validate_known_records_data(fixtures, payloads, report)

        self.assertTrue(report.has_error("known-record"))

    def test_soft_warning_summary_is_ephemeral_and_does_not_fail(self):
        report = validate_catalog.ValidationReport()
        report.warning("record-loss", "resource loss is 5%")
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "github-summary.md"
            validate_catalog.append_github_summary(summary, report)
            text = summary.read_text()

        self.assertTrue(report.conforms)
        self.assertIn("record-loss", text)
        self.assertIn("resource loss is 5%", text)


if __name__ == "__main__":
    unittest.main()
