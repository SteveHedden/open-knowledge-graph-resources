import { formatVectorResult, matchesTextQuery } from "./semantic.js";

const CACHE_TTL = 60 * 60;
const EMBED_MODEL = "@cf/baai/bge-base-en-v1.5";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

let catalogCache = { generationId: null, manifestFingerprint: null, datasets: new Map() };

export class CatalogUnavailableError extends Error {}

export function resetCatalogCache() {
  catalogCache = { generationId: null, manifestFingerprint: null, datasets: new Map() };
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return json(null, 204);
    if (request.method !== "GET") return json({ error: "Method not allowed" }, 405);

    const url = new URL(request.url);
    const path = url.pathname;

    try {
      if (path === "/" || path === "" || path === "/health") return handleRoot(env);
      if (path === "/search" || path === "/ontologies" || path === "/software") {
        return handleSearch(url, env, path);
      }
      return json({ error: "Not found" }, 404);
    } catch (error) {
      const status = error instanceof CatalogUnavailableError ? 503 : 500;
      return json({ error: error.message }, status);
    }
  },
};

export async function handleRoot(env) {
  const manifest = await getLiveManifest(env);
  const snapshot = await loadFallbackCatalog(env, ["ontologies", "software"], manifest);
  const vectorState = await getVectorState(env, snapshot.generationId);
  const mode = searchModeFor(env, vectorState);

  return json({
    name: "Open Knowledge Graphs API",
    description:
      "Semantic search over ontologies, vocabularies, taxonomies, and semantic software tools cataloged from Wikidata.",
    status: mode.searchMode === "semantic" ? "ok" : "degraded",
    searchMode: mode.searchMode,
    catalogGenerationId: snapshot.generationId,
    vectorGenerationId: vectorState.vectorGenerationId,
    fallbackReason: mode.fallbackReason,
    endpoints: {
      "/search":
        "Semantic search across all resources. Params: q, category, type (ontology|software), limit",
      "/ontologies":
        "Semantic search ontologies/vocabularies/taxonomies. Params: q, category, limit",
      "/software": "Semantic search semantic software tools. Params: q, limit",
      "/health": "Catalog/vector generation and search-mode health",
    },
    categories: [
      "Life Sciences & Healthcare",
      "Geospatial",
      "Government & Public Sector",
      "International Development",
      "Finance & Business",
      "Library & Cultural Heritage",
      "Technology & Web",
      "Environment & Agriculture",
      "General / Cross-domain",
    ],
    source: "https://openknowledgegraphs.com",
    total_ontologies: snapshot.datasets.ontologies.length,
    total_software: snapshot.datasets.software.length,
  });
}

export async function handleSearch(url, env, path) {
  const q = (url.searchParams.get("q") || "").trim();
  const category = url.searchParams.get("category") || "";
  const type = url.searchParams.get("type") || "";
  const parsedLimit = Number.parseInt(url.searchParams.get("limit") || "20", 10);
  const limit = Number.isFinite(parsedLimit) ? Math.max(1, Math.min(parsedLimit, 50)) : 20;

  if (!q) return json({ error: "Query parameter 'q' is required" }, 400);

  let manifest;
  try {
    manifest = await getLiveManifest(env);
  } catch (error) {
    return unavailableSearchResponse(error);
  }

  const generationId = manifest.generationId;
  const vectorState = await getVectorState(env, generationId);
  const mode = searchModeFor(env, vectorState);
  const params = { q, category, type, limit, path };

  if (mode.searchMode === "semantic") {
    let embedding;
    try {
      embedding = await embed(env, q);
    } catch (error) {
      return fallbackSearch(env, params, manifest, vectorState.vectorGenerationId, "embedding-error", error);
    }

    try {
      return await semanticSearch(env, params, generationId, embedding);
    } catch (error) {
      return fallbackSearch(env, params, manifest, vectorState.vectorGenerationId, "vector-error", error);
    }
  }

  return fallbackSearch(
    env,
    params,
    manifest,
    vectorState.vectorGenerationId,
    mode.fallbackReason
  );
}

