import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_email_worker as worker

NOW = datetime(2026, 10, 6, 1, tzinfo=UTC)


def config(tmp_path, monkeypatch, *, enabled=True):
    monkeypatch.setattr(worker, "WORK", tmp_path)
    value = {"enabled": enabled, "sender": worker.CONFIRMED_RECIPIENT,
             "recipient": worker.CONFIRMED_RECIPIENT, "hermes_home": str(tmp_path / "hermes"),
             "outbox_path": str(tmp_path / "outbox.sqlite3"), "holdings": [], "initial_report_requested": True}
    path = tmp_path / "delivery.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


class Transport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Outbox:
    def __init__(self, *, pending=0, recovered=None, failure=False):
        self.pending, self.recovered, self.failure = pending, recovered or [], failure
        self.deliver_calls = 0
        self.reconcile_calls = 0
        self.sent = []

    def reconcile_unknown(self, transport, *, now, limit):
        self.reconcile_calls += 1
        assert now == NOW and limit == 10
        if self.failure:
            raise RuntimeError("private-access-token")
        return [{"id": value, "status": "sent"} for value in self.recovered]

    def deliver_once(self, transport, *, now):
        self.deliver_calls += 1
        if not self.pending:
            return None
        self.pending -= 1
        self.sent.append(100 + len(self.sent))
        return {"id": self.sent[-1], "status": "sent"}

    def states(self):
        return {"pending": self.pending, "sent": len(self.sent) + len(self.recovered)}

    def get(self, identifier):
        return {"status": "sent", "receipt_json": json.dumps({
            "gmail_message_id": "gmail" + str(identifier), "thread_id": "thread" + str(identifier),
            "recipient": worker.CONFIRMED_RECIPIENT, "sender": worker.CONFIRMED_RECIPIENT,
            "verified_at": NOW.isoformat(), "provider_accepted_at": NOW.isoformat(),
            "content_sha256": "a" * 64, "content_sha256_version": "mime-tree-v2", "raw_content_sha256": "b" * 64, "readback_raw_sha256": "c" * 64,
            "delivered_label": "INBOX", "rfc822_id": "<exact@monitor.local>",
            "unwanted_secret": "private-access-token"})}


def execute(tmp_path, path, transport, outbox, **kwargs):
    return worker.run_worker(config_path=path, receipt_path=tmp_path / "receipt.json",
                             make_transport=lambda config: transport, make_outbox=lambda path: outbox,
                             check_runtime=lambda home: None, now=lambda: NOW, **kwargs)


def test_disabled_or_missing_configuration_is_quiet_and_never_constructs_transport(tmp_path, monkeypatch):
    path, _ = config(tmp_path, monkeypatch, enabled=False)
    def forbidden(*args):
        pytest.fail("Disabled worker must not load Gmail credentials or mutate an outbox")
    for selected in (path, tmp_path / "absent.json"):
        code, receipt = worker.run_worker(config_path=selected, receipt_path=tmp_path / "receipt.json",
                                          make_transport=forbidden, make_outbox=forbidden,
                                          check_runtime=forbidden, now=lambda: NOW)
        assert code == 0 and receipt["outcome"] == "disabled"
        assert not receipt["newly_sent_ids"] and receipt["completed_at"] == NOW.isoformat()


def test_reconciliation_precedes_bounded_delivery_and_receipts_omit_secrets(tmp_path, monkeypatch):
    path, _ = config(tmp_path, monkeypatch)
    transport, outbox = Transport(), Outbox(pending=7, recovered=[3])
    code, result = execute(tmp_path, path, transport, outbox)
    assert code == 0 and result["status"] == "degraded"
    assert outbox.reconcile_calls == 1 and outbox.deliver_calls == 5 and transport.closed
    assert result["reconciled_ids"] == [3] and len(result["newly_sent_ids"]) == 5
    assert result["states"] == {"pending": 2, "sent": 6}
    persisted = (tmp_path / "receipt.json").read_text(encoding="utf-8")
    assert "private-access-token" not in persisted and "unwanted_secret" not in persisted
    assert result["verified_deliveries"][0]["receipt"]["gmail_message_id"] == "gmail3"


def test_reconcile_only_recovers_receipts_without_calling_delivery(tmp_path, monkeypatch):
    path, _ = config(tmp_path, monkeypatch)
    transport, outbox = Transport(), Outbox(pending=2, recovered=[3])
    code, result = execute(tmp_path, path, transport, outbox, reconcile_only=True)
    assert code == 0 and result["reconciled_ids"] == [3]
    assert not result["newly_sent_ids"] and outbox.deliver_calls == 0 and transport.closed


