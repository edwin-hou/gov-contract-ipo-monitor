import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import ipo_trade_monitor as bridge


def prepared(tmp_path, monkeypatch, *, email_status="ok", email_outcome="disabled", email_sent=None, exit_code=0):
    python = tmp_path / "python.exe"
    poller = tmp_path / "poller.py"
    python.write_bytes(b"fake-runtime")
    poller.write_bytes(b"fake-poller")
    poll_receipt = tmp_path / "poll.json"
    monkeypatch.setattr(bridge, "PYTHON", python)
    monkeypatch.setattr(bridge, "POLLER", poller)
    monkeypatch.setattr(bridge, "WORK", tmp_path)
    monkeypatch.setattr(bridge, "POLL_RECEIPT", poll_receipt)
    monkeypatch.setattr(bridge, "home", lambda: tmp_path / "hermes")
    class Child:
        returncode = exit_code
        def __init__(self, argv, **kwargs):
            assert argv == [str(python), "-I", "-X", "utf8", str(poller)] and kwargs["shell"] is False
        def communicate(self, timeout):
            receipt = {"status": "ok", "outcome": "no_newer_report",
                       "completed_at": datetime.now(timezone.utc).isoformat(),
                       "email_status": email_status, "email_outcome": email_outcome,
                       "email_sent": email_sent if email_sent is not None else [],
                       "mail_receipt_path": str(tmp_path / "mail.json")}
            poll_receipt.write_text(json.dumps(receipt), encoding="utf-8")
            return b"verified local notice\n", b""
    monkeypatch.setattr(bridge.subprocess, "Popen", Child)
    return tmp_path / "hermes" / "state" / "ipo_trade_monitor_receipt.json"


def test_disabled_email_is_successful_and_its_outcome_is_mirrored(tmp_path, monkeypatch, capsys):
    path = prepared(tmp_path, monkeypatch)
    assert bridge.run() == 0
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["status"] == "ok" and value["email_status"] == "ok" and value["email_outcome"] == "disabled"
    assert value["email_sent"] == [] and value["completed_at"]
    assert value["mail_receipt_path"] == str(tmp_path / "mail.json")
    assert "verified local notice" in capsys.readouterr().out


def test_verified_delivery_ids_and_unresolved_email_state_are_preserved(tmp_path, monkeypatch, capsys):
    path = prepared(tmp_path, monkeypatch, email_status="degraded", email_outcome="delivery_unresolved", email_sent=[7, 9])
    assert bridge.run() == 0
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["status"] == "degraded" and value["email_sent"] == [7, 9]
    assert value["email_outcome"] == "delivery_unresolved" and value["completed_at"]


def test_delivery_error_never_renews_native_freshness_or_forwards_success_notice(tmp_path, monkeypatch, capsys):
    path = prepared(tmp_path, monkeypatch, email_status="error", email_outcome="failure")
    assert bridge.run() == 1
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["status"] == "error" and value["email_outcome"] == "failure"
    assert "completed_at" not in value and value["observed_at"]
    assert "verified local notice" not in capsys.readouterr().out


@pytest.mark.parametrize("sent", [[True], [0], list(range(1, 17)), "7"])
def test_invalid_delivery_claim_is_rejected_before_success_publication(tmp_path, monkeypatch, capsys, sent):
    path = prepared(tmp_path, monkeypatch, email_sent=sent)
    assert bridge.run() == 1
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["status"] == "error" and "completed_at" not in value
    assert "verified local notice" not in capsys.readouterr().out


def test_child_environment_removes_interpreter_markers_without_changing_parent_or_profile():
    parent = {"PYTHONPATH": "foreign-site-packages", "PYTHONHOME": "foreign-python", "VIRTUAL_ENV": "foreign-venv",
              "VIRTUAL_ENV_PROMPT": "foreign", "CONDA_PREFIX": "foreign-conda", "__PYVENV_LAUNCHER__": "foreign-launcher",
              "HERMES_HOME": "owning-profile", "PATH": "existing-path", "GIT_TERMINAL_PROMPT": "1"}
    before = dict(parent)
    child = bridge.child_environment(parent)
    assert parent == before
    assert all(key not in child for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT",
                                            "CONDA_PREFIX", "__PYVENV_LAUNCHER__"))
    assert child["HERMES_HOME"] == "owning-profile" and child["PATH"] == "existing-path"
    assert child["GIT_TERMINAL_PROMPT"] == "0"


def test_real_monitor_python_uses_own_native_dependencies_despite_injected_hermes_site_packages(tmp_path):
    foreign = Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent" / "venv" / "Lib" / "site-packages"
    if not bridge.PYTHON.is_file() or not foreign.is_dir():
        pytest.skip("Native Windows deployment probe requires the installed monitor and Hermes runtimes")
    contaminated = dict(os.environ, PYTHONPATH=str(foreign), VIRTUAL_ENV=str(foreign.parent.parent))
    contaminated.pop("PYTHONHOME", None)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # Control: inheriting the native Hermes overlay reproduces the ABI failure.
    failed = subprocess.run([str(bridge.PYTHON), "-c", "import pydantic"], shell=False, env=contaminated,
                            capture_output=True, timeout=30, creationflags=flags)
    assert failed.returncode != 0 and b"pydantic_core._pydantic_core" in failed.stderr
    probe = tmp_path / "import_probe.py"
    probe.write_text(
        "import json,runpy,sys,pydantic,pydantic_core\n"
        f"runpy.run_path({str(bridge.POLLER)!r},run_name='import_only')\n"
        "from contract_ipo_monitor.notifications import EmailOutbox,report_message\n"
        "from contract_ipo_monitor.research import report_markdown\n"
        "class Proof(pydantic.BaseModel):\n value:int\n"
        "assert Proof(value='2').value==2\n"
        "print(json.dumps({'version':list(sys.version_info[:2]),'isolated':sys.flags.isolated,"
        "'prefix':sys.prefix,'native_module':pydantic_core.__file__,'report_imported':True}))\n",
        encoding="utf-8")
    child = bridge.child_environment(contaminated)
    result = subprocess.run([str(bridge.PYTHON), "-I", "-X", "utf8", str(probe)], shell=False, env=child,
                            capture_output=True, timeout=30, creationflags=flags)
    assert result.returncode == 0, "Isolated interpreter failed the bounded import/validation probe"
    metadata = json.loads(result.stdout)
    assert metadata["version"] == [3, 12] and metadata["isolated"] == 1 and metadata["report_imported"]
    assert Path(metadata["native_module"]).is_relative_to(bridge.PYTHON.parent.parent)
    assert Path(metadata["prefix"]) == bridge.PYTHON.parent.parent
    assert contaminated["PYTHONPATH"] == str(foreign)
