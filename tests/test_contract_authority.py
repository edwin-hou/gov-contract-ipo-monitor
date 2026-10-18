"""Stored contract withdrawals must survive replay, awaited quotes and crashes."""
import asyncio
from datetime import timedelta
import sqlite3

import pytest

from contract_ipo_monitor.db import Database
from contract_ipo_monitor.gate import AlertGate
from contract_ipo_monitor.models import Candidate
from contract_ipo_monitor.models import MarketSnapshot
from contract_ipo_monitor.processor import EvidenceProcessor
from test_processor_service import NOW, contract, listing


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status", ["cancelled", "deleted"])
def test_old_active_contract_replay_cannot_revive_withdrawn_evidence(tmp_path, asynchronous, status):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    original = contract()
    processor.ingest_contract(original)
    withdrawal = contract(status=status).model_copy(update={"deleted": status == "deleted"})
    processor.ingest_contract(withdrawal)
    processor.ingest_listing(listing())
    assert db.count("alerts") == 0
    replay = original.model_copy(update={"retrieved_at": NOW + timedelta(minutes=1)})
    if asynchronous:
        asyncio.run(processor.ingest_contract_async(replay))
    else:
        processor.ingest_contract(replay)
    assert db.count("alerts") == 0
    assert db.fetch_outbox() == []


def test_contract_cancelled_during_awaited_quote_cannot_be_queued(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())

    async def lookup(_symbol):
        processor.ingest_contract(contract(status="cancelled"))
        return MarketSnapshot(symbol="ACME", quote_at=NOW, price=4, market_cap=200_000_000, source="test")

    processor.market_lookup = lookup
    asyncio.run(processor.ingest_listing_async(listing().model_copy(update={"ticker": "ACME"})))
    assert db.count("alerts") == 0
    assert db.fetch_outbox() == []


@pytest.mark.parametrize("status", ["cancelled", "deleted"])
def test_outbox_rechecks_contract_when_correction_ingestion_was_interrupted(tmp_path, status):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    assert db.fetch_outbox()[0]["status"] == "pending"
    # An interrupted ingestion can store revocation before enqueue_correction.
    db.store_contract_evidence(contract(status=status).model_copy(update={"deleted": status == "deleted"}),
                               created_at=NOW + timedelta(seconds=1))
    assert db.lease_outbox(now=NOW + timedelta(seconds=2)) is None
    row = db.fetch_outbox()[0]
    assert row["status"] == "cancelled"
    assert row["last_error"] == "contract_authority_withheld"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("kind", ["contract", "listing"])
def test_old_withdrawal_replay_cannot_retract_newer_active_version(tmp_path, asynchronous, kind):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    if kind == "contract":
        withdrawal = contract(status="cancelled")
        processor.ingest_contract(withdrawal)
        restored = contract().model_copy(update={"raw_payload_hash": "new-active", "current_value": 11_000_000})
        processor.ingest_contract(restored)
        processor.ingest_listing(listing())
        ingest = processor.ingest_contract_async if asynchronous else processor.ingest_contract
    else:
        withdrawal = listing(active=False, status="withdrawn")
        processor.ingest_listing(withdrawal)
        processor.ingest_listing(listing().model_copy(update={"raw_payload_hash": "new-active"}))
        processor.ingest_contract(contract())
        ingest = processor.ingest_listing_async if asynchronous else processor.ingest_listing
    before = db.fetch_outbox()
    assert len(before) == 1 and before[0]["status"] == "pending"
    if asynchronous:
        asyncio.run(ingest(withdrawal))
    else:
        ingest(withdrawal)
    assert db.fetch_outbox() == before


