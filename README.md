# Open Knowledge Graphs

Open Knowledge Graphs is a static, daily-refreshed catalog of ontology, semantic software, graph database, and AI agent memory software records sourced from Wikidata. It publishes both machine-readable artifacts (Turtle + JSON) and a searchable browser UI.

## Live Links

- Site: https://openknowledgegraphs.com/
- Semantic Search API: https://api.openknowledgegraphs.com/
- Ontology schema (Turtle): https://openknowledgegraphs.com/ontology.ttl
- Domain categories (SKOS/Turtle): https://openknowledgegraphs.com/vocabularies/categories.ttl
- Software types (SKOS/Turtle): https://openknowledgegraphs.com/vocabularies/software-types.ttl
- Source registry and mappings (Turtle): https://openknowledgegraphs.com/sources.ttl
- Curated classifications (Turtle): https://openknowledgegraphs.com/curation/classifications.ttl
- Ontologies dataset (Turtle): https://openknowledgegraphs.com/data/ontologies.ttl
- Ontologies dataset (JSON): https://openknowledgegraphs.com/data/ontologies.json
- Software dataset (Turtle): https://openknowledgegraphs.com/data/software.ttl
- Software dataset (JSON): https://openknowledgegraphs.com/data/software.json
- Transactional generation manifest: https://openknowledgegraphs.com/data/manifest.json

## API

Semantic search over the full catalog.

```
GET https://api.openknowledgegraphs.com/search?q=movie+ontology&limit=5
GET https://api.openknowledgegraphs.com/ontologies?q=healthcare+vocabulary
GET https://api.openknowledgegraphs.com/software?q=rdf+triplestore
```

**Parameters:** `q` (required), `category`, `type` (ontology|software), `limit` (default 20, max 100)

**Categories:** Life Sciences & Healthcare, Geospatial, Government & Public Sector, International Development, Finance & Business, Library & Cultural Heritage, Technology & Web, Environment & Agriculture, General / Cross-domain

## MCP Server

The `mcp-server/` directory contains an MCP (Model Context Protocol) server that exposes the OKG catalog to AI assistants like Claude Desktop and Claude Code.

### Tools

| Tool | Description |
| --- | --- |
| `okg_get_catalog_info` | Get catalog metadata: counts, categories, and available endpoints |
| `okg_search` | Semantic search across all resources (ontologies + software) |
| `okg_search_ontologies` | Search ontologies, vocabularies, and taxonomies only |
| `okg_search_software` | Search semantic software tools only |

### Quick Start

```bash
cd mcp-server
uv sync
uv run okg-mcp
```

### Configuration

