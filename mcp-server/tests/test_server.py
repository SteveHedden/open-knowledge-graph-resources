"""Network-free compatibility tests for all MCP search tools."""

import unittest
from unittest.mock import AsyncMock, patch

from okg_mcp.models import OntologySearchInput, SearchInput, SoftwareSearchInput
from okg_mcp import server


class SearchToolTests(unittest.IsolatedAsyncioTestCase):
    def search_payload(self) -> dict:
        return {
            "query": "graph",
            "category": None,
            "total": 1,
            "results": [{"title": "Graph result", "wikidataId": "Q1"}],
            "searchMode": "semantic",
            "catalogGenerationId": "generation-a",
            "vectorGenerationId": "generation-a",
            "fallbackReason": None,
        }

    async def test_all_resource_search_exposes_generation_metadata(self) -> None:
        search = AsyncMock(return_value=self.search_payload())
        with patch.object(server, "dual_search", search):
            output = await server.okg_search(SearchInput(q="graph"))

        self.assertIn("**Search mode**: `semantic`", output)
        self.assertIn("**Catalog generation**: `generation-a`", output)
        self.assertEqual(search.await_args.args[0:3], (
            "/search",
            {"q": "graph", "limit": 20},
            ["ontologies", "software"],
        ))

    async def test_ontology_search_exposes_generation_metadata(self) -> None:
        search = AsyncMock(return_value=self.search_payload())
        with patch.object(server, "dual_search", search):
            output = await server.okg_search_ontologies(
                OntologySearchInput(q="graph")
            )

        self.assertIn("**Vector generation**: `generation-a`", output)
        self.assertEqual(search.await_args.args[0:3], (
            "/ontologies",
            {"q": "graph", "limit": 20},
            ["ontologies"],
        ))

    async def test_software_search_exposes_generation_metadata(self) -> None:
        search = AsyncMock(return_value=self.search_payload())
        with patch.object(server, "dual_search", search):
            output = await server.okg_search_software(
                SoftwareSearchInput(q="graph")
            )

        self.assertIn("**Vector generation**: `generation-a`", output)
        self.assertEqual(search.await_args.args[0:3], (
            "/software",
            {"q": "graph", "limit": 20},
            ["software"],
        ))

    async def test_fastmcp_registration_dispatches_every_public_tool(self) -> None:
        catalog_payload = {
            "name": "Open Knowledge Graphs API",
            "searchMode": "semantic",
            "catalogGenerationId": "generation-a",
            "vectorGenerationId": "generation-a",
            "fallbackReason": None,
        }
        tools = await server.mcp.list_tools()
        self.assertEqual(
            {tool.name for tool in tools},
            {
                "okg_get_catalog_info",
                "okg_search",
                "okg_search_ontologies",
                "okg_search_software",
            },
        )

        calls = {
            "okg_get_catalog_info": {},
            "okg_search": {"params": {"q": "graph", "limit": 3}},
            "okg_search_ontologies": {"params": {"q": "ontology", "limit": 3}},
            "okg_search_software": {"params": {"q": "RDF", "limit": 3}},
        }
        with (
            patch.object(server, "api_get", AsyncMock(return_value=catalog_payload)),
            patch.object(server, "dual_search", AsyncMock(return_value=self.search_payload())),
        ):
            for name, arguments in calls.items():
                with self.subTest(tool=name):
                    dispatched = await server.mcp.call_tool(name, arguments)
                    blocks = dispatched[0] if isinstance(dispatched, tuple) else dispatched
                    output = "\n".join(
                        block.text for block in blocks if hasattr(block, "text")
                    )
                    self.assertIn("**Search mode**: `semantic`", output)
                    self.assertIn("**Catalog generation**: `generation-a`", output)
                    self.assertIn("**Vector generation**: `generation-a`", output)


if __name__ == "__main__":
    unittest.main()
