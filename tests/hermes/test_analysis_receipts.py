"""Historical AI decisions cannot acquire a fresh timestamp from a failed poll."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

import pytest

import hermes_market_monitor as monitor
import ipo_trade_monitor as bridge
import market_notifications as mail
from test_hermes_market_monitor import FakeGitHub, NOW, report, setup
from test_ipo_trade_monitor import prepared
from test_market_notifications import Worker, configured


def prior_review():
    return {"status": "complete", "review_identity": "c" * 64, "model": "gpt-5.6-sol",
            "model_called": False, "send_allowed": False,
            "reviews": [{"symbol": "TEST", "decision": "wait", "evidence_ids": ["financial", "price"],
                         "rationale": "Reference is below entry.", "counterargument": "A fresh breakout could change this."}],
            "usage": {"total_tokens": 2500}}


@pytest.mark.parametrize("embedded_metadata", [False, True])
def test_stale_repeated_poll_and_recovery_keep_exact_historical_review(tmp_path, embedded_metadata):
    config = setup(tmp_path, baseline=report(NOW - timedelta(hours=8)))
    ledger = monitor.read_json(config.ledger)
    prior = prior_review()
    claimed_at = NOW - timedelta(hours=7, minutes=55)
    if embedded_metadata:
        prior.update(report_completed_at=(NOW - timedelta(hours=9)).isoformat(), report_sha256="d" * 64,
                     checked_at=claimed_at.isoformat())
    with sqlite3.connect(config.work / "market-analyst.sqlite3") as connection:
        connection.execute("CREATE TABLE analyst_calls(identity TEXT, claimed_at TEXT)")
        connection.execute("INSERT INTO analyst_calls VALUES(?,?)", (prior["review_identity"], claimed_at.isoformat()))
    monitor.write_json(config.receipt, {"ai_analysis": prior, "source_report": ledger["source_report"],
                                      "report_completed_at": ledger["last_report_at"],
                                      "report_sha256": ledger["report_sha256"]})
    failed = FakeGitHub(report(), run_id=102, conclusion="failure")
    monitor.poll(config, now=NOW, github=failed)
    receipt = monitor.read_json(config.receipt)
    assert receipt["failure_code"] == "report_stale"
    assert receipt["ai_analysis"]["status"] == "blocked"
    assert receipt["ai_analysis"]["reason"] == "report_stale"
    assert receipt["ai_analysis"]["model_called"] is False
    assert receipt["ai_analysis"]["send_allowed"] is False
    history = deepcopy(receipt["last_ai_analysis"])
    assert history["analysis"] == prior
    assert history["historical"] is True and history["result_reused"] is True
    assert history["report_completed_at"] == prior.get("report_completed_at")
    assert history["report_sha256"] == prior.get("report_sha256")
    assert history["binding_status"] == ("analysis_metadata" if embedded_metadata else "legacy_receipt_context_only")
    assert history["receipt_report_context"] == {"report_completed_at": ledger["last_report_at"],
                                                "report_sha256": ledger["report_sha256"]}
    assert history["analysis_checked_at"] == prior.get("checked_at")  # Legacy completion time remains unknown.
    assert history["model_claimed_at"] == claimed_at.isoformat()
    monitor.poll(config, now=NOW + timedelta(minutes=1), github=failed)
    assert monitor.read_json(config.receipt)["last_ai_analysis"] == history
    monitor.poll(config, now=NOW + timedelta(minutes=2), github=FakeGitHub(report()))
    receipt = monitor.read_json(config.receipt)
    assert receipt["status"] == "degraded" and "failure_code" not in receipt
    assert receipt["ai_analysis"]["status"] == "not_run"
    assert receipt["ai_analysis"]["model_called"] is False
    assert receipt["last_ai_analysis"] == history


def test_unverified_notification_only_reconciles_and_keeps_prior_review(tmp_path, monkeypatch):
    config, _, _ = configured(tmp_path)
    prior = prior_review()
    receipt = monitor.read_json(config.receipt)
    receipt.update(status="error", outcome="failure", failure_code="report_stale", ai_analysis=prior)
    monitor.write_json(config.receipt, receipt)
    monkeypatch.setattr(mail, "stage_ai_notifications", lambda *a, **k: (_ for _ in ()).throw(AssertionError("analysis started")))
    worker = Worker()
    assert mail.process_notifications(config, now=NOW, worker=worker) == ""
    receipt = monitor.read_json(config.receipt)
    assert worker.calls == [True]
    assert receipt["ai_analysis"]["status"] == "blocked" and receipt["ai_analysis"]["reason"] == "report_stale"
    assert receipt["last_ai_analysis"]["analysis"] == prior
    assert receipt["last_ai_analysis"]["model_claimed_at"] is None


def test_new_notification_check_labels_reused_result_without_calling_model(tmp_path, monkeypatch):
    config, _, data = configured(tmp_path)
    prior = prior_review()
    monkeypatch.setattr(mail, "stage_ai_notifications", lambda *a, **k: ([], prior))
    worker = Worker()
    assert mail.process_notifications(config, now=NOW, worker=worker) == ""
    analysis = monitor.read_json(config.receipt)["ai_analysis"]
    assert analysis["status"] == "complete" and analysis["result_reused"] is True
    assert analysis["checked_at"] == NOW.isoformat()
    assert analysis["report_completed_at"] == data["completed_at"]
    assert analysis["model_claimed_at"] is None
    assert worker.calls == [True]  # A historical WAIT cannot authorize delivery.
    for key, value in prior.items():
        assert analysis[key] == value


def test_native_receipt_preserves_current_status_and_historical_evidence(tmp_path, monkeypatch):
    path = prepared(tmp_path, monkeypatch)
    original = bridge.fresh_receipt
    current = {"status": "blocked", "reason": "report_stale", "model_called": False, "send_allowed": False}
    history = {"historical": True, "analysis": prior_review(), "report_completed_at": "2026-10-05T01:00:00+00:00"}

    def read(*args):
        receipt = original(*args)
        receipt.update(status="error", outcome="failure", ai_analysis=current, last_ai_analysis=history)
        return receipt

    monkeypatch.setattr(bridge, "fresh_receipt", read)
    assert bridge.run() == 1
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["ai_analysis"] == current and value["last_ai_analysis"] == history
    assert "completed_at" not in value