Add to your MCP client config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "open-knowledge-graphs": {
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-server", "run", "okg-mcp"]
    }
  }
}
```

## Architecture Overview

OKG has three semantic layers. The layers are roles, not a requirement that each layer use one file.

1. Structural ontology: `ontology.ttl` owns stable OKG classes and properties, external-vocabulary alignments, and SHACL policy.
2. Controlled vocabulary and source registry:
   - `vocabularies/categories.ttl` and `vocabularies/software-types.ttl` own the versioned SKOS concepts, classifier definitions, and stable filter slugs.
   - `sources.ttl` describes Wikidata and the generated catalogs with DCAT/PROV, and owns Wikidata class/property/value-kind mappings.
3. Catalog instances and curation:
   - `curation/classifications.ttl` is the maintained RDF source for category and software-type assignments.
   - `data/ontologies.ttl` and `data/software.ttl` are the generated authoritative catalog graphs.
   - Catalog JSON, classification JSON, frontend vocabulary options, detail pages, and search records are derived compatibility projections.

`scripts/fetch_data.py` reads the RDF vocabularies, source mappings, and curation graph before querying Wikidata. Python retains SPARQL mechanics such as subclass traversal, creator unions, and version-qualifier handling, while source identifiers and controlled values remain declarative RDF.

Curated assignments persist independently of a refresh. An assignment is emitted into a generated catalog only when its QID is present in that run's eligible Wikidata query result; if a source record temporarily disappears, its assignment remains in `curation/classifications.ttl` and is applied again when the record returns. Detail-page eligibility is a separate content and link-quality filter.

### Source Eligibility and Wikidata Classification Audits

`sources.ttl` also owns the narrow ontology eligibility policy. A raw ontology candidate is automatically rejected only when it is directly typed with one of the declared term/component markers and has a direct part-of parent that independently remains eligible in the same source snapshot. Confirmed exclusions and reviewed exceptions are QID-specific RDF decisions with public evidence; labels, descriptions, generic component types, and fragment-style URLs are audit signals only.

Task 24's reproducible public source snapshot and review-first audit are stored in `audits/wikidata/`. The audit covers the complete six-class pre-filter ontology cohort and records exact before/proposed claims, evidence, rationales, and revision fields. Validate it locally with:

```bash
python scripts/wikidata_classification_audit.py validate
python scripts/wikidata_classification_audit.py execute
```

The second command is a dry run by default. Live writes require both a separate review that changes every planned record to `approved` and the explicit `--apply-approved` option. Credentials are supplied only through `WIKI_USER` and `WIKI_PASS` environment variables and must never be committed.

This lightweight architecture intentionally has no source mirrors, value-level crosswalk layer, Fuseki dependency, or Teacher workflow. Those remain out of scope until a concrete second-source normalization requirement exists.

## Repository Layout

- `curation/`: authoritative maintained classification assignments
- `data/`: authoritative generated catalog RDF plus derived JSON projections
- `mcp-server/`: MCP server for AI assistant integration
- `scripts/`: data refresh and LLM classification scripts
- `site/`: static frontend (HTML/CSS/JS + assets)
- `vocabularies/`: independently versioned SKOS controlled vocabularies
- `.github/workflows/`: CI/CD for data refresh and Pages deploy
- `ontology.ttl`: ontology and SHACL shape definitions
- `sources.ttl`: DCAT/PROV source registry and declarative source-schema mappings

## Local Setup

### Prerequisites

- Python 3.11+ (3.12 recommended)
- `pip`

### Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Refresh Data Locally

```bash
python scripts/fetch_data.py
```

For a publication-style page refresh, preserve the last verified generation's
page membership while checking only new records or records whose homepage has
changed:

```bash
python scripts/generate_pages.py --membership-baseline /path/to/verified-generation
```

Adding `--skip-link-check` with a membership baseline is a conservative,
network-free mode: unchanged verified pages remain, while no new or
homepage-changed page is admitted. The legacy baseline-free
`--skip-link-check` behavior still admits every content-qualified candidate.

Optional automated classification of previously unseen records uses Anthropic during the refresh:

```bash
export ANTHROPIC_API_KEY=your_key_here
python scripts/fetch_data.py
```

Without the key, the refresh preserves and applies existing RDF curation but leaves newly discovered records unclassified.

### Validate a Catalog Candidate

```bash
python scripts/validate_catalog.py --baseline-ref HEAD
python -m unittest discover -s tests -v
```

The shared validator runs pySHACL over the merged semantic layers and enforces RDF/JSON projection parity, declared schema terms, controlled values, source-mapping coverage, complete public IRIs, known-record fixtures, stable URI-registry reservations, generated-page contracts, and sitemap membership.

Resource and software QID sets are compared independently with the committed successful baseline. A loss above 2% is a warning and a loss above 10% is a hard failure; additions cannot conceal losses. Surviving QIDs must retain their URI, and registry entries for disappeared QIDs cannot be removed, changed, or reassigned. Source `Q...`/`P...` mapping literals and registry keys are intentionally allowed, but public RDF/JSON identity and reference fields require complete IRIs.

Soft warnings cover 2–10% record loss, optional-metadata coverage deterioration, and possible external-link availability regressions. They are printed and appended to `$GITHUB_STEP_SUMMARY` in Actions; validation does not create a committed report.

### Verify a Transactional Snapshot

```bash
python scripts/catalog_snapshot.py verify --root .
python scripts/catalog_snapshot.py build-pages --root . --destination /tmp/okg-pages
```

Every published generation has a colocated `data/manifest.json`. Its generation ID combines the UTC completion time with a canonical content digest. The manifest records run timestamps, record/triple counts, schema versions, individual SHA-256 checksums, and deterministic hashes and file counts for the generated resource/software page trees. The manifest excludes itself from digest coverage.

The first tracked manifest is a retrospective bootstrap around the catalog retrieved on 2026-08-13. Its `startedAt` and `sourceRetrievedAt` preserve that extraction time, while its 2026-08-14 `completedAt` records when the pre-existing artifact tree was staged and validated under the new Task 21 contract. The roughly 24-hour interval is therefore specific to bootstrap adoption; normal workflow generations record all three timestamps during one run.

Catalog JSON and instance RDF retain their existing contracts; generation metadata lives only in the manifest. Refreshes whose normalized RDF, JSON items, registries, sitemap membership, and page content are unchanged retain the existing generation and its original timestamps.

`data/jobs/` is deployed alongside the core catalog but tracked by its own colocated `data/jobs/manifest.json` instead of `data/manifest.json`. Its implementation lives in top-level `jobs/` and it refreshes hourly, on a schedule independent of catalog generations; folding it into the core manifest would break `verify` every time the jobs workflow ran between catalog generations. `catalog_snapshot.py verify` checks both manifests and confirms every deployed file belongs to exactly one of them (`finalize-jobs` / `verify-jobs` mirror `finalize` / `verify` for the jobs manifest alone). Because rollback checks out an old commit wholesale, `deploy.yml` explicitly restores the latest verified `data/jobs/` snapshot into the rollback worktree before building the Pages artifact, so rolling back the core catalog never regresses job listings to a stale historical snapshot.

### Run the Site Locally

```bash
python -m http.server 8000
```

Then open: `http://localhost:8000/site/`

