from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

import hermes_market_monitor as monitor
import market_notifications as mail
from contract_ipo_monitor.notifications import EmailOutbox
from test_hermes_market_monitor import report, setup, NOW


def configured(tmp_path):
    config = setup(tmp_path)
    data = report()
    data["listed_discovery"] = []
    data["trade_ideas"][0].update(strategy={"maximum_entry": 101, "setup_valid_through": "2026-10-09"}, indicators={"last_close": 99})
    data["trade_ideas"][1]["strategy"] = {"scope": "paper_reference_only"}
    source = config.outputs / "verified" / "latest.json"
    monitor.write_json(source, data)
    monitor.write_json(config.receipt, {"status": "degraded", "outcome": "no_change", "source_report": str(source),
                                      "report_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    delivery = {"enabled": True, "sender": mail.TARGET, "recipient": mail.TARGET, "holdings": [],
                "outbox_path": str(config.work / "mail.sqlite3"), "hermes_home": str(tmp_path / "hermes"),
                "initial_report_requested": True, "initial_event_key": "first-authorized-report"}
    monitor.write_json(config.work / "market-delivery.json", delivery)
    return config, delivery, data


class Worker:
    def __init__(self):
        self.calls = []

    def __call__(self, config, work, *, reconcile_only=False):
        self.calls.append(reconcile_only)
        box = EmailOutbox(Path(config["outbox_path"]))
        monitor.write_json(work / "hermes-email-check.json", {"status": "degraded" if box.states().get("pending") else "ok",
            "outcome": "delivery_unresolved", "states": box.states(), "newly_sent_ids": [], "reconciled_ids": [],
            "completed_at": datetime.now(UTC).isoformat()})
        return 0


def test_disabled_is_quiet_and_never_starts_worker_or_changes_baseline(tmp_path):
    config = setup(tmp_path)
    monitor.write_json(config.receipt, {"status": "ok"})
    before = config.ledger.read_bytes()
    assert mail.process_notifications(config, now=NOW, worker=lambda *a, **k: pytest.fail("worker started")) == ""
    assert config.ledger.read_bytes() == before
    assert monitor.read_json(config.receipt)["email_outcome"] == "disabled"
    assert not (config.work / "mail.sqlite3").exists()


def test_initial_authorized_report_is_immutable_and_retry_is_checked_even_on_quiet_poll(tmp_path):
    config, delivery, data = configured(tmp_path)
    worker = Worker()
    assert mail.process_notifications(config, now=NOW, worker=worker) == ""
    box = EmailOutbox(Path(delivery["outbox_path"]))
    first = box.event(delivery["initial_event_key"])
    assert first["status"] == "pending" and box.states() == {"pending": 1}
    assert mail.process_notifications(config, now=NOW + timedelta(minutes=1), worker=worker) == ""
    assert box.event(delivery["initial_event_key"])["raw_message"] == first["raw_message"]
    assert worker.calls == [False, False]
    assert monitor.read_json(config.receipt)["email_queued"] == []


def test_changed_recipient_or_nonempty_holdings_requires_review_not_a_send(tmp_path):
    config, delivery, data = configured(tmp_path)
    delivery["recipient"] = "unconfirmed@example.org"
    monitor.write_json(config.work / "market-delivery.json", delivery)
    worker = Worker()
    assert "needs review" in mail.process_notifications(config, now=NOW, worker=worker)
    assert not worker.calls
    assert mail.process_notifications(config, now=NOW, worker=worker) == ""  # repeated failure is quiet
    delivery.update(recipient=mail.TARGET, holdings=["TEST"])
    monitor.write_json(config.work / "market-delivery.json", delivery)
    mail.process_notifications(config, now=NOW, worker=worker)
    assert not worker.calls


def test_failed_core_validation_only_reconciles_unknown_sends_no_new_report(tmp_path):
    config, delivery, data = configured(tmp_path)
    monitor.write_json(config.receipt, {"status": "error", "outcome": "failure"})
    worker = Worker()
    mail.process_notifications(config, now=NOW, worker=worker)
    assert worker.calls == [True]
    assert EmailOutbox(Path(delivery["outbox_path"])).states() == {}


def test_report_hash_and_staleness_prevent_publication(tmp_path):
    config, delivery, data = configured(tmp_path)
    receipt = monitor.read_json(config.receipt)
    source = Path(receipt["source_report"])
    data["trade_ideas"][0]["entry"] = 105
    monitor.write_json(source, data)
    worker = Worker()
    mail.process_notifications(config, now=NOW, worker=worker)
    assert not worker.calls and not Path(delivery["outbox_path"]).exists()
    receipt["report_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    monitor.write_json(config.receipt, receipt)
    mail.process_notifications(config, now=NOW + timedelta(hours=7), worker=worker)
    assert not worker.calls


def test_local_notice_readback_and_source_binding(tmp_path):
    config, delivery, data = configured(tmp_path)
    delivery["initial_report_requested"] = False
    box = EmailOutbox(Path(delivery["outbox_path"]))
    notice = config.outputs / "notice.md"
    notice.write_text("Verified hosted report: " + data["completed_at"], encoding="utf-8")
    receipt = {"outcome": "published_change", "digest_path": str(notice), "digest_sha256": hashlib.sha256(notice.read_bytes()).hexdigest()}
    assert len(mail.stage_notifications(delivery, box, receipt, data, {}, NOW, outputs_root=tmp_path / "outputs")) == 1
    assert mail.stage_notifications(delivery, box, receipt, data, {}, NOW, outputs_root=tmp_path / "outputs") == []
    changed = deepcopy(data)
    changed["completed_at"] = (NOW - timedelta(minutes=1)).isoformat()
    assert mail.stage_notifications(delivery, box, receipt, changed, {}, NOW, outputs_root=tmp_path / "outputs") == []
    notice.write_text("altered", encoding="utf-8")
    with pytest.raises(ValueError, match="readback"):
        mail.stage_notifications(delivery, box, receipt, data, {}, NOW, outputs_root=tmp_path / "outputs")


def test_replay_after_queue_before_plan_save_does_not_duplicate_email(tmp_path, monkeypatch):
    config, delivery, data = configured(tmp_path)
    box = EmailOutbox(Path(delivery["outbox_path"]))
    original = box.save_plans
    monkeypatch.setattr(box, "save_plans", lambda p: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        mail.stage_notifications(delivery, box, {}, data, {}, NOW, outputs_root=tmp_path / "outputs")
    assert box.states() == {"pending": 1}
    monkeypatch.setattr(box, "save_plans", original)
    assert mail.stage_notifications(delivery, box, {}, data, {}, NOW + timedelta(minutes=1), outputs_root=tmp_path / "outputs") == []
    assert box.states() == {"pending": 1} and box.plans()["TEST"]["assumed_position"] is False


def test_recover_local_publication_after_a_new_artifact_without_reusing_old_levels(tmp_path):
    config, delivery, old = configured(tmp_path)
    delivery["initial_report_requested"] = False
    original = config.outputs / "old.json"
    monitor.write_json(original, old)
    notice = config.outputs / "saved.md"
    notice.write_text("Verified hosted report: " + old["completed_at"], encoding="utf-8")
    digest = hashlib.sha256(notice.read_bytes()).hexdigest()
    ledger = {"last_report_at": old["completed_at"], "source_report": str(original), "ideas": monitor.idea_snapshot(old),
              "report_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
              "local_delivery": {"path": str(notice), "sha256": digest}}
    current = deepcopy(old)
    current["completed_at"] = (NOW-timedelta(minutes=1)).isoformat()
    current["trade_ideas"][0]["entry"] = 100.5
    box = EmailOutbox(Path(delivery["outbox_path"]))
    assert len(mail.stage_notifications(delivery, box, {}, current, ledger, NOW, outputs_root=tmp_path / "outputs")) == 1
    row = box.event("local-notice:" + digest)
    from email.parser import BytesParser
    from email import policy
    message = BytesParser(policy=policy.default).parsebytes(bytes(row["raw_message"]))
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "earlier text is historical" in body and "100.5" in body
    attached = next(x for x in message.iter_attachments() if x.get_filename() == "research-report.json")
    assert json.loads(attached.get_payload(decode=True))["completed_at"] == current["completed_at"]
    assert mail.stage_notifications(delivery, box, {}, current, ledger, NOW, outputs_root=tmp_path / "outputs") == []


def test_discovery_and_reference_alerts_remain_review_only_and_are_deduped(tmp_path):
    config, delivery, data = configured(tmp_path)
    delivery["initial_report_requested"] = False
    box = EmailOutbox(Path(delivery["outbox_path"]))
    data["listed_discovery"] = [{"symbol": "NEW", "exchange": "NYSE", "status": "qualified_review", "financials": {"revenue": 123}, "reasons": ["WAIT: price and news required"]}]
    assert len(mail.stage_notifications(delivery, box, {}, data, {}, NOW, outputs_root=tmp_path / "outputs")) == 1
    assert mail.stage_notifications(delivery, box, {}, data, {}, NOW, outputs_root=tmp_path / "outputs") == []
    data["trade_ideas"][0].update(price_as_of="2026-10-05", indicators={"last_close": 100.5})
    assert len(mail.stage_notifications(delivery, box, {}, data, {}, NOW, outputs_root=tmp_path / "outputs")) == 1
    assert box.plans()["TEST"]["state"] == "reference_triggered"
    assert not box.plans()["TEST"]["assumed_position"]
    assert mail.stage_notifications(delivery, box, {}, data, {}, NOW, outputs_root=tmp_path / "outputs") == []
    data["listed_discovery"] = []
    assert len(mail.stage_notifications(delivery, box, {}, data, {}, NOW, outputs_root=tmp_path / "outputs")) == 1


def test_sender_subprocess_has_no_shell_or_foreground_activation(tmp_path, monkeypatch):
    config, delivery, data = configured(tmp_path)
    def run(argv, **kw):
        assert argv[0].endswith("python.exe") and "--reconcile-only" in argv
        assert kw["shell"] is False and kw["capture_output"] and kw["timeout"] == 300
        assert "creationflags" in kw
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(mail.subprocess, "run", run)
    assert mail.run_mail_worker(delivery, config.work, reconcile_only=True) == 0
