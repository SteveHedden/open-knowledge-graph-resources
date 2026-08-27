# KG Jobs

`jobs/` contains the production jobs ingestion, classification, reconciliation,
validation, and local review tooling. The public snapshot remains at
`data/jobs/` so the existing site and transactional jobs manifest keep their
stable deployment contract.

## Authoritative RDF

- Root `sources.ttl` declares all job sources. The five aggregator APIs are
  independent `dcat:Dataset` services. The twelve organization-owned career
  feeds are `okg:CareerSource` and `dcat:DataService` resources with exactly one
  `dcterms:publisher`, a human careers `dcat:landingPage`, a machine
  `dcat:endpointURL`, and a `dcterms:conformsTo` contract.
- Root `organizations.ttl` is the only authoritative organization registry. It
  contains 139 accepted organizations and directly defines the six kind and
  nine ecosystem-role classes. A permanent slug IRI is an identity reservation.
- Root `data/organizations.json` is generated from `organizations.ttl`; it is
  not a second curation authority.
- `vocabularies/kg-jobs.ttl` defines the shared classifier vocabulary and the
  declarative first-party contextual qualification policy.
- `ontology.ttl` defines jobs-specific terms and SHACL constraints.

The organization audit page under `site/organizations.html` is local review
tooling and is not included in the public site.

## Source admission

Aggregator admission is unchanged. A first-party source is production-eligible
only when all of these RDF facts agree:

1. its publisher is active, evidence-reviewed, and has
   `okg:jobsProductionEnabled true`;
2. the career source is evidence-reviewed; and
3. its republication status is `production-approved`.

All network adapters enforce exact hosts and paths plus registry-declared
request, response-byte, record, timeout, and 24-hour refresh bounds. A source
failure leaves the preceding complete runtime snapshot and raw evidence intact.

## Classification

The existing strong controlled-vocabulary route is shared by aggregators and
first-party records. Aggregator behavior is otherwise unchanged.

For approved first-party sources only, the classifier preserves the complete
raw description but evaluates a deterministic job-specific copy after applying
reviewed RDF-declared boilerplate boundaries. If no strong term qualifies the
posting, the contextual route requires:

- a concrete opening, not a placeholder or talent-pool record;
- an approved role family;
- at least two distinct, unnegated contextual KG/product concepts; and
- no generic-role exclusion unless strong job-specific evidence already passed.

Source or employer identity can select a policy but never counts as independent
role evidence. Only first-party `qualified` records enter the public snapshot;
`review` and `not_match` remain in retained source diagnostics. Aggregator
publication policy is unchanged.

The frozen Task 40 review corpus is
`tests/fixtures/first-party-run-183/manifest.json`. It pins GitHub Actions run
183, run ID 33034495838, artifact 9631740022, archive SHA-256, every extracted
file hash, and the exact 82-record baseline (13 qualified, 50 review, 19
not-match). Regenerate its 69-record withheld audit with:

```bash
python jobs/scripts/audit_first_party_qualification.py
```

## Refresh cadence

`.github/workflows/update-jobs.yml` runs hourly. Each source is still governed
by its own RDF minimum refresh interval; all approved first-party feeds use 24
hours. A manual dispatch can select any one of the 17 production sources or
`all`. `dry_run=true` fetches and validates without copying to `data/jobs/`,
committing, or pushing.

Run one first-party source locally without publication:

```bash
python jobs/scripts/live_pipeline.py --live \
  --source first-party-graphwise \
  --runtime-dir /tmp/okg-graphwise-dry-run
```

## Development and verification

From the repository root:

```bash
python jobs/scripts/organization_registry.py --check
python jobs/scripts/audit_first_party_qualification.py --check
python -m pytest jobs/tests -q
python -m unittest discover -s tests -v
python scripts/catalog_snapshot.py verify --root .
git diff --check
```

The pull-request validation workflow installs `jobs/requirements.txt` and runs
the jobs suite alongside the root catalog, API, and MCP suites.

The generated synthetic fixture outputs in `jobs/data/` exercise deterministic
JSON/RDF generation. The deployable snapshot is only `data/jobs/`; Task 40 does
not alter that snapshot until the proposed additions receive manager approval.
