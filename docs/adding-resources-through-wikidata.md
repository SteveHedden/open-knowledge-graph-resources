# Adding Resources Through Wikidata

Open Knowledge Graphs (OKG) does not maintain its own resource records. Everything in the
catalog is pulled from Wikidata by `scripts/fetch_data.py` on a daily refresh. This means the
only way to add, fix, or remove a resource from OKG is to **create or edit the corresponding
Wikidata item**, then let (or trigger) a refresh pick it up.

This guide covers how to do that responsibly, whether you're a human contributor or an agent
working on a batch of candidates. It applies to both humans and AI agents; agents should follow
it exactly, not improvise around it.

## Thematic batches and statuses

Research and implementation work should be organized into coherent thematic batches, with one
GitHub issue per batch. A batch might cover agent memory and context graphs, KG construction,
SHACL validation, ontology engineering, or SPARQL tooling. Aim for roughly 8–15 candidates when
that fits the topic, but favor a coherent scope over an arbitrary count.

Every candidate has one of five statuses:

| Status | Meaning |
|---|---|
| **Candidate** | A potentially useful resource that has not yet been researched. |
| **In review** | Its identity, relevance, OKG coverage, and Wikidata status are being checked. |
| **Approved** | It is a suitable standalone OKG resource; Wikidata/ingestion work remains. |
| **Included** | It has been verified in the published OKG catalog. |
| **Rejected** | It will not be added; the issue must state the short reason. |

Before opening a batch issue, do enough preliminary research to identify obvious duplicates,
organizations, components, forks, existing OKG records, and likely Wikidata matches. It is fine
for an issue to contain a mixture of statuses. The issue is an implementation handoff: another
contributor or agent should be able to pick it up, complete any remaining research, make the
approved Wikidata changes, refresh OKG, and verify the results without repeating the initial
discovery work.

Mark a resource **Included** only after verifying it in OKG, not merely after editing Wikidata.
Use **Rejected** for duplicates, non-resource organizations, non-standalone components, resources
outside OKG's scope, and candidates lacking sufficient evidence. Put the reason in the issue's
Notes column rather than inventing additional statuses.

## 1. Resolve identity before doing anything else

Before creating a new Wikidata item or touching an existing one, confirm what you're looking at:

1. **Search Wikidata by name and known aliases first.** Use the Wikidata search UI or
   `wbsearchentities` for the resource's name, acronym, and any alternate names/former names.
   Standards and vocabularies are especially prone to duplicate items under different acronyms
   (e.g. "HS" vs. "Harmonized System").
2. **Search by URL.** If the resource has an official website, namespace URI, or source
   repository, search Wikidata for items carrying that URL in `P856` (official website),
   `P1324` (source code repository), or `P7510` (URI).
3. **Search the OKG catalog itself** (`data/ontologies.json` / `data/software.json`, or the live
   site's search) to confirm it isn't already present under a different QID or label.
4. **If a candidate item already exists**, do not create a duplicate. Work out whether the gap
   is (a) missing/incomplete claims on the existing item, or (b) a classification problem (see
   §3) that's keeping `fetch_data.py` from picking it up.
5. **If no item exists**, only then proceed to create one (§5).

Getting this step wrong is the single most common failure mode: duplicate items fragment
Wikidata and eventually fragment the OKG catalog too.

## 2. Resources vs. organizations, components, and forks

Wikidata items frequently exist for the *organization* or *ecosystem* around a resource without
one existing for the resource itself, or vice versa. Keep these distinct:

- **The resource** — the ontology, vocabulary, taxonomy, knowledge graph, or software artifact
  itself. This is what OKG catalogs.
- **The maintaining organization** — e.g. the standards body, foundation, or company. This is
  *not* itself an ontology/vocabulary/software item, even if the org's Wikidata item is the one
  that best matches your search. Example from issue #20: **UN/CEFACT** and **GLEIF** each have
  Wikidata items, but those items are classed as organizations — the standards body, not the
  vocabulary it publishes. Don't reclassify an organization's item as a resource; find or create
  the item for the resource itself.
- **A component or sub-module** — e.g. a single vocabulary module that's part of a larger
  ontology suite. Use `P361` (part of) to relate it to the parent rather than treating it as a
  standalone catalog entry, unless it's independently notable and separately maintained.
- **A fork or derivative** — forks of an existing tool/ontology are usually not separate catalog
  entries unless they've diverged into an independently notable project (different maintainers,
  different scope, its own adoption). When in doubt, note the fork relationship (`P361`, or a
  description mentioning the upstream project) rather than adding it as an unrelated entry.

