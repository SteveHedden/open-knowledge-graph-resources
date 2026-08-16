import json
import sys
import tempfile
import unittest
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fetch_data  # noqa: E402
import generate_pages  # noqa: E402
import recommendation_coverage  # noqa: E402
import related_resources  # noqa: E402
import wikidata_relationship_audit  # noqa: E402


OKG = fetch_data.OKG


def add_resource(
    graph: Graph,
    slug: str,
    qid: str,
    *,
    parents=(),
    uses=(),
    source_types=(),
    creators=(),
    repository=None,
    namespace=None,
    category=None,
    software_type=None,
    language=None,
    license_node=None,
    description="A catalog fixture with stable descriptive content.",
) -> URIRef:
    subject = URIRef(f"https://openknowledgegraphs.com/resource/{slug}/")
    graph.add((subject, RDF.type, OKG.Ontology))
    graph.add((subject, OKG.title, Literal(slug.replace("-", " ").title())))
    graph.add((subject, OKG.description, Literal(description)))
    graph.add((subject, OKG.homepage, URIRef(f"https://example.test/{slug}")))
    graph.add((subject, OKG.wikidataId, URIRef(f"https://www.wikidata.org/wiki/{qid}")))
    for parent in parents:
        graph.add((subject, DCTERMS.isPartOf, URIRef(parent)))
    for used_entity in uses:
        graph.add((subject, OKG.uses, URIRef(used_entity)))
    for source_type in source_types:
        graph.add((subject, OKG.sourceType, URIRef(source_type)))
    for creator in creators:
        graph.add((subject, OKG.creator, URIRef(creator)))
    if repository:
        graph.add((subject, OKG.sourceRepo, URIRef(repository)))
    if namespace:
        graph.add((subject, OKG.namespaceURI, URIRef(namespace)))
    if category:
        graph.add((subject, OKG.category, URIRef(category)))
    if software_type:
        graph.add((subject, OKG.softwareType, URIRef(software_type)))
    if language:
        graph.add((subject, OKG.programmingLanguage, Literal(language)))
    if license_node:
        graph.add((subject, OKG.hasLicense, URIRef(license_node)))
    return subject


