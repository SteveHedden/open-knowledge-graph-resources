"""Network-free tests for the generation-aware MCP search client."""

import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from okg_mcp import client


class SearchClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        client._static_cache.clear()

    def tearDown(self) -> None:
        client._static_cache.clear()

    async def test_api_results_are_not_merged_with_static_results(self) -> None:
        payload = {
            "query": "graph",
            "total": 1,
            "results": [{"wikidataId": "Q1", "title": "API result"}],
            "searchMode": "semantic",
            "catalogGenerationId": "generation-a",
            "vectorGenerationId": "generation-a",
            "fallbackReason": None,
        }
        with (
            patch.object(client, "api_get", AsyncMock(return_value=payload)),
            patch.object(
                client,
                "_text_search_snapshot",
                AsyncMock(side_effect=AssertionError("static search must not run")),
            ),
        ):
            result = await client.dual_search(
                "/search", {"q": "graph", "limit": 20}, ["ontologies"]
            )

        self.assertIs(result, payload)
        self.assertEqual(result["searchMode"], "semantic")
        self.assertEqual(result["catalogGenerationId"], "generation-a")
        self.assertEqual(result["vectorGenerationId"], "generation-a")

    async def test_api_text_fallback_metadata_is_preserved(self) -> None:
        payload = {
            "query": "graph",
            "total": 1,
            "results": [{"wikidataId": "Q2", "title": "API text result"}],
            "searchMode": "text-fallback",
            "catalogGenerationId": "generation-b",
            "vectorGenerationId": "generation-a",
            "fallbackReason": "generation-mismatch",
        }
        with patch.object(client, "api_get", AsyncMock(return_value=payload)):
            result = await client.dual_search(
                "/software", {"q": "graph", "limit": 20}, ["software"]
            )

        self.assertIs(result, payload)
        self.assertEqual(result["fallbackReason"], "generation-mismatch")

    async def test_api_failure_uses_current_static_generation_only(self) -> None:
        static_results = [{"wikidataId": "Q3", "title": "Static result"}]
        with (
            patch.object(client, "api_get", AsyncMock(side_effect=RuntimeError("down"))),
            patch.object(
                client,
                "_text_search_snapshot",
                AsyncMock(return_value=(static_results, "generation-c")),
            ),
        ):
            result = await client.dual_search(
                "/ontologies",
                {"q": "graph", "limit": 20},
                ["ontologies"],
                category="Technology & Web",
            )

        self.assertEqual(result["results"], static_results)
        self.assertEqual(result["searchMode"], "text-fallback")
        self.assertEqual(result["catalogGenerationId"], "generation-c")
        self.assertIsNone(result["vectorGenerationId"])
        self.assertEqual(result["fallbackReason"], "api-error")

    async def test_static_cache_is_keyed_by_manifest_generation(self) -> None:
        body_a = json.dumps({"items": [{"title": "Generation A"}]}).encode()
        body_b = json.dumps({"items": [{"title": "Generation B"}]}).encode()
        digest_a = hashlib.sha256(body_a).hexdigest()
        digest_b = hashlib.sha256(body_b).hexdigest()
        response_a = unittest.mock.Mock(content=body_a)
        response_a.raise_for_status.return_value = None
        response_b = unittest.mock.Mock(content=body_b)
        response_b.raise_for_status.return_value = None
        http = unittest.mock.Mock()
        http.get = AsyncMock(side_effect=[response_a, response_b])

        with patch.object(client, "get_http_client", return_value=http):
            first = await client._fetch_static("software", "generation-a", digest_a)
            cached = await client._fetch_static("software", "generation-a", digest_a)
            second = await client._fetch_static("software", "generation-b", digest_b)

        self.assertEqual(first, [{"title": "Generation A"}])
        self.assertIs(cached, first)
        self.assertEqual(second, [{"title": "Generation B"}])
        self.assertEqual(http.get.await_count, 2)
        for call in http.get.await_args_list:
            self.assertIn("cacheBust", call.kwargs["params"])
            self.assertIn("generation", call.kwargs["params"])
            self.assertIn("sha256", call.kwargs["params"])

    async def test_static_dataset_must_match_manifest_digest(self) -> None:
        body = json.dumps({"items": [{"title": "Stale bytes"}]}).encode()
        response = unittest.mock.Mock(content=body)
        response.raise_for_status.return_value = None
        http = unittest.mock.Mock()
        http.get = AsyncMock(return_value=response)

        with (
            patch.object(client, "get_http_client", return_value=http),
            self.assertRaisesRegex(
                client.StaticSnapshotIntegrityError,
                "has SHA-256",
            ),
        ):
            await client._fetch_static("software", "generation-a", "0" * 64)

        self.assertNotIn("software", client._static_cache)

    async def test_static_dataset_requires_matching_manifest_artifact(self) -> None:
        with (
            patch.object(
                client,
                "_fetch_manifest_snapshot",
                AsyncMock(return_value=("generation-a", {})),
            ),
            self.assertRaisesRegex(
                client.StaticSnapshotIntegrityError,
                "does not declare data/software.json",
            ),
        ):
            await client._fetch_static("software", "generation-a", None)

    def test_manifest_artifacts_map_static_json_to_digest(self) -> None:
        digest = "a" * 64
        self.assertEqual(
            client._manifest_dataset_digests(
                {
                    "artifacts": [
                        {"path": "data/software.json", "sha256": digest},
                        {"path": "data/invalid.json", "sha256": "x" * 64},
                        {"path": "data/software.ttl", "sha256": "b" * 64},
                    ]
                }
            ),
            {"software": digest},
        )

    async def test_manifest_request_is_cache_busted(self) -> None:
        digest = "a" * 64
        response = unittest.mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "generationId": "generation-a",
            "artifacts": [
                {"path": "data/software.json", "sha256": digest},
            ],
        }
        http = unittest.mock.Mock()
        http.get = AsyncMock(return_value=response)

        with patch.object(client, "get_http_client", return_value=http):
            generation_id, digests = await client._fetch_manifest_snapshot()

        self.assertEqual(generation_id, "generation-a")
        self.assertEqual(digests, {"software": digest})
        self.assertIn("cacheBust", http.get.await_args.kwargs["params"])

    async def test_snapshot_retries_if_manifest_changes_mid_load(self) -> None:
        manifests = AsyncMock(
            side_effect=[
                ("generation-a", {"software": "a" * 64}),
                ("generation-b", {"software": "b" * 64}),
                ("generation-b", {"software": "b" * 64}),
                ("generation-b", {"software": "b" * 64}),
            ]
        )
        fetch_static = AsyncMock(
            side_effect=[
                [{"title": "stale graph"}],
                [{"title": "current graph", "wikidataId": "Q4"}],
            ]
        )
        with (
            patch.object(client, "_fetch_manifest_snapshot", manifests),
            patch.object(client, "_fetch_static", fetch_static),
        ):
            results, generation_id = await client._text_search_snapshot(
                "graph", ["software"]
            )

        self.assertEqual(generation_id, "generation-b")
        self.assertEqual([item["title"] for item in results], ["current graph"])
        self.assertEqual(
            [(call.args[1], call.args[2]) for call in fetch_static.await_args_list],
            [("generation-a", "a" * 64), ("generation-b", "b" * 64)],
        )

    def test_text_match_uses_complete_semantic_field_set(self) -> None:
        item = {
            "title": "Tool",
            "description": "A useful package",
            "types": ["Software"],
            "softwareType": "Ontology Engineering",
            "programmingLanguages": ["Python"],
            "licenses": ["MIT"],
            "creators": [{"name": "Ada Lovelace", "wikidataId": "Q7259"}],
            "partOf": "Example Project",
            "relatedTools": [
                {"title": "Graph Companion", "canonicalUrl": "https://example.test"}
            ],
        }

        for query in (
            ["ontology", "engineering"],
            ["python"],
            ["mit"],
            ["ada", "lovelace"],
            ["example", "project"],
            ["graph", "companion"],
        ):
            with self.subTest(query=query):
                self.assertTrue(client._text_match(item, query, None))

        self.assertFalse(client._text_match(item, ["q7259"], None))
        self.assertFalse(client._text_match(item, ["example.test"], None))


if __name__ == "__main__":
    unittest.main()