When you're not sure whether something is a distinct resource, organization, component, or fork,
say so explicitly in your research notes and default to the more conservative choice (i.e., don't
create a new item).

## 3. OKG-ingestible classes

`scripts/fetch_data.py` only pulls items whose `wdt:P31/wdt:P279*` (instance of / subclass of)
resolves to one of these Wikidata classes:

| Wikidata QID | OKG class |
|---|---|
| Q324254 | Ontology |
| Q1469824 | ControlledVocabulary |
| Q8269924 | Taxonomy |
| Q33002955 | KnowledgeGraph |
| Q7095059 | OntologyLanguage |
| Q7247749 | ControlledVocabulary (product classification) |

Software resources are pulled separately, matched against:

| Wikidata QID | Meaning |
|---|---|
| Q124653107 | semantic web software |
| Q140639670 | AI agent memory software |
| Q595971 | graph database |
| Q137916409 | graph database management system |

**This is the most common reason a real, notable resource never shows up in OKG:** its Wikidata
item exists but its `P31` value doesn't resolve (via subclass chain) to any of the classes above.
Issue #20 is a working example — ISIC was classed as "industry classification scheme," which did
not resolve to an OKG target class, while LEI was correctly modeled as an identifier type and was
not itself the ontology resource the research was seeking. A catalog refresh alone could not
surface either one, but the correct resolutions differed: ISIC could defensibly receive an
additional `ControlledVocabulary` classification, while LEI should not be reclassified merely to
force ingestion. The review instead identified GLEIF's distinct RDF ontology modules. (By
contrast, product classification — Q7247749 — is already directly ingestible, as the table above
shows; UNSPSC and CPC needed no reclassification once identified.)

Because this kind of edit touches an existing, community-maintained item rather than one you
created, treat it with more caution than creating a net-new item (see §6). Only add a `P31`
claim when the new class is clearly defensible and sourceable — not just convenient for OKG's
ingestion query.

## 4. Wikidata notability and sourcing

Before creating any new item, make sure it would survive on Wikidata on its own merits, not just
because OKG wants to catalog it:

- A project's own spec page or website (`P856`) establishes *what the resource is* — it's a
  primary source, useful for factual claims but not evidence of notability on its own.
  Notability needs something written about the resource by someone other than its maintainers:
  academic citation, standards-body adoption, significant independent coverage, or an existing
  citation elsewhere on Wikidata/Wikipedia. A GitHub README from the project itself doesn't
  count as independent, even though it's a fine source for factual claims like license or repo
  URL.
- Prefer sourcing claims (`P856`, `P1324`, etc.) with references, and keep the independent
  notability source separate from the primary/project source, so the item holds up to scrutiny
  from other Wikidata editors.
- If you can't find independent sourcing, don't create the item — flag it as "insufficient
  notability" in your research notes and move on. This is a valid, expected outcome, not a
  failure.

## 5. Creating new items and recommended properties

When identity resolution (§1) confirms no item exists and notability (§4) is satisfied, create
the item and populate it with the properties `fetch_data.py` actually reads. Missing a property
just means that field is blank in the catalog, but these are the ones worth setting:

