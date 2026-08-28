#!/usr/bin/env python3
"""Run the bounded, nonpublishing Task 42 careers-page discovery check.

The production jobs workflow invokes this once nightly. It records change and
availability diagnostics only and can never add a job to the public snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from task42_discovery import LinkParser, _provider  # noqa: E402
from task42_source_audit import fixed_cohort, task42_review_sources  # noqa: E402

DEFAULT_BASELINE = ROOT / "audits" / "task42-careers-discovery.json"
DEFAULT_OUTPUT = ROOT / "runtime" / "task42-discovery-monitor" / "run.json"
MAX_RESPONSE_BYTES = 512_000
TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 5
MAX_WORKERS = 8


class DiscoveryMonitorError(RuntimeError):
    """The discovery monitor's local contract is invalid."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryMonitorError(f"cannot read valid JSON from {path}") from exc


def _baseline(path: Path) -> dict[str, dict]:
    payload = _read_json(path)
    pages = payload.get("careersPages")
    if not isinstance(pages, list):
        raise DiscoveryMonitorError("discovery baseline lacks careersPages")
    return {
        row["identifier"]: row for row in pages
        if isinstance(row, dict) and row.get("identifier")
    }


def monitored_pages(baseline_path: Path = DEFAULT_BASELINE) -> list[dict]:
    baseline = _baseline(baseline_path)
    sources = task42_review_sources()
    fully_ingested = {source.organization_iri for source in sources.values()}
    rows = []
    for organization in fixed_cohort():
        if not organization.get("careersPage") or organization["iri"] in fully_ingested:
            continue
        prior = baseline.get(organization["identifier"])
        if not prior:
            raise DiscoveryMonitorError(
                f"missing discovery baseline for {organization['identifier']}"
            )
        rows.append({
            "identifier": organization["identifier"],
            "name": organization["name"],
            "careersPage": organization["careersPage"],
            "baselineSha256": prior.get("contentSha256"),
            "baselineFinalUrl": prior.get("finalUrl"),
            "baselineStatus": prior.get("httpStatus"),
        })
    return sorted(rows, key=lambda row: row["identifier"])


def _fetch(url: str, headers: dict[str, str]):
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS
    return session.get(
        url,
        headers=headers,
        stream=True,
        timeout=TIMEOUT_SECONDS,
        allow_redirects=True,
    )


def _check(page: dict, fetcher) -> dict:
    result = {
        **page,
        "change": None,
        "contentSha256": None,
        "contentType": None,
        "finalUrl": None,
        "httpStatus": None,
        "openingMarkers": False,
        "providerCandidates": [],
        "responseBytes": 0,
        "retrievalError": None,
    }
    headers = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        "Range": f"bytes=0-{MAX_RESPONSE_BYTES - 1}",
        "User-Agent": "OKG-career-discovery-monitor/1.0 (+https://openknowledgegraphs.com/)",
    }
    try:
        response = fetcher(page["careersPage"], headers)
        result["httpStatus"] = int(response.status_code)
        result["finalUrl"] = str(response.url)
        result["contentType"] = response.headers.get("Content-Type")
        if response.status_code == 304:
            result["change"] = "not-modified"
            return result
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise DiscoveryMonitorError(
                    f"response-exceeded-{MAX_RESPONSE_BYTES}-byte-cap"
                )
        result["responseBytes"] = len(body)
        result["contentSha256"] = hashlib.sha256(body).hexdigest()
        if response.status_code >= 400:
            result["change"] = "unreachable"
            result["retrievalError"] = f"http-{response.status_code}"
            return result
        result["change"] = (
            "unchanged" if result["contentSha256"] == page["baselineSha256"]
            else "changed"
        )
        if "html" in str(result["contentType"] or "").casefold():
            text = body.decode(response.encoding or "utf-8", errors="replace")
            parser = LinkParser(result["finalUrl"])
            parser.feed(text)
            normalized = " ".join(text.casefold().split())
            result["openingMarkers"] = any(marker in normalized for marker in (
                "open positions", "current vacancies", "job openings",
                "search jobs", "view jobs", "careers",
            ))
            candidates = []
            for candidate in sorted(parser.links):
                provider = _provider(candidate)
                if provider:
                    candidates.append({"provider": provider, "url": candidate})
            result["providerCandidates"] = candidates[:20]
    except (requests.RequestException, DiscoveryMonitorError) as exc:
        result["change"] = "unreachable"
        result["retrievalError"] = f"{type(exc).__name__}:{exc}"
    return result


def run_monitor(
    *,
    output: Path = DEFAULT_OUTPUT,
    baseline_path: Path = DEFAULT_BASELINE,
    retrieved_at: str | None = None,
    fetcher=_fetch,
    max_workers: int = MAX_WORKERS,
) -> dict:
    pages = monitored_pages(baseline_path)
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_check, page, fetcher): page for page in pages}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda row: row["identifier"])
    counts = {
        state: sum(row["change"] == state for row in results)
        for state in ("changed", "not-modified", "unchanged", "unreachable")
    }
    run = {
        "schemaVersion": 1,
        "mode": "nonpublishing-careers-discovery-monitor",
        "retrievedAt": retrieved_at or _utc_now(),
        "publicationPerformed": False,
        "scheduleActivated": True,
        "scheduleCadence": "nightly-via-update-jobs",
        "bounds": {
            "maximumResponseBytesPerPage": MAX_RESPONSE_BYTES,
            "maximumRedirects": MAX_REDIRECTS,
            "maximumRequestsPerPage": 1,
            "requestTimeoutSeconds": TIMEOUT_SECONDS,
        },
        "counts": {**counts, "pages": len(results)},
        "pages": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("network access is disabled by default; pass --live explicitly")
    try:
        run = run_monitor(output=args.output, baseline_path=args.baseline)
    except DiscoveryMonitorError as exc:
        print(f"Discovery monitor failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(run["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
