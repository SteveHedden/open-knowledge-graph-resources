#!/usr/bin/env node

import { fileURLToPath } from "node:url";
import {
  loadCatalog,
  requireConfiguration,
  verifyExistingGeneration,
  verifyMetadataIndexes,
} from "./seed.js";

export async function main() {
  requireConfiguration();
  const { manifest, datasets } = loadCatalog();
  await verifyMetadataIndexes();
  const result = await verifyExistingGeneration(manifest.generationId, datasets);
  console.log(
    `Verified ready generation ${result.generationId}: vectors=${result.counts.total} ontologies=${result.counts.ontologies} software=${result.counts.software}`
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error) => {
    console.error(error.stack || error.message);
    process.exitCode = 1;
  });
}
