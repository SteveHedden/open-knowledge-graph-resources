from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import catalog_snapshot


GENERATION_ID = "20260814T120000Z-0123456789ab"


def write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def make_catalog(root: Path) -> None:
    ontology = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
<https://example.test/ontology> a owl:Ontology ; owl:versionInfo "1.2.3" .
"""
    categories = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
<https://example.test/categories> a owl:Ontology ; owl:versionInfo "2.0.0" .
"""
    software_types = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
<https://example.test/software-types> a owl:Ontology ; owl:versionInfo "3.0.0" .
"""
    data_graph = "@prefix ex: <https://example.test/> . ex:item ex:name \"Example\" .\n"
    write(root / "ontology.ttl", ontology)
    write(root / "sources.ttl", "@prefix ex: <https://example.test/> . ex:source ex:name \"Source\" .\n")
    write(root / "vocabularies/categories.ttl", categories)
    write(root / "vocabularies/software-types.ttl", software_types)
    write(root / "curation/classifications.ttl", "@prefix ex: <https://example.test/> . ex:item ex:class ex:C .\n")
    write(root / "data/ontologies.ttl", data_graph)
    write(root / "data/software.ttl", data_graph.replace("item", "software"))
    write(root / "data/ontologies.json", json.dumps({"generatedAt": "2026-08-14T11:00:00Z", "items": [{"title": "A"}]}) + "\n")
    write(root / "data/software.json", json.dumps({"generatedAt": "2026-08-14T11:00:00Z", "items": [{"title": "B"}]}) + "\n")
    write(root / "data/uri_registry.json", '{"resource": {}, "software": {}}\n')
    write(root / "data/categories.json", "{}\n")
    write(root / "data/software_types.json", "{}\n")
    write(root / "data/controlled_vocabularies.json", "{}\n")
    write(root / "data/page_qids.json", '{"resource": {}, "software": {}}\n')
    write(root / "site/index.html", "<html>catalog</html>\n")
    write(root / "site/app.js", "console.log('catalog');\n")
    write(root / "site/sitemap.xml", '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://openknowledgegraphs.com/</loc></url></urlset>\n')
    write(root / "site/resource/example/index.html", "<html>resource</html>\n")
    write(root / "site/software/example/index.html", "<html>software</html>\n")
    write(
        root / "validation/catalog-manifest.schema.json",
        (ROOT / "validation/catalog-manifest.schema.json").read_text(encoding="utf-8"),
    )


def finalize(root: Path) -> dict:
    return catalog_snapshot.write_manifest(
        root,
        started_at="2026-08-14T11:00:00Z",
        source_retrieved_at="2026-08-14T11:30:00Z",
        completed_at="2026-08-14T12:00:00Z",
    )


class CanonicalDigestTests(unittest.TestCase):
    def test_fixed_stream_uses_sorted_utf8_path_nul_digest_and_newline(self):
        digest_a = hashlib.sha256(b"a").hexdigest()
        digest_unicode = hashlib.sha256(b"unicode").hexdigest()
        stream = (
            "a/path\0".encode("utf-8") + digest_a.encode("ascii") + b"\n"
            + "z/é\0".encode("utf-8") + digest_unicode.encode("ascii") + b"\n"
        )
        expected = hashlib.sha256(stream).hexdigest()
        actual = catalog_snapshot.canonical_artifact_digest(
            [("z/é", digest_unicode), ("a/path", digest_a)]
        )
        self.assertEqual(actual, expected)

    def test_windows_separator_is_canonicalized_and_invalid_entries_are_rejected(self):
        self.assertEqual(
            catalog_snapshot.canonical_artifact_digest([(r"data\ontologies.json", "0" * 64)]),
            catalog_snapshot.canonical_artifact_digest([("data/ontologies.json", "0" * 64)]),
        )
        with self.assertRaises(catalog_snapshot.SnapshotError):
            catalog_snapshot.canonical_artifact_digest([("a", "0" * 64), ("a", "1" * 64)])
        with self.assertRaises(catalog_snapshot.SnapshotError):
            catalog_snapshot.canonical_artifact_digest([("a", "A" * 64)])


class PromotionSafetyTests(unittest.TestCase):
    def test_promotion_preserves_untracked_files_while_removing_obsolete_tracked_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repository"
            candidate = base / "candidate"
            make_catalog(repository)
            make_catalog(candidate)
            write(repository / "site/resource/obsolete/index.html", "obsolete\n")
            write(repository / "site/resource/example/index 2.html", "user-owned\n")
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "add",
                    "ontology.ttl",
                    "sources.ttl",
                    "data",
                    "curation",
                    "vocabularies",
                    "site/index.html",
                    "site/app.js",
                    "site/sitemap.xml",
                    "site/resource/example/index.html",
                    "site/resource/obsolete/index.html",
                    "site/software/example/index.html",
                ],
                cwd=repository,
                check=True,
            )
            finalize(candidate)

            catalog_snapshot.promote_candidate(candidate, repository)

            self.assertFalse((repository / "site/resource/obsolete/index.html").exists())
            self.assertEqual(
                (repository / "site/resource/example/index 2.html").read_text(),
                "user-owned\n",
            )


class JobsManifestIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        make_catalog(self.root)
        write(self.root / "data/jobs/jobs.json", '{"items": []}\n')
        write(self.root / "data/jobs/jobs.ttl", "@prefix ex: <https://example.test/> . ex:job ex:name \"Job\" .\n")
        write(self.root / "data/jobs/run.json", '{"retrievedAt": "2026-08-18T21:00:00Z"}\n')

    def tearDown(self):
        self.temporary.cleanup()

    def finalize_jobs(self):
        return catalog_snapshot.write_jobs_manifest(
            self.root,
            started_at="2026-08-18T21:00:00Z",
            source_retrieved_at="2026-08-18T21:00:00Z",
            completed_at="2026-08-18T21:00:01Z",
        )

    def test_core_manifest_excludes_jobs_files(self):
        manifest = finalize(self.root)
        covered = {entry["path"] for entry in manifest["artifacts"]}
        self.assertFalse(any(path.startswith("data/jobs/") for path in covered))

    def test_core_manifest_content_is_stable_across_jobs_hourly_refresh(self):
        manifest_before = finalize(self.root)
        write(self.root / "data/jobs/jobs.json", '{"items": [{"title": "Changed"}]}\n')
        self.assertEqual(catalog_snapshot.verify_manifest(self.root), manifest_before)

    def test_jobs_manifest_round_trip_covers_every_jobs_file_once(self):
        manifest = self.finalize_jobs()
        self.assertEqual(catalog_snapshot.verify_jobs_manifest(self.root), manifest)
        covered = {entry["path"] for entry in manifest["artifacts"]}
        self.assertEqual(
            covered,
            {"data/jobs/jobs.json", "data/jobs/jobs.ttl", "data/jobs/run.json"},
        )
        self.assertNotIn(catalog_snapshot.JOBS_MANIFEST_PATH, covered)

    def test_jobs_manifest_tamper_breaks_verification(self):
        self.finalize_jobs()
        write(self.root / "data/jobs/jobs.json", '{"items": [{"title": "Tampered"}]}\n')
        with self.assertRaises(catalog_snapshot.SnapshotError):
            catalog_snapshot.verify_jobs_manifest(self.root)

    def test_partition_requires_every_deployed_file_covered_exactly_once(self):
        finalize(self.root)
        self.finalize_jobs()
        catalog_snapshot.verify_manifest_partition(self.root)

    def test_verify_all_fails_when_jobs_manifest_is_missing(self):
        finalize(self.root)
        with self.assertRaises(catalog_snapshot.SnapshotError):
            catalog_snapshot.verify_all_manifests(self.root)

    def test_verify_all_manifests_checks_both_manifests_and_partition(self):
        finalize(self.root)
        self.finalize_jobs()
        catalog_snapshot.verify_all_manifests(self.root)

    def test_build_pages_requires_valid_jobs_manifest_when_jobs_data_present(self):
        finalize(self.root)
        with tempfile.TemporaryDirectory() as destination:
            with self.assertRaises(catalog_snapshot.SnapshotError):
                catalog_snapshot.build_pages_artifact(self.root, Path(destination) / "out")

    def test_substantive_changes_ignores_jobs_files(self):
        finalize(self.root)
        candidate = self.root.parent / "candidate"
        shutil.copytree(self.root, candidate)
        write(candidate / "data/jobs/jobs.ttl", "@prefix ex: <https://example.test/> . ex:job ex:name \"Changed\" .\n")
        self.assertEqual(catalog_snapshot.substantive_changes(candidate, self.root), [])

    def test_build_pages_includes_jobs_files_once_both_manifests_are_valid(self):
        finalize(self.root)
        self.finalize_jobs()
        with tempfile.TemporaryDirectory() as destination:
            output = Path(destination) / "out"
            catalog_snapshot.build_pages_artifact(self.root, output)
            self.assertTrue((output / "data/jobs/jobs.json").is_file())


class ManifestContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        make_catalog(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_manifest_round_trip_covers_every_file_once_and_excludes_itself(self):
        manifest = finalize(self.root)
        self.assertEqual(catalog_snapshot.verify_manifest(self.root), manifest)
        covered = {entry["path"] for entry in manifest["artifacts"]}
        self.assertNotIn(catalog_snapshot.MANIFEST_PATH, covered)
        self.assertNotIn("site/resource/example/index.html", covered)
        self.assertEqual(
            {entry["path"] for entry in manifest["directoryTrees"]},
            {"site/resource", "site/software"},
        )

    def test_generation_id_uses_completed_at_and_canonical_content_suffix(self):
        manifest = finalize(self.root)
        expected_digest = catalog_snapshot.generation_digest(manifest["artifacts"], manifest["directoryTrees"])
        self.assertEqual(manifest["generationId"], f"20260814T120000Z-{expected_digest[:12]}")
        self.assertRegex(manifest["generationId"], catalog_snapshot.GENERATION_ID_RE)

    def test_counts_versions_and_timestamp_semantics_match_staged_artifacts(self):
        manifest = finalize(self.root)
        self.assertEqual(manifest["counts"]["records"], {"resources": 1, "software": 1, "total": 2})
        self.assertEqual(manifest["counts"]["triples"]["total"], 2)
        self.assertEqual(manifest["versions"]["ontology"], "1.2.3")
        self.assertEqual(manifest["versions"]["vocabularies"]["categories"], "2.0.0")
        self.assertEqual(manifest["versions"]["vocabularies"]["softwareTypes"], "3.0.0")
        with self.assertRaises(catalog_snapshot.SnapshotError):
            catalog_snapshot.write_manifest(
                self.root,
                "2026-08-14T12:00:00Z",
                "2026-08-14T11:30:00Z",
                "2026-08-14T13:00:00Z",
            )

    def test_bounded_artifact_tamper_breaks_verification(self):
        finalize(self.root)
        write(self.root / "data/uri_registry.json", '{"resource": {"Q1": "changed"}, "software": {}}\n')
        with self.assertRaises(catalog_snapshot.SnapshotError):
            catalog_snapshot.verify_manifest(self.root)

    def test_page_tree_hash_and_count_change_on_edit_add_and_remove(self):
        original_digest, original_count = catalog_snapshot.tree_digest(self.root, "site/resource")
        write(self.root / "site/resource/example/index.html", "edited")
        edited_digest, edited_count = catalog_snapshot.tree_digest(self.root, "site/resource")
        self.assertNotEqual(edited_digest, original_digest)
        self.assertEqual(edited_count, original_count)
        write(self.root / "site/resource/added/index.html", "added")
        added_digest, added_count = catalog_snapshot.tree_digest(self.root, "site/resource")
        self.assertNotEqual(added_digest, edited_digest)
        self.assertEqual(added_count, edited_count + 1)
        (self.root / "site/resource/example/index.html").unlink()
        removed_digest, removed_count = catalog_snapshot.tree_digest(self.root, "site/resource")
        self.assertNotEqual(removed_digest, added_digest)
        self.assertEqual(removed_count, added_count - 1)

    def test_build_pages_uses_verified_manifest_and_public_layout(self):
        finalize(self.root)
        output = self.root.parent / (self.root.name + "-pages")
        try:
            catalog_snapshot.build_pages_artifact(self.root, output)
            self.assertTrue((output / "index.html").is_file())
            self.assertTrue((output / "resource/example/index.html").is_file())
            self.assertTrue((output / "data/manifest.json").is_file())
            self.assertFalse((output / "site/index.html").exists())
        finally:
            shutil.rmtree(output, ignore_errors=True)

    def test_manifest_schema_document_is_valid_json(self):
        schema = json.loads((ROOT / "validation/catalog-manifest.schema.json").read_text())
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], "1.0.0")
        self.assertFalse(schema["additionalProperties"])

    def test_manifest_is_enforced_against_json_schema(self):
        manifest = finalize(self.root)
        manifest["unexpected"] = True
        write(self.root / catalog_snapshot.MANIFEST_PATH, json.dumps(manifest) + "\n")
        with self.assertRaisesRegex(catalog_snapshot.SnapshotError, "JSON Schema"):
            catalog_snapshot.verify_manifest(self.root)


class NoChangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.baseline = Path(self.temporary.name) / "baseline"
        self.candidate = Path(self.temporary.name) / "candidate"
        make_catalog(self.baseline)
        finalize(self.baseline)
        shutil.copytree(self.baseline, self.candidate)
        (self.candidate / catalog_snapshot.MANIFEST_PATH).unlink()

    def tearDown(self):
        self.temporary.cleanup()

    def test_generated_at_only_change_reuses_existing_generation(self):
        payload = json.loads((self.candidate / "data/ontologies.json").read_text())
        payload["generatedAt"] = "2026-08-15T11:00:00Z"
        write(self.candidate / "data/ontologies.json", json.dumps(payload, indent=2) + "\n")
        self.assertEqual(catalog_snapshot.substantive_changes(self.candidate, self.baseline), [])

    def test_rdf_serialization_and_json_key_order_are_normalized(self):
        write(self.candidate / "data/ontologies.ttl", '<https://example.test/item> <https://example.test/name> "Example" .\n')
        payload = json.loads((self.candidate / "data/uri_registry.json").read_text())
        write(self.candidate / "data/uri_registry.json", json.dumps(payload, indent=4) + "\n")
        self.assertEqual(catalog_snapshot.substantive_changes(self.candidate, self.baseline), [])

    def test_substantive_json_rdf_sitemap_and_page_changes_are_detected(self):
        cases = {
            "json": ("data/ontologies.json", '{"generatedAt":"2026-08-14T11:00:00Z","items":[{"title":"Changed"}]}'),
            "rdf": ("data/ontologies.ttl", '@prefix ex: <https://example.test/> . ex:item ex:name "Changed" .'),
            "sitemap": ("site/sitemap.xml", '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://openknowledgegraphs.com/changed</loc></url></urlset>'),
            "page": ("site/resource/example/index.html", "<html>changed</html>"),
        }
        for name, (relative, content) in cases.items():
            with self.subTest(name=name):
                candidate = Path(self.temporary.name) / f"candidate-{name}"
                shutil.copytree(self.candidate, candidate)
                write(candidate / relative, content)
                self.assertIn(relative, catalog_snapshot.substantive_changes(candidate, self.baseline))

    def test_missing_published_manifest_forces_initial_generation(self):
        (self.baseline / catalog_snapshot.MANIFEST_PATH).unlink()
        self.assertEqual(
            catalog_snapshot.substantive_changes(self.candidate, self.baseline),
            [catalog_snapshot.MANIFEST_PATH],
        )


def smoke_responses(generation_id: str = GENERATION_ID) -> dict[str, bytes]:
    artifacts = []
    responses: dict[str, bytes] = {}
    for relative in catalog_snapshot.SMOKE_CHECKSUM_PATHS:
        content = f"content for {relative}".encode()
        responses["/" + relative] = content
        artifacts.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
    responses["/data/manifest.json"] = json.dumps({"generationId": generation_id, "artifacts": artifacts}).encode()
    responses["/"] = b"homepage"
    responses["/sitemap.xml"] = "\n".join(
        "https://openknowledgegraphs.com" + path for path in catalog_snapshot.PINNED_PAGES
    ).encode()
    for path, qid in catalog_snapshot.PINNED_PAGES.items():
        responses[path] = (
            "https://openknowledgegraphs.com" + path + " https://www.wikidata.org/wiki/" + qid
        ).encode()
    return responses


class SmokeCheckTests(unittest.TestCase):
    def test_live_check_uses_cache_busters_timeout_checksums_and_pinned_identity(self):
        responses = smoke_responses()
        calls: list[tuple[str, float]] = []

        def fetch(url: str, timeout: float) -> tuple[int, bytes]:
            calls.append((url, timeout))
            return 200, responses[urlsplit(url).path]

        attempts = catalog_snapshot.live_smoke_check(
            "https://pages.example/", GENERATION_ID, fetch=fetch, sleep=lambda _: None
        )
        self.assertEqual(attempts, 1)
        for url, timeout in calls:
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query["generation"], [GENERATION_ID])
            self.assertEqual(query["attempt"], ["1"])
            self.assertEqual(timeout, 20.0)

    def test_delayed_propagation_retries_every_fifteen_seconds_then_succeeds(self):
        responses = smoke_responses()
        clock = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        def fetch(url: str, timeout: float) -> tuple[int, bytes]:
            if parse_qs(urlsplit(url).query)["attempt"] == ["1"]:
                return 404, b"not propagated"
            return 200, responses[urlsplit(url).path]

        attempts = catalog_snapshot.live_smoke_check(
            "https://pages.example/", GENERATION_ID, fetch=fetch, sleep=sleep, monotonic=lambda: clock[0]
        )
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [15.0])

    def test_persistent_failure_stops_at_deadline(self):
        clock = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        with self.assertRaises(catalog_snapshot.SmokeCheckError):
            catalog_snapshot.live_smoke_check(
                "https://pages.example/",
                GENERATION_ID,
                fetch=lambda url, timeout: (404, b"missing"),
                sleep=sleep,
                monotonic=lambda: clock[0],
                deadline_seconds=30.0,
            )
        self.assertEqual(sleeps, [15.0, 15.0])

    def test_wrong_generation_checksum_sitemap_and_page_identity_fail(self):
        base = smoke_responses()
        mutations = {
            "generation": ("/data/manifest.json", json.dumps({"generationId": "20260814T120000Z-ffffffffffff", "artifacts": []}).encode()),
            "checksum": ("/data/ontologies.ttl", b"tampered"),
            "sitemap": ("/sitemap.xml", b"missing pinned pages"),
            "identity": ("/software/rdflib/", b"wrong identity"),
        }
        for name, (path, content) in mutations.items():
            with self.subTest(name=name):
                responses = dict(base)
                responses[path] = content
                with self.assertRaises(catalog_snapshot.SnapshotError):
                    catalog_snapshot.check_live_once(
                        "https://pages.example/",
                        GENERATION_ID,
                        1,
                        fetch=lambda url, timeout, responses=responses: (200, responses[urlsplit(url).path]),
                    )


class GitPointerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.repository = self.root / "repository"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", str(self.repository)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=self.repository, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)], cwd=self.repository, check=True)

    def tearDown(self):
        self.temporary.cleanup()

    def commit(self, generation_id: str, marker: str) -> str:
        write(self.repository / "data/manifest.json", json.dumps({"generationId": generation_id}) + "\n")
        write(self.repository / "marker", marker)
        subprocess.run(["git", "add", "."], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "-m", marker], cwd=self.repository, check=True, capture_output=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repository, check=True, capture_output=True, text=True
        ).stdout.strip()

    def tag_target(self, name: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", f"refs/tags/{name}^{{commit}}"],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_success_and_rollback_pointer_semantics_and_immutable_rejection(self):
        first_id = "20260814T120000Z-aaaaaaaaaaaa"
        second_id = "20260815T120000Z-bbbbbbbbbbbb"
        first = self.commit(first_id, "first")
        catalog_snapshot.advance_success_tags(self.repository, first_id, first)
        self.assertEqual(self.tag_target("catalog-current"), first)
        self.assertEqual(self.tag_target(f"catalog-generation/{first_id}"), first)
        second = self.commit(second_id, "second")
        catalog_snapshot.advance_success_tags(self.repository, second_id, second)
        self.assertEqual(self.tag_target("catalog-current"), second)
        self.assertEqual(self.tag_target("catalog-previous"), first)
        with self.assertRaises(catalog_snapshot.SnapshotError):
            catalog_snapshot.advance_success_tags(self.repository, second_id, second)
        catalog_snapshot.advance_rollback_tags(self.repository, first)
        self.assertEqual(self.tag_target("catalog-current"), first)
        self.assertEqual(self.tag_target("catalog-previous"), second)

    def test_success_tag_rejects_generation_id_not_bound_to_commit_manifest(self):
        actual_id = "20260814T120000Z-dddddddddddd"
        supplied_id = "20260814T120000Z-eeeeeeeeeeee"
        commit = self.commit(actual_id, "mismatch")
        with self.assertRaisesRegex(catalog_snapshot.SnapshotError, "not"):
            catalog_snapshot.advance_success_tags(self.repository, supplied_id, commit)

    def test_rollback_target_accepts_generation_id_and_git_ref(self):
        generation_id = "20260814T120000Z-aaaaaaaaaaaa"
        commit = self.commit(generation_id, "first")
        subprocess.run(["git", "tag", f"catalog-generation/{generation_id}", commit], cwd=self.repository, check=True)
        self.assertEqual(catalog_snapshot.resolve_rollback_target(self.repository, generation_id), (commit, generation_id))
        self.assertEqual(catalog_snapshot.resolve_rollback_target(self.repository, commit), (commit, generation_id))

    def test_verified_raw_ref_rollback_receives_immutable_generation_tag(self):
        generation_id = "20260814T120000Z-cccccccccccc"
        commit = self.commit(generation_id, "raw target")
        catalog_snapshot.advance_rollback_tags(self.repository, commit)
        self.assertEqual(self.tag_target("catalog-current"), commit)
        self.assertEqual(self.tag_target(f"catalog-generation/{generation_id}"), commit)


class WorkflowContractTests(unittest.TestCase):
    def test_publication_and_rollback_share_non_canceling_max_queue(self):
        for relative in (".github/workflows/update-data.yml", ".github/workflows/deploy.yml"):
            workflow = (ROOT / relative).read_text()
            self.assertIn("group: catalog-publication", workflow)
            self.assertIn("cancel-in-progress: false", workflow)
            self.assertIn("queue: max", workflow)

    def test_only_refresh_promotes_candidates_and_tag_pushes_cannot_publish(self):
        refresh = (ROOT / ".github/workflows/update-data.yml").read_text()
        rollback = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertNotIn("\n  push:", refresh)
        self.assertNotIn("\n  push:", rollback)
        self.assertIn("workflow_dispatch:", rollback)
        self.assertIn('git worktree add --detach "$RUNNER_TEMP/published-baseline" HEAD', refresh)
        self.assertIn('--membership-baseline "${{ steps.published_baseline.outputs.path }}"', refresh)
        self.assertLess(refresh.index("Verify candidate generation live"), refresh.index("Advance successful-generation tags atomically"))

    def test_exact_commit_verification_rollback_and_summary_fields_exist(self):
        refresh = (ROOT / ".github/workflows/update-data.yml").read_text()
        for text in (
            "Confirm candidate checkout is the exact committed tree",
            "Redeploy catalog-current automatically",
            "repositoryChanged",
            "pagesChanged",
            "liveVerified",
            "rollbackAttempted",
            "rollbackSucceeded",
            "published successfully",
        ):
            self.assertIn(text, refresh)

    def test_rollback_validation_and_build_use_target_generation_scripts(self):
        refresh = (ROOT / ".github/workflows/update-data.yml").read_text()
        rollback = (ROOT / ".github/workflows/deploy.yml").read_text()
        target_script = '$RUNNER_TEMP/rollback-catalog/scripts/catalog_snapshot.py'
        target_validator = '$RUNNER_TEMP/rollback-catalog/scripts/validate_catalog.py'
        target_requirements = '$RUNNER_TEMP/rollback-catalog/requirements.txt'
        self.assertGreaterEqual(refresh.count(target_script), 3)
        self.assertIn(target_validator, refresh)
        self.assertIn(target_requirements, refresh)
        self.assertGreaterEqual(rollback.count(target_script), 3)
        self.assertIn(target_validator, rollback)
        self.assertIn(target_requirements, rollback)


if __name__ == "__main__":
    unittest.main()
