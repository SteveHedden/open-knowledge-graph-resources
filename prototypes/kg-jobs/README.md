# KG Jobs Discovery Prototype

A contained RDF-first prototype for identifying knowledge-graph-related job
postings **from posting content**, not from a preselected employer list. It has
both a network-free synthetic regression corpus and a live ingestion path that
now also runs on a recurring, unattended schedule in production (Task 31).

Scope: all prototype code, vocabulary, and ontology live under
`prototypes/kg-jobs/`; nothing else in the repo is touched by this prototype's
own code. There is no production jobs category on the site and no Wikidata
editing — that boundary still holds. What *has* changed from the prototype's
original, purely local design: the live command now also runs unattended, on
a schedule, in CI (`.github/workflows/update-jobs.yml`), and its output is
committed to repo-root `data/jobs/jobs.json` / `data/jobs/jobs.ttl` so the
production OKG site can read it (see "Scheduled production ingestion" below)
— kept in its own `data/jobs/` subpath, separate from the authoritative
catalog's own `data/*.json` files, since nothing consumes this data yet
(Task 32 is the actual site surface). Running it
by hand, locally, exactly as before, is still fully supported and still the
only way to get a live snapshot for `prototypes/kg-jobs/site/`. The live
command still makes a strictly bounded, attributed public-API pull only when
`--live` is supplied, and its own working directory (`prototypes/kg-jobs/runtime/`)
remains Git-ignored regardless of whether it was invoked by a human or by the
schedule. Synthetic fixtures are plainly labeled as test data everywhere they
appear.

## Why posting-first, not employer-first

A role at any employer — a bank, a pharmaceutical company, a retailer, a
government agency, a university, a consultancy, or anywhere else — can qualify
when the posting itself contains credible KG evidence. Employer identity is
never searched by the classifier and never contributes to the outcome
(`fixtures/f016` is a regression test for exactly this: an employer named
"GraphPoint Hospitality Group" whose posting content is unrelated to KG work,
which correctly comes back `not_match`).

## Architecture

```
prototypes/kg-jobs/
├── ontology.ttl              Local classes/properties + SHACL shapes
├── vocabularies/kg-jobs.ttl  Controlled vocabulary (3 SKOS schemes)
├── sources.ttl               DCAT/PROV source registry
├── fixtures/jobs.json        20 hand-authored, reviewed test postings
├── data/jobs.ttl             Generated RDF (schema:JobPosting + evidence)
├── data/jobs.json            Generated deterministic JSON projection
├── runtime/                  Git-ignored live snapshot (local runs and scheduled CI both use this)
├── scripts/classifier.py     Reusable, vocabulary-driven classifier
├── scripts/generate.py       Fixtures -> RDF + JSON
├── scripts/live_pipeline.py  Live API -> RDF + JSON snapshot (run by hand or by the hourly schedule)
├── requirements.txt          Standalone prototype/test dependencies
├── site/index.html           Local jobs page (live by default; fixtures explicit)
└── tests/                    pytest suite (SHACL, fixtures, determinism)

# repo root (outside prototypes/kg-jobs/) -- kept separate from the
# authoritative catalog's own data/*.json files (see "Scheduled production
# ingestion" below)
.github/workflows/update-jobs.yml        Hourly schedule: calls live_pipeline.py per production source
data/jobs/jobs.json, data/jobs/jobs.ttl  Committed snapshot the production OKG site reads
data/jobs/run.json                       Committed per-source last-refresh history (continuity across CI runs)
data/jobs/raw/<source>.json              Committed raw payload per source, for provenance
```

RDF reuses `schema:JobPosting` (Schema.org), `skos:Concept`/`ConceptScheme`
(SKOS), `dcat:Dataset`/`Distribution` (DCAT), `prov:Activity` (PROV-O), and
Dublin Core Terms directly. Local terms in `kgjobs:` are minted only where none
of those vocabularies has a suitable term: the evidence model
(`kgjobs:Evidence`, `kgjobs:hasEvidence`, `kgjobs:matchedConcept`,
`kgjobs:matchedPhrase`, `kgjobs:sourceField`, `kgjobs:negated`), the
classification outcome (`kgjobs:classification`), and the match-term model
(`kgjobs:MatchTerm`, `kgjobs:termText`, `kgjobs:caseSensitive`) that lets the
vocabulary drive the classifier without duplicating it in code.

