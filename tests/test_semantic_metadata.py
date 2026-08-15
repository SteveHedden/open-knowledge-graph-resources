import json
import sys
import unittest
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS, SH, XSD


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_data  # noqa: E402
import generate_pages  # noqa: E402


PUBLIC_DOMAIN_MARK = "https://creativecommons.org/publicdomain/mark/1.0/"
OKG = fetch_data.OKG


def page_fixture(**overrides):
    item = {
        "canonicalUrl": "https://openknowledgegraphs.com/software/example/",
        "title": "Example",
        "description": "An example semantic software resource.",
        "homepage": "https://example.org/",
        "wikidataId": "https://www.wikidata.org/wiki/Q123",
    }
    item.update(overrides)
    return item


class JsonLdTests(unittest.TestCase):
    def parse_json_ld(self, item, dataset="software"):
        return json.loads(generate_pages.make_json_ld(item, dataset))

    def assert_no_empty_strings(self, value):
        if isinstance(value, dict):
            for nested in value.values():
                self.assert_no_empty_strings(nested)
        elif isinstance(value, list):
            for nested in value:
                self.assert_no_empty_strings(nested)
        elif isinstance(value, str):
            self.assertTrue(value.strip())

    def test_known_license_is_serialized(self):
        result = self.parse_json_ld(page_fixture(licenses=["Apache Software License 2.0"]))
        self.assertEqual(result["license"], "Apache Software License 2.0")

    def test_unknown_license_and_blank_optional_values_are_omitted(self):
        result = self.parse_json_ld(
            page_fixture(
                licenses=[],
                latestVersion="",
                releaseDate="   ",
                sourceRepo="",
                softwareType=None,
                programmingLanguages=["", "   "],
                creators=[{"type": "Person", "name": ""}],
            )
        )

        for key in (
            "license",
            "softwareVersion",
            "datePublished",
            "codeRepository",
            "applicationCategory",
            "programmingLanguage",
            "creator",
        ):
            self.assertNotIn(key, result)
        self.assertNotIn(PUBLIC_DOMAIN_MARK, json.dumps(result))
        self.assert_no_empty_strings(result)

    def test_creator_blank_profile_values_are_omitted(self):
        result = self.parse_json_ld(
            page_fixture(
                creators=[
                    {
                        "type": "Person",
                        "name": "Example Creator",
                        "wikidataId": "https://www.wikidata.org/wiki/Q456",
                        "githubProfile": "",
                        "googleScholarProfile": "   ",
                    }
                ]
            )
        )

        self.assertEqual(result["creator"]["sameAs"], "https://www.wikidata.org/wiki/Q456")
        self.assert_no_empty_strings(result)

    def test_required_identity_fields_cannot_be_empty(self):
        for field in ("canonicalUrl", "title", "description", "homepage", "wikidataId"):
            with self.subTest(field=field):
                item = page_fixture()
                item[field] = ""
                with self.assertRaisesRegex(ValueError, field):
                    generate_pages.make_json_ld(item, "software")

    def test_page_eligibility_rejects_blank_required_content(self):
        self.assertFalse(generate_pages.passes_content_filter(page_fixture(title="   ")))
        self.assertFalse(generate_pages.passes_content_filter(page_fixture(description="   ")))
        self.assertFalse(generate_pages.passes_content_filter(page_fixture(homepage="   ")))


class RdfEmissionTests(unittest.TestCase):
    def build_graph(self, record, **kwargs):
        qid = fetch_data.qid_from_wikidata_iri(record.item_iri)
        return fetch_data.build_graph(
            records={record.item_iri: record},
            license_labels=kwargs.get("license_labels", {}),
            creator_labels={},
            human_creators=set(),
            person_identifiers={},
            include_software_fields=False,
            dataset_path="resource",
            slug_registry={qid: "example"},
        )

    def test_unknown_optional_values_emit_no_triples(self):
        record = fetch_data.ResourceRecord(
            item_iri="http://www.wikidata.org/entity/Q123",
            label="Example",
            types={OKG.Ontology},
        )
        graph = self.build_graph(record)
        subject = URIRef("https://openknowledgegraphs.com/resource/example/")

        for predicate in (
            OKG.description,
            OKG.category,
            OKG.homepage,
            OKG.sourceRepo,
            OKG.namespaceURI,
            OKG.partOf,
            OKG.creator,
            OKG.hasLicense,
        ):
            with self.subTest(predicate=predicate):
                self.assertEqual(list(graph.objects(subject, predicate)), [])

    def test_known_license_emits_license_triples(self):
        license_iri = "http://www.wikidata.org/entity/Q13785927"
        record = fetch_data.ResourceRecord(
            item_iri="http://www.wikidata.org/entity/Q123",
            label="Example",
            types={OKG.Ontology},
            licenses={license_iri},
        )
        graph = self.build_graph(
            record,
            license_labels={license_iri: "Apache Software License 2.0"},
        )
        subject = URIRef("https://openknowledgegraphs.com/resource/example/")
        license_nodes = list(graph.objects(subject, OKG.hasLicense))

        self.assertEqual(len(license_nodes), 1)
        self.assertIn((license_nodes[0], OKG.licenseName, Literal("Apache Software License 2.0")), graph)

    def test_empty_required_label_is_rejected(self):
        record = fetch_data.ResourceRecord(
            item_iri="http://www.wikidata.org/entity/Q123",
            label="",
            types={OKG.Ontology},
        )
        with self.assertRaisesRegex(ValueError, "without a label"):
            self.build_graph(record)


class OntologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ontology = Graph().parse(ROOT / "ontology.ttl", format="turtle")

    def test_all_published_rdf_parses(self):
        for relative_path in ("ontology.ttl", "data/ontologies.ttl", "data/software.ttl"):
            with self.subTest(path=relative_path):
                Graph().parse(ROOT / relative_path, format="turtle")

    def test_ontology_language_is_declared(self):
        self.assertIn((OKG.OntologyLanguage, RDF.type, RDFS.Class), self.ontology)
        self.assertIn((OKG.OntologyLanguage, RDFS.subClassOf, OKG.Resource), self.ontology)

    def test_emitted_properties_are_explicitly_declared(self):
        expected_domains = {
            OKG.creator: OKG.Resource,
            OKG.githubProfile: OKG.Person,
            OKG.googleScholarProfile: OKG.Person,
            OKG.namespaceURI: OKG.Resource,
            OKG.programmingLanguage: OKG.Software,
            OKG.relatedTo: OKG.Resource,
            OKG.sourceRepo: OKG.Resource,
        }
        expected_ranges = {
            OKG.programmingLanguage: XSD.string,
            OKG.relatedTo: OKG.Resource,
        }
        iri_shape_paths = {
            OKG.creator,
            OKG.githubProfile,
            OKG.googleScholarProfile,
            OKG.namespaceURI,
            OKG.sourceRepo,
            DCTERMS.isPartOf,
        }

        for term, domain in expected_domains.items():
            with self.subTest(term=term):
                self.assertIn((term, RDF.type, RDF.Property), self.ontology)
                self.assertIn((term, RDFS.domain, domain), self.ontology)
                self.assertTrue(any(self.ontology.objects(term, RDFS.label)))
                self.assertTrue(any(self.ontology.objects(term, RDFS.comment)))

        for term, range_iri in expected_ranges.items():
            with self.subTest(range=term):
                self.assertIn((term, RDFS.range, range_iri), self.ontology)

        shaped_iri_paths = {
            path
            for property_shape in self.ontology.subjects(SH.nodeKind, SH.IRI)
            for path in self.ontology.objects(property_shape, SH.path)
        }
        self.assertTrue(iri_shape_paths.issubset(shaped_iri_paths))

    def test_every_emitted_okg_class_and_predicate_is_declared(self):
        data = Graph()
        data.parse(ROOT / "data/ontologies.ttl", format="turtle")
        data.parse(ROOT / "data/software.ttl", format="turtle")

        okg_prefix = str(OKG)
        used_predicates = {
            predicate
            for _, predicate, _ in data
            if str(predicate).startswith(okg_prefix)
        }
        used_classes = {
            value
            for _, predicate, value in data
            if predicate == RDF.type and str(value).startswith(okg_prefix)
        }

        undeclared_predicates = {
            predicate
            for predicate in used_predicates
            if (predicate, RDF.type, RDF.Property) not in self.ontology
        }
        undeclared_classes = {
            class_iri
            for class_iri in used_classes
            if (class_iri, RDF.type, RDFS.Class) not in self.ontology
        }

        self.assertEqual(undeclared_predicates, set())
        self.assertEqual(undeclared_classes, set())


class CommittedPageTests(unittest.TestCase):
    @staticmethod
    def committed_pages():
        return sorted(
            [
                *ROOT.glob("site/resource/*/index.html"),
                *ROOT.glob("site/software/*/index.html"),
            ]
        )

    def test_committed_pages_do_not_publish_fabricated_license(self):
        pages = self.committed_pages()
        self.assertTrue(pages)

        offenders = [
            str(page.relative_to(ROOT))
            for page in pages
            if PUBLIC_DOMAIN_MARK in page.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_page_membership_matches_committed_page_registry(self):
        page_registry = json.loads((ROOT / "data/page_qids.json").read_text(encoding="utf-8"))
        registered_paths = {
            f"site/{dataset}/{slug}/index.html"
            for dataset, entries in page_registry.items()
            for slug in entries.values()
        }
        committed_paths = {str(page.relative_to(ROOT)) for page in self.committed_pages()}
        self.assertEqual(committed_paths, registered_paths)

    def test_committed_pages_match_deterministic_catalog_render(self):
        items_by_url = {}
        for catalog_file in ("ontologies.json", "software.json"):
            payload = json.loads((ROOT / "data" / catalog_file).read_text(encoding="utf-8"))
            items_by_url.update(
                {
                    item["canonicalUrl"]: item
                    for item in payload["items"]
                    if item.get("canonicalUrl")
                }
            )

        pages = self.committed_pages()
        survivor_urls = {
            f"{generate_pages.BASE_URL}/{page.parent.parent.name}/{page.parent.name}/"
            for page in pages
        }
        mismatches = []
        for page in pages:
            dataset = page.parent.parent.name
            slug = page.parent.name
            canonical_url = f"{generate_pages.BASE_URL}/{dataset}/{slug}/"
            item = items_by_url.get(canonical_url)
            if item is None:
                mismatches.append(str(page.relative_to(ROOT)))
                continue

            related_tools = item.get("relatedTools")
            if related_tools:
                item = {
                    **item,
                    "relatedTools": [
                        related
                        for related in related_tools
                        if related["canonicalUrl"] in survivor_urls
                    ],
                }

            rendered = generate_pages.make_page(item, dataset, slug)
            if page.read_text(encoding="utf-8") != rendered:
                mismatches.append(str(page.relative_to(ROOT)))

        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
