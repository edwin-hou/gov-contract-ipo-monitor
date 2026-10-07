"""Review changes and interrupted correction replay must not revive alerts."""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from contract_ipo_monitor.db import Database
from contract_ipo_monitor.gate import AlertGate
from contract_ipo_monitor.models import Candidate, ContractEvidence, ListingSignal, MarketSnapshot
from contract_ipo_monitor.processor import EvidenceProcessor
from contract_ipo_monitor.tracking import IPOEvidence, IPOTracker


NOW = datetime(2026, 10, 7, tzinfo=UTC)
ORIGINAL_CIK = "0000000042"
PRIMARY_CIK = "0000000043"
REGISTRATION = "333-123"


@pytest.fixture
def state(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    tracker = IPOTracker(db)
    tracker.initialize()
    with db.connect() as conn:
        conn.execute("CREATE TABLE sec_pending_filings(accession TEXT PRIMARY KEY, entry_json TEXT NOT NULL)")
    return db, tracker


def review(db, accession, *, resolved=False, affected=()):
    value = {
        "status": "resolved" if resolved else "unresolved",
        "reason": "Primary issuer attribution requires review.",
        "affected_registrations": list(affected),
    }
    if resolved:
        value.update({
            "resolved_issuer": {
                "cik": PRIMARY_CIK, "issuer_name": f"Company {PRIMARY_CIK}",
                "registration_id": REGISTRATION,
            },
            "raw_payload_hash": "original-document",
        })
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO sec_pending_filings VALUES(?,?)",
                     (accession, json.dumps({"issuer_review": value})))


def listing(accession="registration", *, cik=ORIGINAL_CIK, withdrawn=False):
    return ListingSignal(
        source="sec", source_url=f"https://www.sec.gov/Archives/{accession}.htm",
        signal_id=accession, issuer_name=f"Company {cik}", cik=cik,
        filed_at=NOW if withdrawn else NOW - timedelta(hours=1),
        active=not withdrawn, status="withdrawn" if withdrawn else "active",
        route="s-1", form_type="RW" if withdrawn else "S-1",
        is_initial_listing=True, intends_public_trading=True,
        ticker="ACME", expected_exchange="NASDAQ", linked_ueis=("UEI42",),
        registration_id=REGISTRATION, raw_payload_hash="original-document",
    )


def event(signal):
    return IPOEvidence(
        event_id=signal.signal_id, issuer_name=signal.issuer_name, cik=signal.cik,
        source=signal.source, source_kind="regulatory", source_url=signal.source_url,
        filed_at=signal.filed_at, registration_id=signal.registration_id,
        event_type="withdrawn" if not signal.active else "registration",
        is_ipo=signal.active, offering_kind="ipo", raw_payload_hash=signal.raw_payload_hash,
    )


def contract():
    return ContractEvidence(
        source="usaspending", source_url="https://usaspending.gov/award/1",
        source_record_id="1", award_id="ABC-123", retrieved_at=NOW, published_at=NOW,
        status="awarded", award_date=NOW.date(), agency="DOE",
        recipient_name=f"Company {ORIGINAL_CIK}", recipient_uei="UEI42", prime=True,
        obligated_amount=10_000_000, current_value=10_000_000,
        award_type="definitive_contract", description="Sensors", evidence_class="A",
        raw_payload_hash="contract-document",
    )


def quote():
    return MarketSnapshot(symbol="ACME", quote_at=NOW, price=4.0,
                          market_cap=200_000_000, source="isolated-test")


def assert_primary_registration_inactive(db, tracker):
    candidates = [item for item in tracker.candidates() if item["cik"] == PRIMARY_CIK]
    assert candidates and all(not item["active"] for item in candidates)
    assert not any(item.active and item.cik == PRIMARY_CIK for item in db.load_listing_signals())