## Employer identity and Wikidata matching

Every job posting's `hiringOrganization` resolves to one stable, reused URI
per distinct employer name (`kgjd:employer-<slug>`, minted by
`scripts/entities.py`) rather than a fresh blank node per posting — the same
company appearing across many postings is one `kgjobs:Employer` resource.
This is deliberately *not* automatically linked to Wikidata: employer
identity remains optional and never a scoring input, per Task 28's original
design.

To propose a Wikidata match for review:

```bash
.venv/bin/python scripts/match_employer_wikidata.py "Neo4j" "OpenAI"
```

It searches Wikidata's public API and, for every candidate, checks whether
its `P31` (instance of) values include a real organization/company/business
type before proposing it — a top search hit alone is not enough, since
Wikidata frequently has an item only for a company's flagship *product*, not
the company itself. Neo4j is the concrete example: Wikidata's top hit for
"Neo4j" is the graph database software (typed `proprietary software` /
`graph database` / `free software`), with no separate item for the company
at all — the script flags that clearly (`NOT ORG-TYPED`) rather than
proposing a false match.

A confirmed match is recorded by hand in `employers.ttl` (a small,
git-tracked, human-reviewed registry — never regenerated, never
auto-written) as one `owl:sameAs` triple. `scripts/generate.py` and
`scripts/live_records.py` both merge in any confirmed match at build time,
but only for an employer actually present in that run's data — so
`employers.ttl` can accumulate more entries than any single run needs
without polluting the output.

## Controlled vocabulary

One Turtle file, three `skos:ConceptScheme` resources:

- **Job Roles** — titles that are themselves a KG signal (Ontologist,
  Knowledge Graph Engineer, Knowledge Graph Architect, Taxonomist, ...)
- **KG Skills and Technologies** — RDF, RDFS, OWL, SPARQL, SHACL, SKOS,
  LinkML, Knowledge Graph, Semantic Web, Linked Data, Graph Database, Neo4j,
  GraphRAG
- **KG Activities** — Entity Resolution, Semantic Integration, Taxonomy
  Management, Graph Reasoning

24 concepts total, deliberately compact rather than comprehensive. Isolated
broad words (`graph`, `knowledge`, `data`, `model`, `ontology` alone) are never
used as match terms — `tests/test_vocab.py::test_avoids_isolated_broad_terms`
enforces this.

Each concept carries `kgjobs:matchStrength` (`strong` or `contextual`) and one
or more `kgjobs:MatchTerm` nodes, each with `kgjobs:termText` and
`kgjobs:caseSensitive`. Short acronyms (RDF, RDFS, OWL, SPARQL, SHACL, SKOS)
and stylized brand names (LinkML, Neo4j, GraphRAG) are marked case-sensitive so
ordinary lowercase words can't trigger them — `f010` is a regression fixture
for exactly this: lowercase "owl" in a wildlife-monitoring posting must not
match the case-sensitive `OWL` term.

`skos:exactMatch` links point only to stable, unambiguous W3C namespace URIs
(e.g. `http://www.w3.org/2002/07/owl#`) that require no external verification.
No Wikidata QIDs are invented or guessed.

## Classification policy

For a posting's `title`, `description`, `qualifications`, and
`responsibilities` fields:

1. Search every match term (bounded, case-sensitive per its flag).
2. For each match, check the ~60 characters immediately preceding it (within
   the current clause, i.e. not crossing a `.`/`!`/`?`/`;`) for a negation cue
   (`no`, `not`, `without`, `excluding`, `except`, `never`, contractions like
   `isn't`/`don't`). If found, the match is recorded as evidence but flagged
   `negated` and excluded from scoring.
3. **qualified** — at least one unnegated **strong** concept match.
4. **review** — no strong match, but at least two *distinct* unnegated
   **contextual** concept matches.