class ScoringTests(unittest.TestCase):
    def score(self, graph, left, right, config=related_resources.DEFAULT_CONFIG):
        return related_resources.score_pair(
            related_resources.features_for_resource(graph, left),
            related_resources.features_for_resource(graph, right),
            config,
        )

    def test_weak_only_auditory_and_dublin_fixture_is_rejected(self):
        graph = Graph()
        shared = {
            "category": f"{OKG}LibraryCulturalHeritage",
            "software_type": f"{OKG}OntologyEngineering",
            "language": "Python",
            "license_node": f"{OKG}License_Apache",
            "description": "Audio format terminology reference classification metadata.",
        }
        auditory = add_resource(
            graph,
            "auditory",
            "Q1",
            repository="https://github.com/example/auditory",
            **shared,
        )
        dublin = add_resource(
            graph,
            "controlled-vocabulary-for-dublin-core-format",
            "Q2",
            repository="https://github.com/example/dublin-format",
            **shared,
        )

        score = self.score(graph, auditory, dublin)
        self.assertFalse(score.qualifies)
        self.assertEqual(score.qualifying_reasons, ())
        self.assertGreater(score.score, 0)

        graph.add((auditory, OKG.relatedTo, dublin))
        diagnostics = related_resources.add_related_resources(graph, "resource")
        self.assertEqual(list(graph.triples((None, OKG.relatedTo, None))), [])
        self.assertEqual(diagnostics["selectedRelationshipCount"], 0)

    def test_strong_parent_ranks_above_correlated_creator_match(self):
        graph = Graph()
        parent = "http://www.wikidata.org/entity/Q900"
        creator = f"{OKG}Creator_Example_Q901"
        source = add_resource(
            graph,
            "source",
            "Q10",
            parents=[parent],
            creators=[creator],
            category=f"{OKG}TechnologyWeb",
            language="Python",
            license_node=f"{OKG}License_Apache",
            description="Deterministic linked graph processing toolkit.",
        )
        parent_match = add_resource(graph, "parent-match", "Q11", parents=[parent])
        creator_match = add_resource(
            graph,
            "creator-match",
            "Q12",
            creators=[creator],
            category=f"{OKG}TechnologyWeb",
            language="Python",
            license_node=f"{OKG}License_Apache",
            description="Deterministic linked graph processing library.",
        )

        context = related_resources.SimilarityContext(
            parent_degrees={parent: 2},
            catalog_source_entities=frozenset({parent}),
        )
        diagnostics = related_resources.add_related_resources(
            graph,
            "resource",
            context=context,
        )
        source_rows = [
            row for row in diagnostics["relationships"] if row["subject"] == str(source)
        ]
        self.assertEqual(
            [row["candidate"] for row in source_rows],
            [str(parent_match), str(creator_match)],
        )
        self.assertGreater(source_rows[0]["score"], source_rows[1]["score"])
        self.assertEqual(source_rows[0]["qualifyingReasons"], ["shared_parent"])

    def test_broad_collection_parent_is_not_similarity_evidence(self):
        graph = Graph()
        broad_parent = "http://www.wikidata.org/entity/Q137008561"
        subjects = [
            add_resource(
                graph,
                f"registry-member-{index:02d}",
                f"Q{700 + index}",
                parents=[broad_parent],
                category=f"{OKG}LifeSciencesHealthcare",
            )
            for index in range(7)
        ]
        context = related_resources.SimilarityContext(
            parent_degrees={broad_parent: 7},
            catalog_source_entities=frozenset({broad_parent}),
        )

        diagnostics = related_resources.add_related_resources(
            graph,
            "resource",
            context=context,
        )

        self.assertEqual(diagnostics["selectedRelationshipCount"], 0)
        self.assertEqual(diagnostics["structuralCandidateCount"], 0)
        self.assertEqual(
            diagnostics["suppressedSharedParents"],
            [
                {
                    "parent": broad_parent,
                    "memberCount": 7,
                    "reasons": ["degree_exceeds_limit"],
                }
            ],
        )
        self.assertTrue(
            all(not list(graph.objects(subject, OKG.relatedTo)) for subject in subjects)
        )

    def test_specific_parent_remains_strong_at_degree_limit(self):
        graph = Graph()
        parent = "http://www.wikidata.org/entity/Q950"
        subjects = [
            add_resource(graph, f"specific-{index:02d}", f"Q{800 + index}", parents=[parent])
            for index in range(related_resources.DEFAULT_CONFIG.max_shared_parent_degree)
        ]
        context = related_resources.SimilarityContext(
            parent_degrees={parent: len(subjects)},
            catalog_source_entities=frozenset({parent}),
        )

        diagnostics = related_resources.add_related_resources(
            graph,
            "resource",
            context=context,
        )

        self.assertGreater(diagnostics["selectedRelationshipCount"], 0)
        self.assertEqual(diagnostics["suppressedSharedParentCount"], 0)
        first_rows = [
            row for row in diagnostics["relationships"] if row["subject"] == str(subjects[0])
        ]
        self.assertTrue(first_rows)
        self.assertTrue(
            all("shared_parent" in row["qualifyingReasons"] for row in first_rows)
        )

    def test_uncataloged_parent_is_not_similarity_evidence(self):
        graph = Graph()
        external_parent = "http://www.wikidata.org/entity/Q54837"
        left = add_resource(graph, "left-external-parent", "Q901", parents=[external_parent])
        right = add_resource(graph, "right-external-parent", "Q902", parents=[external_parent])

        diagnostics = related_resources.add_related_resources(graph, "resource")

        self.assertEqual(diagnostics["selectedRelationshipCount"], 0)
        self.assertEqual(
            diagnostics["suppressedSharedParents"],
            [
                {
                    "parent": external_parent,
                    "memberCount": 2,
                    "reasons": ["parent_not_cataloged"],
                }
            ],
        )
        self.assertNotIn((left, OKG.relatedTo, right), graph)

    def test_direct_relationship_survives_broad_parent_suppression(self):
        graph = Graph()
        parent_qid = "Q960"
        parent_entity = f"http://www.wikidata.org/entity/{parent_qid}"
        parent = add_resource(graph, "cataloged-parent", parent_qid)
        children = [
            add_resource(graph, f"broad-child-{index}", f"Q{970 + index}", parents=[parent_entity])
            for index in range(7)
        ]

        diagnostics = related_resources.add_related_resources(graph, "resource")

        self.assertIn((children[0], OKG.relatedTo, parent), graph)
        child_rows = [
            row for row in diagnostics["relationships"] if row["subject"] == str(children[0])
        ]
        self.assertEqual(child_rows[0]["qualifyingReasons"], ["direct_relationship"])

    def test_broad_parent_does_not_add_points_to_repository_match(self):
        graph = Graph()
        parent = "http://www.wikidata.org/entity/Q980"
        subjects = []
        for index in range(7):
            repository = (
                "https://github.com/example/shared"
                if index < 2
                else f"https://github.com/example/distinct-{index}"
            )
            subjects.append(
                add_resource(
                    graph,
                    f"repo-child-{index}",
                    f"Q{990 + index}",
                    parents=[parent],
                    repository=repository,
                )
            )
        context = related_resources.SimilarityContext(
            parent_degrees={parent: 7},
            catalog_source_entities=frozenset({parent}),
        )

        diagnostics = related_resources.add_related_resources(
            graph,
            "resource",
            context=context,
        )
        row = next(
            item
            for item in diagnostics["relationships"]
            if item["subject"] == str(subjects[0]) and item["candidate"] == str(subjects[1])
        )
        self.assertEqual(row["qualifyingReasons"], ["same_repository"])
        self.assertNotIn("shared_parent", [component["feature"] for component in row["components"]])

    def test_direct_source_relationship_is_strong_and_symmetric_at_pair_level(self):
        graph = Graph()
        parent = add_resource(graph, "parent", "Q21")
        child = add_resource(
            graph,
            "child",
            "Q22",
            parents=["http://www.wikidata.org/entity/Q21"],
        )
        forward = self.score(graph, child, parent)
        reverse = self.score(graph, parent, child)
        self.assertTrue(forward.qualifies)
        self.assertEqual(forward.score, reverse.score)
        self.assertIn("direct_relationship", forward.qualifying_reasons)

    def test_repository_and_namespace_matching_are_conservative(self):
        graph = Graph()
        exact_repo = add_resource(
            graph,
            "exact-repo",
            "Q31",
            repository="http://github.com/Acme/project.git",
        )
        normalized_repo = add_resource(
            graph,
            "normalized-repo",
            "Q32",
            repository="https://github.com/Acme/project/",
        )
        same_owner_only = add_resource(
            graph,
            "same-owner-only",
            "Q33",
            repository="https://github.com/Acme/different",
        )
        namespace_a = add_resource(
            graph,
            "namespace-a",
            "Q34",
            namespace="https://example.org/family#",
        )
        namespace_b = add_resource(
            graph,
            "namespace-b",
            "Q35",
            namespace="https://example.org/family/",
        )
        same_host_only = add_resource(
            graph,
            "same-host-only",
            "Q36",
            namespace="https://example.org/other#",
        )

        self.assertTrue(self.score(graph, exact_repo, normalized_repo).qualifies)
        self.assertFalse(self.score(graph, exact_repo, same_owner_only).qualifies)
        self.assertTrue(self.score(graph, namespace_a, namespace_b).qualifies)
        self.assertFalse(self.score(graph, namespace_a, same_host_only).qualifies)

    def test_repository_hosting_account_page_is_not_repository_evidence(self):
        graph = Graph()
        left = add_resource(
            graph,
            "left-owner-page",
            "Q37",
            repository="https://github.com/Acme",
        )
        right = add_resource(
            graph,
            "right-owner-page",
            "Q38",
            repository="https://github.com/Acme/",
        )

        self.assertIsNone(
            related_resources.canonical_repository("https://github.com/Acme")
        )
        self.assertIsNone(
            related_resources.canonical_repository("https://gitlab.com/Acme")
        )
        self.assertIsNone(
            related_resources.canonical_repository("https://bitbucket.org/Acme")
        )
        self.assertFalse(self.score(graph, left, right).qualifies)
        diagnostics = related_resources.add_related_resources(graph, "resource")
        self.assertEqual(diagnostics["selectedRelationshipCount"], 0)
        self.assertEqual(list(graph.triples((None, OKG.relatedTo, None))), [])

    def test_structural_candidate_below_threshold_is_omitted(self):
        graph = Graph()
        creator = f"{OKG}Creator_Example_Q40"
        left = add_resource(graph, "left", "Q41", creators=[creator])
        right = add_resource(graph, "right", "Q42", creators=[creator])
        config = related_resources.SimilarityConfig(shared_creator=50, score_threshold=60)

        self.assertFalse(self.score(graph, left, right, config).qualifies)
        diagnostics = related_resources.add_related_resources(graph, "resource", config)
        self.assertEqual(diagnostics["selectedRelationshipCount"], 0)
        self.assertEqual(diagnostics["belowThresholdCount"], 2)

    def test_maximum_five_uri_tie_break_and_directionality(self):
        graph = Graph()
        repository = "https://github.com/example/shared-monorepo"
        subjects = {
            slug: add_resource(graph, slug, f"Q{index}", repository=repository)
            for index, slug in enumerate(("a", "b", "c", "d", "e", "f", "z"), start=51)
        }

        related_resources.add_related_resources(graph, "resource")
        z_targets = sorted(str(value) for value in graph.objects(subjects["z"], OKG.relatedTo))
        self.assertEqual(z_targets, [str(subjects[key]) for key in ("a", "b", "c", "d", "e")])
        self.assertIn((subjects["z"], OKG.relatedTo, subjects["a"]), graph)
        self.assertNotIn((subjects["a"], OKG.relatedTo, subjects["z"]), graph)
        self.assertTrue(
            all(len(list(graph.objects(subject, OKG.relatedTo))) <= 5 for subject in subjects.values())
        )

    def test_separate_catalog_graphs_never_create_cross_catalog_links(self):
        parent = "http://www.wikidata.org/entity/Q600"
        resource_graph = Graph()
        software_graph = Graph()
        resource = add_resource(resource_graph, "resource", "Q61", parents=[parent])
        software = add_resource(software_graph, "software", "Q62", parents=[parent])

        related_resources.add_related_resources(resource_graph, "resource")
        related_resources.add_related_resources(software_graph, "software")
        self.assertEqual(list(resource_graph.objects(resource, OKG.relatedTo)), [])
        self.assertEqual(list(software_graph.objects(software, OKG.relatedTo)), [])

    def test_parent_degree_context_is_global_across_catalogs(self):
        parent = "http://www.wikidata.org/entity/Q6000"
        resource_graph = Graph()
        software_graph = Graph()
        identity_graph = Graph()
        add_resource(identity_graph, "parent-identity", "Q6000")
        resources = [
            add_resource(resource_graph, f"resource-{index}", f"Q{6100 + index}", parents=[parent])
            for index in range(5)
        ]
        software = [
            add_resource(software_graph, f"software-{index}", f"Q{6200 + index}", parents=[parent])
            for index in range(2)
        ]
        context = related_resources.build_similarity_context(
            (resource_graph, software_graph, identity_graph)
        )

        self.assertEqual(context.parent_degrees[parent], 7)
        related_resources.add_related_resources(
            resource_graph,
            "resource",
            context=context,
        )
        related_resources.add_related_resources(
            software_graph,
            "software",
            context=context,
        )
        self.assertTrue(
            all(not list(resource_graph.objects(subject, OKG.relatedTo)) for subject in resources)
        )
        self.assertTrue(
            all(not list(software_graph.objects(subject, OKG.relatedTo)) for subject in software)
        )

    def test_sparse_records_do_not_receive_repeated_five_links(self):
        graph = Graph()
        category = f"{OKG}GeneralCrossDomain"
        source = add_resource(graph, "sparse-source", "Q70", category=category)
        for index in range(71, 77):
            add_resource(graph, f"sparse-{index}", f"Q{index}", category=category)
        related_resources.add_related_resources(graph, "resource")
        self.assertEqual(list(graph.objects(source, OKG.relatedTo)), [])

    def test_directional_uses_claim_is_strong_in_both_recommendation_views(self):
        graph = Graph()
        target = add_resource(graph, "linkml", "Q108911530")
        source = add_resource(
            graph,
            "ontogpt",
            "Q125352352",
            uses=["http://www.wikidata.org/entity/Q108911530"],
        )
        diagnostics = related_resources.add_related_resources(graph, "software")
        self.assertIn((source, OKG.relatedTo, target), graph)
        self.assertIn((target, OKG.relatedTo, source), graph)
        rows = [row for row in diagnostics["relationships"] if row["subject"] == str(source)]
        self.assertEqual(rows[0]["qualifyingReasons"], ["direct_uses"])
        self.assertIn("okg:uses", rows[0]["components"][0]["evidence"][0])

    def test_exact_source_type_degree_boundary_is_fixed_and_inclusive(self):
        source_type = "http://www.wikidata.org/entity/Q9000"
        for degree, expected in ((5, True), (6, True), (7, False)):
            with self.subTest(degree=degree):
                graph = Graph()
                subjects = [
                    add_resource(
                        graph,
                        f"typed-{degree}-{index}",
                        f"Q{degree}{index}",
                        source_types=[source_type],
                    )
                    for index in range(degree)
                ]
                context = related_resources.build_similarity_context((graph,))
                pair = related_resources.score_pair(
                    related_resources.features_for_resource(graph, subjects[0]),
                    related_resources.features_for_resource(graph, subjects[1]),
                    context=context,
                )
                self.assertEqual(pair.qualifies, expected)
                self.assertEqual(
                    "shared_source_type" in pair.qualifying_reasons,
                    expected,
                )

    def test_broad_source_type_is_reported_as_suppressed(self):
        graph = Graph()
        broad = "http://www.wikidata.org/entity/Q124653107"
        for index in range(7):
            add_resource(graph, f"broad-type-{index}", f"Q88{index}", source_types=[broad])
        diagnostics = related_resources.add_related_resources(graph, "software")
        self.assertEqual(diagnostics["selectedRelationshipCount"], 0)
        self.assertEqual(
            diagnostics["suppressedSharedSourceTypes"],
            [{"sourceType": broad, "memberCount": 7, "reasons": ["degree_exceeds_limit"]}],
        )


