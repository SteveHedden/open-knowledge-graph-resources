import assert from "node:assert/strict";
import test from "node:test";

process.env.CLOUDFLARE_ACCOUNT_ID = "account";
process.env.CLOUDFLARE_API_TOKEN = "token";

const {
  ensureMetadataIndexes,
  fetchAndValidateAllVectors,
  upsertBatch,
  validateExpectedVectors,
  verifyExistingGeneration,
  verifyMetadataIndexes,
} = await import("../scripts/seed.js");

const datasets = {
  ontologies: [{ title: "Ontology", wikidataId: "https://www.wikidata.org/wiki/Q1" }],
  software: [
    { title: "Software", wikidataId: "https://www.wikidata.org/wiki/Q2" },
    { title: "Second software", wikidataId: "https://www.wikidata.org/wiki/Q3" },
  ],
};

function cloudflareResponse(result) {
  return Response.json({ success: true, errors: [], result });
}

test("existing-generation verification is read-only and checks inventory plus both datasets", async (t) => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    calls.push(url.pathname);
    if (url.pathname.includes("/d1/database/")) {
      return cloudflareResponse([
        {
          results: [
            {
              generation_id: "G1",
              status: "ready",
              vector_count: 3,
              ontology_count: 1,
              software_count: 2,
            },
          ],
        },
      ]);
    }
    if (url.pathname.endsWith("/list")) {
      return cloudflareResponse({
        vectors: [
          { id: "G1:ontologies:Q1" },
          { id: "G1:software:Q2" },
          { id: "G1:software:Q3" },
        ],
        isTruncated: false,
      });
    }
    if (url.pathname.endsWith("/get_by_ids")) {
      return cloudflareResponse([
        {
          id: "G1:ontologies:Q1",
          namespace: "G1",
          values: [1],
          metadata: { generationId: "G1", dataset: "ontologies" },
        },
        {
          id: "G1:software:Q2",
          namespace: "G1",
          values: [2],
          metadata: { generationId: "G1", dataset: "software" },
        },
        {
          id: "G1:software:Q3",
          namespace: "G1",
          values: [3],
          metadata: { generationId: "G1", dataset: "software" },
        },
      ]);
    }
    if (url.pathname.endsWith("/query")) {
      const { vector, filter } = JSON.parse(init.body);
      const ontology = vector[0] === 1;
      assert.equal(filter.dataset, ontology ? "ontologies" : "software");
      return cloudflareResponse({
        matches: [
          {
            id: ontology ? "G1:ontologies:Q1" : "G1:software:Q2",
            namespace: "G1",
            metadata: {
              generationId: "G1",
              dataset: ontology ? "ontologies" : "software",
            },
          },
        ],
      });
    }
    throw new Error(`Unexpected API call: ${url}`);
  };
  t.after(() => {
    globalThis.fetch = original;
  });

  const result = await verifyExistingGeneration("G1", datasets);
  assert.equal(result.counts.total, 3);
  assert.equal(
    calls.filter((path) => path.endsWith("/get_by_ids")).length,
    1,
    "all expected vectors are validated in the batched get-by-IDs pass"
  );
  assert.equal(
    calls.filter((path) => path.includes("/vectorize/") && path.endsWith("/query")).length,
    2
  );
  assert.equal(calls.some((path) => path.includes("/ai/run/")), false);
  assert.equal(calls.some((path) => path.endsWith("/upsert")), false);
});

