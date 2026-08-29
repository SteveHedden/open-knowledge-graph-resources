"""Behavioral truth table for the nightly catalog publication gate."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / ".github/workflows/scripts/catalog_publication_gate.py"
SPEC = importlib.util.spec_from_file_location("catalog_publication_gate", GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def workflow_run(run_id: int, completed_at: str, *, branch: str = "main") -> dict:
    return {
        "id": run_id,
        "conclusion": "success",
        "head_branch": branch,
        "updated_at": completed_at,
    }


def publish_job(started_at: str, *, conclusion: str = "success") -> dict:
    return {
        "name": "publish",
        "conclusion": conclusion,
        "started_at": started_at,
    }


class FakeApi:
    def __init__(self, *, runs=None, jobs=None, fail=False):
        self.runs = runs or {}
        self.jobs = jobs or {}
        self.fail = fail
        self.calls = []

    def workflow_runs(self, workflow, event):
        self.calls.append(("runs", workflow, event))
        if self.fail:
            raise gate.ApiError("simulated API failure")
        return self.runs.get((workflow, event), [])

    def run_jobs(self, run_id):
        self.calls.append(("jobs", run_id))
        if self.fail:
            raise gate.ApiError("simulated API failure")
        return self.jobs.get(run_id, [])


class CatalogPublicationGateTests(unittest.TestCase):
    NOW = datetime(2026, 8, 29, 6, 23, tzinfo=timezone.utc)

    def test_manual_dispatch_always_publishes_without_api_history(self):
        api = FakeApi(fail=True)
        decision = gate.decide(
            event_name="workflow_dispatch",
            default_branch="main",
            now=self.NOW,
            api=api,
        )
        self.assertTrue(decision.should_publish)
        self.assertEqual(decision.reason, "manual-dispatch")
        self.assertEqual(api.calls, [])

    def test_ineligible_upstream_runs_never_publish(self):
        cases = (
            ({"upstream_conclusion": "failure", "upstream_event": "schedule", "upstream_branch": "main"}, "upstream-not-successful"),
            ({"upstream_conclusion": "success", "upstream_event": "workflow_dispatch", "upstream_branch": "main"}, "upstream-not-scheduled"),
            ({"upstream_conclusion": "success", "upstream_event": "schedule", "upstream_branch": "feature"}, "upstream-not-default-branch"),
        )
        for fields, expected_reason in cases:
            with self.subTest(expected_reason):
                api = FakeApi(fail=True)
                decision = gate.decide(
                    event_name="workflow_run",
                    default_branch="main",
                    upstream_completed_at="2026-08-29T04:00:00Z",
                    now=self.NOW,
                    api=api,
                    **fields,
                )
                self.assertFalse(decision.should_publish)
                self.assertEqual(decision.reason, expected_reason)
                self.assertEqual(api.calls, [])

    def test_chain_skips_when_fallback_publish_started_after_jobs_completed(self):
        api = FakeApi(
            runs={(gate.CATALOG_WORKFLOW, "schedule"): [workflow_run(200, "2026-08-29T04:30:00Z")]},
            jobs={200: [publish_job("2026-08-29T04:01:00Z")]},
        )
        decision = gate.decide(
            event_name="workflow_run",
            default_branch="main",
            upstream_branch="main",
            upstream_conclusion="success",
            upstream_event="schedule",
            upstream_completed_at="2026-08-29T04:00:00Z",
            now=self.NOW,
            api=api,
        )
        self.assertFalse(decision.should_publish)
        self.assertEqual(decision.reason, "fallback-already-published-current-jobs")

    def test_chain_runs_when_fallback_publish_started_before_jobs_completed(self):
        api = FakeApi(
            runs={(gate.CATALOG_WORKFLOW, "schedule"): [workflow_run(201, "2026-08-29T04:30:00Z")]},
            jobs={201: [publish_job("2026-08-29T03:59:59Z")]},
        )
        decision = gate.decide(
            event_name="workflow_run",
            default_branch="main",
            upstream_branch="main",
            upstream_conclusion="success",
            upstream_event="schedule",
            upstream_completed_at="2026-08-29T04:00:00Z",
            now=self.NOW,
            api=api,
        )
        self.assertTrue(decision.should_publish)
        self.assertEqual(decision.reason, "successful-scheduled-jobs")

    def test_fallback_skips_after_current_cycle_chained_publication(self):
        api = FakeApi(
            runs={
                (gate.JOBS_WORKFLOW, "schedule"): [workflow_run(300, "2026-08-29T04:00:00Z")],
                (gate.CATALOG_WORKFLOW, "workflow_run"): [workflow_run(301, "2026-08-29T04:30:00Z")],
            },
            jobs={301: [publish_job("2026-08-29T04:01:00Z")]},
        )
        decision = gate.decide(
            event_name="schedule",
            default_branch="main",
            now=self.NOW,
            api=api,
        )
        self.assertFalse(decision.should_publish)
        self.assertEqual(decision.reason, "current-jobs-already-published-by-chain")

    def test_fallback_runs_when_only_prior_cycle_history_exists(self):
        api = FakeApi(
            runs={
                (gate.JOBS_WORKFLOW, "schedule"): [workflow_run(400, "2026-08-28T18:00:00Z")],
                (gate.CATALOG_WORKFLOW, "workflow_run"): [workflow_run(401, "2026-08-28T18:30:00Z")],
            },
            jobs={401: [publish_job("2026-08-28T18:01:00Z")]},
        )
        decision = gate.decide(
            event_name="schedule",
            default_branch="main",
            now=self.NOW,
            api=api,
        )
        self.assertTrue(decision.should_publish)
        self.assertEqual(decision.reason, "no-successful-jobs-run-in-current-cycle")

    def test_fallback_runs_when_current_jobs_have_not_been_published(self):
        api = FakeApi(
            runs={
                (gate.JOBS_WORKFLOW, "schedule"): [workflow_run(500, "2026-08-29T04:00:00Z")],
                (gate.CATALOG_WORKFLOW, "workflow_run"): [workflow_run(501, "2026-08-29T03:50:00Z")],
            },
            jobs={501: [publish_job("2026-08-29T03:45:00Z")]},
        )
        decision = gate.decide(
            event_name="schedule",
            default_branch="main",
            now=self.NOW,
            api=api,
        )
        self.assertTrue(decision.should_publish)
        self.assertEqual(decision.reason, "current-jobs-not-yet-published")

    def test_api_errors_fail_open_for_both_automatic_paths(self):
        api = FakeApi(fail=True)
        fallback = gate.decide(
            event_name="schedule",
            default_branch="main",
            now=self.NOW,
            api=api,
        )
        chain = gate.decide(
            event_name="workflow_run",
            default_branch="main",
            upstream_branch="main",
            upstream_conclusion="success",
            upstream_event="schedule",
            upstream_completed_at="2026-08-29T04:00:00Z",
            now=self.NOW,
            api=api,
        )
        self.assertTrue(fallback.should_publish)
        self.assertTrue(chain.should_publish)
        self.assertEqual(fallback.reason, "actions-api-error-fail-open")
        self.assertEqual(chain.reason, "actions-api-error-fail-open")


if __name__ == "__main__":
    unittest.main()