5. **not_match** — otherwise.

This is deliberately precision-first: a single contextual signal alone (e.g. a
posting titled just "Taxonomist" with no other KG content) does not qualify or
even reach review under this policy — a known limitation, see below.

Every match — positive or negated — is emitted as an explicit `kgjobs:Evidence`
node carrying the matched concept, its label and scheme, the match strength,
the exact matched phrase, the source field, and whether it was negated. There
are no opaque numeric scores, and no embeddings, LLM classification, employer
reputation, or hidden heuristics anywhere in this prototype.

## Fixture corpus

`fixtures/jobs.json` has exactly 20 invented postings spanning banking,
pharma, retail, government, academia, consulting, manufacturing, logistics,
media, non-profit, insurance, telecom, energy, agtech, legal, hospitality,
real estate, transportation, publishing, and AI — 8 qualified, 5 review, 7
not_match. Adversarial cases included:

| Fixture | Case |
|---|---|
| `f008` | Generic "graph analytics" language — not in the controlled vocabulary |
| `f009` | Ordinary taxonomy work ("content taxonomy") without "Taxonomist"/"Taxonomy Management" |
| `f010` | Lowercase "owl" (wildlife) vs. case-sensitive `OWL` acronym |
| `f013` | Negated requirement ("No SPARQL experience is required") |
| `f016` | KG-sounding employer name with unrelated posting content |

Every fixture has a reviewed `expected_classification` and
`expected_concepts`; `tests/test_fixtures.py` parametrizes over all 20 and
fails if the generator's actual output ever drifts from the reviewed
expectation.

## Regenerating and validating

```bash
cd prototypes/kg-jobs
python3 -m venv ../../.venv-kgjobs        # or reuse an existing repo venv
../../.venv-kgjobs/bin/pip install -r ../../requirements.txt pytest
../../.venv-kgjobs/bin/python scripts/generate.py
../../.venv-kgjobs/bin/python -m pytest tests/ -v
```

`scripts/generate.py` is fully network-free: it only reads
`vocabularies/kg-jobs.ttl` and `fixtures/jobs.json` and writes `data/jobs.ttl`
and `data/jobs.json`. Re-running it against unchanged inputs produces an
isomorphic RDF graph and byte-identical JSON — enforced by
`tests/test_determinism.py`.

## Local reviewer page

```bash
cd prototypes/kg-jobs
python3 -m http.server 8008
# open http://localhost:8008/site/
```

The default page reads the ignored `runtime/` live snapshot and presents only
qualified postings inside the existing OKG visual language. Search covers job
metadata and KG concepts; deduplicated concept chips summarize why each job
belongs, and the complete matched phrase, concept, scheme, source field, and
negation evidence remains behind an expandable "Why this job" disclosure. Each
live job links to the canonical source posting and visibly credits its provider.
Internal `review`/`not_match` outcomes remain in local RDF/JSON for audit but are
not product-facing statuses. Use `http://localhost:8008/site/?mode=fixtures` to
view the explicitly labeled synthetic corpus. The prototype is not linked from
the production OKG site.

## Pulling a live local snapshot

Five sources are registered in `sources.ttl`, each independently refreshable
with its own registry-declared query families, request cap, and refresh
interval:

| `--source` | Query model | Refresh interval | Auth |
|---|---|---|---|
| `himalayas` (default) | 4 reviewed queries (`knowledge graph`, `ontology`, `semantic web`, `SPARQL`), ≤20 results each | 24h | none |
| `jobicy` | 8 reviewed queries (`rdf`, `ontology`, `sparql`, `skos`, `shacl`, `linkml`, `semantic web`, `knowledge graph`), ≤20 results each | 1h | none |
| `jooble` | Same 8 queries, ≤30 results each (their fixed page size) | 6h | `JOOBLE_API_KEY` env var |
| `arbeitnow` | Broad, unfiltered feed pull; KG relevance decided entirely by local classification | 1h | none |
| `remotive` | Broad, unfiltered feed pull (their own `search` param was verified live to not filter at all) | 6h | none |

