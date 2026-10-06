from datetime import timedelta
import smtplib

import pytest

from contract_ipo_monitor.db import Database
from contract_ipo_monitor.gate import AlertGate
from contract_ipo_monitor.smtp_worker import SMTPTransport, SMTPWorker
from test_gate import NOW, make_candidate


def prepared(tmp_path, monkeypatch, mode):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    AlertGate(db, now=NOW).evaluate(make_candidate())
    accepted, submitted = [], []
    calls = [0]
    class SMTP:
        def __init__(self, *args, **kwargs):
            calls[0] += 1
            if mode == "preflight" and calls[0] == 1:
                raise TimeoutError("private connection diagnostics")
        def ehlo(self):
            pass
        def send_message(self, message, **kwargs):
            submitted.append(message["Message-ID"])
            if calls[0] == 1:
                if mode == "recipient_temp":
                    raise smtplib.SMTPRecipientsRefused({"a@example.com": (450, b"busy")})
                if mode == "data_temp":
                    raise smtplib.SMTPDataError(451, b"try later")
                if mode == "data_permanent":
                    raise smtplib.SMTPDataError(550, b"rejected")
            accepted.append(message["Message-ID"])
            if mode == "lost_ack":
                raise smtplib.SMTPServerDisconnected("private-token: DATA accepted, response lost")
            if mode == "unexpected_ack":
                raise smtplib.SMTPDataError(354, b"unexpected intermediate response")
            return {}
        def quit(self):
            if mode == "cleanup":
                raise OSError("QUIT failed after successful DATA")
        def close(self):
            if mode == "cleanup":
                raise OSError("Close failed after successful DATA")
    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    clock = [NOW]
    transport = SMTPTransport(host="test.invalid", port=25, username=None, password=None,
                              sender="a@example.com", recipients=("a@example.com",), security="none")
    worker = SMTPWorker(db, transport=transport, now=lambda: clock[0], retry_base=timedelta(seconds=1))
    return db, worker, clock, accepted, submitted


@pytest.mark.parametrize("mode", ["lost_ack", "unexpected_ack"])
def test_uncertain_data_is_fenced_and_never_resent(tmp_path, monkeypatch, mode):
    db, worker, clock, accepted, submitted = prepared(tmp_path, monkeypatch, mode)
    assert not worker.run_once()
    clock[0] += timedelta(minutes=10)
    assert not worker.run_once()
    row = db.fetch_outbox()[0]
    assert row["status"] == "unknown" and row["attempts"] == 1
    assert len(accepted) == len(submitted) == 1 and row["lease_until"] is None
    assert "private-token" not in row["last_error"]
    with db.connect() as connection:
        saved_error = connection.execute("SELECT error FROM dead_letters").fetchone()[0]
    assert "private-token" not in saved_error


@pytest.mark.parametrize("mode", ["preflight", "recipient_temp", "data_temp"])
def test_definite_preflight_or_explicit_rejection_can_retry_without_duplicate_acceptance(tmp_path, monkeypatch, mode):
    db, worker, clock, accepted, submitted = prepared(tmp_path, monkeypatch, mode)
    assert not worker.run_once() and db.fetch_outbox()[0]["status"] == "pending"
    clock[0] += timedelta(seconds=2)
    assert worker.run_once()
    assert db.fetch_outbox()[0]["status"] == "sent" and len(accepted) == 1


def test_permanent_data_rejection_is_not_retried(tmp_path, monkeypatch):
    db, worker, clock, accepted, submitted = prepared(tmp_path, monkeypatch, "data_permanent")
    assert not worker.run_once()
    clock[0] += timedelta(minutes=10)
    assert not worker.run_once()
    assert db.fetch_outbox()[0]["status"] == "dead" and len(submitted) == 1 and not accepted


def test_quit_and_close_errors_cannot_erase_known_acceptance(tmp_path, monkeypatch):
    db, worker, clock, accepted, submitted = prepared(tmp_path, monkeypatch, "cleanup")
    assert worker.run_once() and db.fetch_outbox()[0]["status"] == "sent"
    clock[0] += timedelta(minutes=10)
    assert not worker.run_once() and len(accepted) == 1


def test_crash_after_acceptance_before_database_commit_cannot_resend(tmp_path, monkeypatch):
    db, worker, clock, accepted, submitted = prepared(tmp_path, monkeypatch, "success")
    def interrupted(*args, **kwargs):
        raise OSError("database acknowledgement unavailable")
    monkeypatch.setattr(db, "mark_outbox_sent", interrupted)
    with pytest.raises(OSError):
        worker.run_once()
    assert db.fetch_outbox()[0]["status"] == "leased" and len(accepted) == 1
    clock[0] += timedelta(minutes=10)
    assert not worker.run_once()
    assert db.fetch_outbox()[0]["status"] == "unknown" and len(accepted) == 1


def test_custom_transport_error_is_unknown_without_a_definite_failure_contract(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    AlertGate(db, now=NOW).evaluate(make_candidate())
    def custom(row):
        raise TimeoutError("could follow an accepted send")
    worker = SMTPWorker(db, transport=custom, now=lambda: NOW)
    assert not worker.run_once()
    assert db.fetch_outbox()[0]["status"] == "unknown"