function searchModeFor(env, vectorState) {
  if (!env.VECTORIZE) {
    return { searchMode: "text-fallback", fallbackReason: "index-not-ready" };
  }
  if (!vectorState.ready) {
    return {
      searchMode: "text-fallback",
      fallbackReason: vectorState.fallbackReason || "index-not-ready",
    };
  }
  if (!env.AI) {
    return { searchMode: "text-fallback", fallbackReason: "embedding-error" };
  }
  return { searchMode: "semantic", fallbackReason: null };
}

async function semanticSearch(env, params, generationId, embedding) {
  const { q, category, type, limit, path } = params;
  const filter = {};
  const requiredDataset = selectedDataset(path, type);
  if (requiredDataset) filter.dataset = requiredDataset;
  if (category) filter.category = category;

  const response = await env.VECTORIZE.query(embedding, {
    namespace: generationId,
    topK: limit,
    filter: Object.keys(filter).length ? filter : undefined,
    returnMetadata: "all",
  });

  const matches = response.matches || [];
  for (const match of matches) {
    if (
      (match.namespace && match.namespace !== generationId) ||
      match.metadata?.generationId !== generationId ||
      (requiredDataset && match.metadata?.dataset !== requiredDataset)
    ) {
      throw new Error("Vectorize returned a result outside the live generation namespace");
    }
  }

  const items = matches.map(formatVectorResult);
  await logQuery(env, { q, category, type, path, total: items.length });

  return json({
    query: q,
    category: category || null,
    total: items.length,
    results: items,
    searchMode: "semantic",
    catalogGenerationId: generationId,
    vectorGenerationId: generationId,
    fallbackReason: null,
  });
}

async function fallbackSearch(env, params, manifest, vectorGenerationId, fallbackReason, cause) {
  try {
    const datasets = selectedDatasets(params.path, params.type);
    const snapshot = await loadFallbackCatalog(env, datasets, manifest);
    return textSearch(env, params, snapshot, vectorGenerationId, fallbackReason);
  } catch (error) {
    console.error(
      JSON.stringify({
        event: "search-unavailable",
        fallbackReason,
        semanticError: cause?.message || null,
        catalogError: error.message,
      })
    );
    return unavailableSearchResponse(error, manifest.generationId, vectorGenerationId, fallbackReason);
  }
}

export async function textSearch(env, params, snapshot, vectorGenerationId, fallbackReason) {
  const { q, category, type, limit, path } = params;
  const results = [];
  for (const dataset of selectedDatasets(path, type)) {
    for (const item of snapshot.datasets[dataset]) {
      if (category && (item.category || "").toLowerCase() !== category.toLowerCase()) continue;
      if (matchesTextQuery(item, q)) results.push(item);
    }
  }

  const total = results.length;
  await logQuery(env, { q, category, type, path, total });
  return json({
    query: q,
    category: category || null,
    total,
    results: results.slice(0, limit),
    searchMode: "text-fallback",
    catalogGenerationId: snapshot.generationId,
    vectorGenerationId: vectorGenerationId || null,
    fallbackReason,
  });
}

function selectedDataset(path, type) {
  if (path === "/ontologies" || type === "ontology") return "ontologies";
  if (path === "/software" || type === "software") return "software";
  return null;
}

function selectedDatasets(path, type) {
  const dataset = selectedDataset(path, type);
  return dataset ? [dataset] : ["ontologies", "software"];
}

async function embed(env, text) {
  if (!env.AI) throw new Error("Workers AI binding is unavailable");
  const result = await env.AI.run(EMBED_MODEL, { text: [text] });
  const embedding = result?.data?.[0];
  if (!Array.isArray(embedding)) throw new Error("Workers AI returned no embedding");
  return embedding;
}

