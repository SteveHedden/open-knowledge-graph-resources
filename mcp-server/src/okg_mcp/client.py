"""HTTP client for the Open Knowledge Graphs API.

Uses the generation-aware API as the authoritative search surface. Static JSON
search is retained only as a local fallback when the API cannot be reached;
semantic and static results are never merged because they may represent
different catalog generations.
"""

import asyncio
import hashlib
import json
import time
from typing import Any

import httpx

BASE_URL = "https://api.openknowledgegraphs.com"
STATIC_URL = "https://openknowledgegraphs.com/data"
TIMEOUT = 30.0
CACHE_TTL = 3600.0  # 1 hour

# Module-level shared HTTP client (initialized lazily)
_http_client: httpx.AsyncClient | None = None

# In-memory cache for static datasets. Every entry is tied to the immutable
# catalog generation that produced it; TTL remains a secondary freshness bound.
_static_cache: dict[str, tuple[str, str, list[dict[str, Any]], float]] = {}


class CatalogGenerationChanged(RuntimeError):
    """Raised when the live catalog changes while a static snapshot is loading."""


class StaticSnapshotIntegrityError(RuntimeError):
    """Raised when static bytes do not match the generation manifest."""


def get_http_client() -> httpx.AsyncClient:
    """Return the shared HTTP client, creating it if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=TIMEOUT)
    return _http_client


async def close_http_client() -> None:
    """Close the shared HTTP client."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


async def api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make a GET request to the OKG semantic search API."""
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    client = get_http_client()
    response = await client.get(f"{BASE_URL}{path}", params=clean_params)
    response.raise_for_status()
    return response.json()


def _manifest_dataset_digests(manifest: dict[str, Any]) -> dict[str, str]:
    """Return manifest SHA-256 digests keyed by static JSON dataset name."""
    digests: dict[str, str] = {}
    for artifact in manifest.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        path = artifact.get("path")
        digest = artifact.get("sha256")
        if (
            isinstance(path, str)
            and path.startswith("data/")
            and path.endswith(".json")
            and isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdefABCDEF" for character in digest)
        ):
            digests[path.removeprefix("data/").removesuffix(".json")] = digest.lower()
    return digests


async def _fetch_manifest_snapshot() -> tuple[str, dict[str, str]]:
    """Return an uncached live generation and its static artifact digests."""
    client = get_http_client()
    response = await client.get(
        f"{STATIC_URL}/manifest.json",
        params={"cacheBust": time.time_ns()},
    )
    response.raise_for_status()
    manifest = response.json()
    if not isinstance(manifest, dict):
        raise ValueError("Live manifest is not a JSON object.")
    generation_id = manifest.get("generationId")
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ValueError("Live manifest does not contain a generationId.")
    return generation_id, _manifest_dataset_digests(manifest)


async def _fetch_manifest_generation() -> str:
    """Return the live catalog generation without caching the manifest."""
    generation_id, _digests = await _fetch_manifest_snapshot()
    return generation_id


async def _fetch_static(
    dataset: str,
    generation_id: str | None = None,
    expected_sha256: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch and verify one static dataset from an explicit manifest snapshot."""
    if generation_id is None or expected_sha256 is None:
        manifest_generation, digests = await _fetch_manifest_snapshot()
        if generation_id is not None and manifest_generation != generation_id:
            raise CatalogGenerationChanged(
                f"Requested generation {generation_id!r}, but the live manifest is "
                f"{manifest_generation!r}."
            )
        generation_id = manifest_generation
        expected_sha256 = digests.get(dataset)

    if not expected_sha256:
        raise StaticSnapshotIntegrityError(
            f"Live manifest does not declare data/{dataset}.json."
        )
    expected_sha256 = expected_sha256.lower()

    if dataset in _static_cache:
        cached_generation, cached_digest, items, cached_at = _static_cache[dataset]
        if (
            cached_generation == generation_id
            and cached_digest == expected_sha256
            and time.monotonic() - cached_at < CACHE_TTL
        ):
            return items

    client = get_http_client()
    response = await client.get(
        f"{STATIC_URL}/{dataset}.json",
        params={
            "generation": generation_id,
            "sha256": expected_sha256,
            "cacheBust": time.time_ns(),
        },
    )
    response.raise_for_status()
    body = response.content
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if actual_sha256 != expected_sha256:
        raise StaticSnapshotIntegrityError(
            f"Downloaded data/{dataset}.json has SHA-256 {actual_sha256}, "
            f"but generation {generation_id} declares {expected_sha256}."
        )

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StaticSnapshotIntegrityError(
            f"Verified data/{dataset}.json is not valid JSON."
        ) from error
    if not isinstance(payload, dict):
        raise ValueError(f"Static {dataset} dataset is not a JSON object.")
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise ValueError(f"Static {dataset} dataset does not contain an items list.")
    _static_cache[dataset] = (
        generation_id,
        expected_sha256,
        items,
        time.monotonic(),
    )
    return items


