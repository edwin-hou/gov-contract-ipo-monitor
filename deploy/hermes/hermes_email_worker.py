"""Local deterministic Gmail outbox worker; invoked by the owning Hermes runtime.

Only the explicitly confirmed recipient is supported. The native credential
helper owns Google refresh locking; neither tokens nor provider errors are logged.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

WORK = Path(__file__).resolve().parent
CONFIG = WORK / "market-delivery.json"
RECEIPT = WORK / "hermes-email-check.json"
REPO_SOURCE = WORK / "gov-contract-ipo-monitor" / "src"
from deployment_settings import settings as deployment_settings
CONFIRMED_RECIPIENT = deployment_settings()["confirmed_recipient"]
RECEIPT_KEYS = {
    "gmail_message_id", "thread_id", "provider_accepted_at", "verified_at", "delivered_label",
    "content_sha256", "raw_content_sha256", "recipient", "sender", "rfc822_id", "readback_raw_sha256",
    "provider_rfc822_id", "rfc822_identity_status", "content_sha256_version",
}


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".mail-receipt-", suffix=".json", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_config(path: Path) -> dict:
    if not path.exists():
        return {"enabled": False, "disabled_reason": "configuration_missing"}
    if not path.is_file() or path.stat().st_size > 64_000:
        raise ValueError("invalid_delivery_configuration")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
        raise ValueError("invalid_delivery_configuration")
    if not value["enabled"]:
        return {"enabled": False, "disabled_reason": "configuration_disabled"}
    if value.get("sender") != CONFIRMED_RECIPIENT or value.get("recipient") != CONFIRMED_RECIPIENT:
        raise ValueError("recipient_not_confirmed")
    for key in ("hermes_home", "outbox_path"):
        if not isinstance(value.get(key), str) or not Path(value[key]).is_absolute():
            raise ValueError("absolute_runtime_paths_required")
    outbox = Path(value["outbox_path"]).resolve()
    if not outbox.is_relative_to(WORK.resolve()) or outbox == WORK.resolve():
        raise ValueError("outbox_must_belong_to_monitor_workspace")
    value["outbox_path"] = outbox
    value["hermes_home"] = Path(value["hermes_home"]).resolve()
    return value


@contextmanager
def owning_home(home: Path):
    """Scope imports and refresh to the configured profile without global changes."""
    previous = os.environ.get("HERMES_HOME")
    old_path = list(sys.path)
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(home / "scripts"))
    try:
        yield
    finally:
        sys.path[:] = old_path
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


def canonical_credentials(home: Path):
    """Call the installed helper; preserve its token refresh lease and cache."""
    source = home / "scripts" / "gmail_sync.py"
    if not source.is_file():
        raise RuntimeError("canonical_google_helper_missing")
    with owning_home(home):
        helper = importlib.import_module("gmail_sync")
        if Path(helper.__file__).resolve() != source.resolve():
            raise RuntimeError("canonical_google_helper_mismatch")
        if Path(helper.TOKEN_FILE).resolve().parent != home.resolve():
            raise RuntimeError("google_helper_profile_mismatch")
        return helper.credentials(require_sheets=False)


def ensure_runtime(home: Path) -> None:
    expected = home / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    if Path(sys.executable).resolve() != expected.resolve():
        raise RuntimeError("owning_hermes_interpreter_required")


def repo_modules():
    if str(REPO_SOURCE) not in sys.path:
        sys.path.insert(0, str(REPO_SOURCE))
    from contract_ipo_monitor.gmail_delivery import GmailOAuthTransport
    from contract_ipo_monitor.notifications import EmailOutbox
    return GmailOAuthTransport, EmailOutbox


def transport_factory(config: dict):
    transport_type, _ = repo_modules()
    return transport_type(credentials_loader=lambda: canonical_credentials(config["hermes_home"]),
                          expected_sender=config["sender"], allowed_recipient=config["recipient"])


def outbox_factory(path: Path):
    _, outbox_type = repo_modules()
    return outbox_type(path)


def public_receipt(value: object) -> dict:
    repo_modules()
    from contract_ipo_monitor.gmail_delivery import RFC822_ID_PATTERN
    if not isinstance(value, dict):
        raise ValueError("verified_provider_receipt_missing")
    result = {key: item for key, item in value.items() if key in RECEIPT_KEYS}
    if any(not isinstance(item, str) or len(item) > 500 for item in result.values()):
        raise ValueError("provider_receipt_exceeds_bound")
    required = {"gmail_message_id", "thread_id", "provider_accepted_at", "verified_at", "delivered_label",
                "content_sha256", "content_sha256_version", "raw_content_sha256", "recipient", "sender", "rfc822_id", "readback_raw_sha256"}
    if not required.issubset(result) or result["recipient"] != CONFIRMED_RECIPIENT or result["sender"] != CONFIRMED_RECIPIENT:
        raise ValueError("verified_provider_receipt_invalid")
    if result["content_sha256_version"] != "mime-tree-v2":
        raise ValueError("verified_provider_receipt_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", result["gmail_message_id"]) or result["delivered_label"] not in {"SENT", "INBOX"}:
        raise ValueError("verified_provider_receipt_invalid")
    for key in ("content_sha256", "raw_content_sha256", "readback_raw_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", result[key]):
            raise ValueError("verified_provider_receipt_invalid")
    for key in ("provider_accepted_at", "verified_at"):
        stamp = datetime.fromisoformat(result[key].replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            raise ValueError("verified_provider_receipt_invalid")
    if not re.fullmatch("<" + RFC822_ID_PATTERN + ">", result["rfc822_id"]):
        raise ValueError("verified_provider_receipt_invalid")
    if "provider_rfc822_id" in result and not re.fullmatch("<" + RFC822_ID_PATTERN + ">", result["provider_rfc822_id"]):
        raise ValueError("verified_provider_receipt_invalid")
    if "rfc822_identity_status" in result and result["rfc822_identity_status"] not in {"preserved", "provider_rewritten"}:
        raise ValueError("verified_provider_receipt_invalid")
    return result


def sent_record(outbox: Any, identifier: int, *, reconciled: bool) -> dict:
    record = outbox.get(identifier)
    if record["status"] != "sent":
        raise ValueError("outbox_sent_readback_missing")
    return {"outbox_id": identifier, "reconciled": reconciled,
            "receipt": public_receipt(json.loads(record["receipt_json"]))}


def run_worker(*, config_path: Path = CONFIG, receipt_path: Path = RECEIPT,
               reconcile_only: bool = False, make_transport: Callable = transport_factory,
               make_outbox: Callable = outbox_factory, now: Callable = lambda: datetime.now(UTC),
               check_runtime: Callable = ensure_runtime) -> tuple[int, dict]:
    transport = None
    outcome: dict = {"status": "error", "outcome": "failure", "states": {},
                     "newly_sent_ids": [], "reconciled_ids": [], "verified_deliveries": []}
    try:
        config = read_config(Path(config_path))
        if not config["enabled"]:
            outcome.update(status="ok", outcome="disabled", disabled_reason=config["disabled_reason"])
        else:
            check_runtime(config["hermes_home"])
            outbox = make_outbox(config["outbox_path"])
            transport = make_transport(config)
            stamp = now()
            if stamp.utcoffset() is None:
                raise ValueError("aware_worker_clock_required")
            recovered = outbox.reconcile_unknown(transport, now=stamp, limit=10)
            for row in recovered:
                if row.get("status") == "sent":
                    outcome["reconciled_ids"].append(row["id"])
                    outcome["verified_deliveries"].append(sent_record(outbox, row["id"], reconciled=True))
            if not reconcile_only:
                for _ in range(5):
                    row = outbox.deliver_once(transport, now=now())
                    if row is None:
                        break
                    if row.get("status") == "sent":
                        outcome["newly_sent_ids"].append(row["id"])
                        outcome["verified_deliveries"].append(sent_record(outbox, row["id"], reconciled=False))
            states = outbox.states()
            if not isinstance(states, dict) or any(not isinstance(key, str) or not isinstance(count, int) or count < 0
                                                   for key, count in states.items()):
                raise ValueError("outbox_states_invalid")
            outcome["states"] = states
            blocked = any(states.get(key, 0) for key in ("pending", "sending", "unknown"))
            rejected = bool(states.get("rejected", 0))
            outcome.update(status="error" if rejected else "degraded" if blocked else "ok",
                           outcome="delivery_rejected" if rejected else "delivery_unresolved" if blocked else
                           "verified_delivery" if outcome["verified_deliveries"] else "no_pending_mail",
                           reconcile_only=reconcile_only)
    except Exception:
        # Provider errors can contain tokens or raw mail. Persist only a fixed code.
        outcome.update(status="error", outcome="failure", error_code="email_worker_failed")
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                outcome.update(status="error", outcome="failure", error_code="email_transport_cleanup_failed")
    stamp = now()
    if stamp.utcoffset() is None:
        raise ValueError("aware_worker_clock_required")
    outcome["completed_at"] = stamp.astimezone(UTC).isoformat()
    atomic_json(Path(receipt_path), outcome)
    return (1 if outcome["status"] == "error" else 0), outcome


def main() -> int:
    parser = argparse.ArgumentParser(description="Deliver the confirmed local research outbox with Gmail readback")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--receipt", type=Path, default=RECEIPT)
    parser.add_argument("--reconcile-only", action="store_true")
    args = parser.parse_args()
    try:
        code, result = run_worker(config_path=args.config, receipt_path=args.receipt,
                                  reconcile_only=args.reconcile_only)
    except Exception:
        print("Email worker could not persist its local receipt.", file=sys.stderr)
        return 1
    if code:
        print("Email delivery requires review; see the local delivery receipt.", file=sys.stderr)
    elif result["newly_sent_ids"] or result["reconciled_ids"]:
        print(f"Verified {len(result['newly_sent_ids'])} new and {len(result['reconciled_ids'])} reconciled research email(s).")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
