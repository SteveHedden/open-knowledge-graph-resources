#!/usr/bin/env node

import { fileURLToPath } from "node:url";
import {
  ensureMetadataIndexes,
  requireConfiguration,
  verifyMetadataIndexes,
} from "./seed.js";

export async function main() {
  requireConfiguration();
  const mutations = await ensureMetadataIndexes();
  const indexes = await verifyMetadataIndexes();
  console.log(
    `Verified Vectorize metadata indexes: ${indexes
      .map(({ propertyName, indexType }) => `${propertyName}:${indexType}`)
      .join(", ")} (mutations=${mutations.length})`
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error) => {
    console.error(error.stack || error.message);
    process.exitCode = 1;
  });
}
