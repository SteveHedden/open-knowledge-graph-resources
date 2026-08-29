# Contributing

Thanks for contributing to Open Knowledge Graph Resources.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Common Tasks

Refresh data:

```bash
python scripts/fetch_data.py
```

Classify categories (optional, requires Anthropic key):

```bash
export ANTHROPIC_API_KEY=your_key_here
python scripts/classify_categories.py
```

Local site preview:

```bash
python -m http.server 8000
```

Open: `http://localhost:8000/site/`

## Quality Checks Before PR

```bash
python3 -m py_compile scripts/fetch_data.py scripts/category_classifier.py scripts/classify_categories.py
node --check site/app.js
python3 - <<'PY'
from rdflib import Graph
Graph().parse('ontology.ttl', format='turtle')
print('ontology.ttl parse ok')
PY
```

If your change affects generated datasets, include updated files in `data/`.

## Adding a Resource

All catalog resources are sourced from Wikidata — there is no direct way to add one to OKG
itself. See [`docs/adding-resources-through-wikidata.md`](docs/adding-resources-through-wikidata.md)
for the full workflow (identity resolution, notability, ingestible classes, and verification),
and use the "Resource batch" issue template to propose and track a batch of candidates.

## Pull Request Guidelines

- Keep PRs focused and scoped to one task/theme.
- Include a short summary of behavior changes.
- Link related issues/tasks where relevant.
- Add or update docs when behavior, workflows, or schema changes.
- Preserve optional-field behavior in JSON output (omit missing keys).
- Treat Wikidata edits as public external mutations: commit and review an evidence-backed dry-run audit first, and never execute live edits without separate explicit approval.

## Data and Schema Guidelines

- Do not introduce required fields unless explicitly approved.
- Preserve current SHACL constraints unless the task requires updates.
- Keep `wikidataId` as an IRI-valued field.
- Prefer deterministic output ordering where possible.
- Declare source eligibility markers, exclusions, and reviewed exceptions in `sources.ttl`; do not duplicate their QIDs in Python or turn labels, descriptions, or URL shapes into automatic exclusions.

## Workflow and Deployment Notes

- Data refresh workflow (`update-data.yml`) follows a successful scheduled jobs refresh, with an independent daily 06:23 UTC fallback.
- The refresh workflow stages, validates, manifests, commits, deploys, and live-verifies one complete generation under the shared publication concurrency group.
- `deploy.yml` is a manual rollback path accepting a generation ID or Git ref; successful rollback moves catalog pointers without reverting repository history.
- Do not move `catalog-generation/*` tags. They are immutable successful-publication records.
- Use `openknowledgegraphs.com` URLs in docs and public references.

## Reporting Problems

- Open an issue with:
  - repro steps
  - expected vs actual behavior
  - relevant logs/screenshots
  - affected files/workflow runs
