import assert from "node:assert/strict";
import test from "node:test";
import { ensureGenerationState, seedVerifiedGeneration } from "../src/vector-generation.js";

const datasets = {
  ontologies: [{ title: "Resource", wikidataId: "https://www.wikidata.org/wiki/Q1" }],
  software: [{ title: "Software", wikidataId: "https://www.wikidata.org/wiki/Q1" }],
};

test("candidate is marked ready only after complete inventory and representative verification", async () => {
  const states = [];
  const calls = [];
  const result = await seedVerifiedGeneration({
    generationId: "G1",
    datasets,
    batchSize: 1,
    embedBatch: async (texts) => texts.map(() => [0.1, 0.2]),
    upsertBatch: async (vectors) => {
      calls.push(["upsert", vectors[0].id, vectors[0].namespace]);
      return `mutation-${calls.length}`;
    },
    waitForMutation: async (mutationId) => calls.push(["mutation", mutationId]),
    waitForInventory: async (ids) => calls.push(["inventory", ...ids]),
    verifyAllVectors: async (ids) => calls.push(["all-vectors", ...ids]),
    verifyRepresentatives: async (vectors) =>
      calls.push(["representatives", ...vectors.map((vector) => vector.id)]),
    writeReadiness: async (state) => states.push(state),
  });

  assert.deepEqual(result.expectedIds, ["G1:ontologies:Q1", "G1:software:Q1"]);
  assert.deepEqual(states.map(({ status }) => status), ["seeding", "ready"]);
  assert.deepEqual(calls.slice(-4).map(([name]) => name), [
    "mutation",
    "inventory",
    "all-vectors",
    "representatives",
  ]);
  assert.equal(result.mutationIds.length, 2);
});

test("partial batch failure records failed state and never verifies or marks ready", async () => {
  const states = [];
  let upserts = 0;
  let verified = false;
  await assert.rejects(
    seedVerifiedGeneration({
      generationId: "G1",
      datasets,
      batchSize: 1,
      embedBatch: async (texts) => texts.map(() => [0.1]),
      upsertBatch: async () => {
        upserts += 1;
        if (upserts === 2) throw new Error("batch failed");
        return "mutation-1";
      },
      waitForMutation: async () => {
        verified = true;
      },
      waitForInventory: async () => {
        verified = true;
      },
      verifyAllVectors: async () => {
        verified = true;
      },
      verifyRepresentatives: async () => {
        verified = true;
      },
      writeReadiness: async (state) => states.push(state),
    }),
    /batch failed/
  );

  assert.equal(verified, false);
  assert.deepEqual(states.map(({ status }) => status), ["seeding", "failed"]);
  assert.equal(states.some(({ status }) => status === "ready"), false);
  assert.deepEqual(states.at(-1).mutationIds, ["mutation-1"]);
});

test("failed visibility verification cannot create a readiness record", async () => {
  const states = [];
  await assert.rejects(
    seedVerifiedGeneration({
      generationId: "G1",
      datasets,
      embedBatch: async (texts) => texts.map(() => [0.1]),
      upsertBatch: async () => "mutation",
      waitForMutation: async () => {},
      waitForInventory: async () => {
        throw new Error("not query-visible");
      },
      verifyAllVectors: async () => {},
      verifyRepresentatives: async () => {},
      writeReadiness: async (state) => states.push(state),
    }),
    /not query-visible/
  );
  assert.deepEqual(states.map(({ status }) => status), ["seeding", "failed"]);
});

test("ready verification failure never provisions or reseeds", async () => {
  let provisioned = false;
  let seeded = false;
  await assert.rejects(
    ensureGenerationState({
      readiness: { status: "ready" },
      verifyReady: async () => {
        throw new Error("ready inventory is corrupt");
      },
      provision: async () => {
        provisioned = true;
      },
      seed: async () => {
        seeded = true;
      },
    }),
    /ready inventory is corrupt/
  );
  assert.equal(provisioned, false);
  assert.equal(seeded, false);
});

test("only a non-ready generation provisions before seeding", async () => {
  const calls = [];
  await ensureGenerationState({
    readiness: { status: "failed" },
    verifyReady: async () => assert.fail("failed generation cannot use ready path"),
    provision: async () => calls.push("provision"),
    seed: async () => calls.push("seed"),
  });
  assert.deepEqual(calls, ["provision", "seed"]);
});
