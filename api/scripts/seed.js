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
const GET_BY_IDS_BATCH_SIZE = 20;
const LIST_PAGE_SIZE = Number.parseInt(process.env.VECTOR_LIST_PAGE_SIZE || "1000", 10);
const LIST_PAGE_INTERVAL_MS = Number.parseInt(
  process.env.VECTOR_LIST_PAGE_INTERVAL_MS || process.env.VECTOR_LIST_INTERVAL_MS || "0",
  10
);
const LIST_RESTART_INTERVAL_MS = Number.parseInt(
  process.env.VECTOR_LIST_RESTART_INTERVAL_MS || "1000",
  10
);
const VERIFY_TIMEOUT_MS = Number.parseInt(process.env.VECTOR_VERIFY_TIMEOUT_MS || "300000", 10);
const VERIFY_INTERVAL_MS = Number.parseInt(process.env.VECTOR_VERIFY_INTERVAL_MS || "5000", 10);
const API_BASE = `https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}`;
const authHeaders = { Authorization: `Bearer ${API_TOKEN}` };

export class CloudflareApiError extends Error {
  constructor(status, details, retryAfterMs = null) {
    super(`Cloudflare API error (${status}): ${details}`);
    this.name = "CloudflareApiError";
    this.status = status;
    this.retryAfterMs = retryAfterMs;
  }
}

export class CloudflareTransportError extends Error {
  constructor(cause) {
    super(`Cloudflare request failed: ${cause instanceof Error ? cause.message : String(cause)}`);
    this.name = "CloudflareTransportError";
    this.cause = cause;
  }
}

export class VectorVisibilityPendingError extends Error {
  constructor(message) {
    super(message);
    this.name = "VectorVisibilityPendingError";
  }
}

export class VectorListSnapshotError extends Error {
  constructor(message) {
    super(message);
    this.name = "VectorListSnapshotError";
  }
}

export function isRetryableCloudflareError(error) {
  return (
    error instanceof CloudflareTransportError ||
    (error instanceof CloudflareApiError &&
      (error.status === 408 || error.status === 425 || error.status === 429 || error.status >= 500))
  );
}

export function isRecoverableVectorListError(error) {
  if (error instanceof VectorListSnapshotError) return true;
  if (!(error instanceof CloudflareApiError) || error.status !== 400) return false;
  return (
    /\bcursor\b/i.test(error.message) &&
    /\b(?:corrupt\w*|expir\w*|invalid|no longer valid)\b/i.test(error.message)
  );
}

function retryAfterMs(response) {
  const value = response.headers.get("retry-after");
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
  const date = Date.parse(value);
  return Number.isFinite(date) ? Math.max(0, date - Date.now()) : null;
}

