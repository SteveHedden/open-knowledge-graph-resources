#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  ensureGenerationState,
  expectedVectorIds,
  seedVerifiedGeneration,
} from "../src/vector-generation.js";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const ACCOUNT_ID = process.env.CLOUDFLARE_ACCOUNT_ID;
const API_TOKEN = process.env.CLOUDFLARE_API_TOKEN;
const DATABASE_ID =
  process.env.CLOUDFLARE_D1_DATABASE_ID || "bd880501-9e56-4102-a810-975631aeb045";
const INDEX_NAME = process.env.VECTORIZE_INDEX_NAME || "okg-catalog";
const EMBED_MODEL = "@cf/baai/bge-base-en-v1.5";
const BATCH_SIZE = Number.parseInt(process.env.VECTOR_BATCH_SIZE || "100", 10);
const VERIFY_TIMEOUT_MS = Number.parseInt(process.env.VECTOR_VERIFY_TIMEOUT_MS || "300000", 10);
const VERIFY_INTERVAL_MS = Number.parseInt(process.env.VECTOR_VERIFY_INTERVAL_MS || "5000", 10);
const API_BASE = `https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}`;
const authHeaders = { Authorization: `Bearer ${API_TOKEN}` };

export const REQUIRED_METADATA_INDEXES = Object.freeze([
  Object.freeze({ propertyName: "dataset", indexType: "string" }),
  Object.freeze({ propertyName: "category", indexType: "string" }),
]);

function canonicalMetadataIndexType(indexType) {
  return typeof indexType === "string" ? indexType.toLowerCase() : indexType;
}

const READINESS_SCHEMA = `
CREATE TABLE IF NOT EXISTS vector_generations (
  generation_id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('seeding', 'ready', 'failed', 'retiring', 'retired')),
  vector_count INTEGER NOT NULL DEFAULT 0,
  ontology_count INTEGER NOT NULL DEFAULT 0,
  software_count INTEGER NOT NULL DEFAULT 0,
  mutation_ids TEXT NOT NULL DEFAULT '[]',
  retirement_mutation_ids TEXT NOT NULL DEFAULT '[]',
  started_at TEXT NOT NULL,
  verified_at TEXT,
  retired_at TEXT,
  failure_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_vector_generations_ready
  ON vector_generations (status, verified_at DESC);
`;

export function requireConfiguration() {
  if (!ACCOUNT_ID || !API_TOKEN) {
    throw new Error(
      "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required (the token needs Workers Scripts edit/deploy, Workers AI, Vectorize read/write, and D1 edit access)"
    );
  }
}

export function loadCatalog() {
  const dataDir = process.env.OKG_DATA_DIR || join(SCRIPT_DIR, "..", "..", "data");
  const manifest = JSON.parse(readFileSync(join(dataDir, "manifest.json"), "utf8"));
  const ontologies = JSON.parse(readFileSync(join(dataDir, "ontologies.json"), "utf8"));
  const software = JSON.parse(readFileSync(join(dataDir, "software.json"), "utf8"));
  if (!manifest.generationId) throw new Error("data/manifest.json has no generationId");
  return {
    manifest,
    datasets: { ontologies: ontologies.items || [], software: software.items || [] },
  };
}

