"""History-independent job-tag enrichment regressions."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from job_normalization import URL_RE, add_job_tags  # noqa: E402


EXPECTED_TAGS = [
    {
        "label": "Cypher",
        "matchedPhrase": "Cypher",
        "relatedCatalogPage": "https://openknowledgegraphs.com/software/neo4j/",
    },
    {"label": "GQL", "matchedPhrase": "GQL"},
    {"label": "SPARQL", "matchedPhrase": "SPARQL"},
]


def test_stale_and_clean_inputs_rebuild_to_identical_tags():
    clean = {
        "id": "history-independent",
        "title": "Engineer",
        "description": "Use Cypher, GQL, and SPARQL.",
    }
    stale = {
        **clean,
        "jobTags": [
            {"label": "Removed Language", "matchedPhrase": "Removed Language"},
            {"label": "GQL", "matchedPhrase": "stale-gql"},
            {
                "label": "Cypher",
                "matchedPhrase": "stale-cypher",
                "relatedCatalogPage": "https://example.test/stale-target/",
            },
        ],
    }

    clean_result = add_job_tags([clean])[0]
    stale_result = add_job_tags([stale])[0]

    assert clean_result["jobTags"] == EXPECTED_TAGS
    assert stale_result["jobTags"] == EXPECTED_TAGS
    assert stale_result == clean_result


def test_stale_tags_disappear_when_the_description_no_longer_supports_them():
    record = {
        "id": "retired-tags",
        "title": "SPARQL Engineer",
        "description": "Build unrelated application services.",
        "jobTags": deepcopy(EXPECTED_TAGS),
    }

    result = add_job_tags([record])[0]

    assert "jobTags" not in result
    assert result == {
        "id": "retired-tags",
        "title": "SPARQL Engineer",
        "description": "Build unrelated application services.",
    }


def test_job_tag_rebuild_is_idempotent():
    records = [
        {
            "id": "positive",
            "title": "Engineer",
            "description": "Use Cypher, GQL, and SPARQL.",
        },
        {
            "id": "negative",
            "title": "SPARQL Engineer",
            "description": "No controlled language appears in this description.",
            "jobTags": [{"label": "SPARQL", "matchedPhrase": "stale"}],
        },
    ]

    once = add_job_tags(records)
    twice = add_job_tags(once)

    assert twice == once


def test_fresh_live_and_snapshot_rebuild_inputs_have_tag_parity():
    fresh_live = {
        "id": "same-job",
        "title": "Engineer",
        "description": "Company boilerplate mentions SPARQL; use Cypher and GQL.",
        "firstParty": True,
        "catalogMentions": [{
            "title": "Neo4j",
            "matchedPhrase": "Neo4j",
            "canonicalUrl": "https://openknowledgegraphs.com/software/neo4j/",
            "dataset": "software",
            "qid": "Q1628290",
        }],
    }
    prior_snapshot = {
        **fresh_live,
        "jobTags": [{"label": "GQL", "matchedPhrase": "obsolete"}],
    }

    live_result = add_job_tags([fresh_live])[0]
    rebuild_result = add_job_tags([prior_snapshot])[0]

    assert live_result["jobTags"] == EXPECTED_TAGS
    assert rebuild_result == live_result


def test_bare_domain_urls_with_query_fragment_or_port_cannot_create_sparql_tags():
    descriptions = (
        "RDF docs: example.test?query=SPARQL.",
        "Knowledge graph docs: example.test#SPARQL.",
        "Ontology API: example.test:443/SPARQL.",
        "Ontology API: example.test:443?query=SPARQL.",
        "Ontology API: example.test:443#SPARQL.",
    )

    tagged = add_job_tags([
        {"id": str(index), "description": description}
        for index, description in enumerate(descriptions)
    ])

    assert all("jobTags" not in record for record in tagged)


def test_bare_product_text_and_non_port_colons_are_not_masked_as_urls():
    assert URL_RE.search("data.world") is None
    assert URL_RE.search("data.world: platform") is None
    assert URL_RE.search("data.world?documentation=1") is not None
    assert URL_RE.search("data.world#documentation") is not None
    assert URL_RE.search("data.world:443/documentation") is not None

    tagged = add_job_tags([{
        "id": "plain-product",
        "description": "The data.world: platform role requires SPARQL experience.",
    }])[0]
    assert tagged["jobTags"] == [{"label": "SPARQL", "matchedPhrase": "SPARQL"}]