class ProjectionAndDiagnosticsTests(unittest.TestCase):
    def test_two_software_exemplars_keep_exact_uses_predicate_and_qids(self):
        mappings = fetch_data.load_source_mappings(fetch_data.SOURCES_PATH)
        uses_pid = mappings.property_id_for("usesEntity", value_kind="iri")
        pairs = (("Q125352352", "Q108911530"), ("Q140639972", "Q140639984"))
        qids = {qid for pair in pairs for qid in pair}
        records = {
            f"http://www.wikidata.org/entity/{qid}": fetch_data.ResourceRecord(
                item_iri=f"http://www.wikidata.org/entity/{qid}",
                label=qid,
                types={OKG.Software},
                homepages={f"https://example.test/{qid.lower()}"},
            )
            for qid in qids
        }
        edges = tuple(
            wikidata_relationship_audit.DirectIriEdge(subject, uses_pid, target)
            for subject, target in pairs
        )
        fetch_data.apply_declared_relationships(records, edges, mappings)
        slugs = {qid: qid.lower() for qid in qids}
        graph = fetch_data.build_graph(
            records=records,
            license_labels={},
            creator_labels={},
            human_creators=set(),
            person_identifiers={},
            include_software_fields=True,
            dataset_path="software",
            slug_registry=slugs,
        )
        diagnostics = related_resources.add_related_resources(graph, "software")
        selected = {
            (row["subject"], row["candidate"]): row
            for row in diagnostics["relationships"]
        }
        for subject_qid, target_qid in pairs:
            self.assertEqual(uses_pid, "P2283")
            source = f"https://openknowledgegraphs.com/software/{subject_qid.lower()}/"
            target = f"https://openknowledgegraphs.com/software/{target_qid.lower()}/"
            self.assertIn((source, target), selected)
            self.assertEqual(selected[(source, target)]["qualifyingReasons"], ["direct_uses"])
            self.assertIn(
                wikidata_relationship_audit.DirectIriEdge(
                    subject_qid, uses_pid, target_qid
                ),
                edges,
            )

    def test_declared_source_types_and_uses_are_preserved_as_distinct_iris(self):
        mappings = fetch_data.load_source_mappings(fetch_data.SOURCES_PATH)
        source_type_pid = mappings.property_id_for("sourceType", value_kind="iri")
        uses_pid = mappings.property_id_for("usesEntity", value_kind="iri")
        self.assertNotIn('"P2283"', Path(fetch_data.__file__).read_text(encoding="utf-8"))
        record = fetch_data.ResourceRecord(
            item_iri="http://www.wikidata.org/entity/Q4866972",
            label="Basic Formal Ontology",
            types={OKG.Ontology},
        )
        edges = (
            wikidata_relationship_audit.DirectIriEdge("Q4866972", source_type_pid, "Q324254"),
            wikidata_relationship_audit.DirectIriEdge("Q4866972", uses_pid, "Q28729320"),
        )
        fetch_data.apply_declared_relationships({record.item_iri: record}, edges, mappings)
        graph = fetch_data.build_graph(
            records={record.item_iri: record},
            license_labels={},
            creator_labels={},
            human_creators=set(),
            person_identifiers={},
            include_software_fields=False,
            dataset_path="resource",
            slug_registry={"Q4866972": "basic-formal-ontology"},
        )
        subject = URIRef("https://openknowledgegraphs.com/resource/basic-formal-ontology/")
        self.assertIn((subject, RDF.type, OKG.Ontology), graph)
        self.assertIn(
            (subject, OKG.sourceType, URIRef("http://www.wikidata.org/entity/Q324254")),
            graph,
        )
        self.assertIn(
            (subject, OKG.uses, URIRef("http://www.wikidata.org/entity/Q28729320")),
            graph,
        )

    def test_relationship_audit_discovers_unreviewed_predicates_without_scoring_them(self):
        Edge = wikidata_relationship_audit.DirectIriEdge
        report = wikidata_relationship_audit.audit_document(
            (
                Edge("Q1", "P2283", "Q2"),
                Edge("Q1", "P2283", "Q999"),
                Edge("Q2", "P999999", "Q1"),
                Edge("Q1", "P361", "Q3"),
            ),
            {
                "Q1": frozenset({"resource"}),
                "Q2": frozenset({"resource", "software"}),
                "Q3": frozenset({"software"}),
            },
            {"P2283", "P361"},
        )
        predicates = {row["sourcePropertyId"]: row for row in report["predicates"]}
        self.assertTrue(predicates["P2283"]["reviewedForScoring"])
        self.assertFalse(predicates["P999999"]["reviewedForScoring"])
        self.assertEqual(predicates["P361"]["sameCatalogEdgeCount"], 0)
        self.assertEqual(predicates["P361"]["crossCatalogEdgeCount"], 1)
        reviewed = {
            row["sourcePropertyId"]: row
            for row in report["reviewedPredicateAvailability"]
        }
        self.assertEqual(reviewed["P2283"]["edgeCount"], 2)
        self.assertEqual(reviewed["P2283"]["uniqueObjectCount"], 2)

    def test_truthy_claim_audit_uses_preferred_rank_and_all_item_predicates(self):
        entities = {
            "Q1": {
                "claims": {
                    "P1": [
                        {"rank": "normal", "mainsnak": {"datatype": "wikibase-item", "datavalue": {"value": {"id": "Q2"}}}},
                        {"rank": "preferred", "mainsnak": {"datatype": "wikibase-item", "datavalue": {"value": {"id": "Q3"}}}},
                    ],
                    "P2": [
                        {"rank": "normal", "mainsnak": {"datatype": "string", "datavalue": {"value": "ignored"}}}
                    ],
                }
            }
        }
        self.assertEqual(
            wikidata_relationship_audit.truthy_item_edges(entities),
            (wikidata_relationship_audit.DirectIriEdge("Q1", "P1", "Q3"),),
        )

    def test_final_page_coverage_gate_uses_surviving_targets_and_boundaries(self):
        def page(qid, target=None):
            value = {
                "canonicalUrl": f"https://openknowledgegraphs.com/resource/{qid.lower()}/",
                "wikidataId": f"https://www.wikidata.org/wiki/{qid}",
            }
            if target:
                value["relatedTools"] = [{"canonicalUrl": target}]
            return value

        target_url = "https://openknowledgegraphs.com/resource/q2/"
        survivors = [
            ("resource", page("Q1", target_url), "Q1", "q1"),
            ("resource", page("Q2", "https://openknowledgegraphs.com/resource/q1/"), "Q2", "q2"),
            ("software", {**page("Q3", target_url), "canonicalUrl": "https://openknowledgegraphs.com/software/q3/"}, "Q3", "q3"),
            ("software", {**page("Q4", "https://invalid.test/"), "canonicalUrl": "https://openknowledgegraphs.com/software/q4/"}, "Q4", "q4"),
        ]
        coverage = recommendation_coverage.coverage_by_catalog(survivors)
        self.assertEqual(coverage["resource"]["coverageShare"], 1.0)
        self.assertEqual(coverage["software"]["coverageShare"], 0.5)
        policy = {
            "maximumEmptyShare": 0.5,
            "maximumUnreviewedCoverageDecline": 0.05,
            "acceptedDeclines": [],
            "acceptedShortfalls": [],
        }
        passed = recommendation_coverage.evaluate_coverage(coverage, coverage, policy, "generation")
        self.assertTrue(passed["gate"]["passed"])
        exact_boundary = {
            **coverage,
            "software": {**coverage["software"], "coverageShare": 0.55},
        }
        allowed = recommendation_coverage.evaluate_coverage(
            coverage, exact_boundary, policy, "generation"
        )
        self.assertTrue(allowed["gate"]["passed"])
        baseline = {**coverage, "software": {**coverage["software"], "coverageShare": 0.550001}}
        failed = recommendation_coverage.evaluate_coverage(coverage, baseline, policy, "generation")
        self.assertFalse(failed["gate"]["passed"])
        reviewed_policy = {
            **policy,
            "acceptedDeclines": [
                {
                    "baselineGenerationId": "generation",
                    "catalog": "software",
                    "rationale": "Explicitly reviewed fixture decline.",
                }
            ],
        }
        reviewed = recommendation_coverage.evaluate_coverage(
            coverage, baseline, reviewed_policy, "generation"
        )
        self.assertTrue(reviewed["gate"]["passed"])

    def test_reviewed_coverage_shortfall_is_one_generation_and_catalog_specific(self):
        candidate = {
            "resource": {"coverageShare": 0.49, "emptyShare": 0.51},
            "software": {"coverageShare": 0.288235, "emptyShare": 0.711765},
        }
        baseline = {
            "resource": {"coverageShare": 0.182, "emptyShare": 0.818},
            "software": {"coverageShare": 0.1, "emptyShare": 0.9},
        }
        policy = {
            "maximumEmptyShare": 0.5,
            "maximumUnreviewedCoverageDecline": 0.05,
            "acceptedDeclines": [],
            "acceptedShortfalls": [
                {
                    "baselineGenerationId": "approved-generation",
                    "catalog": "resource",
                    "minimumCoverageShare": 0.48,
                    "rationale": "Reviewed interim resource floor.",
                },
                {
                    "baselineGenerationId": "approved-generation",
                    "catalog": "software",
                    "minimumCoverageShare": 0.28,
                    "rationale": "Reviewed interim software floor.",
                },
            ],
        }
        approved = recommendation_coverage.evaluate_coverage(
            candidate, baseline, policy, "approved-generation"
        )
        self.assertTrue(approved["gate"]["passed"])
        self.assertTrue(
            approved["comparisons"]["software"]["reviewedShortfallApplied"]
        )
        wrong_generation = recommendation_coverage.evaluate_coverage(
            candidate, baseline, policy, "new-generation"
        )
        self.assertFalse(wrong_generation["gate"]["passed"])
        below_reviewed_floor = {
            **candidate,
            "software": {"coverageShare": 0.279999, "emptyShare": 0.720001},
        }
        below = recommendation_coverage.evaluate_coverage(
            below_reviewed_floor, baseline, policy, "approved-generation"
        )
        self.assertFalse(below["gate"]["passed"])

    def test_coverage_policy_rejects_unreviewable_shortfalls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            base = {
                "maximumEmptyShare": 0.5,
                "maximumUnreviewedCoverageDecline": 0.05,
                "acceptedDeclines": [],
            }
            for entry in (
                {
                    "baselineGenerationId": "generation",
                    "catalog": "resource",
                    "minimumCoverageShare": 0.48,
                    "rationale": "",
                },
                {
                    "baselineGenerationId": "generation",
                    "catalog": "unknown",
                    "minimumCoverageShare": 0.48,
                    "rationale": "Reviewed.",
                },
            ):
                with self.subTest(entry=entry):
                    path.write_text(
                        json.dumps({**base, "acceptedShortfalls": [entry]}),
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        recommendation_coverage.load_policy(path)

    def test_checked_in_deferred_floor_preserves_regression_protection(self):
        policy = recommendation_coverage.load_policy(
            ROOT / "validation" / "recommendation-coverage-policy.json"
        )
        self.assertEqual(policy["maximumEmptyShare"], 0.75)
        self.assertEqual(policy["acceptedShortfalls"], [])
        candidate = {
            "resource": {"coverageShare": 343 / 698, "emptyShare": 1 - 343 / 698},
            "software": {"coverageShare": 50 / 171, "emptyShare": 1 - 50 / 171},
        }
        baseline = {
            "resource": {"coverageShare": 340 / 697, "emptyShare": 1 - 340 / 697},
            "software": {"coverageShare": 49 / 170, "emptyShare": 1 - 49 / 170},
        }
        approved = recommendation_coverage.evaluate_coverage(
            candidate,
            baseline,
            policy,
            "current-generation",
        )
        self.assertTrue(approved["gate"]["passed"])

        below_absolute_floor = {
            **candidate,
            "software": {"coverageShare": 0.249999, "emptyShare": 0.750001},
        }
        rejected_floor = recommendation_coverage.evaluate_coverage(
            below_absolute_floor,
            baseline,
            policy,
            "current-generation",
        )
        self.assertFalse(rejected_floor["gate"]["passed"])

        regressed = {
            **candidate,
            "software": {"coverageShare": 0.26, "emptyShare": 0.74},
        }
        regression_baseline = {
            **baseline,
            "software": {"coverageShare": 0.32, "emptyShare": 0.68},
        }
        rejected_regression = recommendation_coverage.evaluate_coverage(
            regressed,
            regression_baseline,
            policy,
            "current-generation",
        )
        self.assertFalse(rejected_regression["gate"]["passed"])

    def test_every_source_parent_identity_survives_row_normalization(self):
        item = "http://www.wikidata.org/entity/Q79"
        rows = [
            {
                "item": {"value": item},
                "matchedTypeQid": {"value": "Q324254"},
                "partOfEntity": {"value": f"http://www.wikidata.org/entity/{parent}"},
            }
            for parent in ("Q791", "Q792")
        ]
        labels = {
            item: "Child",
            "http://www.wikidata.org/entity/Q791": "First Parent",
            "http://www.wikidata.org/entity/Q792": "Second Parent",
        }
        records, _, _ = fetch_data.parse_ontology_rows(
            rows,
            labels,
            {},
            {"Q324254": OKG.Ontology},
        )
        self.assertEqual(
            records[item].part_of_entities,
            {
                "http://www.wikidata.org/entity/Q791",
                "http://www.wikidata.org/entity/Q792",
            },
        )
        self.assertEqual(records[item].part_of_labels, {"First Parent", "Second Parent"})

    def test_all_parent_identities_and_single_label_projection_are_preserved(self):
        record = fetch_data.ResourceRecord(
            item_iri="http://www.wikidata.org/entity/Q80",
            label="Child",
            types={OKG.Ontology},
            part_of_entities={
                "http://www.wikidata.org/entity/Q801",
                "http://www.wikidata.org/entity/Q802",
            },
            part_of_labels={"Zulu Parent", "Alpha Parent"},
        )
        graph = fetch_data.build_graph(
            records={record.item_iri: record},
            license_labels={},
            creator_labels={},
            human_creators=set(),
            person_identifiers={},
            include_software_fields=False,
            dataset_path="resource",
            slug_registry={"Q80": "child"},
        )
        subject = URIRef("https://openknowledgegraphs.com/resource/child/")
        self.assertEqual(
            set(graph.objects(subject, DCTERMS.isPartOf)),
            {
                URIRef("http://www.wikidata.org/entity/Q801"),
                URIRef("http://www.wikidata.org/entity/Q802"),
            },
        )
        self.assertEqual(list(graph.objects(subject, OKG.partOf)), [Literal("Alpha Parent")])
        item = fetch_data.extract_items_from_graph(
            graph,
            {OKG.Ontology},
            False,
            {OKG.Ontology: "Ontology"},
        )[0]
        self.assertEqual(item["partOf"], "Alpha Parent")
        self.assertNotIn("partOfEntities", item)

    def test_diagnostics_and_json_projection_are_deterministic(self):
        graph = Graph()
        parent = "http://www.wikidata.org/entity/Q900"
        left = add_resource(graph, "left", "Q91", parents=[parent])
        right = add_resource(graph, "right", "Q92", parents=[parent])
        context = related_resources.SimilarityContext(
            parent_degrees={parent: 2},
            catalog_source_entities=frozenset({parent}),
        )
        catalog = related_resources.add_related_resources(
            graph,
            "resource",
            context=context,
        )
        document = related_resources.diagnostics_document([catalog])

        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            related_resources.write_diagnostics_atomic(document, first)
            related_resources.write_diagnostics_atomic(document, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            parsed = json.loads(first.read_text(encoding="utf-8"))

        relationship = next(
            row
            for row in parsed["catalogs"][0]["relationships"]
            if row["subject"] == str(left) and row["candidate"] == str(right)
        )
        self.assertEqual(relationship["qualifyingReasons"], ["shared_parent"])
        self.assertEqual(relationship["components"][0]["score"], 100)

        projected = fetch_data.related_tools_for_resource(graph, left)
        self.assertEqual(
            projected,
            [{"title": "Right", "canonicalUrl": str(right)}],
        )
        self.assertEqual(
            {str(value) for value in graph.objects(left, OKG.relatedTo)},
            {item["canonicalUrl"] for item in projected},
        )

    def test_detail_page_omits_empty_related_section(self):
        item = {
            "canonicalUrl": "https://openknowledgegraphs.com/resource/lonely/",
            "title": "Lonely",
            "description": "A complete standalone catalog fixture.",
            "homepage": "https://example.test/lonely",
            "wikidataId": "https://www.wikidata.org/wiki/Q100",
            "types": ["Ontology"],
        }
        page = generate_pages.make_page(item, "resource", "lonely")
        self.assertNotIn("Related resources", page)


if __name__ == "__main__":
    unittest.main()