@pytest.mark.parametrize("first_store", ["tracker", "listing"])
def test_resolved_withdrawal_holds_target_through_both_interrupted_replay_orders(state, first_store):
    db, tracker = state
    for item in [listing("a"), listing("b", cik=PRIMARY_CIK), listing("withdrawal", withdrawn=True)]:
        tracker.record(event(item))
        db.store_listing_signal(item, created_at=NOW)
    affected = [{"cik": cik, "registration_id": REGISTRATION} for cik in [ORIGINAL_CIK, PRIMARY_CIK]]
    review(db, "withdrawal", affected=affected)
    assert_primary_registration_inactive(db, tracker)

    # The catalogue is resolved before consumers replace their historical rows.
    review(db, "withdrawal", resolved=True, affected=affected)
    assert_primary_registration_inactive(db, tracker)
    corrected = listing("withdrawal", cik=PRIMARY_CIK, withdrawn=True)
    if first_store == "tracker":
        tracker.record(event(corrected))
    else:
        db.store_listing_signal(corrected, created_at=NOW)
    # One consumer's completed replay cannot authorize the other's stale rows.
    assert_primary_registration_inactive(db, tracker)
    if first_store == "tracker":
        db.store_listing_signal(corrected, created_at=NOW)
    else:
        tracker.record(event(corrected))
    assert_primary_registration_inactive(db, tracker)
    primary = next(item for item in tracker.candidates() if item["cik"] == PRIMARY_CIK)
    assert primary["status"] == "withdrawn"


def prepare_historical_replay(db):
    processor = EvidenceProcessor(db, now=lambda: NOW, market_lookup=lambda _: quote())
    original = listing()
    assert processor.ingest_listing(original) == []
    review(db, original.signal_id, resolved=True)
    assert processor.ingest_listing(listing(cik=PRIMARY_CIK)) == []
    current = db.load_listing_signals()[0]
    assert current.active and current.cik == PRIMARY_CIK
    db.store_contract_evidence(contract(), created_at=NOW)
    return processor, original


def test_duplicate_historical_listing_cannot_borrow_corrected_versions_authority(state):
    db, _ = state
    processor, original = prepare_historical_replay(db)
    assert processor.ingest_listing(original) == []
    assert db.count("alerts") == 0
    assert db.count("outbox_messages") == 0
    assert db.load_listing_signals()[0].cik == PRIMARY_CIK


@pytest.mark.asyncio
async def test_async_duplicate_historical_listing_cannot_borrow_corrected_versions_authority(state):
    db, _ = state
    processor, original = prepare_historical_replay(db)
    assert await processor.ingest_listing_async(original) == []
    assert db.count("alerts") == 0
    assert db.count("outbox_messages") == 0


@pytest.mark.parametrize("entrypoint", ["contract", "listing"])
def test_review_committed_during_synchronous_quote_lookup_prevents_real_alert(state, entrypoint):
    db, _ = state
    signal = listing()
    if entrypoint == "contract":
        db.store_listing_signal(signal, created_at=NOW)
    else:
        db.store_contract_evidence(contract(), created_at=NOW)

    def lookup(_):
        review(db, signal.signal_id)
        return quote()

    processor = EvidenceProcessor(db, now=lambda: NOW, market_lookup=lookup)
    result = processor.ingest_contract(contract()) if entrypoint == "contract" else processor.ingest_listing(signal)
    assert not any(item.alert_created for item in result)
    assert not db.load_listing_signals()[0].active
    assert db.count("alerts") == 0
    assert db.count("outbox_messages") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["contract", "listing"])
async def test_review_committed_during_awaited_quote_lookup_prevents_real_alert(state, entrypoint):
    db, _ = state
    signal = listing()
    if entrypoint == "contract":
        db.store_listing_signal(signal, created_at=NOW)
    else:
        db.store_contract_evidence(contract(), created_at=NOW)

    async def lookup(_):
        await asyncio.sleep(0)
        review(db, signal.signal_id)
        return quote()

    processor = EvidenceProcessor(db, now=lambda: NOW, market_lookup=lookup)
    result = (await processor.ingest_contract_async(contract()) if entrypoint == "contract"
              else await processor.ingest_listing_async(signal))
    assert not any(item.alert_created for item in result)
    assert not db.load_listing_signals()[0].active
    assert db.count("alerts") == 0
    assert db.count("outbox_messages") == 0