def _search_values(value: Any) -> list[str]:
    """Flatten human-readable catalog values for deterministic text matching."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        values: list[str] = []
        for member in value:
            values.extend(_search_values(member))
        return values
    if isinstance(value, dict):
        # Only labels/titles/names are semantic prose. URLs and identifiers are
        # response metadata and must not silently alter fallback relevance.
        values = []
        for key in ("title", "name", "label"):
            values.extend(_search_values(value.get(key)))
        return values
    return []


def _text_match(item: dict[str, Any], terms: list[str], category: str | None) -> bool:
    """Check if an item matches all search terms."""
    if category and (item.get("category") or "").lower() != category.lower():
        return False
    fields = (
        "title",
        "description",
        "types",
        "category",
        "softwareType",
        "programmingLanguages",
        "licenses",
        "creators",
        "partOf",
        "relatedTools",
    )
    text = " ".join(
        part
        for field in fields
        for part in _search_values(item.get(field))
    ).lower()
    return all(t in text for t in terms)


async def _text_search_snapshot(
    q: str,
    datasets: list[str],
    category: str | None = None,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], str]:
    """Search one generation-consistent static catalog snapshot."""
    last_integrity_error: StaticSnapshotIntegrityError | None = None
    for _attempt in range(2):
        generation_before, digests = await _fetch_manifest_snapshot()
        try:
            all_items = await asyncio.gather(
                *[
                    _fetch_static(dataset, generation_before, digests.get(dataset))
                    for dataset in datasets
                ]
            )
        except StaticSnapshotIntegrityError as error:
            # A CDN transition can momentarily expose a manifest and dataset
            # from different generations. Discard everything and retry once;
            # never label unverified bytes with the manifest generation.
            last_integrity_error = error
            _static_cache.clear()
            continue

        generation_after, _after_digests = await _fetch_manifest_snapshot()
        if generation_before == generation_after:
            terms = q.lower().split()
            results = []
            for items in all_items:
                for item in items:
                    if _text_match(item, terms, category):
                        results.append({**item, "match": "text"})
            return results[:limit], generation_before

        # Never serve a mixed snapshot. Retry once using the new generation.
        _static_cache.clear()

    if last_integrity_error is not None:
        raise last_integrity_error
    raise CatalogGenerationChanged(
        "Live catalog generation changed repeatedly while loading static data."
    )


async def text_search(
    q: str,
    datasets: list[str],
    category: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search the current generation's static JSON datasets by text matching."""
    results, _generation_id = await _text_search_snapshot(
        q, datasets, category, limit
    )
    return results


async def dual_search(
    path: str,
    params: dict[str, Any],
    datasets: list[str],
    category: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Use authoritative API search, with a current-generation local fallback."""
    try:
        data = await api_get(path, params)
    except Exception:
        results, generation_id = await _text_search_snapshot(
            params["q"], datasets, category, limit
        )
        return {
            "query": params["q"],
            "category": category,
            "total": len(results),
            "results": results,
            "searchMode": "text-fallback",
            "catalogGenerationId": generation_id,
            "vectorGenerationId": None,
            "fallbackReason": "api-error",
        }

    # The API owns semantic/text fallback selection and generation validation.
    # Return its payload intact so MCP cannot reintroduce stale vector results.
    return data


def handle_api_error(e: Exception) -> str:
    """Format API errors into actionable messages."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 400:
            return "Error: Bad request. Check that the 'q' parameter is provided."
        if status == 429:
            return "Error: Rate limit exceeded. Please wait before making more requests."
        return f"Error: API request failed with status {status}."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out after 30s. The API may be temporarily unavailable."
    return f"Error: {type(e).__name__}: {e}"