const sleep = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

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
  let response;
  try {
    response = await fetch(url, {
      ...init,
      headers: { ...authHeaders, ...(init.headers || {}) },
    });
  } catch (error) {
    throw new CloudflareTransportError(error);
  }
  let body;
  try {
    body = await response.json();
  } catch (error) {
    if (!response.ok) {
      throw new CloudflareApiError(
        response.status,
        response.statusText || "non-JSON error response",
        retryAfterMs(response)
      );
    }
    throw error;
  }
  if (!body || typeof body !== "object") {
    throw new Error("Cloudflare API returned an invalid JSON response");
  }
  if (!response.ok || !body.success) {
    const details = body.errors?.map((error) => error.message).join("; ") || response.statusText;
    throw new CloudflareApiError(response.status, details, retryAfterMs(response));
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

  // The processed mutation pointer can advance past our mutation before it is
  // observed. The concrete metadata-index visibility check is authoritative.
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

export async function listAllVectorIds({
  sleepFn = sleep,
  nowFn = Date.now,
  deadline = nowFn() + VERIFY_TIMEOUT_MS,
} = {}) {
  let lastError;
  for (;;) {
    const ids = [];
    const seenCursors = new Set();
    let expectedTotal = null;
    let cursor = null;
    try {
      do {
        const url = new URL(`${API_BASE}/vectorize/v2/indexes/${INDEX_NAME}/list`);
        url.searchParams.set("count", String(LIST_PAGE_SIZE));
        if (cursor) url.searchParams.set("cursor", cursor);
        const page = await cloudflareJson(url.toString());
        ids.push(...(page.vectors || []).map(({ id }) => id));
        if (Number.isInteger(page.totalCount)) {
          if (expectedTotal === null) expectedTotal = page.totalCount;
          if (page.totalCount !== expectedTotal) {
            throw new VectorListSnapshotError(
              "Vectorize list snapshot total changed during pagination"
            );
          }
        }
        if (page.isTruncated && !page.nextCursor) {
          throw new VectorListSnapshotError(
            "Vectorize list response is truncated but has no next cursor"
          );
        }
        if (page.nextCursor && seenCursors.has(page.nextCursor)) {
          throw new VectorListSnapshotError("Vectorize list response repeated a cursor");
        }
        if (page.nextCursor) seenCursors.add(page.nextCursor);
        cursor = page.isTruncated ? page.nextCursor : null;
        if (cursor && LIST_PAGE_INTERVAL_MS > 0) {
          await sleepFn(LIST_PAGE_INTERVAL_MS);
        }
      } while (cursor);
      const uniqueIds = [...new Set(ids)].sort();
      if (uniqueIds.length !== ids.length) {
        throw new VectorListSnapshotError(
          "Vectorize list snapshot contained duplicate vector IDs"
        );
      }
      if (expectedTotal !== null && uniqueIds.length !== expectedTotal) {
        throw new VectorListSnapshotError(
          `Vectorize list snapshot count mismatch: expected=${expectedTotal} actual=${uniqueIds.length}`
        );
      }
      return uniqueIds;
    } catch (error) {
      if (!isRecoverableVectorListError(error)) throw error;
      lastError = error;
      const remaining = Math.max(0, deadline - nowFn());
      if (remaining === 0) throw lastError;
      const delay = Math.min(LIST_RESTART_INTERVAL_MS, remaining);
      if (delay > 0) {
        await sleepFn(delay);
      }
    }
  }
}

function expectedDatasetForId(generationId, id) {
  for (const dataset of ["ontologies", "software"]) {
    if (id.startsWith(`${generationId}:${dataset}:`)) return dataset;
  }
  throw new Error(`Vector ID is outside the expected generation datasets: ${id}`);
}

function validateVectorSubset(generationId, requestedIds, vectors) {
  const requestedIdSet = new Set(requestedIds);
  const byId = new Map(vectors.map((vector) => [vector.id, vector]));
  const unexpected = vectors.find((vector) => !requestedIdSet.has(vector.id));
  if (unexpected) {
    throw new Error(`Vector get-by-IDs returned an unexpected record: ${unexpected.id}`);
  }
  if (byId.size !== vectors.length) {
    throw new Error("Vector get-by-IDs returned duplicate vector IDs");
  }
  for (const vector of vectors) {
    const dataset = expectedDatasetForId(generationId, vector.id);
    if (
      vector.namespace !== generationId ||
      vector.metadata?.generationId !== generationId ||
      vector.metadata?.dataset !== dataset
    ) {
      throw new Error(`Vector provenance mismatch: ${vector.id}`);
    }
  }
  return vectors;
}

export function validateExpectedVectors(generationId, expectedIds, vectors) {
  const expectedIdSet = new Set(expectedIds);
  validateVectorSubset(generationId, expectedIds, vectors);
  if (vectors.length < expectedIds.length) {
    throw new VectorVisibilityPendingError(
      `Vector content is not yet complete: expected=${expectedIds.length} actual=${vectors.length}`
    );
  }
  if (vectors.length > expectedIds.length) {
    throw new Error(
      `Vector content count mismatch: expected=${expectedIds.length} actual=${vectors.length}`
    );
  }
  const actualIdSet = new Set(vectors.map(({ id }) => id));
  const missing = [...expectedIdSet].find((id) => !actualIdSet.has(id));
  if (missing) throw new Error(`Expected vector is unavailable: ${missing}`);
  return vectors;
}

export async function fetchAndValidateAllVectors(generationId, expectedIds) {
  const vectors = await getVectorsByIds(expectedIds);
  return validateExpectedVectors(generationId, expectedIds, vectors);
}

export async function getVectorsByIds(
  ids,
  { deadline = Date.now() + VERIFY_TIMEOUT_MS, sleepFn = sleep } = {}
) {
  const vectors = [];
  for (let offset = 0; offset < ids.length; offset += GET_BY_IDS_BATCH_SIZE) {
    const batchIds = ids.slice(offset, offset + GET_BY_IDS_BATCH_SIZE);
    let result;
    while (true) {
      try {
        result = await cloudflareJson(
          `${API_BASE}/vectorize/v2/indexes/${INDEX_NAME}/get_by_ids`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids: batchIds }),
          }
        );
        break;
      } catch (error) {
        if (!isRetryableCloudflareError(error) || Date.now() >= deadline) throw error;
        const remaining = Math.max(0, deadline - Date.now());
        const delay = Math.min(error.retryAfterMs ?? VERIFY_INTERVAL_MS, remaining);
        await sleepFn(delay);
      }
    }
    if (!Array.isArray(result)) {
      throw new Error(
        `Vectorize get-by-IDs returned an invalid response for ${batchIds.length} IDs`
      );
    }
    vectors.push(...result);
  }
  return vectors;
}