## Data API Documentation

There is no server-side API; the JSON files are the API surface.

### `ontologies.json`

Top-level object:

```json
{
  "generatedAt": "2026-03-08T03:21:55Z",
  "items": []
}
```

`items[]` fields:

- Required:
  - `title` (string)
  - `wikidataId` (IRI string to Wikidata page)
  - `types` (string array, may contain multiple values)
- Optional (omitted when absent):
  - `description` (string)
  - `homepage` (IRI string)
  - `partOf` (string)
  - `relatedTools` (array projected from qualified `okg:relatedTo` links)
  - `licenses` (string array)
  - `category` (string, one of predefined domain categories)

### `software.json`

Top-level object:

```json
{
  "generatedAt": "2026-03-08T03:21:55Z",
  "items": []
}
```

`items[]` fields:

- Required:
  - `title` (string)
  - `wikidataId` (IRI string to Wikidata page)
  - `types` (string array)
- Optional:
  - `description` (string)
  - `homepage` (IRI string)
  - `licenses` (string array)
  - `latestVersion` (string)
  - `releaseDate` (ISO date string)
  - `relatedTools` (array projected from qualified `okg:relatedTo` links)

## Ontology Documentation

Schema source: https://openknowledgegraphs.com/ontology.ttl

The schema deliberately excludes controlled-term instances and dataset descriptions. Those live in the SKOS vocabulary files and `sources.ttl`, respectively. Existing `okg:*` catalog predicates remain the compatibility contract; selected exact relationships are aligned to Dublin Core and schema.org, while the architecture directly uses or references SKOS, DCAT, PROV-O, VANN, and DOAP where appropriate, without wholesale co-emission into catalog instances.

### Classes

| Class | Description |
| --- | --- |
| `okg:Resource` | Base class for all catalog resources |
| `okg:Ontology` | Ontology resources |
| `okg:ControlledVocabulary` | Controlled vocabulary resources |
| `okg:Taxonomy` | Taxonomy resources |
| `okg:Software` | Software/tooling resources |
| `okg:License` | License nodes attached to resources |
| `okg:Category` | Compatibility class for domain-category SKOS concepts |
| `okg:SoftwareType` | Compatibility class for software-type SKOS concepts |
| `okg:SourceClassMapping` | Declarative source-class mapping records |
| `okg:SourcePropertyMapping` | Declarative source-property mapping records |

