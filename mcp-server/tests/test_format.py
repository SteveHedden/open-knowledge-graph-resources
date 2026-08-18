"""Tests for MCP search output metadata."""

import unittest

from okg_mcp.format import format_catalog, format_search_results


class SearchFormatTests(unittest.TestCase):
    def test_catalog_status_exposes_generation_metadata(self) -> None:
        output = format_catalog(
            {
                "name": "Open Knowledge Graphs API",
                "searchMode": "semantic",
                "catalogGenerationId": "generation-a",
                "vectorGenerationId": "generation-a",
                "fallbackReason": None,
            }
        )

        self.assertIn("**Search mode**: `semantic`", output)
        self.assertIn("**Catalog generation**: `generation-a`", output)
        self.assertIn("**Vector generation**: `generation-a`", output)

    def test_semantic_generation_metadata_is_visible(self) -> None:
        output = format_search_results(
            {
                "query": "graph",
                "total": 1,
                "results": [{"title": "Graph tool", "score": 0.9}],
                "searchMode": "semantic",
                "catalogGenerationId": "generation-a",
                "vectorGenerationId": "generation-a",
                "fallbackReason": None,
            }
        )

        self.assertIn("**Search mode**: `semantic`", output)
        self.assertIn("**Catalog generation**: `generation-a`", output)
        self.assertIn("**Vector generation**: `generation-a`", output)
        self.assertNotIn("Fallback reason", output)
        self.assertIn("### 1. Graph tool (score: 0.90)", output)

    def test_fallback_metadata_is_visible_even_without_results(self) -> None:
        output = format_search_results(
            {
                "query": "missing",
                "total": 0,
                "results": [],
                "searchMode": "text-fallback",
                "catalogGenerationId": "generation-b",
                "vectorGenerationId": None,
                "fallbackReason": "index-not-ready",
            }
        )

        self.assertIn("**Search mode**: `text-fallback`", output)
        self.assertIn("**Catalog generation**: `generation-b`", output)
        self.assertIn("**Vector generation**: `none`", output)
        self.assertIn("**Fallback reason**: `index-not-ready`", output)
        self.assertTrue(output.endswith("No results found."))

    def test_legacy_api_payload_remains_compatible(self) -> None:
        output = format_search_results(
            {
                "query": "legacy",
                "total": 1,
                "results": [{"title": "Legacy result"}],
            }
        )

        self.assertIn('## Search: "legacy" — 1 result', output)
        self.assertIn("### 1. Legacy result", output)
        self.assertNotIn("Search metadata", output)


if __name__ == "__main__":
    unittest.main()