test("metadata filters are provisioned and verified before seeding can proceed", async (t) => {
  const original = globalThis.fetch;
  const calls = [];
  let listCalls = 0;
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    calls.push([url.pathname, init.method || "GET", init.body || null]);
    if (url.pathname.endsWith("/metadata_index/list")) {
      listCalls += 1;
      return cloudflareResponse({
        metadataIndexes:
          listCalls === 1
            ? []
            : [
                { propertyName: "dataset", indexType: "string" },
                { propertyName: "category", indexType: "string" },
              ],
      });
    }
    if (url.pathname.endsWith("/metadata_index/create")) {
      const { propertyName } = JSON.parse(init.body);
      return cloudflareResponse({ mutationId: `metadata-${propertyName}` });
    }
    if (url.pathname.endsWith("/info")) {
      return cloudflareResponse({ processedUpToMutation: "metadata-category" });
    }
    throw new Error(`Unexpected API call: ${url}`);
  };
  t.after(() => {
    globalThis.fetch = original;
  });

  const mutations = await ensureMetadataIndexes();
  assert.deepEqual(mutations, ["metadata-dataset", "metadata-category"]);
  assert.deepEqual(
    calls
      .filter(([path]) => path.endsWith("/metadata_index/create"))
      .map(([, , body]) => JSON.parse(body)),
    [
      { propertyName: "dataset", indexType: "string" },
      { propertyName: "category", indexType: "string" },
    ]
  );
  assert.equal(calls.some(([path]) => path.endsWith("/upsert")), false);
  await verifyMetadataIndexes();
});

test("Cloudflare metadata-index type casing is normalized during verification", async (t) => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    calls.push([url.pathname, init.method || "GET"]);
    if (url.pathname.endsWith("/metadata_index/list")) {
      return cloudflareResponse({
        metadataIndexes: [
          { propertyName: "dataset", indexType: "String" },
          { propertyName: "category", indexType: "String" },
        ],
      });
    }
    throw new Error(`Unexpected API call: ${url}`);
  };
  t.after(() => {
    globalThis.fetch = original;
  });

  assert.deepEqual(await ensureMetadataIndexes(), []);
  await verifyMetadataIndexes();
  assert.equal(calls.every(([, method]) => method === "GET"), true);
});

test("full-vector verification respects Cloudflare's 20-ID lookup limit", async (t) => {
  const original = globalThis.fetch;
  const batchSizes = [];
  const expectedIds = Array.from(
    { length: 45 },
    (_, index) => `G1:software:Q${index + 1}`
  );
  globalThis.fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    if (!url.pathname.endsWith("/get_by_ids")) {
      throw new Error(`Unexpected API call: ${url}`);
    }
    const { ids } = JSON.parse(init.body);
    batchSizes.push(ids.length);
    assert.ok(ids.length <= 20);
    return cloudflareResponse(
      ids.map((id) => ({
        id,
        namespace: "G1",
        values: [1],
        metadata: { generationId: "G1", dataset: "software" },
      }))
    );
  };
  t.after(() => {
    globalThis.fetch = original;
  });

  const vectors = await fetchAndValidateAllVectors("G1", expectedIds);
  assert.equal(vectors.length, 45);
  assert.deepEqual(batchSizes, [20, 20, 5]);
});

test("REST upsert fails closed on any unparsable vector", async (t) => {
  const original = globalThis.fetch;
  let requestedUrl;
  globalThis.fetch = async (input) => {
    requestedUrl = new URL(String(input));
    return cloudflareResponse({ mutationId: "mutation" });
  };
  t.after(() => {
    globalThis.fetch = original;
  });
  const mutationId = await upsertBatch([
    {
      id: "G1:software:Q1",
      namespace: "G1",
      values: [0.1],
      metadata: { generationId: "G1", dataset: "software" },
    },
  ]);
  assert.equal(mutationId, "mutation");
  assert.equal(requestedUrl.searchParams.get("unparsable-behavior"), "error");
});

test("full-vector verification rejects wrong generation, namespace, or dataset metadata", () => {
  assert.throws(
    () =>
      validateExpectedVectors("G1", ["G1:software:Q1"], [
        {
          id: "G1:software:Q1",
          namespace: "G1",
          metadata: { generationId: "G1", dataset: "ontologies" },
        },
      ]),
    /provenance mismatch/
  );
});