### Core Properties

| Property | Range | Notes |
| --- | --- | --- |
| `okg:title` | `xsd:string` | required; max 1 |
| `okg:wikidataId` | IRI | required; max 1 |
| `okg:description` | `xsd:string` | optional; max 1 |
| `okg:category` | `okg:Category` | optional; max 1 |
| `okg:homepage` | IRI | optional; max 1 |
| `okg:hasLicense` | `okg:License` | optional; multi-valued |
| `okg:partOf` | `xsd:string` | optional; max 1 |
| `dcterms:isPartOf` | IRI | optional; multi-valued canonical parent identity |
| `okg:uses` | IRI | optional; multi-valued source relationship |
| `okg:sourceType` | IRI | exact source `P31` identity; multi-valued |
| `okg:latestVersion` | `xsd:string` | software only; optional; max 1 |
| `okg:releaseDate` | `xsd:date` | software only; optional; max 1 |
| `okg:licenseName` | `xsd:string` | license node label |

SHACL constraints are defined in `okg:ResourceShape`, `okg:SoftwareShape`, and related shapes in `ontology.ttl`.

### Related-resource scoring

`scripts/related_resources.py` computes `okg:relatedTo` independently within each catalog. Every emitted link requires at least one identity-based structural signal and a score of at least 60. Results are directional, capped at five per subject, and ordered by descending score followed by canonical URI.

| Signal | Weight | Qualification role |
| --- | ---: | --- |
| Direct `dcterms:isPartOf` relationship between records | 120 | structural |
| Direct source `P2283` / `okg:uses` relationship between records | 120 | structural |
| Exact shared parent/project IRI that resolves to a cataloged resource and has at most 6 members | 100 | structural |
| Same canonical repository | 90 | structural |
| Same specific namespace base | 70 | structural |
| Exact shared source `P31` type with 2–6 catalog members | 65 | structural |
| Exact shared creator or maintaining organization | 55 | structural; requires corroboration to reach 60 |
| Shared category | 4 | corroborating only |
| Shared software type | 4 | corroborating only |
| Shared RDF type | 3 | corroborating only |
| Shared programming language | 4 | corroborating only |
| Shared license | 2 | corroborating only |
| Text-token Jaccard similarity of at least 0.25 | 3 | corroborating only |

Repository normalization ignores HTTP versus HTTPS, query/fragment suffixes, a trailing slash, and `.git`, but never equates different repositories merely because they share an owner. Namespace matching requires the same scheme, authority, and complete base path; a shared host alone is insufficient. Shared parent/project identity qualifies only when the parent resolves to an eligible catalog resource and has at most six members across both catalogs. Exact source types likewise qualify only when shared by two to six comparable records; broader types are non-discriminating and suppressed. These limits prevent external registry, aggregator, topic, collection, and broad type buckets from producing false similarity. Direct child-to-parent and `uses` links remain structural regardless of degree. Exact creator identity is structural but needs corroboration to clear the score threshold. Missing values contribute no evidence, and broad or textual signals can never qualify a pair without structural evidence.

Each refresh writes deterministic component scores and qualifying reasons to `build/related-resources.json` by default. CI redirects that private diagnostic to the runner temporary directory and uploads it as a workflow artifact; scoring internals are not added to public RDF or catalog JSON.

### Related-resource page metadata

Generated detail pages project their final, page-surviving `relatedTools` array as
Schema.org `mentions` in the embedded JSON-LD. `okg:relatedTo` remains the
authoritative catalog relationship; `mentions` is only its page-discovery
projection. The value is always an array, including for one related entity, and
preserves the same deterministic order used by catalog JSON and the visible
Related resources or Related tools links. Each node contains the canonical OKG
page as `@id`, the display title as `name`, and a type inferred from that generated
page: `DefinedTermSet` for `/resource/` and `SoftwareApplication` for `/software/`.
Targets pruned by page eligibility are absent from all page projections.

