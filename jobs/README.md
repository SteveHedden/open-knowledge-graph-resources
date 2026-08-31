# KG Jobs

`jobs/` contains the production jobs ingestion, classification, reconciliation,
validation, and local review tooling. The public snapshot remains at
`data/jobs/` so the existing site and transactional jobs manifest keep their
stable deployment contract.

## Authoritative RDF

- Root `sources.ttl` declares all job sources. Registry-enabled aggregator APIs
  are independent `dcat:Dataset` services. Organization-owned career feeds are
  `okg:CareerSource` and `dcat:DataService` resources with exactly one
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

Aggregator discovery and production admission are separate RDF decisions.
`kgjobs:searchEnabled true` permits deliberate local retrieval;
`kgjobs:productionEnabled true` is additionally required by both the default
pipeline and scheduler. The production aggregator set is Adzuna, Arbeitnow,
Himalayas, Jobicy, and Jooble. Remotive remains local-review-only.

A first-party source is production-eligible
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

`.github/workflows/update-jobs.yml` runs nightly at `03:00 UTC` with a 150-minute
timeout. Its successful scheduled completion triggers catalog generation, while
an independent `06:23 UTC` catalog cron remains as a staggered fallback. All 34
production sources are derived from `sources.ttl` and fetched in deterministic
waves of at most four isolated processes and 128 declared per-batch requests. A source's
`maxRequestsPerRun` bounds its complete invocation; `maxRequestsPerBatch` is the
separate scheduling weight for providers that page or hydrate in multiple
batches. A manual dispatch may name one production-cleared source.
`dry_run=true` fetches and validates without copying to `data/jobs/`, committing,
or pushing.

Task 41's fixed 20-organization review and live decision counts are recorded in
`audits/task41-commercial-source-audit.json`. Its viable sources remain
`local-review-only`; they are intentionally absent from the scheduled source
set and public snapshot until manager approval changes both sides of the RDF
approval gate.

Task 42's fixed 107-organization monitoring review is recorded in
`audits/task42-organization-source-audit.json`; its bounded landing/deeper-link
retrieval evidence is in `audits/task42-careers-discovery.json`. Every one of
the 85 organizations with a recorded careers page is either linked to a
successful full-ingestion source or has a specific external
blocker. The compact, durable replay inputs for all 17 review sources are in
`audits/task42-live-review-inputs.zip`, with checksums and source contracts in
`audits/task42-live-review-inputs-manifest.json`; the working runtime remains
ignored. Final publication approval is recorded in
`audits/task42-production-approval.json`: all 17 sources are production-enabled,
but only `qualified` first-party records may enter the public snapshot. The
EMBL-EBI BioImaging Bioinformatician and UMD AI Research Assistant are the two
approved current postings; EMBL's Bioinformatician remains review-only.

Issue 63 / Task 43 adds eight evidence-reviewed peer employers to the canonical
organization registry. After manager approval, six exact, bounded sources are
production-enabled:
JPMorganChase Oracle Recruiting, Accenture/CrowdStrike/Capital One Workday,
Amazon Jobs, and SAP SuccessFactors RMK. Siemens and Bloomberg are deferred
because their Avature pages did not expose a deterministic public filtered
contract. The compact live review (485 IDs, titles, links, classifications, and
raw hashes) is tracked in `audits/task43-peer-employer-review.json`; regenerate
it from the ignored isolated runtimes with
`python jobs/scripts/build_task43_audit.py`. The approval is recorded in
`audits/task43-production-approval.json`; only qualified first-party records are
eligible for publication through the existing nightly pipeline.

The production runner covers the five aggregators, the prior 12 first-party
sources, Task 42's 17 sources, and Task 43's six approved sources. It enforces a
720-second per-source wall-clock
cap, validates successful workers by replaying their raw responses into one
candidate runtime, retains failed sources' last-good normalized and raw data,
and swaps the complete runtime atomically. The workflow then atomically promotes
that validated directory into `data/jobs/`. In the same nightly invocation, the
nonpublishing discovery monitor checks all 68 unresolved careers pages; the 22
organizations without careers pages remain uncovered. Its results are uploaded
as diagnostics and never contribute jobs. Thirteen source batches plus the explicit
12-minute workflow-overhead allowance produce a 10,080-second (168-minute)
worst-case budget, leaving 12 minutes of headroom inside the 180-minute timeout. The machine-readable
contract is in
`audits/task42-nightly-operational-plan.json`.

First-party normalized snapshots and raw payloads are restored and saved through
a private Actions cache so isolated failures on a later ephemeral runner retain
their previous evidence. They are also uploaded as a 30-day diagnostic artifact,
but remain excluded from the public Pages `data/jobs/` directory and manifest.

The schedule and approval wiring are ready for final review but have not been
committed, pushed, dispatched, or deployed. Run the complete nightly design
locally with:

```bash
python jobs/scripts/task42_nightly.py --live --runtime-dir jobs/runtime
```

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