@pytest.mark.parametrize("kind", ["contract", "listing"])
def test_current_withdrawal_replay_recovers_interrupted_correction(tmp_path, kind):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    if kind == "contract":
        withdrawal = contract(status="cancelled")
        db.store_contract_evidence(withdrawal, created_at=NOW)
        ingest = processor.ingest_contract
    else:
        withdrawal = listing(active=False, status="withdrawn")
        db.store_listing_signal(withdrawal, created_at=NOW)
        ingest = processor.ingest_listing
    ingest(withdrawal)
    assert [row["status"] for row in db.fetch_outbox()] == ["cancelled", "pending"]
    assert db.fetch_outbox()[1]["subject"].startswith("[CORRECTION]")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_older_modification_cancellation_cannot_retract_newer_active_award(tmp_path, asynchronous):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW+timedelta(minutes=3))
    withdrawal = contract(status="cancelled").model_copy(update={
        "source_record_id": "mod-1", "modification_number": "1", "published_at": NOW})
    processor.ingest_contract(withdrawal)
    restored = contract().model_copy(update={"source_record_id": "mod-2", "modification_number": "2",
        "published_at": NOW+timedelta(minutes=1), "raw_payload_hash": "newer-active-modification"})
    processor.ingest_contract(restored)
    processor.ingest_listing(listing())
    before = db.fetch_outbox()
    assert len(before) == 1 and before[0]["status"] == "pending"
    ingest = processor.ingest_contract_async if asynchronous else processor.ingest_contract
    if asynchronous:
        asyncio.run(ingest(withdrawal))
    else:
        ingest(withdrawal)
    assert db.fetch_outbox() == before
    assert db.contract_is_current_authorized(restored)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_latest_other_transaction_deletion_cannot_lift_award_cancellation(tmp_path, asynchronous):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW+timedelta(minutes=3))
    original = contract()
    processor.ingest_contract(original)
    processor.ingest_listing(listing())
    withdrawal = contract(status="cancelled").model_copy(update={
        "source_record_id": "cancellation", "published_at": NOW+timedelta(minutes=1)})
    # Simulate a crash after state storage but before correction publication.
    db.store_contract_evidence(withdrawal, created_at=NOW+timedelta(minutes=1))
    unrelated_deletion = contract(status="deleted").model_copy(update={"deleted": True,
        "source_record_id": "other-transaction", "published_at": NOW+timedelta(minutes=2)})
    db.store_contract_evidence(unrelated_deletion, created_at=NOW+timedelta(minutes=2))
    assert not db.contract_is_current_authorized(original)
    assert not any(not item.cancelled and not item.deleted for item in db.load_contracts())
    ingest = processor.ingest_contract_async if asynchronous else processor.ingest_contract
    if asynchronous:
        asyncio.run(ingest(withdrawal))
    else:
        ingest(withdrawal)
    assert [row["status"] for row in db.fetch_outbox()] == ["cancelled", "pending"]
    assert db.fetch_outbox()[1]["subject"].startswith("[CORRECTION]")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_correction_rechecks_award_status_after_initial_ingestion_check(tmp_path, monkeypatch, asynchronous):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW+timedelta(minutes=3))
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    before = db.fetch_outbox()
    original_enqueue = db.enqueue_correction

    def advance_award_before_correction(**kwargs):
        restored = contract().model_copy(update={"source_record_id": "new-active-modification",
            "raw_payload_hash": "new-active", "published_at": NOW+timedelta(minutes=2)})
        db.store_contract_evidence(restored, created_at=NOW+timedelta(minutes=2))
        return original_enqueue(**kwargs)

    monkeypatch.setattr(db, "enqueue_correction", advance_award_before_correction)
    withdrawal = contract(status="cancelled").model_copy(update={
        "source_record_id": "older-cancellation", "published_at": NOW+timedelta(minutes=1)})
    ingest = processor.ingest_contract_async if asynchronous else processor.ingest_contract
    if asynchronous:
        asyncio.run(ingest(withdrawal))
    else:
        ingest(withdrawal)
    assert db.fetch_outbox() == before


def test_correction_award_authority_is_checked_inside_writer_fence(tmp_path, monkeypatch):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    withdrawal = contract(status="cancelled")
    db.store_contract_evidence(withdrawal, created_at=NOW)
    authority = db.contract_correction_is_current_authorized
    checks = []

    def require_writer_fence(evidence):
        with sqlite3.connect(db.path, timeout=0, isolation_level=None) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        checks.append(True)
        return authority(evidence)

    monkeypatch.setattr(db, "contract_correction_is_current_authorized", require_writer_fence)
    assert db.enqueue_correction(award_id=withdrawal.award_id, contract_authority=withdrawal,
        reason="Current award cancellation", source_url=withdrawal.source_url, created_at=NOW) == 1
    assert checks == [True]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_old_related_withdrawal_cannot_retract_a_newer_target_listing(tmp_path, asynchronous):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    withdrawal = listing(active=False, status="withdrawn").model_copy(update={
        "signal_id": "RW-old", "related_signal_id": "S1-1", "filed_at": NOW-timedelta(hours=1)})
    processor.ingest_listing(withdrawal)
    processor.ingest_listing(listing().model_copy(update={"filed_at": NOW-timedelta(minutes=30)}))
    processor.ingest_contract(contract())
    before = db.fetch_outbox()
    assert len(before) == 1 and before[0]["status"] == "pending"
    ingest = processor.ingest_listing_async if asynchronous else processor.ingest_listing
    if asynchronous:
        asyncio.run(ingest(withdrawal))
    else:
        ingest(withdrawal)
    assert db.fetch_outbox() == before