export async function getVectorState(env, catalogGenerationId) {
  if (!env.DB) {
    return { ready: false, vectorGenerationId: null, fallbackReason: "index-not-ready" };
  }

  try {
    const current = await env.DB.prepare(
      "SELECT generation_id, status, vector_count, verified_at FROM vector_generations WHERE generation_id = ? LIMIT 1"
    )
      .bind(catalogGenerationId)
      .first();

    if (current?.status === "ready") {
      return {
        ready: true,
        vectorGenerationId: current.generation_id,
        fallbackReason: null,
        vectorCount: current.vector_count,
        verifiedAt: current.verified_at,
      };
    }

    const latestReady = await env.DB.prepare(
      "SELECT generation_id, vector_count, verified_at FROM vector_generations WHERE status = 'ready' ORDER BY verified_at DESC LIMIT 1"
    ).first();

    return {
      ready: false,
      vectorGenerationId: latestReady?.generation_id || null,
      fallbackReason: current ? "index-not-ready" : latestReady ? "generation-mismatch" : "index-not-ready",
    };
  } catch (error) {
    console.error("D1 readiness error:", error.message);
    return { ready: false, vectorGenerationId: null, fallbackReason: "index-not-ready" };
  }
}

export async function getLiveManifest(env) {
  const manifest = await fetchOriginJson(env, "data/manifest.json", true);
  const generationId = manifest?.generationId;
  if (!generationId) throw new CatalogUnavailableError("Live catalog manifest has no generationId");
  const fingerprint = manifestFingerprint(manifest);
  if (
    catalogCache.generationId !== generationId ||
    catalogCache.manifestFingerprint !== fingerprint
  ) {
    catalogCache = { generationId, manifestFingerprint: fingerprint, datasets: new Map() };
  }
  return manifest;
}

function manifestFingerprint(manifest) {
  const artifacts = (manifest.artifacts || [])
    .filter(({ path }) => path === "data/ontologies.json" || path === "data/software.json")
    .map(({ path, sha256 }) => `${path}:${sha256}`)
    .sort()
    .join("|");
  const counts = manifest.counts?.records || {};
  return `${manifest.generationId}|${counts.resources ?? "?"}|${counts.software ?? "?"}|${artifacts}`;
}

function datasetContract(manifest, dataset) {
  const path = `data/${dataset}.json`;
  const artifact = (manifest.artifacts || []).find((candidate) => candidate.path === path);
  if (!artifact || !/^[a-f0-9]{64}$/i.test(artifact.sha256 || "")) {
    throw new CatalogUnavailableError(`Live manifest has no valid SHA-256 for ${path}`);
  }
  const countKey = dataset === "ontologies" ? "resources" : "software";
  const expectedCount = manifest.counts?.records?.[countKey];
  if (!Number.isSafeInteger(expectedCount) || expectedCount < 0) {
    throw new CatalogUnavailableError(`Live manifest has no valid record count for ${path}`);
  }
  return { path, sha256: artifact.sha256.toLowerCase(), expectedCount };
}

