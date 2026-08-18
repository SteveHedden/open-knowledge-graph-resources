# OKG API generation contract

The API never treats the long-lived `okg-catalog` Vectorize index as one mutable catalog. Each Task 21 catalog generation is an immutable Vectorize namespace. Vector IDs are globally unique:

```text
<generationId>:<dataset>:<QID>
```

`dataset` is `ontologies` or `software`, so a Wikidata item present in both catalogs cannot collide. The generation ID is also stored as the vector namespace and in metadata.

## Prepare a generation

Apply the D1 readiness migration once:

```sh
npm run d1:migrate
```

Provision and verify the string metadata indexes used by live filters before
the first generation is seeded:

```sh
CLOUDFLARE_ACCOUNT_ID=... \
CLOUDFLARE_API_TOKEN=... \
npm run vectors:provision
```

`vectors:ensure` performs this provisioning automatically for a missing or
non-ready generation before its first upsert. A generation already marked
`ready` is immutable: ensure uses a strictly read-only verification path, and
any missing metadata index or vector mismatch aborts instead of reseeding it.

From the exact catalog commit/worktree that will be published, ensure its `data/manifest.json` generation:

```sh
CLOUDFLARE_ACCOUNT_ID=... \
CLOUDFLARE_API_TOKEN=... \
npm run vectors:ensure
```

The token needs Workers Scripts edit/deploy, Workers AI, Vectorize read/write,
and D1 edit permissions because the publication and rollback workflows also
deploy or restore the API Worker. `CLOUDFLARE_D1_DATABASE_ID` and
`VECTORIZE_INDEX_NAME` may override their OKG defaults.

The seeder:

1. verifies the `dataset` and `category` string metadata indexes;
2. writes a `seeding` readiness row;
3. embeds the deterministic labeled projection in `src/semantic.js`;
4. upserts generation-qualified vectors into an isolated namespace;
5. polls every generation-qualified expected ID until its vector and provenance are visible;
6. after visibility stabilizes, verifies the generation's complete, exact ID inventory;
7. verifies representative ontology and software namespace queries; and
8. only then marks the D1 row `ready`.

A partial batch, visibility timeout, count mismatch, or representative-query failure records `failed`; it never marks the namespace ready.

`vectors:ensure` is idempotent: if the exact D1 readiness row, metadata indexes,
ID inventory, counts, every vector's provenance, and representative namespace
queries already verify, it performs zero embeddings and zero Vectorize writes.
If that read-only check fails for a ready row, the command fails closed. Use
`npm run vectors:verify` for the same strict read-only acceptance check. Both
commands accept `OKG_DATA_DIR` so rollback workflows can operate on an exact
target worktree.

After cross-surface acceptance and advancement of `catalog-current`/`catalog-previous`, retire older generation-qualified IDs while retaining those two verified namespaces:

```sh
RETAIN_GENERATIONS="$CURRENT_GENERATION,$PREVIOUS_GENERATION" npm run vectors:prune
```

Pruning refuses to start unless every retained generation is ready, writes `retiring` before deletion, waits for asynchronous deletions to become visible, and then records `retired` plus its deletion mutation IDs. Failed deletion remains auditable as `retiring`; it cannot silently appear retired.

## Serving and failure behavior

Every API request reads the live `data/manifest.json`. Semantic search runs only when D1 says that exact generation is ready, and every Vectorize query supplies that generation as its namespace. A manifest change immediately drops the in-memory static-data cache.

Generation mismatch, an incomplete index, Workers AI failure, or Vectorize failure returns HTTP 200 using current-generation static JSON with:

- `searchMode: "text-fallback"`
- `catalogGenerationId`
- `vectorGenerationId` (or `null`)
- `fallbackReason`

HTTP 503 is reserved for cases where the live manifest or the current static catalog required for fallback is unavailable. `/` and `/health` expose the same generation and mode state.

Static fallback is generation-bound rather than merely fresh-looking: each
dataset request is cache-busted by generation and artifact digest, the raw
bytes must match the SHA-256 in `data/manifest.json`, and the parsed item count
must match the manifest record count. Mismatched or mixed-generation data is
never served as a fallback.

## Tests

```sh
npm test
```

The suite is network-free and covers projections, identifiers, dataset/QID collisions, partial seeding, readiness gates, namespace isolation, mismatches, AI/Vectorize failures, static failure, and manifest-driven cache invalidation.