@pytest.mark.parametrize("asynchronous", [False, True])
def test_related_target_chronology_is_rechecked_inside_correction_transaction(tmp_path, monkeypatch, asynchronous):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_listing(listing().model_copy(update={"filed_at": NOW-timedelta(hours=2)}))
    processor.ingest_contract(contract())
    before = db.fetch_outbox()
    original_enqueue = db.enqueue_correction

    def advance_target_before_correction(**kwargs):
        db.store_listing_signal(listing().model_copy(update={"filed_at": NOW-timedelta(minutes=30),
            "raw_payload_hash": "newer-target-listing"}), created_at=NOW)
        return original_enqueue(**kwargs)

    monkeypatch.setattr(db, "enqueue_correction", advance_target_before_correction)
    withdrawal = listing(active=False, status="withdrawn").model_copy(update={
        "signal_id": "RW-old", "related_signal_id": "S1-1", "filed_at": NOW-timedelta(hours=1)})
    ingest = processor.ingest_listing_async if asynchronous else processor.ingest_listing
    if asynchronous:
        asyncio.run(ingest(withdrawal))
    else:
        ingest(withdrawal)
    assert db.fetch_outbox() == before


def test_withdrawal_corrects_older_registration_target_while_preserving_newer_related_target(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    newer = listing().model_copy(update={"filed_at": NOW-timedelta(minutes=30), "registration_id": "333-1"})
    older = listing().model_copy(update={"signal_id": "S1-older", "filed_at": NOW-timedelta(hours=2),
                                       "registration_id": "333-1"})
    processor.ingest_listing(newer)
    processor.ingest_listing(older)
    processor.ingest_contract(contract())
    withdrawal = listing(active=False, status="withdrawn").model_copy(update={
        "signal_id": "RW-old", "related_signal_id": "S1-1", "filed_at": NOW-timedelta(hours=1),
        "registration_id": "333-1"})
    processor.ingest_listing(withdrawal)
    originals = {row["alert_id"]: row for row in db.fetch_outbox() if row["correction_key"] is None}
    with db.connect() as connection:
        alerts = {row["signal_id"]: row["id"] for row in connection.execute("SELECT id,signal_id FROM alerts")}
    assert originals[alerts["S1-1"]]["status"] == "pending"
    assert originals[alerts["S1-older"]]["status"] == "cancelled"
    corrections = [row for row in db.fetch_outbox() if row["correction_key"] is not None]
    assert len(corrections) == 1 and corrections[0]["alert_id"] == alerts["S1-older"]


def test_related_withdrawal_can_correct_a_legacy_unstored_listing_using_its_exact_trace(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    assert AlertGate(db, now=NOW).evaluate(Candidate(contract=contract(), listing=listing())).alert_created
    processor = EvidenceProcessor(db, now=lambda: NOW)
    withdrawal = listing(active=False, status="withdrawn").model_copy(update={
        "signal_id": "RW-new", "related_signal_id": "S1-1", "filed_at": NOW})
    processor.ingest_listing(withdrawal)
    assert [row["status"] for row in db.fetch_outbox()] == ["cancelled", "pending"]


def test_standalone_gate_lease_is_revoked_after_related_withdrawal_before_correction(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    original = listing()
    assert AlertGate(db, now=NOW).evaluate(Candidate(contract=contract(), listing=original)).alert_created
    assert db.listing_is_current_authorized(original)
    withdrawal = listing(active=False, status="withdrawn").model_copy(update={
        "signal_id": "RW-new", "related_signal_id": original.signal_id, "filed_at": NOW})
    db.store_listing_signal(withdrawal, created_at=NOW)
    assert db.lease_outbox(now=NOW) is None
    assert db.fetch_outbox()[0]["status"] == "cancelled"
    assert db.fetch_outbox()[0]["last_error"] == "listing_authority_withheld"


def test_listing_target_authority_is_checked_inside_writer_fence(tmp_path, monkeypatch):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    withdrawal = listing(active=False, status="withdrawn").model_copy(update={
        "signal_id": "RW-new", "related_signal_id": "S1-1", "filed_at": NOW})
    db.store_listing_signal(withdrawal, created_at=NOW)
    authority = db.listing_correction_is_current_authorized
    checks = []

    def require_writer_fence(signal, target):
        with sqlite3.connect(db.path, timeout=0, isolation_level=None) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        checks.append(True)
        return authority(signal, target)

    monkeypatch.setattr(db, "listing_correction_is_current_authorized", require_writer_fence)
    assert db.enqueue_correction(signal_id="S1-1", listing_authority=withdrawal,
        reason="Current related withdrawal", source_url=withdrawal.source_url, created_at=NOW) == 1
    assert checks == [True]
