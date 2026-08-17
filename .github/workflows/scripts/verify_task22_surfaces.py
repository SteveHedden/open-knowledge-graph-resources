#!/usr/bin/env python3
"""Verify that every public Task 22 search surface serves one generation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_PAGES_URL = "https://openknowledgegraphs.com"
DEFAULT_API_URL = "https://api.openknowledgegraphs.com"

MCP_ACCEPTANCE_CALLS: dict[str, dict[str, Any]] = {
    "okg_get_catalog_info": {},
    "okg_search": {"params": {"q": "knowledge graph", "limit": 3}},
    "okg_search_ontologies": {"params": {"q": "ontology", "limit": 3}},
    "okg_search_software": {"params": {"q": "RDF", "limit": 3}},
}


def fetch_json(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "okg-task22-verifier/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected an object from {url}")
    return payload


def assert_generation_metadata(payload: dict[str, Any], generation_id: str, label: str) -> None:
    if payload.get("searchMode") != "semantic":
        raise AssertionError(
            f"{label} is not semantic: mode={payload.get('searchMode')!r} "
            f"reason={payload.get('fallbackReason')!r}"
        )
    if payload.get("catalogGenerationId") != generation_id:
        raise AssertionError(
            f"{label} catalog generation is {payload.get('catalogGenerationId')!r}, "
            f"expected {generation_id!r}"
        )
    if payload.get("vectorGenerationId") != generation_id:
        raise AssertionError(
            f"{label} vector generation is {payload.get('vectorGenerationId')!r}, "
            f"expected {generation_id!r}"
        )


def verify_http_surfaces(pages_url: str, api_url: str, generation_id: str) -> None:
    cache_buster = urllib.parse.urlencode({"generation": generation_id, "ts": time.time_ns()})
    manifest = fetch_json(f"{pages_url.rstrip('/')}/data/manifest.json?{cache_buster}")
    if manifest.get("generationId") != generation_id:
        raise AssertionError(
            f"Live manifest generation is {manifest.get('generationId')!r}, expected {generation_id!r}"
        )

    for path, label in (("/", "API root"), ("/health", "API health")):
        health = fetch_json(f"{api_url.rstrip('/')}{path}?{cache_buster}")
        assert_generation_metadata(health, generation_id, label)

    endpoints = (
        ("/search", {"q": "knowledge graph", "limit": 3}),
        ("/ontologies", {"q": "ontology", "limit": 3}),
        ("/software", {"q": "RDF", "limit": 3}),
    )
    for path, params in endpoints:
        query = urllib.parse.urlencode(params)
        payload = fetch_json(f"{api_url.rstrip('/')}{path}?{query}")
        assert_generation_metadata(payload, generation_id, f"API {path}")
        if not isinstance(payload.get("results"), list) or not payload["results"]:
            raise AssertionError(f"API {path} returned no results for its acceptance query")


def mcp_dispatch_text(result: Any) -> str:
    """Extract text returned by FastMCP's registered-tool dispatch path."""
    content = result[0] if isinstance(result, tuple) else result
    if not isinstance(content, (list, tuple)):
        raise AssertionError(f"FastMCP dispatch returned unexpected content: {result!r}")
    text_parts = [
        block.text
        for block in content
        if isinstance(getattr(block, "text", None), str)
    ]
    if not text_parts:
        raise AssertionError(f"FastMCP dispatch returned no text content: {result!r}")
    return "\n".join(text_parts)


async def verify_registered_mcp_tools(mcp: Any, generation_id: str) -> None:
    """List and dispatch every registered MCP tool through FastMCP."""
    tools = await mcp.list_tools()
    registered = {tool.name for tool in tools}
    expected = set(MCP_ACCEPTANCE_CALLS)
    missing = sorted(expected - registered)
    unexpected = sorted(registered - expected)
    if missing or unexpected:
        raise AssertionError(
            "MCP registration differs from the verified tool contract: "
            f"missing={missing}, unexpected={unexpected}"
        )

    required = (
        "**Search mode**: `semantic`",
        f"**Catalog generation**: `{generation_id}`",
        f"**Vector generation**: `{generation_id}`",
    )
    for name, arguments in MCP_ACCEPTANCE_CALLS.items():
        output = mcp_dispatch_text(await mcp.call_tool(name, arguments))
        if output.startswith("Error:"):
            raise AssertionError(f"{name} failed: {output}")
        for marker in required:
            if marker not in output:
                raise AssertionError(f"{name} did not report {marker}: {output}")


async def verify_mcp_surfaces(pages_url: str, api_url: str, generation_id: str) -> None:
    from okg_mcp import client, server

    client.BASE_URL = api_url.rstrip("/")
    client.STATIC_URL = f"{pages_url.rstrip('/')}/data"
    client._static_cache.clear()
    try:
        await verify_registered_mcp_tools(server.mcp, generation_id)
    finally:
        await client.close_http_client()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--pages-base-url", default=DEFAULT_PAGES_URL)
    parser.add_argument("--api-base-url", default=DEFAULT_API_URL)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--interval-seconds", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    deadline = time.monotonic() + args.timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() <= deadline:
        try:
            verify_http_surfaces(args.pages_base_url, args.api_base_url, args.generation_id)
            asyncio.run(
                verify_mcp_surfaces(args.pages_base_url, args.api_base_url, args.generation_id)
            )
            print(
                f"Task 22 verified: manifest, API, vector namespace, and all MCP tools "
                f"serve {args.generation_id} in semantic mode"
            )
            return 0
        except Exception as error:  # retries include transient network and assertion failures
            last_error = error
            print(f"Task 22 verification pending: {error}", file=sys.stderr)
            if time.monotonic() + args.interval_seconds > deadline:
                break
            time.sleep(args.interval_seconds)

    print(f"Task 22 verification failed: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
