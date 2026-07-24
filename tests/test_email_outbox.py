from datetime import UTC, datetime, timedelta
from pathlib import Path

from contract_ipo_monitor.db import Database
from contract_ipo_monitor.gate import AlertGate
from contract_ipo_monitor.smtp_worker import SMTPWorker
from test_gate import NOW, make_candidate


def test_email_contains_required_evidence_and_risk_sections(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    AlertGate(db, now=NOW).evaluate(make_candidate())
    message = db.fetch_outbox()[0]
    body = message["text_body"]
    for heading in (
        "Why this alert fired",
        "Company and listing status",
        "Contract details",
        "Small-company screen",
        "Materiality context",
        "Risk report",
        "Evidence and timestamps",
        "not investment advice",
    ):
        assert heading.lower() in body.lower()


def test_smtp_worker_marks_message_sent_and_records_acceptance(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    AlertGate(db, now=NOW).evaluate(make_candidate())
    sent = []

    def transport(message):
        sent.append(message)
        return "smtp-id-1"

    worker = SMTPWorker(db, transport=transport, now=lambda: NOW)
    assert worker.run_once() is True
    row = db.fetch_outbox()[0]
    assert row["status"] == "sent"
    assert row["smtp_message_id"] == "smtp-id-1"
    assert len(sent) == 1


def test_smtp_worker_retries_temporary_failure(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    AlertGate(db, now=NOW).evaluate(make_candidate())

    def broken(_message):
        raise TimeoutError("temporary")

    worker = SMTPWorker(db, transport=broken, now=lambda: NOW, max_attempts=3)
    assert worker.run_once() is False
    row = db.fetch_outbox()[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "temporary" in row["last_error"]
    assert datetime.fromisoformat(row["next_attempt_at"]) > NOW


def test_expired_lease_can_be_recovered_after_restart(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    AlertGate(db, now=NOW).evaluate(make_candidate())
    leased = db.lease_outbox(now=NOW, lease_for=timedelta(seconds=5))
    assert leased is not None
    assert db.lease_outbox(now=NOW + timedelta(seconds=3)) is None
    assert db.lease_outbox(now=NOW + timedelta(seconds=6)) is not None


def test_repeated_failures_move_message_to_dead_letter(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    AlertGate(db, now=NOW).evaluate(make_candidate())

    def broken(_message):
        raise OSError("mail down")

    current = [NOW]
    worker = SMTPWorker(db, transport=broken, now=lambda: current[0], max_attempts=2, retry_base=timedelta(seconds=1))
    worker.run_once()
    current[0] = NOW + timedelta(seconds=2)
    worker.run_once()
    row = db.fetch_outbox()[0]
    assert row["status"] == "dead"
    assert db.count("dead_letters") == 1
