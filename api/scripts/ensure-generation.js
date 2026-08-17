#!/usr/bin/env node

import { fileURLToPath } from "node:url";
import { main as seedOrReuseGeneration } from "./seed.js";

export async function main() {
  return seedOrReuseGeneration();
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((error) => {
    console.error(error.stack || error.message);
    process.exitCode = 1;
  });
}