function bytesToHex(bytes) {
  return [...new Uint8Array(bytes)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

async function sha256Hex(bytes) {
  return bytesToHex(await crypto.subtle.digest("SHA-256", bytes));
}

async function fetchVerifiedDataset(env, manifest, dataset) {
  const contract = datasetContract(manifest, dataset);
  const origin = String(env.ORIGIN || "").replace(/\/$/, "");
  if (!origin) throw new CatalogUnavailableError("ORIGIN is not configured");
  const url = new URL(`${origin}/${contract.path}`);
  url.searchParams.set("catalog-generation", manifest.generationId);
  url.searchParams.set("artifact-sha256", contract.sha256);

  let response;
  try {
    response = await fetch(url.toString(), {
      headers: { "Cache-Control": "no-cache" },
      cf: { cacheTtl: 0, cacheEverything: false },
    });
  } catch (error) {
    throw new CatalogUnavailableError(`Failed to fetch ${url}: ${error.message}`);
  }
  if (!response.ok) {
    throw new CatalogUnavailableError(`Failed to fetch ${url}: ${response.status}`);
  }

  const bytes = await response.arrayBuffer();
  const actualDigest = await sha256Hex(bytes);
  if (actualDigest !== contract.sha256) {
    throw new CatalogUnavailableError(
      `Artifact digest mismatch for ${contract.path}: expected=${contract.sha256} actual=${actualDigest}`
    );
  }

  let data;
  try {
    data = JSON.parse(new TextDecoder().decode(bytes));
  } catch (error) {
    throw new CatalogUnavailableError(`Invalid JSON from ${url}: ${error.message}`);
  }
  if (!Array.isArray(data.items)) {
    throw new CatalogUnavailableError(`${contract.path} has no items array`);
  }
  if (data.items.length !== contract.expectedCount) {
    throw new CatalogUnavailableError(
      `Artifact count mismatch for ${contract.path}: expected=${contract.expectedCount} actual=${data.items.length}`
    );
  }
  return { items: data.items, contract };
}

export async function loadFallbackCatalog(env, datasetNames, manifest, retry = true) {
  const generationId = manifest.generationId;
  const datasets = {};
  let fetched = false;

  for (const dataset of datasetNames) {
    const contract = datasetContract(manifest, dataset);
    const entry = catalogCache.datasets.get(dataset);
    const now = Date.now();
    if (
      catalogCache.generationId === generationId &&
      entry?.generationId === generationId &&
      entry.sha256 === contract.sha256 &&
      entry.count === contract.expectedCount &&
      now - entry.fetchedAt < CACHE_TTL * 1000
    ) {
      datasets[dataset] = entry.items;
      continue;
    }

    let verified;
    try {
      verified = await fetchVerifiedDataset(env, manifest, dataset);
    } catch (error) {
      if (retry) {
        const currentManifest = await getLiveManifest(env);
        if (manifestFingerprint(currentManifest) !== manifestFingerprint(manifest)) {
          return loadFallbackCatalog(env, datasetNames, currentManifest, false);
        }
      }
      throw error;
    }
    const items = verified.items;
    catalogCache.datasets.set(dataset, {
      generationId,
      sha256: verified.contract.sha256,
      count: verified.contract.expectedCount,
      items,
      fetchedAt: now,
    });
    datasets[dataset] = items;
    fetched = true;
  }

  if (fetched) {
    const confirmedManifest = await getLiveManifest(env);
    if (
      confirmedManifest.generationId !== generationId ||
      manifestFingerprint(confirmedManifest) !== manifestFingerprint(manifest)
    ) {
      if (!retry) throw new CatalogUnavailableError("Catalog changed while static data was loading");
      return loadFallbackCatalog(env, datasetNames, confirmedManifest, false);
    }
  }

  return { generationId, manifest, datasets };
}

async function fetchOriginJson(env, path, bypassCache = false) {
  const origin = String(env.ORIGIN || "").replace(/\/$/, "");
  if (!origin) throw new CatalogUnavailableError("ORIGIN is not configured");
  const url = `${origin}/${path}`;
  let response;
  try {
    response = await fetch(url, {
      headers: bypassCache ? { "Cache-Control": "no-cache" } : undefined,
      cf: bypassCache ? { cacheTtl: 0, cacheEverything: false } : undefined,
    });
  } catch (error) {
    throw new CatalogUnavailableError(`Failed to fetch ${url}: ${error.message}`);
  }
  if (!response.ok) throw new CatalogUnavailableError(`Failed to fetch ${url}: ${response.status}`);
  try {
    return await response.json();
  } catch (error) {
    throw new CatalogUnavailableError(`Invalid JSON from ${url}: ${error.message}`);
  }
}

function unavailableSearchResponse(error, catalogGenerationId = null, vectorGenerationId = null, fallbackReason = "catalog-unavailable") {
  return json(
    {
      error: error.message,
      searchMode: "unavailable",
      catalogGenerationId,
      vectorGenerationId,
      fallbackReason,
    },
    503
  );
}

async function logQuery(env, { q, category, type, path, total }) {
  const timestamp = new Date().toISOString();
  console.log(
    JSON.stringify({
      event: "search",
      q,
      category: category || null,
      type: type || null,
      path,
      results: total,
      timestamp,
    })
  );

  if (env.DB) {
    try {
      await env.DB.prepare(
        "INSERT INTO queries (query, category, type, path, results, timestamp) VALUES (?, ?, ?, ?, ?, ?)"
      )
        .bind(q, category || null, type || null, path, total, timestamp)
        .run();
    } catch (error) {
      console.error("D1 log error:", error.message);
    }
  }
}

function json(data, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(data, null, 2), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}
