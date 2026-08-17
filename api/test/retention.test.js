import assert from "node:assert/strict";
import test from "node:test";
import {
  generationFromVectorId,
  obsoleteGenerationGroups,
  retireObsoleteGenerations,
} from "../src/retention.js";

const vectorIds = [
  "G3:ontologies:Q1",
  "G3:software:Q2",
  "G2:ontologies:Q3",
  "G1:software:Q4",
  "G1:ontologies:Q5",
  "legacy-Q6",
];

test("retention plan preserves current and previous and ignores unqualified legacy IDs", () => {
  assert.equal(generationFromVectorId("G1:software:Q4"), "G1");
  assert.equal(generationFromVectorId("legacy-Q6"), null);
  assert.deepEqual(obsoleteGenerationGroups(vectorIds, ["G3", "G2"]), [
    { generationId: "G1", ids: ["G1:ontologies:Q5", "G1:software:Q4"] },
  ]);
});

test("retirement is audited before deletion and marked retired only after visibility clears", async () => {
  const events = [];
  const retired = await retireObsoleteGenerations({
    vectorIds,
    retainedGenerations: ["G3", "G2"],
    batchSize: 1,
    markStatus: async ({ generationId, status, mutationIds }) =>
      events.push(["status", generationId, status, [...mutationIds]]),
    deleteBatch: async (ids) => {
      events.push(["delete", ...ids]);
      return `mutation-${ids[0]}`;
    },
    waitUntilAbsent: async (ids) => events.push(["absent", ...ids]),
  });

  assert.equal(retired.length, 1);
  assert.deepEqual(events.map((event) => event[0]), [
    "status",
    "delete",
    "delete",
    "status",
    "absent",
    "status",
  ]);
  assert.equal(events.at(-1)[2], "retired");
});

test("failed deletion remains retiring and is never marked retired", async () => {
  const statuses = [];
  await assert.rejects(
    retireObsoleteGenerations({
      vectorIds,
      retainedGenerations: ["G3", "G2"],
      markStatus: async (state) => statuses.push(state),
      deleteBatch: async () => {
        throw new Error("delete failed");
      },
      waitUntilAbsent: async () => assert.fail("absence check should not run"),
    }),
    /delete failed/
  );
  assert.deepEqual(statuses.map(({ status }) => status), ["retiring", "retiring"]);
  assert.equal(statuses.at(-1).failureReason, "delete failed");
});