Jobicy's own `tag` search was verified against the live API to be genuine for
most terms but to silently fall back to an unfiltered result set for the bare
words `knowledge` and `owl` specifically — those two are deliberately not
queried; `knowledge graph` and `ontology` cover the same ground. Every
source's local classification is the sole eligibility decision regardless of
how well or poorly its own search filtered candidates — this is a defense
against exactly that kind of source-side unreliability.

**Running `jooble` requires a real, approved API key**, obtained instantly
and for free at <https://jooble.org/api/about> (fill in your name, position,
email, and a real website — the request form's own language is aimed at
"webmasters," so an honest answer like the OKG catalog site works). The key
is never written to `sources.ttl` or any other file in this repo — export it
as an environment variable before running the pipeline:

```bash
export JOOBLE_API_KEY="your-key-here"
```

Jooble is also the only source that requires a POST request with a JSON body
(verified directly: an equivalent GET request returns nothing at all), so it
is fetched through a dedicated code path in `scripts/live_sources.py` rather
than the shared GET fetcher used by the other four sources.

Four of these five sources — Himalayas, Jobicy, Jooble, and Arbeitnow — are
cleared under their own published terms for public, repeatedly-refreshed
display and are the ones the production schedule below actually runs.
Remotive is **not** in production scope: it remains local-evaluation-only,
run by hand with `--source remotive` exactly as before.

```bash
cd prototypes/kg-jobs
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/live_pipeline.py --live               # himalayas (default)
.venv/bin/python scripts/live_pipeline.py --live --source jobicy
JOOBLE_API_KEY=... .venv/bin/python scripts/live_pipeline.py --live --source jooble
.venv/bin/python -m http.server 8008
# open http://localhost:8008/site/
```

There is no rate-limit bypass flag; a successful run must age past that
source's registry interval before it can be fetched again. The ignored run
manifest keeps the last successful retrieval time for every source
independently, so refreshing one source never resets another's interval —
**and refreshing one source never discards another's most recently published
records**: `runtime/jobs.json` accumulates the latest snapshot from every
source you've run, not just the one you just refreshed. `runtime/raw/`
carries forward every source's last raw payload the same way.

The command fetches a bounded current feed, strips source HTML to plain text,
normalizes and deduplicates records, applies the existing RDF vocabulary-driven
classifier, validates RDF/SHACL, and atomically promotes a complete merged
snapshot to `runtime/`. A failed request or validation never replaces the
previous good snapshot.

The reviewer page (`site/index.html`) shows `qualified` and `review` postings
together — that split is an internal classifier-confidence signal, not
something meaningful to an end user — ranked newest-`datePosted`-first, with
number of matched strong signals as a tiebreaker.

Generated local artifacts:

- `runtime/raw/<source-key>.json` — parsed JSON payloads paired with their declared queries, one file per source you've run at least once
- `runtime/jobs.ttl` — `schema:JobPosting` records plus evidence and PROV links, across all sources
- `runtime/jobs.json` — deterministic browser projection containing all outcomes, across all sources
- `runtime/run.json` — the most recent run's source, retrieval time, discovery queries, and counts (see `sourceRefreshes` for every source's last retrieval time)

## Scheduled production ingestion

`.github/workflows/update-jobs.yml` runs hourly and calls this same
`scripts/live_pipeline.py --live --source <key>`, unmodified, once for each
of the four production-cleared sources (Himalayas, Jobicy, Jooble,
Arbeitnow). It is a separate, purpose-built workflow, not an extension of
the main catalog's `update-data.yml` publication pipeline — that pipeline's
Cloudflare/vectors/rollback sequence runs once a day for the Wikidata
catalog, on a completely different cadence and failure model than an hourly
per-source jobs refresh.