export async function cloudflareJson(url, init = {}) {
  const response = await fetch(url, {
    ...init,
    headers: { ...authHeaders, ...(init.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok || !body.success) {
    const details = body.errors?.map((error) => error.message).join("; ") || response.statusText;
    throw new Error(`Cloudflare API error (${response.status}): ${details}`);
  }
  return body.result;
}

export async function d1Query(sql, params = []) {
  return cloudflareJson(`${API_BASE}/d1/database/${DATABASE_ID}/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sql, params }),
  });
}

function metadataIndexesFrom(result) {
  if (Array.isArray(result)) return result;
  return result?.metadataIndexes || [];
}

export async function listMetadataIndexes() {
  const result = await cloudflareJson(
    `${API_BASE}/vectorize/v2/indexes/${INDEX_NAME}/metadata_index/list`
  );
  return metadataIndexesFrom(result);
}

export function assertRequiredMetadataIndexes(indexes) {
  for (const required of REQUIRED_METADATA_INDEXES) {
    const actual = indexes.find(({ propertyName }) => propertyName === required.propertyName);
    if (!actual) {
      throw new Error(`Vectorize metadata index is missing: ${required.propertyName}`);
    }
    if (canonicalMetadataIndexType(actual.indexType) !== required.indexType) {
      throw new Error(
        `Vectorize metadata index ${required.propertyName} has type ${actual.indexType}; expected ${required.indexType}`
      );
    }
  }
  return indexes;
}

export async function verifyMetadataIndexes() {
  return assertRequiredMetadataIndexes(await listMetadataIndexes());
}

export async function waitForMutation(mutationId) {
  const deadline = Date.now() + VERIFY_TIMEOUT_MS;
  let processed = null;
  while (Date.now() <= deadline) {
    const info = await cloudflareJson(
      `${API_BASE}/vectorize/v2/indexes/${INDEX_NAME}/info`
    );
    processed = info?.processedUpToMutation || null;
    if (processed === mutationId) return info;
    await new Promise((resolve) => setTimeout(resolve, VERIFY_INTERVAL_MS));
  }
  throw new Error(
    `Vectorize mutation did not become visible: expected=${mutationId} processed=${processed || "none"}`
  );
}

async function waitForMetadataIndexes() {
  const deadline = Date.now() + VERIFY_TIMEOUT_MS;
  let lastError;
  while (Date.now() <= deadline) {
    try {
      return await verifyMetadataIndexes();
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, VERIFY_INTERVAL_MS));
  }
  throw lastError || new Error("Vectorize metadata indexes did not become visible");
}

/** Provision the two filters used by the Worker before any vector upsert. */
export async function ensureMetadataIndexes() {
  const existing = await listMetadataIndexes();
  for (const current of existing) {
    const required = REQUIRED_METADATA_INDEXES.find(
      ({ propertyName }) => propertyName === current.propertyName
    );
    if (required && canonicalMetadataIndexType(current.indexType) !== required.indexType) {
      throw new Error(
        `Vectorize metadata index ${current.propertyName} has type ${current.indexType}; expected ${required.indexType}`
      );
    }
  }

  const mutations = [];
  for (const required of REQUIRED_METADATA_INDEXES) {
    if (existing.some(({ propertyName }) => propertyName === required.propertyName)) continue;
    const result = await cloudflareJson(
      `${API_BASE}/vectorize/v2/indexes/${INDEX_NAME}/metadata_index/create`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(required),
      }
    );
    const mutationId = result?.mutationId || result?.mutation_id;
    if (!mutationId) {
      throw new Error(`Metadata-index creation returned no mutation ID: ${required.propertyName}`);
    }
    mutations.push(mutationId);
  }

  if (mutations.length) await waitForMutation(mutations.at(-1));
  await waitForMetadataIndexes();
  return mutations;
}

export async function ensureReadinessTable() {
  for (const statement of READINESS_SCHEMA.split(";").map((sql) => sql.trim()).filter(Boolean)) {
    await d1Query(statement);
  }
}

function readinessWriter(startedAt) {
  return async ({ generationId, status, counts, mutationIds, failureReason }) => {
    const verifiedAt = status === "ready" ? new Date().toISOString() : null;
    await d1Query(
      `INSERT INTO vector_generations (
        generation_id, namespace, status, vector_count, ontology_count, software_count,
        mutation_ids, started_at, verified_at, failure_reason
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(generation_id) DO UPDATE SET
        namespace = excluded.namespace,
        status = excluded.status,
        vector_count = excluded.vector_count,
        ontology_count = excluded.ontology_count,
        software_count = excluded.software_count,
        mutation_ids = excluded.mutation_ids,
        started_at = excluded.started_at,
        verified_at = excluded.verified_at,
        retirement_mutation_ids = '[]',
        retired_at = NULL,
        failure_reason = excluded.failure_reason`,
      [
        generationId,
        generationId,
        status,
        counts.total,
        counts.ontologies,
        counts.software,
        JSON.stringify(mutationIds),
        startedAt,
        verifiedAt,
        failureReason,
      ]
    );
  };
}

async function embedBatch(projections) {
  const result = await cloudflareJson(`${API_BASE}/ai/run/${EMBED_MODEL}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: projections }),
  });
  return result.data;
}

export async function upsertBatch(vectors) {
  const ndjson = vectors.map((vector) => JSON.stringify(vector)).join("\n");
  const url = new URL(`${API_BASE}/vectorize/v2/indexes/${INDEX_NAME}/upsert`);
  url.searchParams.set("unparsable-behavior", "error");
  const result = await cloudflareJson(url.toString(), {
    method: "POST",
    headers: { "Content-Type": "application/x-ndjson" },
    body: ndjson,
  });
  const mutationId = result.mutationId || result.mutation_id;
  if (!mutationId) throw new Error("Vectorize upsert returned no mutation ID");
  process.stdout.write(".");
  return mutationId;
}

export async function listAllVectorIds() {
  const ids = [];
  let cursor = null;
  do {
    const url = new URL(`${API_BASE}/vectorize/v2/indexes/${INDEX_NAME}/list`);
    url.searchParams.set("count", "1000");
    if (cursor) url.searchParams.set("cursor", cursor);
    const page = await cloudflareJson(url.toString());
    ids.push(...(page.vectors || []).map(({ id }) => id));
    cursor = page.isTruncated ? page.nextCursor : null;
  } while (cursor);
  return ids.sort();
}

export async function listGenerationIds(generationId) {
  const prefix = `${generationId}:`;
  return (await listAllVectorIds()).filter((id) => id.startsWith(prefix));
}

export function sameIds(actual, expected) {
  return actual.length === expected.length && actual.every((id, index) => id === expected[index]);
}

function expectedDatasetForId(generationId, id) {
  for (const dataset of ["ontologies", "software"]) {
    if (id.startsWith(`${generationId}:${dataset}:`)) return dataset;
  }
  throw new Error(`Vector ID is outside the expected generation datasets: ${id}`);
}

export function validateExpectedVectors(generationId, expectedIds, vectors) {
  const byId = new Map(vectors.map((vector) => [vector.id, vector]));
  if (byId.size !== expectedIds.length || vectors.length !== expectedIds.length) {
    throw new Error(
      `Vector content count mismatch: expected=${expectedIds.length} actual=${vectors.length}`
    );
  }

  for (const id of expectedIds) {
    const vector = byId.get(id);
    const dataset = expectedDatasetForId(generationId, id);
    if (!vector) throw new Error(`Expected vector is unavailable: ${id}`);
    if (
      vector.namespace !== generationId ||
      vector.metadata?.generationId !== generationId ||
      vector.metadata?.dataset !== dataset
    ) {
      throw new Error(`Vector provenance mismatch: ${id}`);
    }
  }
  return vectors;
}

export async function fetchAndValidateAllVectors(generationId, expectedIds) {
  const vectors = [];
  for (let offset = 0; offset < expectedIds.length; offset += BATCH_SIZE) {
    const ids = expectedIds.slice(offset, offset + BATCH_SIZE);
    const result = await cloudflareJson(
      `${API_BASE}/vectorize/v2/indexes/${INDEX_NAME}/get_by_ids`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
      }
    );
    if (!Array.isArray(result)) {
      throw new Error(`Vectorize get-by-IDs returned an invalid response for ${ids.length} IDs`);
    }
    vectors.push(...result);
  }
  return validateExpectedVectors(generationId, expectedIds, vectors);
}

