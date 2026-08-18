import assert from "node:assert/strict";
import test from "node:test";
import {
  buildSemanticProjection,
  buildVectorRecord,
  exactIdentifierValues,
  formatVectorResult,
  matchesTextQuery,
  vectorId,
} from "../src/semantic.js";

const item = {
  title: "  Example   Graph  ",
  description: "A semantic graph toolkit",
  wikidataId: "https://www.wikidata.org/wiki/Q42",
  canonicalUrl: "https://openknowledgegraphs.com/software/example/",
  homepage: "https://example.test/docs",
  sourceRepo: "https://github.com/example/graph",
  namespaceURI: "https://example.test/ns#",
  types: ["Software", "Knowledge graph software", "Software"],
  category: "Technology & Web",
  softwareType: "Knowledge Graph Construction",
  programmingLanguages: ["Rust", "Python"],
  licenses: ["MIT License", "Apache-2.0"],
  creators: [{ name: "Zed Example" }, { name: "Ada Example" }],
  partOf: "Example Project",
  relatedTools: [{ title: "Zulu" }, { title: "Alpha" }],
  latestVersion: "9.8.7",
  releaseDate: "2026-08-17",
};

test("semantic projection is deterministic, labeled, complete, and excludes raw identifiers", () => {
  const projection = buildSemanticProjection(item);
  assert.equal(
    projection,
    [
      "title: Example Graph",
      "description: A semantic graph toolkit",
      "resource types: Knowledge graph software | Software",
      "category: Technology & Web",
      "software type: Knowledge Graph Construction",
      "programming languages: Python | Rust",
      "licenses: Apache-2.0 | MIT License",
      "creators: Ada Example | Zed Example",
      "part of: Example Project",
      "related resources: Alpha | Zulu",
    ].join("\n")
  );
  for (const rawValue of [
    item.wikidataId,
    item.canonicalUrl,
    item.homepage,
    item.sourceRepo,
    item.namespaceURI,
    item.latestVersion,
    item.releaseDate,
  ]) {
    assert.equal(projection.includes(rawValue), false);
  }
});

test("text fallback uses semantic fields plus exact-only raw identifier lookup", () => {
  assert.equal(matchesTextQuery(item, "Ada Python graph"), true);
  assert.equal(matchesTextQuery(item, item.wikidataId.toLowerCase()), true);
  assert.equal(matchesTextQuery(item, item.sourceRepo), true);
  assert.equal(matchesTextQuery(item, "github.com/example"), false);
  assert.ok(exactIdentifierValues(item).includes(item.releaseDate));
});

test("generation and dataset both qualify vector IDs", () => {
  assert.equal(vectorId("G1", "ontologies", item), "G1:ontologies:Q42");
  assert.equal(vectorId("G1", "software", item), "G1:software:Q42");
  assert.equal(vectorId("G2", "software", item), "G2:software:Q42");
  assert.throws(
    () => vectorId("x".repeat(60), "software", item),
    /64-byte limit/
  );
});

test("vector metadata preserves response fields and generation provenance", () => {
  const vector = buildVectorRecord(item, "software", "G1", [0.1, 0.2]);
  assert.equal(vector.namespace, "G1");
  assert.equal(vector.metadata.generationId, "G1");
  assert.equal(vector.metadata.dataset, "software");

  const result = formatVectorResult({ score: 0.99, metadata: vector.metadata });
  assert.equal(result.canonicalUrl, item.canonicalUrl);
  assert.equal(result.namespaceURI, item.namespaceURI);
  assert.deepEqual(result.types, ["Knowledge graph software", "Software"]);
  assert.deepEqual(result.programmingLanguages, ["Python", "Rust"]);
  assert.deepEqual(result.licenses, ["Apache-2.0", "MIT License"]);
});
