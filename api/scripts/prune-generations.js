#!/usr/bin/env node

import { fileURLToPath } from "node:url";
import { retireObsoleteGenerations } from "../src/retention.js";
import {
  cloudflareJson,
  d1Query,
  ensureReadinessTable,
  listAllVectorIds,
  readinessFor,
  requireConfiguration,
} from "./seed.js";

const ACCOUNT_ID = process.env.CLOUDFLARE_ACCOUNT_ID;
const INDEX_NAME = process.env.VECTORIZE_INDEX_NAME || "okg-catalog";
const VERIFY_TIMEOUT_MS = Number.parseInt(process.env.VECTOR_VERIFY_TIMEOUT_MS || "300000", 10);
const VERIFY_INTERVAL_MS = Number.parseInt(process.env.VECTOR_VERIFY_INTERVAL_MS || "5000", 10);
const API_BASE = `https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}`;

function retainedGenerations() {
  const values = (process.env.RETAIN_GENERATIONS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  if (!values.length || values.length > 2) {
    throw new Error(
      "RETAIN_GENERATIONS must contain catalog-current and, when available, catalog-previous (comma separated)"
    );
  }
  return [...new Set(values)];
}

async function assertRetainedReady(generations) {
  for (const generationId of generations) {
    const readiness = await readinessFor(generationId);
    if (readiness?.status !== "ready") {
      throw new Error(`Refusing to prune: retained generation ${generationId} is not ready`);
    }
  }
}

async function markStatus({ generationId, status, mutationIds, failureReason }) {
  const now = new Date().toISOString();
  await d1Query(
    `INSERT INTO vector_generations (
       generation_id, namespace, status, vector_count, ontology_count, software_count,
       mutation_ids, retirement_mutation_ids, started_at, verified_at, retired_at, failure_reason
     ) VALUES (?, ?, ?, 0, 0, 0, '[]', ?, ?, NULL, ?, ?)
     ON CONFLICT(generation_id) DO UPDATE SET
       status = excluded.status,
       retirement_mutation_ids = excluded.retirement_mutation_ids,
       retired_at = excluded.retired_at,
       failure_reason = excluded.failure_reason`,
    [
      generationId,
      generationId,
      status,
      JSON.stringify(mutationIds),
      now,
      status === "retired" ? now : null,
      failureReason,
    ]
  );
}

async function deleteBatch(ids) {
  const result = await cloudflareJson(
    `${API_BASE}/vectorize/v2/indexes/${INDEX_NAME}/delete_by_ids`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    }
  );
  const mutationId = result.mutationId || result.mutation_id;
  if (!mutationId) throw new Error("Vectorize delete returned no mutation ID");
  return mutationId;
}

async function waitUntilAbsent(ids) {
  const target = new Set(ids);
  const deadline = Date.now() + VERIFY_TIMEOUT_MS;
  while (Date.now() <= deadline) {
    const remaining = (await listAllVectorIds()).filter((id) => target.has(id));
    if (!remaining.length) return;
    await new Promise((resolve) => setTimeout(resolve, VERIFY_INTERVAL_MS));
  }
  throw new Error(`Timed out waiting for ${ids.length} retired vector IDs to disappear`);
}

export async function main() {
  requireConfiguration();
  const retained = retainedGenerations();
  await ensureReadinessTable();
  await assertRetainedReady(retained);
  const vectorIds = await listAllVectorIds();
  const retired = await retireObsoleteGenerations({
    vectorIds,
    retainedGenerations: retained,
    markStatus,
    deleteBatch,
    waitUntilAbsent,
  });
  console.log(
    retired.length
      ? `Retired ${retired.length} old generations; retained ${retained.join(", ")}`
      : `No obsolete generation-qualified vectors; retained ${retained.join(", ")}`
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error) => {
    console.error(error.stack || error.message);
    process.exitCode = 1;
  });
}
