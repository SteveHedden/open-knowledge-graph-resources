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

Optional automated classification of previously unseen records uses Anthropic during the refresh:

```bash
export ANTHROPIC_API_KEY=your_key_here
python scripts/fetch_data.py
```

Without the key, the refresh preserves and applies existing RDF curation but leaves newly discovered records unclassified.

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
| `okg:latestVersion` | `xsd:string` | software only; optional; max 1 |
| `okg:releaseDate` | `xsd:date` | software only; optional; max 1 |
| `okg:licenseName` | `xsd:string` | license node label |

SHACL constraints are defined in `okg:ResourceShape`, `okg:SoftwareShape`, and related shapes in `ontology.ttl`.

## CI/CD Pipeline

### Data Refresh Workflow

File: `.github/workflows/update-data.yml`

- Trigger: daily at `0 6 * * *` (06:00 UTC) + manual dispatch
- Installs Python dependencies
- Runs `python scripts/fetch_data.py`
- Commits changed data files as `github-actions[bot]` with:
  - `chore(data): refresh catalog from Wikidata`

### Deployment Workflow

File: `.github/workflows/deploy.yml`

- Trigger: pushes to `main` affecting `site/**`, `data/**`, `curation/**`, `vocabularies/**`, `ontology.ttl`, `sources.ttl`, or the workflow file
- Builds Pages artifact from:
  - `site/` (frontend)
  - `data/` (datasets)
  - `curation/` (maintained classification RDF)
  - `vocabularies/` (SKOS schemes)
  - `ontology.ttl` (schema)
  - `sources.ttl` (source registry and mappings)
- Deploys via GitHub Pages actions

## Fork and Deploy

1. Fork the repository.
2. In your fork, enable GitHub Pages with source set to GitHub Actions.
3. (Optional) Configure a custom domain:
   - add `site/CNAME`
   - set DNS records
   - enable HTTPS in Pages settings
4. If using category classification, add `ANTHROPIC_API_KEY` as a repository secret or environment variable for the workflow runtime.
5. Run `Update Catalog Data` manually once to generate/refresh data.
6. Push a change to any deployed artifact path listed above to trigger deploy.

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
  - no data diff detected in tracked outputs

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
