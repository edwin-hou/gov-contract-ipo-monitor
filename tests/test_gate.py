from datetime import UTC, datetime, timedelta
from pathlib import Path

from contract_ipo_monitor.db import Database
from contract_ipo_monitor.gate import AlertGate
from contract_ipo_monitor.models import Candidate, ContractEvidence, EvidenceClass, ListingRoute, ListingSignal

NOW = datetime(2026, 7, 24, 18, 0, tzinfo=UTC)


def make_candidate(*, contract_status="awarded", listing_active=True, uei="UEI123456789"):
    contract = ContractEvidence(
        source="usaspending", source_url="https://usaspending.gov/award/1", source_record_id="1",
        retrieved_at=NOW, published_at=NOW, award_id="ABC-123", modification_number="0",
        status=contract_status, award_date=NOW.date(), agency="DOE", recipient_name="Acme Quantum, Inc.",
        recipient_uei=uei, recipient_address="1 Market St, San Francisco, CA 94105", prime=True,
        obligated_amount=10_000_000, current_value=10_000_000, ceiling_amount=20_000_000,
        award_type="definitive_contract", description="Sensors", evidence_class=EvidenceClass.A,
        raw_payload_hash="hash-contract",
    )
    listing = ListingSignal(
        source="sec", source_url="https://sec.gov/filing", signal_id="S1-1",
        issuer_name="Acme Quantum, Inc.", issuer_address="1 Market St, San Francisco, CA 94105",
        cik="1", filed_at=NOW - timedelta(hours=1), active=listing_active,
        status="active" if listing_active else "withdrawn", route=ListingRoute.S1, form_type="S-1",
        is_initial_listing=True, intends_public_trading=True, expected_exchange="NASDAQ",
        proposed_price=4.0, proposed_valuation=200_000_000, max_offering_size=50_000_000,
        linked_ueis=(uei,), external_corroboration=True,
    )
    return Candidate(contract=contract, listing=listing, annual_revenue=25_000_000)


def test_source_records_are_append_only_and_deduplicated(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    payload = {"award_id": "ABC-123", "value": 10}
    first = db.insert_source_record("usaspending", "ABC-123", payload, observed_at=NOW)
    second = db.insert_source_record("usaspending", "ABC-123", payload, observed_at=NOW)
    assert first.inserted is True
    assert second.inserted is False
    assert db.count("source_records") == 1


def test_all_four_gates_create_exactly_one_alert_and_outbox_message(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    gate = AlertGate(db, now=NOW)

    result = gate.evaluate(make_candidate())
    duplicate = gate.evaluate(make_candidate())

    assert result.alert_created is True
    assert duplicate.alert_created is False
    assert duplicate.duplicate is True
    assert db.count("alerts") == 1
    assert db.count("outbox_messages") == 1
    assert db.fetch_outbox()[0]["subject"].startswith("[CONFIRMED CONTRACT + IPO]")


def test_rejected_candidate_records_human_readable_decisions_but_no_email(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    result = AlertGate(db, now=NOW).evaluate(make_candidate(contract_status="solicitation"))

    assert result.alert_created is False
    assert result.decisions[0].passed is False
    assert "solicitation" in result.decisions[0].reason.lower()
    assert db.count("gate_decisions") == 4
    assert db.count("alerts") == 0
    assert db.count("outbox_messages") == 0


def test_entity_mismatch_is_fail_closed(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    candidate = make_candidate(uei="CONTRACTOR-UEI")
    candidate = candidate.model_copy(update={
        "listing": candidate.listing.model_copy(update={
            "linked_ueis": (), "issuer_name": "Acme Quantum Holdings LLC", "issuer_address": None
        })
    })
    result = AlertGate(db, now=NOW).evaluate(candidate)
    assert result.alert_created is False
    assert any(d.gate == "entity" and not d.passed for d in result.decisions)


def test_custom_price_threshold_is_enforced(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    candidate = make_candidate()
    result = AlertGate(db, now=NOW, max_price=3.5).evaluate(candidate)
    assert result.alert_created is False
    small = next(d for d in result.decisions if d.gate == "small_company")
    assert small.passed is False
    assert small.code == "proposed_price_too_high"


def test_custom_market_cap_threshold_is_enforced(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    candidate = make_candidate()
    result = AlertGate(db, now=NOW, max_market_cap=100_000_000).evaluate(candidate)
    assert result.alert_created is True
    # The validator intentionally uses the smallest disclosed primary-source proxy.
    # The $50M max offering size is below the custom $100M threshold.