def test_transport_cleanup_after_failure_and_sanitized_error_receipt(tmp_path, monkeypatch):
    path, _ = config(tmp_path, monkeypatch)
    transport = Transport()
    code, result = execute(tmp_path, path, transport, Outbox(failure=True))
    assert code == 1 and transport.closed and result["error_code"] == "email_worker_failed"
    assert "private-access-token" not in (tmp_path / "receipt.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("change", ["recipient", "outbox_path"])
def test_unconfirmed_target_and_external_outbox_fail_before_factory(tmp_path, monkeypatch, change):
    path, value = config(tmp_path, monkeypatch)
    value[change] = "different@example.com" if change == "recipient" else str(tmp_path.parent / "unrelated.sqlite3")
    path.write_text(json.dumps(value), encoding="utf-8")
    def forbidden(*args):
        pytest.fail("Invalid configuration must not construct a client or outbox")
    code, result = worker.run_worker(config_path=path, receipt_path=tmp_path / "receipt.json",
                                     make_transport=forbidden, make_outbox=forbidden,
                                     check_runtime=forbidden, now=lambda: NOW)
    assert code == 1 and result["status"] == "error"


def test_worker_rejects_wrong_interpreter_before_loading_credentials(tmp_path, monkeypatch):
    path, _ = config(tmp_path, monkeypatch)
    def forbidden(*args):
        pytest.fail("Wrong runtime must fail before Gmail and outbox loading")
    code, result = worker.run_worker(config_path=path, receipt_path=tmp_path / "receipt.json",
                                     make_transport=forbidden, make_outbox=forbidden, now=lambda: NOW)
    assert code == 1 and result["status"] == "error"


def test_canonical_loader_scopes_home_and_preserves_refresh_helper_arguments(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    source = home / "scripts" / "gmail_sync.py"
    source.parent.mkdir(parents=True)
    source.write_text("# canonical helper\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", "unrelated-profile")
    calls = []
    def credentials(*, require_sheets):
        calls.append(require_sheets)
        assert os.environ["HERMES_HOME"] == str(home)
        return SimpleNamespace(token="private-test-token")
    helper = SimpleNamespace(__file__=str(source), TOKEN_FILE=home / "google_token.json", credentials=credentials)
    monkeypatch.setattr(worker.importlib, "import_module", lambda name: helper)
    original = list(worker.sys.path)
    result = worker.canonical_credentials(home)
    assert result.token == "private-test-token" and calls == [False]
    assert os.environ["HERMES_HOME"] == "unrelated-profile" and worker.sys.path == original


def test_cached_wrong_profile_helper_cannot_refresh_unrelated_credentials(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    source = home / "scripts" / "gmail_sync.py"
    source.parent.mkdir(parents=True)
    source.write_text("# canonical helper\n", encoding="utf-8")
    def forbidden(**kwargs):
        pytest.fail("The wrong cached profile must never refresh credentials")
    helper = SimpleNamespace(__file__=str(source), TOKEN_FILE=tmp_path / "other" / "google_token.json", credentials=forbidden)
    monkeypatch.setattr(worker.importlib, "import_module", lambda name: helper)
    with pytest.raises(RuntimeError, match="profile_mismatch"):
        worker.canonical_credentials(home)


def test_factory_injects_canonical_loader_and_owned_transport_contract(tmp_path, monkeypatch):
    seen = {}
    class ActualTransport:
        def __init__(self, **kwargs):
            seen.update(kwargs)
    monkeypatch.setattr(worker, "repo_modules", lambda: (ActualTransport, object))
    monkeypatch.setattr(worker, "canonical_credentials", lambda home: ("scoped", home))
    home = tmp_path / "hermes"
    worker.transport_factory({"sender": worker.CONFIRMED_RECIPIENT, "recipient": worker.CONFIRMED_RECIPIENT, "hermes_home": home})
    assert seen["expected_sender"] == seen["allowed_recipient"] == worker.CONFIRMED_RECIPIENT
    assert seen["credentials_loader"]() == ("scoped", home)


def test_unverified_or_unbounded_receipt_cannot_be_published_as_delivery():
    receipt = json.loads(Outbox().get(1)["receipt_json"])
    for field, value in (("content_sha256", "not-a-sha"), ("recipient", "other@example.com"),
                         ("verified_at", "2026-10-06T00:00:00"), ("delivered_label", "DRAFT"),
                         ("gmail_message_id", "too-long" * 100), ("content_sha256_version", "leaf-v1"),
                         ("content_sha256_version", "unknown-v3"), ("content_sha256_version", None)):
        malformed = {**receipt, field: value}
        with pytest.raises(ValueError):
            worker.public_receipt(malformed)


def test_valid_gmail_generated_dot_atom_identity_in_receipt_is_preserved():
    receipt = json.loads(Outbox().get(1)["receipt_json"])
    receipt["provider_rfc822_id"] = "<CAP0eM+r=qeU6D2_hDn=UTP9ttSXegRN8J+3iGchBegxtowAhEQ@mail.gmail.com>"
    receipt["rfc822_identity_status"] = "provider_rewritten"
    assert worker.public_receipt(receipt)["provider_rfc822_id"] == receipt["provider_rfc822_id"]