**For an ontology / vocabulary / taxonomy / knowledge graph:**
- `P31` — instance of (one of the classes in §3, or a subclass of one)
- `P856` — official website
- `P1324` — source code repository (if applicable)
- `P7510` — URI (namespace URI, for RDF/OWL vocabularies)
- `P275` — license
- `P361` — part of (parent ontology/organization, if applicable)
- `P170` / `P50` — creator / author

**For software:**
- `P31` — instance of (Q124653107 semantic web software, Q140639670 AI agent memory
  software, Q595971 graph database, or Q137916409 graph database management system,
  as appropriate)
- `P856` — official website
- `P1324` — source code repository
- `P275` — license
- `P361` — part of (if it's part of a larger project/org)
- `P178` / `P170` / `P50` — developer / creator / author
- `P348` — software version (with a qualifier date where possible)

A note on `creator`/`author`/`developer`: OKG preserves human credits and also audits apparent
organization-valued `P170`, `P50`, and `P178` relationships for its reviewed organization registry.
Use the Wikidata property that describes the real relationship; do not substitute `creator` merely
to make an organization appear. The registry validates that the value is actually an organization
and keeps the resource-specific role and source property, so ambiguous or mistyped values may be
marked unresolved or rejected rather than published as organizations.

Follow the existing script pattern for bot-driven creation (see §6) rather than hand-editing
items one field at a time in the Wikidata UI, especially for batches.

## 6. Cautious editing — human and agent, bot policy

- **Prefer scripted, reviewable edits over ad-hoc UI clicks**, especially for batches. The
  established pattern in this repo is a one-off `scripts/create_<resource>_item.py` script per
  new item: it logs in as the project's Wikidata bot account, creates the item, and adds claims
  one at a time (`P31` first, then license/language/repo/website/version as known). Support a
  `--dry-run` mode so the script's intended writes can be reviewed before they hit Wikidata.
- **Never bulk-create or bulk-edit without a dry run first.** Wikidata edits are public,
  attributed, and scrutinized by the broader editor community — a bad batch is a reputational
  problem, not just a data problem.
- **Editing existing, community-maintained items (Track B–style P31 additions) deserves more
  scrutiny than creating net-new items (Track A–style).** Wikidata items have no single owner,
  but other editors may be actively maintaining or watching one. Only add claims that are
  independently defensible, cite sources where you can, and avoid removing or overwriting
  existing classifications — add to them.
- **Bot accounts should follow Wikidata's bot policy**: run under an approved bot flag/account
  where required, avoid high-volume edits without a Wikidata bot approval for sustained/large
  batches, identify the bot's owner/contact in its edit summary or user agent, and pause if
  reverted rather than re-applying the same edit.
- **Agents should not merge their own Wikidata edits into a PR-worthy "done" state without a
  human reviewing the diff of claims added**, particularly for Track B–style edits to
  externally-maintained items.

## 7. Post-refresh verification

After new items are created or existing items are reclassified, they won't appear in OKG until
the next catalog refresh:

1. Run `python scripts/fetch_data.py` (or wait for the daily `update-data.yml` GitHub Action).
2. Confirm the resource now appears in `data/ontologies.json` / `data/software.json` (or the
   corresponding `.ttl` file) with the QID you created/edited.
3. Spot-check that the fields you expect (website, license, category, software type) are
   populated, not just that the record exists.
4. Confirm categorization: new items go through `scripts/classify_categories.py` (backfill) or
   the incremental classification step inside `fetch_data.py` — check `data/categories.json` for
   a sane category rather than leaving it unclassified.
5. If a resource you expected to see is still missing after a refresh, re-check §3 first — it's
   almost always a `P31` classification issue, not a bug in `fetch_data.py`.
6. Note the outcome (created, reclassified, excluded for notability, deferred) in the tracking
   issue so the batch has a clear paper trail — see the resource-batch issue template for the
   expected structure.

## Scope note

This workflow and its issue template cover **Wikidata research, modeling, and catalog
ingestion only**. Social-media promotion of newly added resources is a separate, non-GitHub
workflow and should not be folded into these issues or this guide.
