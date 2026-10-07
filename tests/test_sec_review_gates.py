"""Historical assertions must obey later source-backed issuer reviews."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from contract_ipo_monitor.db import Database
from contract_ipo_monitor.models import ListingSignal
from contract_ipo_monitor.processor import EvidenceProcessor
from contract_ipo_monitor.research import report_markdown
from contract_ipo_monitor.tracking import IPOEvidence, IPOTracker


NOW = datetime(2026, 10, 7, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    tracker = IPOTracker(db)
    tracker.initialize()
    with db.connect() as conn:
        conn.execute("CREATE TABLE sec_pending_filings(accession TEXT PRIMARY KEY, entry_json TEXT NOT NULL)")
    return db, tracker


def hold(db, accession, status="unresolved", **fields):
    review = {"status": status, "reason": "Two primary filers require attribution.", **fields}
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO sec_pending_filings VALUES(?,?)",
                     (accession, json.dumps({"issuer_review": review})))


def evidence(accession="registration", event_type="registration", cik="0000000042", registration="333-123", **fields):
    return IPOEvidence(event_id=accession, issuer_name=f"Company {cik}", cik=cik,
        source="sec", source_kind="regulatory", source_url=f"https://www.sec.gov/Archives/{accession}.htm",
        filed_at=NOW + (timedelta(days=1) if event_type == "withdrawn" else timedelta()),
        event_type=event_type, registration_id=registration, offering_kind="ipo",
        is_ipo=event_type == "registration", raw_payload_hash="original-document", **fields)


def listing(accession="registration", cik="0000000042", registration="333-123", **fields):
    return ListingSignal(signal_id=accession, issuer_name=f"Company {cik}", cik=cik,
        source="sec", source_url=f"https://www.sec.gov/Archives/{accession}.htm",
        filed_at=NOW, route="s-1", registration_id=registration,
        raw_payload_hash="original-document", **fields)


@pytest.mark.parametrize("status", ["unresolved", "retry_requested"])
def test_later_review_withholds_old_registration_without_deleting_history(state, status):
    db, tracker = state
    tracker.record(evidence())
    db.store_listing_signal(listing(), created_at=NOW)
    hold(db, "registration", status)
    candidate = tracker.candidates()[0]
    assert candidate["status"] == "issuer_unresolved"
    assert not candidate["active"] and not candidate["ipo_confirmed"]
    assert "Two primary filers" in " ".join(candidate["limitations"])
    assert tracker.summary()["issuer_unresolved_candidates"] == 1
    assert tracker.summary()["confirmed_ipos"] == 0
    assert not db.load_listing_signals()[0].active
    assert tracker.evidence()[0].is_ipo
    assert db.all_listing_signals()[0].active


def test_held_withdrawal_does_not_resurrect_registration_or_affect_unrelated_scope(state):
    db, tracker = state
    for item in [evidence(), evidence("withdrawal", "withdrawn"), evidence("other", registration="333-999")]:
        tracker.record(item)
    db.store_listing_signal(listing(), created_at=NOW)
    db.store_listing_signal(listing("withdrawal", active=False, status="withdrawn"), created_at=NOW)
    db.store_listing_signal(listing("other", registration="333-999"), created_at=NOW)
    hold(db, "withdrawal")
    candidates = {item["registration_id"]: item for item in tracker.candidates()}
    assert candidates["333-123"]["status"] == "issuer_unresolved"
    assert not candidates["333-123"]["active"]
    assert candidates["333-999"]["active"]
    signals = {item.signal_id: item for item in db.load_listing_signals()}
    assert not signals["registration"].active
    assert not signals["withdrawal"].active
    assert signals["other"].active
    assert len(tracker.evidence()) == 3


def test_paired_notice_holds_only_explicit_issuer_registration_scopes(state):
    db, tracker = state
    for accession, cik, registration in [("a", "0000000042", "333-123"), ("b", "0000000043", "333-123"),
                                         ("c", "0000000042", "333-999")]:
        tracker.record(evidence(accession, cik=cik, registration=registration))
        db.store_listing_signal(listing(accession, cik=cik, registration=registration), created_at=NOW)
    hold(db, "unclassified-effect", affected_registrations=[
        {"cik": "0000000042", "registration_id": "333-123"},
        {"cik": "0000000043", "registration_id": "333-123"},
    ])
    candidates = {(item["cik"], item["registration_id"]): item for item in tracker.candidates()}
    assert not candidates[("0000000042", "333-123")]["active"]
    assert not candidates[("0000000043", "333-123")]["active"]
    assert candidates[("0000000042", "333-999")]["active"]
    assert {item.signal_id: item.active for item in db.load_listing_signals()} == {"a": False, "b": False, "c": True}


@pytest.mark.parametrize("mismatch", ["issuer", "name", "document", "registration", "registration_absent"])
def test_resolved_authority_cannot_unblock_stale_version_before_ingestion(state, mismatch):
    db, tracker = state
    tracker.record(evidence())
    db.store_listing_signal(listing(), created_at=NOW)
    hold(db, "registration", "resolved", resolved_issuer={
        "cik": "0000000043" if mismatch == "issuer" else "0000000042",
        "issuer_name": "Different Company" if mismatch == "name" else "Company 0000000042",
        "registration_id": None if mismatch == "registration_absent" else "333-999" if mismatch == "registration" else "333-123",
    }, raw_payload_hash="new-document" if mismatch == "document" else "original-document")
    assert not tracker.candidates()[0]["ipo_confirmed"]
    assert not db.load_listing_signals()[0].active


def test_matching_resolution_and_corrected_version_restore_only_primary_issuer(state):
    db, tracker = state
    tracker.record(evidence())
    db.store_listing_signal(listing(), created_at=NOW)
    hold(db, "registration", "resolved", resolved_issuer={"cik": "0000000043", "issuer_name": "Company 0000000043", "registration_id": "333-123"},
         raw_payload_hash="original-document")
    tracker.record(evidence(cik="0000000043"))
    db.store_listing_signal(listing(cik="0000000043"), created_at=NOW)
    assert len(tracker.evidence()) == 2
    assert tracker.candidates()[0]["cik"] == "0000000043"
    assert tracker.candidates()[0]["ipo_confirmed"]
    assert db.load_listing_signals()[0].cik == "0000000043"
    assert db.load_listing_signals()[0].active


def test_ingestion_does_not_evaluate_original_active_signal_under_review(state, monkeypatch):
    db, _ = state
    hold(db, "registration", "retry_requested")
    processor = EvidenceProcessor(db, now=lambda: NOW)
    def forbidden_contract_read():
        pytest.fail("Held listing reached contract matching")
    monkeypatch.setattr(db, "load_contracts", forbidden_contract_read)
    assert processor.ingest_listing(listing()) == []
    assert not db.load_listing_signals()[0].active


@pytest.mark.asyncio
async def test_async_ingestion_does_not_evaluate_original_active_signal_under_review(state, monkeypatch):
    db, _ = state
    hold(db, "registration", "retry_requested")
    processor = EvidenceProcessor(db, now=lambda: NOW)
    def forbidden_contract_read():
        pytest.fail("Held listing reached contract matching")
    monkeypatch.setattr(db, "load_contracts", forbidden_contract_read)
    assert await processor.ingest_listing_async(listing()) == []
    assert not db.load_listing_signals()[0].active


def test_saved_report_displays_issuer_holds_and_escapes_source_reason():
    report = {"completed_at": NOW.isoformat(), "status": "degraded",
              "sec_collection": {"issuer_review_count": 2, "issuer_reviews": [
                  {"accession": "shared-effect", "form": "EFFECT", "reason": "<script>two filers</script>"}]}}
    rendered = report_markdown(report)
    assert "2 SEC submissions await issuer attribution" in rendered
    assert "Affected IPO and listing assertions remain inactive" in rendered
    assert "shared-effect (EFFECT)" in rendered
    assert "<script>" not in rendered
