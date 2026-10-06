"""Hermes script-only bridge; bounded shortlist reviews use a separate worker."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

def deployed_work():
    manifest = Path(__file__).with_name("ipo_monitor_deployment.json")
    if not manifest.exists():
        return Path(__file__).resolve().parent
    if not manifest.is_file() or manifest.stat().st_size > 4096:
        raise ValueError("Invalid monitor deployment manifest")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"work"} or not isinstance(value["work"], str) or not Path(value["work"]).is_absolute():
        raise ValueError("Absolute monitor work path required")
    return Path(value["work"]).resolve()

WORK = deployed_work()
PYTHON = WORK / "monitor-venv" / "Scripts" / "python.exe"
POLLER = WORK / "hermes_market_monitor.py"
POLL_RECEIPT = WORK / "hermes-trade-check.json"
TIMEOUT_SECONDS = 18 * 60


def child_environment(parent: dict[str, str] | None = None) -> dict[str, str]:
    # Native Hermes Windows scripts may run the base 3.11 interpreter with its
    # venv injected through PYTHONPATH. The external 3.12 child owns its packages.
    environment = dict(os.environ if parent is None else parent)
    blocked = {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "CONDA_PREFIX",
               "CONDA_DEFAULT_ENV", "CONDA_SHLVL", "__PYVENV_LAUNCHER__"}
    environment = {key: value for key, value in environment.items() if key.upper() not in blocked}
    environment.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", PYTHONIOENCODING="utf-8")
    return environment


def poller_argv() -> list[str]:
    # -I also blocks user-site, implicit cwd imports and Python env injection;
    # the poller explicitly adds its reviewed work/source paths. -X preserves
    # UTF-8 output because isolated mode ignores PYTHONIOENCODING.
    return [str(PYTHON), "-I", "-X", "utf8", str(POLLER)]


def home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    # Deployed asset location is authoritative even when the terminal sanitizer
    # omits the profile variable; this script always lives under home/scripts.
    location = Path(__file__).resolve()
    if location.parent.name == "scripts" and (location.parent.parent / "config.yaml").is_file():
        return location.parent.parent
    raise RuntimeError("The bridge must run from the owning Hermes scripts directory")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".ipo-receipt-", suffix=".json", dir=path.parent)
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


def aware(value: object) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Receipt must have an aware timestamp")
    return stamp.astimezone(timezone.utc)


def fresh_receipt(path: Path, started_at: datetime, ended_at: datetime) -> dict:
    if not path.is_file() or path.stat().st_size > 256_000:
        raise ValueError("Missing or oversized poll receipt")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("status") not in {"ok", "degraded", "error"}:
        raise ValueError("Invalid poll receipt status")
    completed = aware(value.get("completed_at"))
    if not started_at - timedelta(seconds=1) <= completed <= ended_at + timedelta(seconds=30):
        raise ValueError("Poll receipt is stale or future dated")
    if path.stat().st_mtime < started_at.timestamp() - 1:
        raise ValueError("Poll receipt was not written by this invocation")
    if value.get("outcome") not in {"no_change", "no_newer_report", "pending_report", "published_change", "recovered_local_delivery", "failure"}:
        raise ValueError("Invalid poll outcome")
    if "email_status" in value and value["email_status"] not in {"ok", "degraded", "error"}:
        raise ValueError("Invalid email status")
    if "email_sent" in value and (not isinstance(value["email_sent"], list) or len(value["email_sent"]) > 15
                                  or any(type(item) is not int or item < 1 for item in value["email_sent"])):
        raise ValueError("Invalid email delivery identifiers")
    if "email_outcome" in value and (not isinstance(value["email_outcome"], str) or len(value["email_outcome"]) > 100):
        raise ValueError("Invalid email outcome")
    if "mail_receipt_path" in value and (not isinstance(value["mail_receipt_path"], str)
                                         or not Path(value["mail_receipt_path"]).is_absolute()):
        raise ValueError("Invalid email receipt path")
    return value


def stop_owned_child(proc: subprocess.Popen) -> None:
    # Snapshot only this helper's descendants. The existing Hermes gateway and
    # browser are never children of the bridge and are never signalled.
    import psutil
    descendants = []
    try:
        descendants = psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        pass
    for child in reversed(descendants):
        try:
            child.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=10)


def run() -> int:
    receipt_path = home() / "state" / "ipo_trade_monitor_receipt.json"
    started_at = datetime.now(timezone.utc)
    result = {"schema": "hermes-ipo-trade-bridge-v1", "status": "error", "outcome": "failure",
              "poll_receipt_path": str(POLL_RECEIPT), "started_at": started_at.isoformat()}
    try:
        if not PYTHON.is_file() or not POLLER.is_file():
            raise ValueError("Missing monitor runtime or poller")
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        environment = child_environment()
        proc = subprocess.Popen(poller_argv(), shell=False, cwd=str(WORK),
                                env=environment, creationflags=flags, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            stop_owned_child(proc)
            raise ValueError("Monitor poll exceeded its bounded runtime")
        ended_at = datetime.now(timezone.utc)
        value = fresh_receipt(POLL_RECEIPT, started_at, ended_at)
        result.update({key: value[key] for key in (
            "status", "outcome", "newest_run", "source_report", "main_sha", "verified_run",
            "head_sha", "artifact_id", "report_sha256", "report_completed_at", "source_gaps",
            "digest_path", "digest_sha256", "failure_code", "message",
            "email_status", "email_sent", "mail_receipt_path", "email_outcome", "ai_analysis",
        ) if key in value})
        result["poller_exit_code"] = proc.returncode
        if value.get("email_status") == "degraded" and result["status"] == "ok":
            result["status"] = "degraded"
        if proc.returncode != 0 or value["status"] == "error" or value.get("email_status") == "error":
            result["status"] = "error"
            result["observed_at"] = ended_at.isoformat()
            atomic_json(receipt_path, result)
            sys.stderr.write("The monitor poll failed; its sanitized receipt is saved locally.\n")
            return 1
        # Freshness is renewed only after a completed real poll and fresh receipt.
        result["completed_at"] = value["completed_at"]
        atomic_json(receipt_path, result)
        # Only a verified fresh successful poll can publish its trusted notice.
        # Empty stdout remains a silent native no_agent success.
        sys.stdout.buffer.write(stdout)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(stderr)
        sys.stderr.buffer.flush()
        return 0
    except Exception as exc:
        result.update(status="error", outcome="failure", failure_code=type(exc).__name__,
                      observed_at=datetime.now(timezone.utc).isoformat())
        # Arbitrary exceptions can contain signed URLs or credentials; the
        # poller's sanitized outcome remains at its receipt path for inspection.
        result["message"] = "The monitor bridge could not verify a fresh successful poll."
        atomic_json(receipt_path, result)
        sys.stderr.write(result["message"] + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