Because each GitHub Actions run starts from a clean checkout, the workflow
restores the last committed snapshot into `prototypes/kg-jobs/runtime/`
before calling the pipeline (so `enforce_refresh_interval` and
`preserve_first_seen`/`other_source_records` see continuous history across
runs, not a blank slate every hour), then copies the resulting
`runtime/jobs.json` / `runtime/jobs.ttl` / `runtime/run.json` / `runtime/raw/`
back out to a committed path at repo root — `data/jobs/jobs.json`,
`data/jobs/jobs.ttl`, `data/jobs/run.json`, `data/jobs/raw/` — and commits
only if that output actually changed. Pages serves from the repository
tree, so this is what lets the production OKG site read the snapshot;
publishing only as a GitHub Actions build artifact would not be fetchable
by a static Pages build without extra plumbing, and it would expire. This
output is deliberately kept under its own `data/jobs/` subpath rather than
flat inside `data/` alongside the authoritative catalog's own
`software.json` / `ontologies.json` / etc. — nothing reads `data/jobs/`
yet (the actual Jobs tab is Task 32), so it stays out of the catalog
directory that `generate_pages.py` reads from until something does.

Because `enforce_refresh_interval` raises the dedicated
`RefreshNotDueError` (a `LivePipelineError` subclass) rather than a generic
one when a source's `minRefreshIntervalSeconds` has not elapsed yet, and
`scripts/live_pipeline.py`'s `main()` exits `0` for that specific case, an
hourly run legitimately "doing nothing" for Himalayas (24h) or Jooble (6h)
on most invocations is expected and is not a workflow failure. Only a
genuine fetch or SHACL-validation failure exits non-zero — and because
`publish_snapshot` only replaces `runtime/` atomically after validation
succeeds, and the workflow only copies `runtime/` out to `data/jobs/` after
every source has been attempted, a failure on one source can never
overwrite the last good committed snapshot with partial or missing data.

Jooble's API key is provisioned as the `JOOBLE_API_KEY` GitHub Actions
secret on the repository and is passed to that one step's environment only —
never written to a file, logged, or committed.

A manual dispatch (Actions tab -> "Run workflow") accepts an optional
`dry_run` input: when true, the pipeline still runs and its logs are still
useful, but the "Publish snapshot" and "Commit and push" steps are both
skipped entirely, so nothing under `data/jobs/` is touched and nothing is
committed or pushed. This is the safe way to test a single source (via the
`source` input) without risk to the published snapshot.

## Known limitations / deliberately deferred

This prototype answers two bounded questions—*can KG relevance be detected from
posting content alone, and can that classification run over a real attributed
feed without touching production?* It still defers everything required to run
jobs as a public OKG catalog:

- **Production source portfolio**: direct Greenhouse/Lever employer feeds and
  other sources need separate registry entries, review, and coverage work.
- **Publication rights**: local API access is not permission to republish a
  source's listings in a public job catalog.
- **Deduplication**: the same posting appearing across multiple boards.
- **History and expiry**: the local feed is a current snapshot, not a durable
  first-seen/last-seen job history.
- **Employer reconciliation**: linking `hiringOrganization` strings to
  Wikidata QIDs — deliberately optional and never a scoring input.
- **Wikidata enrichment**: no Wikidata reads or writes happen anywhere in this
  prototype.
- **Scheduling and deployment**: no cron, no CI wiring, no production catalog
  category.
- **Recall on single-signal postings**: a posting with exactly one contextual
  signal and nothing else is `not_match` under the current policy, which
  favors precision over recall — worth revisiting with a larger reviewed
  fixture set before any production use.
- **English-only**: normalization and negation-cue detection assume English
  postings.
- **Cross-linking to software/ontology pages (Task 32, deliberately deferred)**:
  showing a matching job posting on the entity page it relates to (e.g. a
  Neo4j-related posting surfaced on the Neo4j software page) is a deliberate
  non-goal of the production Jobs tab shipped in Task 32. It would require a
  posting-to-entity matching step that does not exist yet (today's classifier
  only ever asks "is this posting KG-related," never "which catalog entity is
  this posting about"), and touching every existing entity page template is a
  materially larger, separately-scoped change. Revisit only as its own task,
  with its own matching-accuracy review, once the Jobs tab itself has run in
  production and proven out.