def test_review_committed_after_validation_is_checked_at_alert_commit(state, monkeypatch):
    db, _ = state
    signal = listing()
    db.store_listing_signal(signal, created_at=NOW)
    save = db.save_evaluation

    def review_then_save(*args, **kwargs):
        review(db, signal.signal_id)
        return save(*args, **kwargs)

    monkeypatch.setattr(db, "save_evaluation", review_then_save)
    result = AlertGate(db, now=NOW).evaluate(Candidate(contract=contract(), listing=signal, market=quote()))
    assert not result.alert_created
    assert db.count("alerts") == 0
    assert db.count("outbox_messages") == 0


def test_valid_unreviewed_candidate_still_creates_one_real_alert_and_outbox(state):
    db, _ = state
    db.store_listing_signal(listing(), created_at=NOW)
    processor = EvidenceProcessor(db, now=lambda: NOW, market_lookup=lambda _: quote())
    result = processor.ingest_contract(contract())
    assert len(result) == 1 and result[0].alert_created
    assert all(item.passed for item in result[0].decisions)
    assert db.count("alerts") == 1
    assert db.count("outbox_messages") == 1
    leased = db.lease_outbox(now=NOW)
    assert leased and leased["status"] == "leased"


def queue_valid_alert(db, signal=None):
    signal = signal or listing()
    db.store_listing_signal(signal, created_at=NOW)
    result = AlertGate(db, now=NOW).evaluate(Candidate(contract=contract(), listing=signal, market=quote()))
    assert result.alert_created
    return db.fetch_outbox()[-1]


def assert_cancelled_with_retained_history(db, original):
    current = next(item for item in db.fetch_outbox() if item["id"] == original["id"])
    assert current["status"] == "cancelled"
    assert isinstance(current["last_error"], str) and current["last_error"].strip()
    assert current["subject"] == original["subject"]
    assert current["text_body"] == original["text_body"]
    assert current["html_body"] == original["html_body"]
    assert current["smtp_message_id"] is None and current["sent_at"] is None
    assert db.count("candidate_matches") >= 1
    assert db.count("alerts") >= 1


def test_later_issuer_hold_cancels_unsent_alert_at_lease_without_erasing_history(state):
    db, _ = state
    original = queue_valid_alert(db)
    review(db, "registration")
    assert db.lease_outbox(now=NOW) is None
    assert_cancelled_with_retained_history(db, original)
    with db.connect() as conn:
        historical = json.loads(conn.execute("SELECT listing_json FROM candidate_matches").fetchone()[0])
    assert historical["active"] is True and historical["cik"] == ORIGINAL_CIK


def test_changed_current_listing_version_cancels_unsent_old_projection_at_lease(state):
    db, _ = state
    original = queue_valid_alert(db)
    db.store_listing_signal(listing(cik=PRIMARY_CIK), created_at=NOW)
    assert db.load_listing_signals()[0].active
    assert db.load_listing_signals()[0].cik == PRIMARY_CIK
    assert db.lease_outbox(now=NOW) is None
    assert_cancelled_with_retained_history(db, original)


def test_held_earliest_pending_alert_does_not_block_unrelated_valid_alert(state):
    db, _ = state
    original = queue_valid_alert(db)
    unrelated = listing("unrelated").model_copy(update={"registration_id": "333-999"})
    unaffected = queue_valid_alert(db, unrelated)
    review(db, "registration")
    leased = db.lease_outbox(now=NOW)
    assert leased and leased["id"] == unaffected["id"] and leased["status"] == "leased"
    assert_cancelled_with_retained_history(db, original)
    assert db.count("alerts") == 2 and db.count("candidate_matches") == 2


def test_later_issuer_hold_preserves_already_sent_provider_receipt(state):
    db, _ = state
    original = queue_valid_alert(db)
    leased = db.lease_outbox(now=NOW)
    assert leased and leased["id"] == original["id"]
    # A receipt fixture records completed history; no transport is invoked.
    db.mark_outbox_sent(original["id"], sent_at=NOW, smtp_message_id="historical-provider-receipt",
                        lease_until=leased["lease_until"])
    completed = db.fetch_outbox()[0]
    review(db, "registration")
    assert db.lease_outbox(now=NOW) is None
    assert db.fetch_outbox()[0] == completed
    assert completed["status"] == "sent"
    assert completed["smtp_message_id"] == "historical-provider-receipt"
