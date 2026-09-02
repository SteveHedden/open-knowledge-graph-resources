"""Source-aware first-party pilot preservation regressions for Task 46."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
FIXTURES = ROOT / "tests" / "fixtures" / "first-party-pilot"
sys.path.insert(0, str(ROOT / "scripts"))

import first_party_pilot as pilot  # noqa: E402


SOURCE_A = "https://openknowledgegraphs.com/jobs/source/first-party-a"
SOURCE_B = "https://openknowledgegraphs.com/jobs/source/first-party-b"
AGGREGATOR = "https://openknowledgegraphs.com/jobs/source/himalayas"


def record(record_id: str, source: str, description: str) -> dict:
    return {
        "id": record_id,
        "sourceDataset": source,
        "description": description,
    }


def test_successful_refresh_replaces_only_that_sources_committed_population():
    committed = [
        record("a-active", SOURCE_A, "old active"),
        record("a-closed", SOURCE_A, "closed"),
        record("b-retained", SOURCE_B, "unselected"),
        record("aggregator-retained", AGGREGATOR, "aggregator"),
    ]
    refreshed = [
        record("a-active", SOURCE_A, "new active"),
        record("a-new", SOURCE_A, "new opening"),
    ]

    merged = pilot.preserve_partial_pilot_records(
        refreshed,
        committed,
        replaced_source_datasets={SOURCE_A},
    )

    assert [row["id"] for row in merged] == [
        "a-active",
        "a-new",
        "aggregator-retained",
        "b-retained",
    ]
    assert next(row for row in merged if row["id"] == "a-active")["description"] == (
        "new active"
    )
    assert all(row["id"] != "a-closed" for row in merged)


def test_successful_empty_refresh_removes_the_sources_committed_population():
    committed = [
        record("a-closed", SOURCE_A, "closed"),
        record("b-retained", SOURCE_B, "unselected"),
    ]

    merged = pilot.preserve_partial_pilot_records(
        [],
        committed,
        replaced_source_datasets={SOURCE_A},
    )

    assert merged == [committed[1]]


def test_failed_and_unselected_sources_retain_the_committed_baseline():
    committed = [
        record("a-failed", SOURCE_A, "last good"),
        record("b-unselected", SOURCE_B, "unselected"),
    ]

    assert pilot.preserve_partial_pilot_records(
        [],
        committed,
        replaced_source_datasets=set(),
    ) == committed


def test_refreshed_same_id_wins_once_and_output_order_is_deterministic():
    committed = [
        record("z", SOURCE_B, "retained"),
        record("a", SOURCE_A, "old"),
    ]
    refreshed = [record("a", SOURCE_A, "new")]

    merged = pilot.preserve_partial_pilot_records(
        refreshed,
        committed,
        replaced_source_datasets={SOURCE_A},
    )

    assert [(row["id"], row["description"]) for row in merged] == [
        ("a", "new"),
        ("z", "retained"),
    ]


def test_run_pilot_replaces_a_successfully_refreshed_selected_source(
    tmp_path, monkeypatch
):
    committed = json.loads((REPO_ROOT / "data" / "jobs" / "jobs.json").read_text())
    neo4j_source = (
        "https://openknowledgegraphs.com/jobs/source/first-party-neo4j"
    )
    stale = next(row for row in committed if row.get("sourceDataset") == neo4j_source)
    retained = next(
        row
        for row in committed
        if row.get("firstParty") and row.get("sourceDataset") != neo4j_source
    )
    monkeypatch.setattr(
        pilot,
        "load_aggregator_records",
        lambda: [dict(stale), dict(retained)],
    )

    result = pilot.run_pilot(
        fixtures=FIXTURES,
        runtime_dir=tmp_path / "first-party",
        retrieved_at="2026-09-02T12:00:00Z",
        selected_sources=["first-party-neo4j"],
    )
    output = json.loads(
        (tmp_path / "first-party" / "jobs.json").read_text(encoding="utf-8")
    )

    assert len(result["sourceResults"]) == 1
    assert result["sourceResults"][0]["sourceKey"] == "first-party-neo4j"
    assert result["sourceResults"][0]["status"] == "refreshed"
    assert stale["id"] not in {row["id"] for row in output}
    assert retained["id"] in {row["id"] for row in output}
    assert "firstparty:first-party-neo4j:1001" in {row["id"] for row in output}