async function waitForInventory(generationId, expectedIds) {
  const deadline = Date.now() + VERIFY_TIMEOUT_MS;
  let actual = [];
  while (Date.now() <= deadline) {
    actual = await listGenerationIds(generationId);
    if (sameIds(actual, expectedIds)) return;
    await new Promise((resolve) => setTimeout(resolve, VERIFY_INTERVAL_MS));
  }
  const missing = expectedIds.filter((id) => !actual.includes(id)).length;
  const unexpected = actual.filter((id) => !expectedIds.includes(id)).length;
  throw new Error(
    `Vector inventory did not become complete: expected=${expectedIds.length} actual=${actual.length} missing=${missing} unexpected=${unexpected}`
  );
}

async function verifyRepresentatives(generationId, representatives) {
  for (const representative of representatives) {
    const result = await cloudflareJson(`${API_BASE}/vectorize/v2/indexes/${INDEX_NAME}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        vector: representative.values,
        namespace: generationId,
        topK: 5,
        filter: { dataset: representative.metadata.dataset },
        returnMetadata: "all",
      }),
    });
    const matches = result.matches || [];
    const ownMatch = matches.find((match) => match.id === representative.id);
    if (
      !ownMatch ||
      ownMatch.namespace !== generationId ||
      ownMatch.metadata?.generationId !== generationId ||
      ownMatch.metadata?.dataset !== representative.metadata.dataset
    ) {
      throw new Error(`Representative namespace query failed for ${representative.id}`);
    }
  }
}

function d1Rows(result) {
  if (!Array.isArray(result)) return result?.results || [];
  return result[0]?.results || [];
}

export async function readinessFor(generationId) {
  const result = await d1Query(
    `SELECT generation_id, status, vector_count, ontology_count, software_count, verified_at
     FROM vector_generations WHERE generation_id = ? LIMIT 1`,
    [generationId]
  );
  return d1Rows(result)[0] || null;
}

export async function verifyExistingGeneration(generationId, datasets) {
  const expectedIds = expectedVectorIds(generationId, datasets);
  const counts = {
    total: expectedIds.length,
    ontologies: (datasets.ontologies || []).length,
    software: (datasets.software || []).length,
  };
  const readiness = await readinessFor(generationId);
  if (
    readiness?.status !== "ready" ||
    readiness.vector_count !== counts.total ||
    readiness.ontology_count !== counts.ontologies ||
    readiness.software_count !== counts.software
  ) {
    throw new Error(`Generation ${generationId} has no matching ready D1 record`);
  }

  const actualIds = await listGenerationIds(generationId);
  if (!sameIds(actualIds, expectedIds)) {
    throw new Error(
      `Generation ${generationId} inventory mismatch: expected=${expectedIds.length} actual=${actualIds.length}`
    );
  }

  const allVectors = await fetchAndValidateAllVectors(generationId, expectedIds);

  const representativeIds = [
    expectedIds.find((id) => id.includes(":ontologies:")),
    expectedIds.find((id) => id.includes(":software:")),
  ].filter(Boolean);
  if (representativeIds.length) {
    const representatives = representativeIds.map((id) =>
      allVectors.find((vector) => vector.id === id)
    );
    await verifyRepresentatives(generationId, representatives);
  }

  return { generationId, counts, expectedIds, readiness };
}

export async function main() {
  requireConfiguration();
  const { manifest, datasets } = loadCatalog();
  const readiness = await readinessFor(manifest.generationId);

  const result = await ensureGenerationState({
    readiness,
    verifyReady: async () => {
      await verifyMetadataIndexes();
      return verifyExistingGeneration(manifest.generationId, datasets);
    },
    provision: ensureMetadataIndexes,
    seed: () =>
      seedVerifiedGeneration({
        generationId: manifest.generationId,
        datasets,
        batchSize: BATCH_SIZE,
        embedBatch,
        upsertBatch,
        waitForMutation,
        waitForInventory: (expectedIds) =>
          waitForInventory(manifest.generationId, expectedIds),
        verifyAllVectors: (expectedIds) =>
          fetchAndValidateAllVectors(manifest.generationId, expectedIds),
        verifyRepresentatives: (representatives) =>
          verifyRepresentatives(manifest.generationId, representatives),
        writeReadiness: readinessWriter(new Date().toISOString()),
      }),
  });

  if (result.reused) {
    console.log(`Reused ready generation ${result.generationId} (${result.counts.total} vectors)`);
  } else {
    console.log(
      `\nReady: generation=${result.generationId} vectors=${result.counts.total} mutations=${result.mutationIds.length}`
    );
  }
  return result;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error) => {
    console.error(`\n${error.stack || error.message}`);
    process.exitCode = 1;
  });
}
