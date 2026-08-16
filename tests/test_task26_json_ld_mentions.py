import html
import json
import re
import sys
import unittest
from pathlib import Path

from rdflib import Graph, Literal, URIRef


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fetch_data  # noqa: E402
import generate_pages  # noqa: E402


OKG = fetch_data.OKG


def page_fixture(dataset="resource", **overrides):
    item = {
        "canonicalUrl": f"https://openknowledgegraphs.com/{dataset}/example/",
        "title": "Example",
        "description": "A complete semantic catalog fixture for Task 26.",
        "homepage": "https://example.test/",
        "wikidataId": "https://www.wikidata.org/wiki/Q100",
    }
    item.update(overrides)
    return item


def parse_json_ld_from_page(page):
    match = re.search(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
        page,
        flags=re.DOTALL,
    )
    if not match:
        raise AssertionError("Generated page has no application/ld+json block.")
    return json.loads(match.group(1))


def visible_related_links(page):
    match = re.search(
        r'<div class="detail-related-links">\s*(.*?)\s*</div>',
        page,
        flags=re.DOTALL,
    )
    if not match:
        return []
    return [
        {"canonicalUrl": html.unescape(url), "title": html.unescape(title)}
        for url, title in re.findall(r'<a href="([^"]+)">([^<]+)</a>', match.group(1))
    ]


class JsonLdMentionsTests(unittest.TestCase):
    def parse(self, item, dataset="resource"):
        return json.loads(generate_pages.make_json_ld(item, dataset))

    def test_single_resource_mention_is_still_an_array(self):
        related = {
            "canonicalUrl": "https://openknowledgegraphs.com/resource/related-vocabulary/",
            "title": "Related Vocabulary",
        }
        result = self.parse(page_fixture(relatedTools=[related]))

        self.assertEqual(
            result["mentions"],
            [
                {
                    "@type": "DefinedTermSet",
                    "@id": related["canonicalUrl"],
                    "name": related["title"],
                }
            ],
        )

    def test_multiple_mentions_preserve_input_order_and_target_types(self):
        related = [
            {
                "canonicalUrl": "https://openknowledgegraphs.com/software/zeta-tool/",
                "title": "Zeta Tool",
            },
            {
                "canonicalUrl": "https://openknowledgegraphs.com/resource/alpha-vocabulary/",
                "title": "Alpha Vocabulary",
            },
        ]
        result = self.parse(page_fixture("software", relatedTools=related), "software")

        self.assertEqual(
            [(entry["@id"], entry["@type"]) for entry in result["mentions"]],
            [
                (related[0]["canonicalUrl"], "SoftwareApplication"),
                (related[1]["canonicalUrl"], "DefinedTermSet"),
            ],
        )
        self.assertEqual([entry["name"] for entry in result["mentions"]], ["Zeta Tool", "Alpha Vocabulary"])

    def test_mentions_is_omitted_for_absent_empty_or_non_page_targets(self):
        fixtures = [
            page_fixture(),
            page_fixture(relatedTools=[]),
            page_fixture(
                relatedTools=[
                    {"canonicalUrl": "https://example.test/not-an-okg-page/", "title": "Dead"},
                    {"canonicalUrl": "https://openknowledgegraphs.com/resource/nested/path/", "title": "Nested"},
                    {"canonicalUrl": "https://openknowledgegraphs.com/resource/blank/", "title": "   "},
                ]
            ),
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture.get("relatedTools")):
                self.assertNotIn("mentions", self.parse(fixture))

    def test_visible_links_and_json_ld_have_exact_projection_parity(self):
        related = [
            {
                "canonicalUrl": "https://openknowledgegraphs.com/resource/alpha/",
                "title": "Alpha & Beta",
            },
            {
                "canonicalUrl": "https://openknowledgegraphs.com/resource/zeta/",
                "title": "Zeta",
            },
        ]
        item = page_fixture(relatedTools=related)
        page = generate_pages.make_page(item, "resource", "example")
        embedded = parse_json_ld_from_page(page)

        self.assertEqual(visible_related_links(page), related)
        self.assertEqual(
            [
                {"canonicalUrl": entry["@id"], "title": entry["name"]}
                for entry in embedded["mentions"]
            ],
            related,
        )

    def test_non_page_targets_are_absent_from_visible_and_embedded_projections(self):
        valid = {
            "canonicalUrl": "https://openknowledgegraphs.com/resource/survivor/",
            "title": "Survivor",
        }
        dead = {"canonicalUrl": "https://example.test/dead", "title": "Dead"}
        item = page_fixture(relatedTools=[dead, valid])
        page = generate_pages.make_page(item, "resource", "example")
        embedded = parse_json_ld_from_page(page)

        self.assertEqual(visible_related_links(page), [valid])
        self.assertEqual([entry["@id"] for entry in embedded["mentions"]], [valid["canonicalUrl"]])
        self.assertNotIn(dead["canonicalUrl"], page)

    def test_related_targets_never_use_other_schema_properties(self):
        related_url = "https://openknowledgegraphs.com/software/related/"
        result = self.parse(
            page_fixture(
                "software",
                relatedTools=[{"canonicalUrl": related_url, "title": "Related"}],
            ),
            "software",
        )

        for forbidden in ("citation", "subjectOf", "relatedLink"):
            self.assertNotIn(forbidden, result)
        self.assertNotEqual(result["sameAs"], related_url)
        self.assertNotIn(related_url, json.dumps(result["isPartOf"]))


class RdfJsonJsonLdParityTests(unittest.TestCase):
    def test_embedded_ids_equal_rdf_targets_and_related_tools_projection(self):
        graph = Graph()
        source = URIRef("https://openknowledgegraphs.com/resource/source/")
        alpha = URIRef("https://openknowledgegraphs.com/resource/alpha/")
        zeta = URIRef("https://openknowledgegraphs.com/resource/zeta/")
        graph.add((source, OKG.relatedTo, zeta))
        graph.add((source, OKG.relatedTo, alpha))
        graph.add((alpha, OKG.title, Literal("Alpha")))
        graph.add((zeta, OKG.title, Literal("Zeta")))

        related_tools = fetch_data.related_tools_for_resource(graph, source)
        item = page_fixture(canonicalUrl=str(source), relatedTools=related_tools)
        embedded = json.loads(generate_pages.make_json_ld(item, "resource"))

        rdf_targets = {str(target) for target in graph.objects(source, OKG.relatedTo)}
        json_targets = {entry["canonicalUrl"] for entry in related_tools}
        json_ld_targets = {entry["@id"] for entry in embedded["mentions"]}
        self.assertEqual(rdf_targets, json_targets)
        self.assertEqual(json_targets, json_ld_targets)
        self.assertEqual([entry["name"] for entry in embedded["mentions"]], ["Alpha", "Zeta"])


if __name__ == "__main__":
    unittest.main()
