"""Network-free workflow contracts for generation-aware semantic publication."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PUBLISH_PATH = ROOT / ".github/workflows/update-data.yml"
JOBS_PATH = ROOT / ".github/workflows/update-jobs.yml"
ROLLBACK_PATH = ROOT / ".github/workflows/deploy.yml"
VALIDATE_PATH = ROOT / ".github/workflows/validate.yml"
VERIFIER_PATH = ROOT / ".github/workflows/scripts/verify_task22_surfaces.py"
WORKER_DEPLOYMENT_PATH = ROOT / ".github/workflows/scripts/worker_deployment.py"
PUBLICATION_GATE_PATH = ROOT / ".github/workflows/scripts/catalog_publication_gate.py"


def step_block(workflow: str, name: str) -> str:
    marker = f"      - name: {name}"
    start = workflow.index(marker)
    next_step = workflow.find("\n      - name:", start + len(marker))
    return workflow[start:] if next_step == -1 else workflow[start:next_step]


def top_level_block(workflow: str, key: str) -> str:
    marker = f"{key}:\n"
    start = workflow.index(marker)
    lines = workflow[start:].splitlines(keepends=True)
    end = len(lines)
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() and not line.startswith((" ", "\t")):
            end = index
            break
    return "".join(lines[:end]).rstrip()


class Task37PublicationConcurrencyContractTests(unittest.TestCase):
    EXPECTED_CONCURRENCY = """concurrency:
  group: repository-publication
  cancel-in-progress: false
  queue: max"""

    EXPECTED_TRIGGERS = {
        PUBLISH_PATH: """on:
  schedule:
    - cron: "23 6 * * *"
  workflow_run:
    workflows: ["Update KG Jobs Data"]
    types: [completed]
    branches: [main]
  workflow_dispatch:
    inputs:
      initialize_semantic_search:
        description: Initialize or repair semantic search without changing catalog content
        required: false
        default: false
        type: boolean""",
        JOBS_PATH: """on:
  schedule:
    - cron: "0 3 * * *"
  workflow_dispatch:
    inputs:
      source:
        description: Production source identifier from sources.ttl; default refreshes all in bounded batches
        required: false
        default: all
        type: string
      dry_run:
        description: Dry run -- refresh and log, but skip publishing to data/jobs/ and committing
        required: false
        default: false
        type: boolean""",
        ROLLBACK_PATH: """on:
  workflow_dispatch:
    inputs:
      target:
        description: Immutable generation ID or Git ref to redeploy
        required: true
        type: string""",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflows = {
            path: path.read_text(encoding="utf-8")
            for path in (PUBLISH_PATH, JOBS_PATH, ROLLBACK_PATH)
        }

    def test_all_repository_writers_share_one_waiting_queue(self) -> None:
        concurrency_blocks = {
            path: top_level_block(workflow, "concurrency")
            for path, workflow in self.workflows.items()
        }
        self.assertEqual(
            set(concurrency_blocks.values()),
            {self.EXPECTED_CONCURRENCY},
            concurrency_blocks,
        )

    def test_triggers_and_manual_dispatch_inputs_match_the_approved_contract(self) -> None:
        for path, workflow in self.workflows.items():
            self.assertEqual(
                top_level_block(workflow, "on"),
                self.EXPECTED_TRIGGERS[path],
                path,
            )

    def test_catalog_chains_only_from_a_successful_scheduled_jobs_run(self) -> None:
        workflow = self.workflows[PUBLISH_PATH]
        self.assertIn("actions: read", workflow)
        for upstream_field in ("conclusion", "event", "head_branch"):
            self.assertIn(
                f"github.event.workflow_run.{upstream_field} || ''",
                workflow,
            )
        self.assertIn("UPSTREAM_COMPLETED_AT:", workflow)
        self.assertIn(
            "python .github/workflows/scripts/catalog_publication_gate.py",
            workflow,
        )
        self.assertTrue(PUBLICATION_GATE_PATH.is_file())
        self.assertIn("needs: publication_gate", workflow)
        self.assertIn("if: needs.publication_gate.outputs.should_publish == 'true'", workflow)

    def test_queued_runs_checkout_the_current_trigger_branch_tip(self) -> None:
        for path, workflow in self.workflows.items():
            self.assertIn("ref: ${{ github.ref_name }}", workflow, path)

    def test_publication_workflows_do_not_reconcile_after_generation(self) -> None:
        forbidden_commands = (
            "git pull",
            "git rebase",
            "git merge",
            "git push --force",
            "git push -f",
        )
        for path, workflow in self.workflows.items():
            for command in forbidden_commands:
                self.assertNotIn(command, workflow, f"{path}: {command}")


class Task22WorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.publish = PUBLISH_PATH.read_text(encoding="utf-8")
        cls.rollback = ROLLBACK_PATH.read_text(encoding="utf-8")

    def test_credentials_fail_before_catalog_commit(self) -> None:
        credential_step = "Require semantic-publication credentials before any catalog commit"
        self.assertIn("secrets.CLOUDFLARE_ACCOUNT_ID", self.publish)
        self.assertIn("secrets.CLOUDFLARE_API_TOKEN", self.publish)
        self.assertLess(
            self.publish.index(credential_step),
            self.publish.index("Commit complete generated artifact set"),
        )
        self.assertIn("No catalog commit has been created", step_block(self.publish, credential_step))

    def test_normal_publication_is_generation_atomic_across_all_surfaces(self) -> None:
        ordered_steps = (
            "Commit complete generated artifact set",
            "Confirm candidate checkout is the exact committed tree",
            "Bootstrap retained live vector generations before Worker upgrade",
            "Deploy generation-aware API Worker from exact candidate commit",
            "Verify upgraded API remains pinned to the live baseline",
            "Seed and verify candidate vector generation from exact commit",
            "Deploy candidate Pages artifact",
            "Verify candidate generation live",
            "Verify live API, vector generation, and all local MCP search tools",
            "Advance successful-generation tags atomically",
            "Prune vector generations older than catalog-current and catalog-previous",
        )
        offsets = [self.publish.index(step) for step in ordered_steps]
        self.assertEqual(offsets, sorted(offsets))
        self.assertIn("npm --prefix api run vectors:ensure", self.publish)
        self.assertIn("npm --prefix api run vectors:verify", self.publish)
        self.assertIn("npm --prefix api run deploy", self.publish)
        self.assertIn("steps.candidate_semantic.outcome == 'success'", step_block(
            self.publish, "Advance successful-generation tags atomically"
        ))

    def test_no_change_generation_performs_no_vector_or_api_mutation(self) -> None:
        for name in (
            "Bootstrap retained live vector generations before Worker upgrade",
            "Deploy generation-aware API Worker from exact candidate commit",
            "Seed and verify candidate vector generation from exact commit",
        ):
            self.assertIn(
                "steps.changes.outputs.changed == 'true'",
                step_block(self.publish, name),
            )
        self.assertIn("initialize_semantic_search:", self.publish)
        bootstrap = step_block(
            self.publish, "Initialize and verify unchanged semantic generation explicitly"
        )
        self.assertIn("inputs.initialize_semantic_search == true", bootstrap)
        self.assertIn("steps.changes.outputs.changed != 'true'", bootstrap)
        self.assertIn("vectors:provision", bootstrap)
        self.assertIn("vectors:ensure", bootstrap)
        self.assertIn("vectors:verify", bootstrap)
        common_migration = step_block(self.publish, "Apply semantic-readiness migration")
        self.assertIn("inputs.initialize_semantic_search == true", common_migration)
        for catalog_step in (
            "Refresh staged catalog from Wikidata",
            "Generate staged detail pages",
            "Validate complete staged catalog",
            "Detect substantive catalog changes",
        ):
            self.assertIn(
                "inputs.initialize_semantic_search != true",
                step_block(self.publish, catalog_step),
            )

    def test_automatic_rollback_restores_exact_worker_and_pre_advance_baseline(self) -> None:
        ordered_steps = (
            "Capture pre-publication API Worker deployment",
            "Restore pre-publication API Worker deployment automatically",
            "Prepare automatic rollback to pre-publication baseline",
            "Redeploy pre-publication Pages baseline automatically",
            "Verify automatic rollback live",
            "Verify automatic rollback API, vector generation, and MCP tools",
        )
        offsets = [self.publish.index(step) for step in ordered_steps]
        self.assertEqual(offsets, sorted(offsets))
        restore = step_block(self.publish, ordered_steps[1])
        self.assertIn("wrangler versions deploy", restore)
        self.assertIn("--config api/wrangler.toml", restore)
        self.assertIn("worker_deployment.py specs", restore)
        self.assertNotIn("npm --prefix api run deploy", restore)
        self.assertIn("steps.successful_tags.outcome == 'failure'", restore)
        for page_step in (
            "candidate_pages_config",
            "candidate_pages_build",
            "candidate_pages_upload",
        ):
            self.assertIn(f"steps.{page_step}.outcome == 'failure'", restore)
        prepare = step_block(self.publish, ordered_steps[2])
        self.assertIn('OKG_DATA_DIR="$RUNNER_TEMP/rollback-catalog/data"', prepare)
        self.assertIn("run vectors:ensure", prepare)
        self.assertIn("run vectors:verify", prepare)
        self.assertIn("steps.run.outputs.baseline_commit", prepare)
        self.assertNotIn("catalog-current", prepare)
        failure = step_block(self.publish, "Fail publication after candidate or rollback failure")
        self.assertIn("steps.successful_tags.outcome != 'success'", failure)
        tags = step_block(self.publish, "Advance successful-generation tags atomically")
        self.assertIn("continue-on-error: true", tags)

    def test_failed_explicit_bootstrap_restores_worker_without_touching_pages(self) -> None:
        restore = step_block(
            self.publish, "Restore pre-bootstrap API Worker deployment after failure"
        )
        self.assertIn("steps.changes.outputs.changed != 'true'", restore)
        self.assertIn("inputs.initialize_semantic_search == true", restore)
        self.assertIn("wrangler versions deploy", restore)
        self.assertIn("--config api/wrangler.toml", restore)
        self.assertNotIn("deploy-pages", restore)
        verify = step_block(
            self.publish, "Verify explicit semantic bootstrap across API and MCP tools"
        )
        self.assertIn("steps.bootstrap_manifest.outputs.generation_id", verify)

        baseline = step_block(
            self.publish, "Capture pre-publication API Worker deployment"
        )
        self.assertIn("wrangler deployments status", baseline)
        self.assertIn("--config api/wrangler.toml", baseline)

    def test_manual_rollback_rebuilds_before_exact_api_and_pages_cutover(self) -> None:
        ordered_steps = (
            "Resolve immutable rollback target",
            "Check out target in an isolated worktree",
            "Ensure and verify target vector generation from exact rollback worktree",
            "Deploy generation-aware API Worker for exact rollback data",
            "Deploy rollback generation",
            "Verify rollback generation live",
            "Verify rollback API, vector generation, and all local MCP search tools",
            "Advance moving pointers after verified rollback",
            "Prune vector generations older than catalog-current and catalog-previous",
        )
        offsets = [self.rollback.index(step) for step in ordered_steps]
        self.assertEqual(offsets, sorted(offsets))
        ensure = step_block(self.rollback, ordered_steps[2])
        self.assertIn("runner.temp }}/rollback-catalog/data", ensure)
        self.assertIn("vectors:ensure", ensure)
        self.assertIn("vectors:verify", ensure)
        self.assertIn("RETAIN_GENERATIONS", self.rollback)

    def test_retention_preserves_current_and_previous_only_after_success(self) -> None:
        prune = step_block(
            self.publish,
            "Prune vector generations older than catalog-current and catalog-previous",
        )
        self.assertIn("steps.successful_tags.outcome == 'success'", prune)
        self.assertIn("catalog-current catalog-previous", prune)
        self.assertIn("RETAIN_GENERATIONS", prune)
        self.assertIn("vectors:prune", prune)

    def test_pull_request_ci_runs_api_mcp_and_workflow_contracts(self) -> None:
        validate = VALIDATE_PATH.read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests -v", validate)
        self.assertIn("npm --prefix api test", validate)
        self.assertIn("python -m unittest discover -s mcp-server/tests -v", validate)


class Task22SurfaceVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("task22_verifier", VERIFIER_PATH)
        assert spec and spec.loader
        cls.verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.verifier)

    def test_http_verifier_requires_one_semantic_generation_everywhere(self) -> None:
        generation = "20260817T120000Z-aaaaaaaaaaaa"

        def response(url: str, **_kwargs):
            if "manifest.json" in url:
                return {"generationId": generation}
            if "/health" in url or "api.example/?" in url:
                return {
                    "searchMode": "semantic",
                    "catalogGenerationId": generation,
                    "vectorGenerationId": generation,
                    "fallbackReason": None,
                }
            return {
                "searchMode": "semantic",
                "catalogGenerationId": generation,
                "vectorGenerationId": generation,
                "fallbackReason": None,
                "results": [{"title": "fixture"}],
            }

        with patch.object(self.verifier, "fetch_json", side_effect=response) as fetch:
            self.verifier.verify_http_surfaces("https://pages.example", "https://api.example", generation)
        self.assertEqual(fetch.call_count, 6)

    def test_verifier_rejects_text_fallback_and_dispatches_all_mcp_tools(self) -> None:
        generation = "20260817T120000Z-bbbbbbbbbbbb"
        payload = {
            "searchMode": "text-fallback",
            "catalogGenerationId": generation,
            "vectorGenerationId": generation,
            "fallbackReason": "vector-error",
            "results": [{"title": "fixture"}],
        }
        with self.assertRaisesRegex(AssertionError, "not semantic"):
            self.verifier.assert_generation_metadata(payload, generation, "fixture")

        class FakeFastMCP:
            def __init__(self) -> None:
                self.calls = []

            async def list_tools(self):
                return [
                    SimpleNamespace(name=name)
                    for name in self.verifier.MCP_ACCEPTANCE_CALLS
                ]

            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                output = "\n".join(
                    (
                        "**Search mode**: `semantic`",
                        f"**Catalog generation**: `{generation}`",
                        f"**Vector generation**: `{generation}`",
                    )
                )
                return ([SimpleNamespace(text=output)], {"result": output})

        mcp = FakeFastMCP()
        mcp.verifier = self.verifier
        asyncio.run(self.verifier.verify_registered_mcp_tools(mcp, generation))
        self.assertEqual(
            {name for name, _arguments in mcp.calls},
            set(self.verifier.MCP_ACCEPTANCE_CALLS),
        )
        self.assertEqual(len(mcp.calls), 4)

    def test_verifier_rejects_unverified_mcp_registrations(self) -> None:
        class FakeFastMCP:
            async def list_tools(self):
                return [SimpleNamespace(name="unverified_tool")]

        with self.assertRaisesRegex(AssertionError, "registration differs"):
            asyncio.run(
                self.verifier.verify_registered_mcp_tools(
                    FakeFastMCP(), "20260817T120000Z-cccccccccccc"
                )
            )


class WorkerDeploymentSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("worker_deployment", WORKER_DEPLOYMENT_PATH)
        assert spec and spec.loader
        cls.worker_deployment = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.worker_deployment)

    def test_preserves_every_active_version_and_traffic_percentage(self) -> None:
        versions = self.worker_deployment.deployment_versions(
            {
                "id": "deployment-1",
                "versions": [
                    {"version_id": "version-a", "percentage": 15},
                    {"version_id": "version-b", "percentage": 85},
                ],
            }
        )
        self.assertEqual(versions, [("version-a", 15.0), ("version-b", 85.0)])

    def test_rejects_incomplete_or_malformed_deployment_snapshot(self) -> None:
        with self.assertRaisesRegex(ValueError, "no active versions"):
            self.worker_deployment.deployment_versions({"versions": []})
        with self.assertRaisesRegex(ValueError, "not 100%"):
            self.worker_deployment.deployment_versions(
                {"versions": [{"version_id": "version-a", "percentage": 50}]}
            )


if __name__ == "__main__":
    unittest.main()