## CI/CD Pipeline

### Data Refresh Workflow

File: `.github/workflows/update-data.yml`

- Trigger: daily at `0 6 * * *` (06:00 UTC) + manual dispatch
- Uses the shared `catalog-publication` concurrency queue without canceling an active publication
- Generates the complete catalog in an isolated staging tree and validates it against the last live-verified generation
- Creates and verifies `data/manifest.json`, then commits the complete snapshot only when normalized content changed
- Builds the Pages artifact from that exact commit and performs cache-busting live verification for up to five minutes
- Advances immutable `catalog-generation/<generation-id>` and moving `catalog-current`/`catalog-previous` tags only after live verification
- Automatically redeploys `catalog-current` if candidate deployment or live verification fails

### Manual Rollback Workflow

File: `.github/workflows/deploy.yml`

- Trigger: manual dispatch with an immutable generation ID or Git ref
- Resolves and verifies the target manifest before constructing the Pages artifact
- Deploys and live-verifies the target before atomically moving `catalog-current` and `catalog-previous`
- Does not revert newer repository history; summaries report repository and live Pages state separately

Tag pushes never trigger publication, so pointer maintenance cannot create deployment loops. Git history is the durable snapshot archive; GitHub Actions artifacts are only temporary transfer copies.

### Pull-request Validation Workflow

File: `.github/workflows/validate.yml`

- Runs the complete unit-test suite
- Runs the shared validator against the pull request's base branch
- Validates the reproducible Task 24 Wikidata audit and exact dry-run plan
- Rejects structural, identity, mapping, taxonomy, regression, and output-contract failures before merge

## Fork and Deploy

1. Fork the repository.
2. In your fork, enable GitHub Pages with source set to GitHub Actions.
3. (Optional) Configure a custom domain:
   - add `site/CNAME`
   - set DNS records
   - enable HTTPS in Pages settings
4. If using category classification, add `ANTHROPIC_API_KEY` as a repository secret or environment variable for the workflow runtime.
5. Run `Publish Catalog Generation` manually once to generate, validate, and publish data.
6. Use `Roll Back Catalog Generation` with a generation ID or Git ref when a prior verified snapshot must be restored.

## Migration from Streamlit

The Streamlit app has been removed from `main` (`app.py` no longer exists). See the full migration and feature mapping guide:

- [docs/MIGRATION.md](docs/MIGRATION.md)

Legacy reference data model remains available in `dist/catalog.ttl` (ignored in git).

## Troubleshooting

- `fetch_data.py` fails with HTTP/timeout errors:
  - rerun; the script has retry/backoff for WDQS throttling
- No category assignments added:
  - confirm `ANTHROPIC_API_KEY` is set
  - confirm the relevant SKOS scheme contains definitions and the new record has no assignment in `curation/classifications.ttl`
- Site loads but data is empty locally:
  - serve from repo root and open `http://localhost:8000/site/`
  - verify `data/*.json` exists and is valid JSON
- Workflow runs but no commit is created:
  - normalized catalog content matched the last live-verified generation, so its manifest and timestamps were retained

## FAQ

### Is this only open-source software?

No. The catalog includes both open and proprietary resources if they are represented in Wikidata.

### Are records manually curated?

Primary metadata is sourced from Wikidata queries. Category and software-type assignments are maintained in `curation/classifications.ttl`; the JSON mapping files are regenerated compatibility projections and must not be edited as inputs.

### Why do some fields appear missing?

Wikidata coverage is uneven. Optional fields (homepage, license, version, release date, category) are omitted when unavailable.

### How often is data refreshed?

Daily at 06:00 UTC via GitHub Actions, plus manual runs.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution workflow and quality checks.

## Data and License

- Data source: Wikidata (CC0)
- Code license: Apache License 2.0 (see `LICENSE`)
