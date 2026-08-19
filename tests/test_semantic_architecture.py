import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, OWL, PROV, RDF, RDFS, SKOS


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import category_classifier  # noqa: E402
import fetch_data  # noqa: E402
import semantic_config  # noqa: E402


OKG = semantic_config.OKG
DCAT = Namespace("http://www.w3.org/ns/dcat#")
DOAP = Namespace("http://usefulinc.com/ns/doap#")
SCHEMA = Namespace("https://schema.org/")
SRC = Namespace("https://openknowledgegraphs.com/sources#")

BASELINE_JSON_FIELDS = {
    "ontologies": {
        "aliases",
        "canonicalUrl",
        "category",
        "creators",
        "description",
        "homepage",
        "licenses",
        "namespaceURI",
        "partOf",
        "relatedTools",
        "sourceRepo",
        "title",
        "types",
        "wikidataId",
    },
    "software": {
        "aliases",
        "canonicalUrl",
        "creators",
        "description",
        "homepage",
        "latestVersion",
        "licenses",
        "partOf",
        "programmingLanguages",
        "relatedTools",
        "releaseDate",
        "softwareType",
        "sourceRepo",
        "title",
        "types",
        "wikidataId",
    },
}


class SemanticArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.category_vocab = semantic_config.load_controlled_vocabulary(
            semantic_config.CATEGORIES_VOCAB_PATH
        )
        cls.software_vocab = semantic_config.load_controlled_vocabulary(
            semantic_config.SOFTWARE_TYPES_VOCAB_PATH
        )
        cls.source_mappings = semantic_config.load_source_mappings()
        cls.curation = semantic_config.load_curated_assignments(
            semantic_config.CURATION_PATH,
            cls.category_vocab,
            cls.software_vocab,
        )
        cls.ontology = Graph().parse(ROOT / "ontology.ttl", format="turtle")
        cls.sources = Graph().parse(ROOT / "sources.ttl", format="turtle")
        cls.ontologies = Graph().parse(ROOT / "data/ontologies.ttl", format="turtle")
        cls.software = Graph().parse(ROOT / "data/software.ttl", format="turtle")
        cls.uri_registry = json.loads((ROOT / "data/uri_registry.json").read_text())

    def test_every_layer_parses_individually_and_as_a_merged_graph(self):
        paths = (
            "ontology.ttl",
            "vocabularies/categories.ttl",
            "vocabularies/software-types.ttl",
            "sources.ttl",
            "curation/classifications.ttl",
            "data/ontologies.ttl",
            "data/software.ttl",
        )
        merged = Graph()
        for relative_path in paths:
            with self.subTest(path=relative_path):
                parsed = Graph().parse(ROOT / relative_path, format="turtle")
                self.assertTrue(parsed)
                merged += parsed
        self.assertGreater(len(merged), len(self.ontologies) + len(self.software))

    def test_instance_graph_identity_matches_derived_json_after_source_refresh(self):
        specifications = (
            (
                self.ontologies,
                "ontologies.json",
                {
                    OKG.Ontology,
                    OKG.ControlledVocabulary,
                    OKG.Taxonomy,
                    OKG.KnowledgeGraph,
                    OKG.OntologyLanguage,
                },
            ),
            (self.software, "software.json", {OKG.Software}),
        )
        for graph, json_name, catalog_types in specifications:
            with self.subTest(dataset=json_name):
                rdf_qids = {
                    str(value).rsplit("/", 1)[-1]
                    for catalog_type in catalog_types
                    for subject in graph.subjects(RDF.type, catalog_type)
                    for value in graph.objects(subject, OKG.wikidataId)
                }
                payload = json.loads((ROOT / "data" / json_name).read_text())
                json_qids = {
                    item["wikidataId"].rsplit("/", 1)[-1]
                    for item in payload["items"]
                }
                self.assertTrue(json_qids <= rdf_qids)

    def test_instance_graphs_contain_no_dataset_or_provenance_metadata(self):
        allowed_predicate_namespaces = (
            str(OKG),
            str(RDF),
            str(RDFS),
        )
        for graph in (self.ontologies, self.software):
            self.assertFalse(any(graph.subjects(RDF.type, DCAT.Dataset)))
            self.assertFalse(any(graph.triples((None, PROV.wasDerivedFrom, None))))
            self.assertTrue(
                all(
                    predicate == DCTERMS.isPartOf
                    or str(predicate).startswith(allowed_predicate_namespaces)
                    for _, predicate, _ in graph
                )
            )

    def test_source_registry_owns_dataset_level_provenance(self):
        wikidata = SRC.WikidataDataset
        generated = (
            semantic_config.ONTOLOGIES_DATASET,
            semantic_config.SOFTWARE_DATASET,
        )
        self.assertIn((wikidata, RDF.type, DCAT.Dataset), self.sources)
        for dataset in generated:
            with self.subTest(dataset=dataset):
                self.assertIn((dataset, RDF.type, DCAT.Dataset), self.sources)
                self.assertIn((dataset, PROV.wasDerivedFrom, wikidata), self.sources)
                self.assertTrue(any(self.sources.objects(dataset, DCTERMS.title)))
                self.assertFalse(any(self.sources.objects(dataset, DCTERMS.modified)))
                distributions = list(self.sources.objects(dataset, DCAT.distribution))
                self.assertEqual(len(distributions), 2)
                for distribution in distributions:
                    self.assertIn((distribution, RDF.type, DCAT.Distribution), self.sources)
                    self.assertTrue(any(self.sources.objects(distribution, DCAT.downloadURL)))

        merged_instances = self.ontologies + self.software
        resource_subjects = set(merged_instances.subjects(OKG.wikidataId, None))
        self.assertFalse(
            any((subject, PROV.wasDerivedFrom, None) in merged_instances for subject in resource_subjects)
        )

    def test_controlled_terms_keep_uris_and_belong_to_exactly_one_scheme(self):
        expected_category_terms = {
            OKG.LifeSciencesHealthcare,
            OKG.Geospatial,
            OKG.GovernmentPublicSector,
            OKG.InternationalDevelopment,
            OKG.FinanceBusiness,
            OKG.LibraryCulturalHeritage,
            OKG.TechnologyWeb,
            OKG.EnvironmentAgriculture,
            OKG.GeneralCrossDomain,
        }
        expected_software_terms = {
            OKG.GraphDatabase,
            OKG.SparqlTooling,
            OKG.OntologyEngineering,
            OKG.ReasoningInference,
            OKG.DataMappingETL,
            OKG.DeveloperLibrary,
            OKG.KnowledgeGraphConstruction,
            OKG.AIAgentTooling,
            OKG.Visualization,
            OKG.StreamProcessing,
        }
        self.assertEqual(set(self.category_vocab.by_iri), expected_category_terms)
        self.assertEqual(set(self.software_vocab.by_iri), expected_software_terms)

        for vocabulary in (self.category_vocab, self.software_vocab):
            self.assertEqual(
                str(next(vocabulary.graph.objects(vocabulary.scheme, OWL.versionInfo))),
                "0.1.0",
            )
            for predicate in (
                DCTERMS.title,
                DCTERMS.description,
                DCTERMS.issued,
                DCTERMS.modified,
            ):
                self.assertTrue(any(vocabulary.graph.objects(vocabulary.scheme, predicate)))
            for concept in vocabulary.concepts:
                with self.subTest(concept=concept.iri):
                    self.assertIn((concept.iri, RDF.type, SKOS.Concept), vocabulary.graph)
                    self.assertIn((concept.iri, RDF.type, vocabulary.concept_class), vocabulary.graph)
                    self.assertEqual(
                        set(vocabulary.graph.objects(concept.iri, SKOS.inScheme)),
                        {vocabulary.scheme},
                    )
                    self.assertTrue(concept.label)
                    self.assertTrue(concept.definition)
                    self.assertTrue(concept.scope_note)

        controlled_terms = expected_category_terms | expected_software_terms
        self.assertFalse(
            any(
                (term, RDF.type, SKOS.Concept) in self.ontology
                or (term, RDF.type, OKG.Category) in self.ontology
                or (term, RDF.type, OKG.SoftwareType) in self.ontology
                for term in controlled_terms
            )
        )

    def test_curated_rdf_projects_to_unchanged_compatibility_json(self):
        expected_categories = json.loads((ROOT / "data/categories.json").read_text())
        expected_software_types = json.loads((ROOT / "data/software_types.json").read_text())
        self.assertEqual(
            semantic_config.classification_label_projection(
                self.curation.categories,
                self.category_vocab,
            ),
            expected_categories,
        )
        self.assertEqual(
            semantic_config.classification_label_projection(
                self.curation.software_types,
                self.software_vocab,
            ),
            expected_software_types,
        )

    def test_curated_assignments_use_existing_okg_or_wikidata_subjects(self):
        graph = Graph().parse(semantic_config.CURATION_PATH, format="turtle")
        for mapping, vocabulary, registry_key in (
            (self.curation.categories, self.category_vocab, "resource"),
            (self.curation.software_types, self.software_vocab, "software"),
        ):
            for qid, concept in mapping.items():
                slug = self.uri_registry[registry_key].get(qid)
                if slug is None:
                    subject = URIRef(f"http://www.wikidata.org/entity/{qid}")
                else:
                    subject = URIRef(
                        f"https://openknowledgegraphs.com/{registry_key}/{slug}/"
                    )
                with self.subTest(qid=qid, registry=registry_key):
                    self.assertIn((subject, vocabulary.classification_predicate, concept), graph)
                    self.assertIn(
                        (
                            subject,
                            OKG.wikidataId,
                            URIRef(f"https://www.wikidata.org/wiki/{qid}"),
                        ),
                        graph,
                    )

    def test_absent_source_assignments_are_retained_but_not_generated(self):
        generated_category_qids = {
            semantic_config.qid_from_wikidata_value(str(wikidata_id))
            for subject in self.ontologies.subjects(OKG.category, None)
            for wikidata_id in self.ontologies.objects(subject, OKG.wikidataId)
        }
        absent = set(self.curation.categories) - generated_category_qids
        self.assertTrue(absent)
        for qid in absent:
            slug = self.uri_registry["resource"].get(qid)
            if slug is None:
                subject = URIRef(f"http://www.wikidata.org/entity/{qid}")
            else:
                subject = URIRef(f"https://openknowledgegraphs.com/resource/{slug}/")
            self.assertFalse(any(self.ontologies.triples((subject, OKG.category, None))))

        for graph, predicate, assignments in (
            (self.ontologies, OKG.category, self.curation.categories),
            (self.software, OKG.softwareType, self.curation.software_types),
        ):
            for subject, concept in graph.subject_objects(predicate):
                wikidata_id = next(graph.objects(subject, OKG.wikidataId))
                qid = semantic_config.qid_from_wikidata_value(str(wikidata_id))
                self.assertEqual(assignments[qid], concept)

    def test_source_class_property_and_value_metadata_drive_queries(self):
        source_text = (ROOT / "sources.ttl").read_text()
        with tempfile.TemporaryDirectory() as directory:
            changed_path = Path(directory) / "sources.ttl"
            changed_path.write_text(
                source_text.replace("Q324254", "Q999999999").replace("P856", "P999"),
                encoding="utf-8",
            )
            changed = semantic_config.load_source_mappings(changed_path)
            class_id = changed.class_ids_for(semantic_config.ONTOLOGIES_DATASET)[0]
            query = fetch_data.build_type_base_query(class_id, changed)
            self.assertIn("wd:Q999999999", query)
            self.assertIn("wdt:P999", query)
            self.assertNotIn("wd:Q324254", query)
            self.assertNotIn("wdt:P856", query)

            changed.graph.remove((SRC["property-P999"], OKG.valueKind, None))
            changed.graph.add((SRC["property-P999"], OKG.valueKind, Literal("literal")))
            changed.graph.serialize(destination=changed_path, format="turtle")
            changed_value_kind = semantic_config.load_source_mappings(changed_path)
            with self.assertRaises(semantic_config.SemanticConfigError):
                fetch_data.build_type_base_query(class_id, changed_value_kind)

        pipeline_source = "\n".join(
            (ROOT / path).read_text()
            for path in (
                "scripts/fetch_data.py",
                "scripts/category_classifier.py",
            )
        )
        self.assertIsNone(re.search(r"\b[QP]\d{2,}\b", pipeline_source))

    def test_rdf_concept_definitions_drive_classifier_prompts(self):
        graph = Graph().parse(semantic_config.CATEGORIES_VOCAB_PATH, format="turtle")
        concept = OKG.Geospatial
        graph.remove((concept, SKOS.prefLabel, None))
        graph.remove((concept, SKOS.definition, None))
        graph.add((concept, SKOS.prefLabel, Literal("Spatial Test", lang="en")))
        graph.add((concept, SKOS.definition, Literal("Fixture-controlled definition.", lang="en")))

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "categories.ttl"
            graph.serialize(destination=fixture, format="turtle")
            vocabulary = semantic_config.load_controlled_vocabulary(fixture)
            prompt = category_classifier._build_prompt(
                [{"qid": "Q1", "title": "Example", "description": "Fixture"}],
                category_options=vocabulary.labels,
                definitions=vocabulary.prompt_definitions,
            )
        self.assertIn("Spatial Test", prompt)
        self.assertIn("Fixture-controlled definition.", prompt)

    def test_curated_rdf_changes_drive_record_assignments(self):
        qid = next(iter(sorted(self.curation.categories)))
        current = self.curation.categories[qid]
        replacement = next(iri for iri in self.category_vocab.by_iri if iri != current)
        fixture_mapping = dict(self.curation.categories)
        fixture_mapping[qid] = replacement
        record = fetch_data.ResourceRecord(
            item_iri=f"http://www.wikidata.org/entity/{qid}",
            label="Fixture",
            types={OKG.Ontology},
        )
        missing = fetch_data.apply_existing_categories(
            {record.item_iri: record},
            fixture_mapping,
            self.category_vocab,
        )
        self.assertEqual(missing, [])
        self.assertEqual(record.category, replacement)

    def test_frontend_vocabulary_projection_is_derived_from_rdf(self):
        expected = semantic_config.controlled_vocabulary_projection(
            self.category_vocab,
            self.software_vocab,
        )
        actual = json.loads((ROOT / "data/controlled_vocabularies.json").read_text())
        self.assertEqual(actual, expected)
        frontend_source = (ROOT / "site/app.js").read_text() + (ROOT / "site/index.html").read_text()
        self.assertIn("controlled_vocabularies.json", frontend_source)
        for concept in self.category_vocab.concepts + self.software_vocab.concepts:
            self.assertNotIn(concept.label, frontend_source)

    def test_json_field_names_are_unchanged(self):
        for dataset, expected_fields in BASELINE_JSON_FIELDS.items():
            payload = json.loads((ROOT / "data" / f"{dataset}.json").read_text())
            fields = set().union(*(item.keys() for item in payload["items"]))
            self.assertEqual(fields, expected_fields)

    def test_catalog_json_is_a_deterministic_rdf_projection(self):
        type_labels = self.source_mappings.projection_type_labels
        specifications = (
            (
                "ontologies",
                self.ontologies,
                {
                    OKG.Ontology,
                    OKG.ControlledVocabulary,
                    OKG.Taxonomy,
                    OKG.KnowledgeGraph,
                    OKG.OntologyLanguage,
                },
                False,
            ),
            ("software", self.software, {OKG.Software}, True),
        )
        for name, graph, allowed_types, include_software_fields in specifications:
            with self.subTest(dataset=name):
                expected = fetch_data.extract_items_from_graph(
                    graph,
                    allowed_types,
                    include_software_fields,
                    type_labels,
                )
                actual = json.loads((ROOT / "data" / f"{name}.json").read_text())["items"]
                self.assertEqual(actual, expected)

    def test_structural_mapping_terms_and_external_alignments_are_declared(self):
        mapping_classes = (
            OKG.SourceMapping,
            OKG.SourceClassMapping,
            OKG.SourcePropertyMapping,
            OKG.SourceEligibilityPolicy,
            OKG.SourceExclusion,
            OKG.SourceEligibilityException,
        )
        mapping_properties = (
            OKG.conceptClass,
            OKG.classificationPredicate,
            OKG.urlSlug,
            OKG.sortOrder,
            OKG.sourceDataset,
            OKG.catalogDataset,
            OKG.sourceClassId,
            OKG.sourcePropertyId,
            OKG.targetTerm,
            OKG.projectionValue,
            OKG.normalizedField,
            OKG.valueKind,
            OKG.cardinality,
            OKG.termComponentMarker,
            OKG.sourceExclusion,
            OKG.eligibilityException,
            OKG.sourceEntity,
        )
        for class_iri in mapping_classes:
            self.assertIn((class_iri, RDF.type, RDFS.Class), self.ontology)
        for property_iri in mapping_properties:
            self.assertIn((property_iri, RDF.type, RDF.Property), self.ontology)

        self.assertIn((OKG.Category, RDFS.subClassOf, SKOS.Concept), self.ontology)
        self.assertIn((OKG.SoftwareType, RDFS.subClassOf, SKOS.Concept), self.ontology)
        self.assertIn((OKG.title, RDFS.subPropertyOf, DCTERMS.title), self.ontology)
        self.assertIn((OKG.creator, RDFS.subPropertyOf, DCTERMS.creator), self.ontology)
        self.assertIn((OKG.relatedTo, RDFS.subPropertyOf, DCTERMS.relation), self.ontology)
        self.assertIn((OKG.sourceRepo, RDFS.subPropertyOf, SCHEMA.codeRepository), self.ontology)
        self.assertNotIn((OKG.sourceRepo, RDFS.subPropertyOf, DOAP.repository), self.ontology)
        part_of_mapping = next(
            mapping
            for mapping in self.source_mappings.property_mappings
            if mapping.normalized_field == "partOfEntity"
        )
        self.assertEqual(part_of_mapping.target_term, DCTERMS.isPartOf)

    def test_no_crosswalk_or_source_mirror_infrastructure_was_introduced(self):
        artifacts = self.ontology + self.sources + self.category_vocab.graph + self.software_vocab.graph
        forbidden = ("crosswalk", "mirror", "fuseki", "teacher")
        for subject, predicate, value in artifacts:
            rendered = f"{subject} {predicate} {value}".casefold()
            self.assertFalse(any(term in rendered for term in forbidden))


if __name__ == "__main__":
    unittest.main()
