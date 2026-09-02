"""Network-free acceptance coverage for Task 46 cross-source enrichment."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog_mentions import (  # noqa: E402
    add_catalog_mentions,
    build_match_index,
    load_match_index,
)
from first_party_classifier import (  # noqa: E402
    FirstPartyPolicy,
    SourceStripPolicy,
    job_specific_text_projection,
    load_first_party_policy,
)
from first_party_sources import load_production_first_party_sources  # noqa: E402
from first_party_pilot import preserve_partial_pilot_records  # noqa: E402
from job_normalization import add_job_tags  # noqa: E402
from rebuild_catalog_mentions import build_candidate  # noqa: E402
import task46_review_rebuild  # noqa: E402


def index():
    return load_match_index(REPO_ROOT, ROOT / "catalog-mention-policy.json")


def mentions(description: str, **extra):
    record = {"id": "fixture", "title": "Engineer", "description": description, **extra}
    return add_catalog_mentions([record], index())[0]["catalogMentions"]


def test_tq_surfaces_converge_and_preserve_the_earliest_phrase():
    for surface in (
        "TopQuadrant",
        "TopBraid EDG",
        "TopBraid Enterprise Data Governance",
        "TQ Data Foundation",
    ):
        result = mentions(f"Experience with ontology tools including {surface}.")
        assert [(row["qid"], row["matchedPhrase"]) for row in result] == [
            ("Q140443441", surface)
        ]
        assert result[0]["canonicalUrl"].endswith("/software/tq-data-foundation/")

    result = mentions(
        "Ontology tools: TopQuadrant, TopBraid EDG, and TQ Data Foundation."
    )
    assert [(row["qid"], row["matchedPhrase"]) for row in result] == [
        ("Q140443441", "TopQuadrant")
    ]


def test_distinct_topbraid_products_do_not_collapse_into_tq():
    result = mentions("Use TopBraid Composer and TopBraid SHACL API as ontology tools.")
    assert {row["qid"] for row in result}.isdisjoint({"Q140443441"})
    # Composer is not currently page-backed; the SHACL API is and remains distinct.
    assert {row["qid"] for row in result} == {"Q140676799"}


def test_context_aliases_ignore_metadata_urls_emails_and_company_boilerplate():
    assert mentions("Read https://data.world/docs and email help@data.world.") == []
    assert mentions("Read data.world/docs before applying.") == []
    assert mentions("Experience with tools documented at careers.data.world.") == []
    assert mentions("TopQuadrant announced an office move.") == []
    assert mentions(
        "We are TopQuadrant, a knowledge graph company.",
        hiringOrganization="TopQuadrant",
        firstParty=True,
    ) == []
    assert mentions(
        "At TopQuadrant, our knowledge graph tools help customers.",
        hiringOrganization="TopQuadrant",
        firstParty=True,
    ) == []
    assert mentions(
        "The data.world Data Catalog team builds our platform.",
        hiringOrganization="data.world",
        firstParty=True,
    ) == []
    assert mentions(
        "Required ontology tools include TopQuadrant and Data.World."
    )[-2:][0]["qid"] == "Q140443441"


def test_data_world_exact_reviewed_case_variants_only():
    for surface in ("data.world", "Data.World"):
        result = mentions(f"Experience with linked data tools such as {surface}.")
        assert [(row["qid"], row["matchedPhrase"]) for row in result] == [
            ("Q141112432", surface)
        ]
    for rejected in ("DATA.WORLD", "Data.world", "dataworld"):
        assert mentions(f"Experience with linked data tools such as {rejected}.") == []


def test_unadmitted_anzograph_and_neptune_never_emit_pills():
    result = mentions(
        "Ontology and graph database tools: AnzoGraph, Amazon Neptune, AWS Neptune, Neptune."
    )
    assert not ({"Q124653370", "Q48843359", "Q124653384"} & {row["qid"] for row in result})


def test_neptune_canonical_choice_preserves_both_reserved_uris_without_fake_redirect():
    registry = json.loads((REPO_ROOT / "data/uri_registry.json").read_text())
    assert registry["software"]["Q48843359"] == "amazon-neptune"
    assert registry["software"]["Q124653384"] == "aws-neptune"
    assert not (REPO_ROOT / "site/software/aws-neptune/index.html").exists()
    audit = json.loads((ROOT / "audits/task46-page-admission.json").read_text())
    decision = audit["neptune"]["canonicalDecision"]
    assert decision["selectedQid"] == "Q48843359"
    assert decision["rejectedDuplicateQid"] == "Q124653384"
    assert audit["neptune"]["uriCompatibility"]["previouslyPublishedPageFound"] is False


def test_admitted_neptune_rejects_energy_planet_and_unrelated_proper_names():
    software = {"items": [{
        "title": "Amazon Neptune",
        "wikidataId": "https://www.wikidata.org/wiki/Q48843359",
        "aliases": [],
    }]}
    page_qids = {"resource": {}, "software": {"Q48843359": "amazon-neptune"}}
    policy = {
        "schemaVersion": 1, "shortAcronymAllowlist": [], "denylist": [],
        "reviewedAliases": {}, "disambiguationOverrides": {},
        "pageGatedAliases": {
            "AWS Neptune": {"dataset": "software", "qid": "Q48843359"},
            "Neptune": {"dataset": "software", "qid": "Q48843359"},
        },
        "contextRequiredAliases": ["Neptune"], "employerGuardAliases": [],
        "exactCaseVariants": {
            "Amazon Neptune": ["Amazon Neptune"],
            "AWS Neptune": ["AWS Neptune"], "Neptune": ["Neptune"],
        },
    }
    admitted = build_match_index({"items": []}, software, page_qids, policy)
    for description in (
        "Neptune Energy uses graph database tools.",
        "The planet Neptune appears in our graph database.",
        "A project named Neptune uses graph database tools.",
        "Our engineer Neptune works on graph database tools.",
    ):
        assert add_catalog_mentions([{
            "id": description, "title": "Engineer", "description": description,
        }], admitted)[0]["catalogMentions"] == []
    result = add_catalog_mentions([{
        "id": "context", "title": "Engineer",
        "description": "Graph database tools include Neptune.",
    }], admitted)[0]["catalogMentions"]
    assert [(row["qid"], row["matchedPhrase"]) for row in result] == [
        ("Q48843359", "Neptune")
    ]


def test_first_party_projection_removes_boilerplate_without_changing_display_text():
    source = "https://example.test/source/topquadrant"
    policy = FirstPartyPolicy(
        role_families=(), excluded_title_terms=(), placeholder_title_terms=(),
        contextual_terms=(), source_policies={source: SourceStripPolicy(
            prefix_markers=("START JOB",), suffix_markers=("ABOUT COMPANY",),
            identity_concepts=frozenset(),
        )},
    )
    record = {
        "id": "first-party", "title": "Engineer", "firstParty": True,
        "sourceDataset": source,
        "description": "TopQuadrant is our company. START JOB Use RDF tools. ABOUT COMPANY Data.World knowledge graph company.",
        "hiringOrganization": "TopQuadrant",
    }
    enriched = add_catalog_mentions(
        [record], index(),
        text_projection=lambda row: job_specific_text_projection(row, policy),
    )[0]
    assert enriched["description"] == record["description"]
    assert [row["qid"] for row in enriched["catalogMentions"]] == ["Q54872"]


def test_first_party_projection_fails_closed_without_explicit_source_policy():
    policy = FirstPartyPolicy(
        role_families=(), excluded_title_terms=(), placeholder_title_terms=(),
        contextual_terms=(), source_policies={},
    )
    record = {
        "id": "unknown-source", "title": "TopQuadrant SPARQL Engineer", "firstParty": True,
        "sourceDataset": "https://example.test/unreviewed",
        "description": "Use TopQuadrant and SPARQL.",
    }
    projection = lambda row: job_specific_text_projection(row, policy)
    enriched = add_job_tags(
        add_catalog_mentions([record], index(), text_projection=projection)
    )[0]
    assert enriched.get("catalogMentions") == []
    assert enriched["jobTags"] == [{"label": "SPARQL", "matchedPhrase": "SPARQL"}]


def test_every_approved_first_party_source_declares_a_matching_projection_policy():
    policy = load_first_party_policy(ROOT / "vocabularies/kg-jobs.ttl")
    approved = load_production_first_party_sources()
    missing = {
        source.dataset_uri for source in approved.values()
        if source.dataset_uri not in policy.source_policies
    }
    assert missing == set()


def test_fresh_partial_pilot_preserves_unfetched_records_and_overlays_by_id():
    committed = [
        {"id": "a", "firstParty": True, "description": "committed a"},
        {"id": "b", "firstParty": True, "description": "unfetched b"},
        {"id": "c", "firstParty": False, "description": "aggregator c"},
    ]
    refreshed = [{"id": "a", "firstParty": True, "description": "refreshed a"}]
    merged = preserve_partial_pilot_records(refreshed, committed)
    assert [row["id"] for row in merged] == ["a", "b", "c"]
    assert merged[0]["description"] == "refreshed a"
    assert merged[1] == committed[1]
    assert merged[2] == committed[2]


def test_sparql_is_exact_case_sensitive_description_only_and_unlinked():
    positives = add_job_tags([
        {"id": "a", "title": "Engineer", "description": "Query RDF using SPARQL."},
        {"id": "b", "title": "SPARQL Engineer", "description": "Other work."},
    ])
    assert positives[0]["jobTags"] == [{"label": "SPARQL", "matchedPhrase": "SPARQL"}]
    assert "relatedCatalogPage" not in positives[0]["jobTags"][0]
    assert "jobTags" not in positives[1]

    negatives = add_job_tags([
        {"id": str(i), "description": value}
        for i, value in enumerate((
            "sparql", "GeoSPARQL", "SPARQL.js", "SPARQL.JS", "SPARQL.Js",
            "SPARQL.jS", "xSPARQL", "https://example.test/SPARQL",
            "example.test/SPARQL", "SPARQL@example.test",
            "JSON XML SQL",
        ))
    ])
    assert all("jobTags" not in row for row in negatives)


def test_review_rebuild_discards_unsupported_existing_tags():
    record = {
        "id": "existing", "description": "SPARQL is required.",
        "jobTags": [{
            "label": "Cypher", "matchedPhrase": "Cypher",
            "relatedCatalogPage": "https://openknowledgegraphs.com/software/neo4j/",
        }],
    }
    enriched = add_job_tags([record])[0]
    assert [tag["label"] for tag in enriched["jobTags"]] == ["SPARQL"]


def test_pinned_capital_one_record_adds_tq_data_world_and_sparql_only():
    records = json.loads((REPO_ROOT / "data/jobs/jobs.json").read_text())
    pinned = next(row for row in records if row["id"] == "firstparty:first-party-capital-one:R999238")
    enriched = add_job_tags(add_catalog_mentions([pinned], index()))[0]
    qids = [row["qid"] for row in enriched["catalogMentions"]]
    assert qids == [
        "Q54872", "Q1751819", "Q826165", "Q2288360", "Q29377821",
        "Q2066865", "Q140443441", "Q28136436", "Q91147741", "Q141112432",
    ]
    assert "Q124653370" not in qids
    assert "Q48843359" not in qids
    assert enriched["jobTags"][-1] == {"label": "SPARQL", "matchedPhrase": "SPARQL"}


def test_standard_and_temporary_review_rebuild_share_one_candidate_builder():
    assert task46_review_rebuild.build_candidate is build_candidate
