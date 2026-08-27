"""Static, network-free contracts for the two local Task 38 review views."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_jobs_view_reads_only_the_local_reconciled_review_snapshot_by_default():
    page = (ROOT / "site" / "index.html").read_text()
    assert "../runtime/first-party/jobs.json" in page
    assert "../runtime/first-party/run.json" in page
    assert "organizations.html" in page
    assert "Reconciled aggregator and reviewed first-party postings" in page
    assert "scripts/first_party_pilot.py --live" in page
    assert "record.attributionText" in page


def test_organization_view_exposes_required_audit_fields_and_is_responsive():
    page = (ROOT / "site" / "organizations.html").read_text()
    for required in (
        "Kind", "Ecosystem roles", "Inclusion evidence", "Review reason",
        "Official source", "Career source", "Terms status", "Last verified",
        "Jobs enabled", "Accepted", "Unresolved", "Rejected",
        "Jobs publication", "Not a vendor allowlist",
    ):
        assert required in page
    assert "../../data/organizations.json" in page
    assert "../audits/organization-registry-audit.json" in page
    assert "../runtime/first-party/run.json" in page
    assert "@media (max-width:760px)" in page
    assert "local audit view · not published" in page
