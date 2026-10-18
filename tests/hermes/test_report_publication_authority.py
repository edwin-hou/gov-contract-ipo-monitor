"""A healthy baseline check is not current-main authority for AI or email."""
from datetime import timedelta
import hashlib
from pathlib import Path
import sys

import pytest

import hermes_market_monitor as monitor
import market_notifications as notifications
from contract_ipo_monitor.analyst import prepare_candidates
from test_ai_notifications import Model, NOW, configured, refreshed, report
from test_hermes_market_monitor import FakeGitHub, SHA, setup
from test_market_notifications import Worker


VERIFICATION = "downloaded_actual_report_artifact; current_main_run_identity"


def runtime(tmp_path):
    delivery, box, work = configured(tmp_path)
    data = report()
    assert len(prepare_candidates(data, now=NOW)[0]) == 1
    config = setup(tmp_path, baseline=data)
    source = monitor.read_json(config.ledger)["source_report"]
    receipt = {"status": "degraded", "outcome": "no_change", "source_report": source,
               "report_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
               "report_completed_at": data["completed_at"], "verified_run": "100", "head_sha": SHA,
               "main_sha": SHA, "verification": VERIFICATION, "publication_authorized": True}
    monitor.write_json(config.receipt, receipt)
    monitor.write_json(work / "market-delivery.json", delivery)
    return config, box, work, receipt


def simulated_analysis(monkeypatch):
    model, refreshes = Model(), []
    original = notifications.stage_ai_notifications

    def refresh(data, at):
        refreshes.append(at)
        return refreshed(data, at)

    def stage(*args, **kwargs):
        return original(*args, **kwargs, infer=model, refresh=refresh, clock=lambda: NOW)

    monkeypatch.setattr(notifications, "stage_ai_notifications", stage)
    return model, refreshes


def test_new_main_pending_cannot_reuse_buy_eligible_old_main_baseline_for_analysis(tmp_path, monkeypatch):
    config, box, _, receipt = runtime(tmp_path)
    receipt.update(main_sha="b"*40, head_sha="b"*40)
    monitor.write_json(config.receipt, receipt)
    before = config.ledger.read_bytes()
    monitor.poll(config, now=NOW, github=FakeGitHub(report(), run_id=102, status="queued", conclusion=None))
    assert monitor.read_json(config.receipt)["outcome"] == "pending_report"
    model, refreshes = simulated_analysis(monkeypatch)
    worker = Worker()
    assert notifications.process_notifications(config, now=NOW, worker=worker) == ""
    assert model.calls == [] and refreshes == []
    assert worker.calls == [True] and box.states() == {}
    assert config.ledger.read_bytes() == before
    checked = monitor.read_json(config.receipt)
    assert checked["publication_authorized"] is False
    assert checked["ai_analysis"]["status"] == "blocked"
    assert checked["ai_analysis"]["send_allowed"] is False


@pytest.mark.parametrize("change", [
    {"outcome": "pending_report"}, {"outcome": "unknown"}, {"status": "error"},
    {"head_sha": "b"*40}, {"main_sha": "invalid"}, {"verified_run": None},
    {"verified_run": True}, {"verification": None}, {"publication_authorized": False},
    {"publication_authorized": None},
])
def test_incomplete_or_mismatched_publication_authority_is_reconcile_only(tmp_path, monkeypatch, change):
    config, box, _, receipt = runtime(tmp_path)
    monitor.write_json(config.receipt, {**receipt, **change})
    model, refreshes = simulated_analysis(monkeypatch)
    worker = Worker()
    notifications.process_notifications(config, now=NOW, worker=worker)
    assert model.calls == [] and refreshes == []
    assert worker.calls == [True] and box.states() == {}


def test_current_main_authority_can_still_queue_a_simulated_approved_opportunity(tmp_path, monkeypatch):
    config, box, _, _ = runtime(tmp_path)
    model, refreshes = simulated_analysis(monkeypatch)
    worker = Worker()
    notifications.process_notifications(config, now=NOW, worker=worker)
    assert len(model.calls) == 1 and refreshes == [NOW]
    assert worker.calls == [False] and box.states() == {"pending": 1}


def test_pending_baseline_cannot_regain_publication_authority_from_maintenance_outcome(tmp_path, monkeypatch):
    config, _, _, receipt = runtime(tmp_path)
    monitor.write_json(config.receipt, receipt)
    monitor.poll(config, now=NOW, github=FakeGitHub(report(), status="queued", conclusion=None))
    pending = monitor.read_json(config.receipt)
    # A maintenance digest may legitimately replace the scheduler outcome, but
    # it cannot substitute for this poll's absent current-main run evidence.
    pending["outcome"] = "published_change"
    assert pending["head_sha"] == pending["main_sha"]
    assert not monitor.report_publication_authorized(pending)


def test_current_main_baseline_download_and_cached_readback_each_reestablish_authority(tmp_path):
    config, _, _, receipt = runtime(tmp_path)
    # Baseline path has no preceding verified-run receipt to borrow.
    receipt.pop("verified_run")
    monitor.write_json(config.receipt, receipt)
    monitor.poll(config, now=NOW, github=FakeGitHub(report(), run_id=100))
    assert monitor.report_publication_authorized(monitor.read_json(config.receipt))
    assert "existing_notified_baseline" in monitor.read_json(config.receipt)["verification"]
    monitor.poll(config, now=NOW, github=FakeGitHub(report(), run_id=101))
    assert monitor.report_publication_authorized(monitor.read_json(config.receipt))
    assert "downloaded_actual_report_artifact" in monitor.read_json(config.receipt)["verification"]
    monitor.poll(config, now=NOW, github=FakeGitHub(report(), run_id=101))
    assert monitor.report_publication_authorized(monitor.read_json(config.receipt))
    assert "sealed_current_main_report_readback" in monitor.read_json(config.receipt)["verification"]


def test_native_final_receipt_completion_follows_notification_work(tmp_path, monkeypatch):
    config = monitor.Config(tmp_path)
    at = [NOW]

    class Clock:
        @staticmethod
        def now(_):
            return at[0]

    def poll(configuration):
        monitor.write_json(configuration.receipt, {"status": "degraded", "outcome": "pending_report",
            "completed_at": at[0].isoformat(), "poll_started_at": at[0].isoformat()})
        return ""

    def process(configuration):
        at[0] += timedelta(seconds=30)
        receipt = monitor.read_json(configuration.receipt)
        receipt["ai_analysis"] = {"status": "blocked", "checked_at": at[0].isoformat()}
        monitor.write_json(configuration.receipt, receipt)
        at[0] += timedelta(seconds=1)
        return ""

    monkeypatch.setattr(monitor, "datetime", Clock)
    monkeypatch.setattr(monitor, "poll", poll)
    monkeypatch.setattr(monitor, "request_due_collection", lambda _: None)
    monkeypatch.setattr(notifications, "process_notifications", process)
    monkeypatch.setattr(notifications, "delivery_config", lambda *args: {"notification_policy": "ai_approved_only"})
    monkeypatch.setattr(sys, "argv", ["hermes_market_monitor.py", "--base", str(tmp_path)])
    assert monitor.main() == 0
    receipt = monitor.read_json(config.receipt)
    assert receipt["completed_at"] == (NOW+timedelta(seconds=31)).isoformat()
    assert receipt["poll_started_at"] == NOW.isoformat()
    assert receipt["completed_at"] > receipt["ai_analysis"]["checked_at"]
