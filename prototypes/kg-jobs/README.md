# KG Jobs Discovery Prototype

A contained, network-free proof of concept for identifying knowledge-graph-related
job postings **from posting content**, not from a preselected employer list.

Scope: everything lives under `prototypes/kg-jobs/`. Nothing outside this directory
is touched. There is no production jobs category, no live job APIs, no scraping, no
Wikidata edits, and no commits/pushes/deploys performed by this prototype's own
code. Synthetic fixtures are plainly labeled as test data everywhere they appear.

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
├── scripts/classifier.py     Reusable, vocabulary-driven classifier
├── scripts/generate.py       Fixtures -> RDF + JSON
├── site/index.html           Static reviewer page (reads data/jobs.json)
└── tests/                    pytest suite (SHACL, fixtures, determinism)
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

The page fetches `../data/jobs.json`, shows the "Local synthetic prototype —
not live jobs" banner, and lets a reviewer filter Qualified / Review / Not
match, with the matched phrase, concept, scheme, and source field shown for
every piece of evidence (negated matches are shown struck through, so a
reviewer can see what was found but excluded). It is not linked from the
production OKG site.

## Known limitations / deliberately deferred

This prototype answers one question only — *can KG relevance be detected from
posting content alone* — and defers everything about running this in
production:

- **Live source discovery**: which job boards or ATSs to read from, and how.
- **Source terms and access rules**: API vs. scraping, rate limits, ToS.
- **Crawling or APIs**: no fetcher exists; `sources.ttl` only models the
  *pattern* for a future real source registry.
- **Deduplication**: the same posting appearing across multiple boards.
- **Expiry**: postings going stale or being pulled.
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
