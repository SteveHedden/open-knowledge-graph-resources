import { buildSemanticProjection, buildVectorRecord, vectorId } from "./semantic.js";

export function catalogSeedItems(datasets) {
  return [
    ...(datasets.ontologies || []).map((item) => ({ dataset: "ontologies", item })),
    ...(datasets.software || []).map((item) => ({ dataset: "software", item })),
  ];
}

export function expectedVectorIds(generationId, datasets) {
  return catalogSeedItems(datasets)
    .map(({ dataset, item }) => vectorId(generationId, dataset, item))
    .sort();
}

/**
 * A ready generation is immutable. Its acceptance path is deliberately
 * read-only: verification failures surface to the caller and never fall
 * through into provisioning or reseeding. Only a missing or non-ready row may
 * enter the mutating preparation path.
 */
export async function ensureGenerationState({
  readiness,
  verifyReady,
  provision,
  seed,
}) {
  if (readiness?.status === "ready") {
    const result = await verifyReady();
    return { ...result, reused: true, mutationIds: [] };
  }

  await provision();
  return seed();
}

/**
 * Build an isolated candidate, verify it, and only then make it ready. All
 * infrastructure operations are injected so this orchestration is fully
 * network-free in tests and usable by the Cloudflare REST seeder.
 */
export async function seedVerifiedGeneration({
  generationId,
  datasets,
  batchSize = 100,
  embedBatch,
  upsertBatch,
  waitForVectors,
  verifyRepresentatives,
  writeReadiness,
}) {
  const seedItems = catalogSeedItems(datasets);
  const expectedIds = expectedVectorIds(generationId, datasets);
  const counts = {
    total: seedItems.length,
    ontologies: (datasets.ontologies || []).length,
    software: (datasets.software || []).length,
  };
  const mutations = [];
  const representativeVectors = [];

  await writeReadiness({
    generationId,
    status: "seeding",
    counts,
    mutationIds: [],
    failureReason: null,
  });

  try {
    for (let offset = 0; offset < seedItems.length; offset += batchSize) {
      const batch = seedItems.slice(offset, offset + batchSize);
      const projections = batch.map(({ item }) => buildSemanticProjection(item));
      const embeddings = await embedBatch(projections);
      if (!Array.isArray(embeddings) || embeddings.length !== batch.length) {
        throw new Error(
          `Embedding count mismatch: expected ${batch.length}, received ${embeddings?.length ?? 0}`
        );
      }

      const vectors = batch.map(({ dataset, item }, index) =>
        buildVectorRecord(item, dataset, generationId, embeddings[index])
      );
      const mutationId = await upsertBatch(vectors);
      if (mutationId) mutations.push(mutationId);

      for (const vector of vectors) {
        if (!representativeVectors.some((candidate) => candidate.metadata.dataset === vector.metadata.dataset)) {
          representativeVectors.push(vector);
        }
      }
    }

    await waitForVectors(expectedIds);
    await verifyRepresentatives(representativeVectors);
    await writeReadiness({
      generationId,
      status: "ready",
      counts,
      mutationIds: mutations,
      failureReason: null,
    });

    return { generationId, counts, mutationIds: mutations, expectedIds };
  } catch (error) {
    await writeReadiness({
      generationId,
      status: "failed",
      counts,
      mutationIds: mutations,
      failureReason: error instanceof Error ? error.message : String(error),
    });
    throw error;
  }
}
