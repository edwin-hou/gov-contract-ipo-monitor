from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.health import HealthRegistry, create_health_app
from contract_ipo_monitor.models import ContractEvidence, EvidenceClass, ListingRoute, ListingSignal
from contract_ipo_monitor.processor import EvidenceProcessor

NOW = datetime(2026, 7, 24, 18, 0, tzinfo=UTC)


def contract(name="Acme Quantum, Inc.", uei="UEI123456789", status="awarded"):
    return ContractEvidence(
        source="usaspending", source_url="https://usaspending.gov/a", source_record_id="a",
        retrieved_at=NOW, published_at=NOW, award_id="ABC-123", modification_number="0",
        status=status, award_date=NOW.date(), agency="DOE", recipient_name=name, recipient_uei=uei,
        recipient_address="1 Market St, San Francisco, CA 94105", prime=True,
        obligated_amount=10_000_000, current_value=10_000_000, ceiling_amount=20_000_000,
        award_type="definitive_contract", description="Sensors", evidence_class=EvidenceClass.A,
        raw_payload_hash=f"contract-{status}", cancelled=status == "cancelled",
    )


def listing(name="Acme Quantum, Inc.", uei="UEI123456789", active=True, status="active"):
    return ListingSignal(
        source="sec", source_url="https://sec.gov/s1", signal_id="S1-1", issuer_name=name,
        issuer_address="1 Market St, San Francisco, CA 94105", cik="1", filed_at=NOW - timedelta(hours=1),
        active=active, status=status, route=ListingRoute.S1, form_type="S-1", is_initial_listing=True,
        intends_public_trading=True, expected_exchange="NASDAQ", proposed_price=4.0,
        proposed_valuation=200_000_000, max_offering_size=50_000_000,
        linked_ueis=(uei,) if uei else (), external_corroboration=True,
        raw_payload_hash=f"listing-{status}",
    )


def test_processor_joins_only_plausible_counterparts_and_alerts_once(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)

    assert processor.ingest_contract(contract()) == []
    results = processor.ingest_listing(listing())
    assert len(results) == 1
    assert results[0].alert_created is True
    assert db.count("alerts") == 1
    assert db.count("outbox_messages") == 1


def test_processor_does_not_cross_join_similar_names(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract(name="Acme Quantum Holdings LLC", uei="UEI-A"))
    results = processor.ingest_listing(listing(name="Acme Quantum Inc", uei="UEI-B"))
    assert results == []
    assert db.count("outbox_messages") == 0


def test_official_uei_bridge_can_join_exact_legal_name_without_contract_uei(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    no_uei_contract = contract(uei=None).model_copy(update={"recipient_address": None})
    processor.ingest_contract(no_uei_contract)
    results = processor.ingest_listing(listing(uei="UEI123456789"))
    assert len(results) == 1
    assert results[0].alert_created is True
    entity_decision = next(d for d in results[0].decisions if d.gate == "entity")
    assert entity_decision.code == "official_recipient_identity"


def test_withdrawal_after_alert_queues_correction(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    processor.ingest_listing(listing(active=False, status="withdrawn"))
    rows = db.fetch_outbox()
    assert len(rows) == 2
    assert rows[1]["subject"].startswith("[CORRECTION]")
    assert "withdrawn" in rows[1]["text_body"].lower()


def test_settings_validation_requires_contact_and_mail_configuration(tmp_path: Path):
    settings = Settings(database_path=tmp_path / "db.sqlite", evidence_archive_path=tmp_path / "evidence")
    errors = settings.runtime_errors()
    assert any("SEC_USER_AGENT" in error for error in errors)
    assert any("SMTP_HOST" in error for error in errors)


def test_health_endpoints_distinguish_liveness_and_readiness():
    registry = HealthRegistry()
    app = create_health_app(registry)
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    registry.mark_success("sec")
    registry.mark_success("usaspending")
    registry.set_database_ready(True)
    assert client.get("/readyz").status_code == 200


def test_dashboard_and_rejection_api_explain_gate_failures(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    rejected = listing().model_copy(update={"proposed_price": 7.0})
    processor.ingest_listing(rejected)

    registry = HealthRegistry()
    client = TestClient(create_health_app(registry, db))
    response = client.get("/api/rejections")
    assert response.status_code == 200
    rows = response.json()["rejections"]
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme Quantum, Inc."
    assert "small_company" in rows[0]["failed_gates"]

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Acme Quantum, Inc." in dashboard.text
    assert "price_too_high" in dashboard.text