export async function anyVectorsByIds(ids) {
  for (let offset = 0; offset < ids.length; offset += GET_BY_IDS_BATCH_SIZE) {
    const vectors = await getVectorsByIds(ids.slice(offset, offset + GET_BY_IDS_BATCH_SIZE));
    if (vectors.length) return true;
  }
  return false;
}

export async function verifyGenerationInventory(generationId, expectedIds, listOptions) {
  const prefix = `${generationId}:`;
  const actualIds = (await listAllVectorIds(listOptions))
    .filter((id) => id.startsWith(prefix))
    .sort();
  const expected = [...expectedIds].sort();
  const exact =
    actualIds.length === expected.length &&
    actualIds.every((id, index) => id === expected[index]);
  if (!exact) {
    const expectedSet = new Set(expected);
    const actualSet = new Set(actualIds);
    const missing = expected.filter((id) => !actualSet.has(id)).length;
    const unexpected = actualIds.filter((id) => !expectedSet.has(id)).length;
    const message = `Generation ${generationId} inventory mismatch: expected=${expected.length} actual=${actualIds.length} missing=${missing} unexpected=${unexpected}`;
    if (missing > 0 && unexpected === 0) {
      throw new VectorVisibilityPendingError(message);
    }
    throw new Error(message);
  }
  return actualIds;
}

export async function waitForExpectedVectors(generationId, expectedIds) {
  // A generation is immutable and its expected IDs are derived from the exact
  // catalog commit. Use direct lookups for visibility polling, then perform one
  // exact inventory traversal to reject ghost records.
  const deadline = Date.now() + VERIFY_TIMEOUT_MS;
  const pending = new Set(expectedIds);
  const verified = new Map();
  let lastError = null;
  while (pending.size && Date.now() <= deadline) {
    try {
      const requested = [...pending];
      const vectors = await getVectorsByIds(requested, { deadline });
      validateVectorSubset(generationId, requested, vectors);
      for (const vector of vectors) {
        verified.set(vector.id, vector);
        pending.delete(vector.id);
      }
      if (pending.size) {
        lastError = new VectorVisibilityPendingError(
          `Vector content is not yet complete: missing=${pending.size}`
        );
      }
    } catch (error) {
      if (!isRetryableCloudflareError(error)) throw error;
      lastError = error;
    }
    if (pending.size) await sleep(VERIFY_INTERVAL_MS);
  }
  if (pending.size) {
    throw new Error(
      `Vector contents did not become complete for ${generationId}: ${
        lastError instanceof Error ? lastError.message : `missing=${pending.size}`
      }`
    );
  }

  const vectors = expectedIds.map((id) => verified.get(id));
  validateExpectedVectors(generationId, expectedIds, vectors);

  // The exact inventory remains the final absence-of-ghosts assertion. Keep
  // this as a separate phase so a lagging list never repeats every ID lookup.
  while (Date.now() <= deadline) {
    try {
      await verifyGenerationInventory(generationId, expectedIds, { deadline });
      return vectors;
    } catch (error) {
      if (!(error instanceof VectorVisibilityPendingError) && !isRetryableCloudflareError(error)) {
        throw error;
      }
      lastError = error;
    }
    await sleep(VERIFY_INTERVAL_MS);
  }
  throw new Error(
    `Vector contents did not become complete for ${generationId}: ${
      lastError instanceof Error ? lastError.message : String(lastError || "unknown error")
    }`
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

  const allVectors = await fetchAndValidateAllVectors(generationId, expectedIds);
  await verifyGenerationInventory(generationId, expectedIds);

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
        waitForVectors: (expectedIds) =>
          waitForExpectedVectors(manifest.generationId, expectedIds),
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
