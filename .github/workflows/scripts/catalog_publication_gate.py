#!/usr/bin/env python3
"""Select exactly one automatic catalog publication for each nightly jobs cycle.

The preferred path is a ``workflow_run`` after the scheduled jobs refresh.  A
staggered cron remains independent so a delayed or dropped GitHub scheduler
event cannot prevent the catalog from refreshing.  The gate compares the
actual start time of successful ``publish`` jobs with the current jobs-cycle
completion, rather than relying on workflow creation times.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


JOBS_WORKFLOW = "update-jobs.yml"
CATALOG_WORKFLOW = "update-data.yml"
JOBS_CYCLE_TIME = time(hour=3, tzinfo=timezone.utc)


class ApiError(RuntimeError):
    """Raised when GitHub run history cannot be inspected safely."""


class ActionsApi(Protocol):
    def workflow_runs(self, workflow: str, event: str) -> Sequence[Mapping[str, Any]]:
        """Return recent successful runs for one workflow and event."""

    def run_jobs(self, run_id: int) -> Sequence[Mapping[str, Any]]:
        """Return the latest-attempt jobs for one workflow run."""


@dataclass(frozen=True)
class Decision:
    should_publish: bool
    reason: str


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def current_jobs_cycle_boundary(now: datetime) -> datetime:
    """Return the 03:00 UTC boundary for the current nightly jobs cycle."""

    now = now.astimezone(timezone.utc)
    boundary = datetime.combine(now.date(), JOBS_CYCLE_TIME)
    if now < boundary:
        boundary -= timedelta(days=1)
    return boundary


class GitHubActionsApi:
    def __init__(self, repository: str, token: str, api_url: str) -> None:
        if not repository or not token:
            raise ApiError("GITHUB_REPOSITORY and GH_TOKEN are required")
        self.repository = repository
        self.token = token
        self.api_url = api_url.rstrip("/")

    def _get(self, path: str, params: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        url = f"{self.api_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "open-knowledge-graphs-catalog-publication-gate",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed GitHub API base
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ApiError(f"GitHub Actions API request failed for {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError(f"GitHub Actions API returned a non-object for {path}")
        return payload

    def workflow_runs(self, workflow: str, event: str) -> Sequence[Mapping[str, Any]]:
        payload = self._get(
            f"/repos/{self.repository}/actions/workflows/{workflow}/runs",
            {"event": event, "status": "success", "per_page": "30"},
        )
        runs = payload.get("workflow_runs")
        if not isinstance(runs, list):
            raise ApiError("GitHub Actions workflow-runs response omitted workflow_runs")
        return [row for row in runs if isinstance(row, dict)]

    def run_jobs(self, run_id: int) -> Sequence[Mapping[str, Any]]:
        payload = self._get(
            f"/repos/{self.repository}/actions/runs/{run_id}/jobs",
            {"filter": "latest", "per_page": "100"},
        )
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise ApiError("GitHub Actions jobs response omitted jobs")
        return [row for row in jobs if isinstance(row, dict)]


def latest_jobs_completion_for_cycle(
    api: ActionsApi,
    now: datetime,
    default_branch: str,
) -> datetime | None:
    boundary = current_jobs_cycle_boundary(now)
    completions: list[datetime] = []
    for run in api.workflow_runs(JOBS_WORKFLOW, "schedule"):
        if run.get("conclusion") != "success":
            continue
        if default_branch and run.get("head_branch") not in (None, default_branch):
            continue
        completed_at = parse_timestamp(run.get("updated_at"))
        if completed_at is not None and completed_at >= boundary:
            completions.append(completed_at)
    return max(completions, default=None)


def successful_publish_after(
    api: ActionsApi,
    event: str,
    threshold: datetime,
) -> Mapping[str, Any] | None:
    """Find a successful catalog publish job that began after ``threshold``."""

    for run in api.workflow_runs(CATALOG_WORKFLOW, event):
        if run.get("conclusion") != "success":
            continue
        run_id = run.get("id")
        if not isinstance(run_id, int):
            continue
        for job in api.run_jobs(run_id):
            if job.get("name") != "publish" or job.get("conclusion") != "success":
                continue
            started_at = parse_timestamp(job.get("started_at"))
            if started_at is not None and started_at >= threshold:
                return {"run": run, "job": job}
    return None


def decide(
    *,
    event_name: str,
    default_branch: str,
    upstream_branch: str = "",
    upstream_conclusion: str = "",
    upstream_event: str = "",
    upstream_completed_at: str = "",
    now: datetime | None = None,
    api: ActionsApi | None = None,
) -> Decision:
    """Return a fail-open publication decision for the current trigger."""

    if event_name == "workflow_dispatch":
        return Decision(True, "manual-dispatch")

    if event_name == "workflow_run":
        if upstream_conclusion != "success":
            return Decision(False, "upstream-not-successful")
        if upstream_event != "schedule":
            return Decision(False, "upstream-not-scheduled")
        if not default_branch or upstream_branch != default_branch:
            return Decision(False, "upstream-not-default-branch")
        completed_at = parse_timestamp(upstream_completed_at)
        if completed_at is None:
            return Decision(True, "upstream-time-unavailable-fail-open")
        if api is None:
            return Decision(True, "actions-api-unavailable-fail-open")
        try:
            fallback = successful_publish_after(api, "schedule", completed_at)
        except ApiError:
            return Decision(True, "actions-api-error-fail-open")
        if fallback is not None:
            return Decision(False, "fallback-already-published-current-jobs")
        return Decision(True, "successful-scheduled-jobs")

    if event_name == "schedule":
        if api is None:
            return Decision(True, "actions-api-unavailable-fail-open")
        current_time = now or datetime.now(timezone.utc)
        try:
            jobs_completed_at = latest_jobs_completion_for_cycle(
                api,
                current_time,
                default_branch,
            )
            if jobs_completed_at is None:
                return Decision(True, "no-successful-jobs-run-in-current-cycle")
            chained = successful_publish_after(api, "workflow_run", jobs_completed_at)
        except ApiError:
            return Decision(True, "actions-api-error-fail-open")
        if chained is not None:
            return Decision(False, "current-jobs-already-published-by-chain")
        return Decision(True, "current-jobs-not-yet-published")

    return Decision(False, "unsupported-trigger")


def _append(path: str, line: str) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def main() -> int:
    event_name = os.environ.get("EVENT_NAME", "")
    api: ActionsApi | None = None
    if event_name in {"schedule", "workflow_run"}:
        try:
            api = GitHubActionsApi(
                repository=os.environ.get("GITHUB_REPOSITORY", ""),
                token=os.environ.get("GH_TOKEN", ""),
                api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            )
        except ApiError as exc:
            print(f"warning: {exc}", file=sys.stderr)

    decision = decide(
        event_name=event_name,
        default_branch=os.environ.get("DEFAULT_BRANCH", ""),
        upstream_branch=os.environ.get("UPSTREAM_BRANCH", ""),
        upstream_conclusion=os.environ.get("UPSTREAM_CONCLUSION", ""),
        upstream_event=os.environ.get("UPSTREAM_EVENT", ""),
        upstream_completed_at=os.environ.get("UPSTREAM_COMPLETED_AT", ""),
        api=api,
    )
    rendered = "true" if decision.should_publish else "false"
    _append(os.environ.get("GITHUB_OUTPUT", ""), f"should_publish={rendered}")
    _append(os.environ.get("GITHUB_OUTPUT", ""), f"reason={decision.reason}")
    _append(
        os.environ.get("GITHUB_STEP_SUMMARY", ""),
        f"Catalog publication gate: **{rendered}** (`{decision.reason}`).",
    )
    print(f"should_publish={rendered} reason={decision.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
