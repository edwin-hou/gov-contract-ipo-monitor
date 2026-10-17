"""Run the real publication gate, preserving failed collection authority."""
import importlib.util
import json
import runpy
from pathlib import Path

import pytest

from contract_ipo_monitor.db import Database
from contract_ipo_monitor.research import ResearchStore, checkpoint_database
from contract_ipo_monitor.checkpoint_history import history_receipts, verify_history


ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_complete_real_export_is_required_to_authorize_state_upload(tmp_path):
    source = Database(tmp_path / "source.db")
    source.initialize()
    source.update_collector_state("sec", cursor="retained source progress")
    directory = tmp_path / "export"
    checkpoint_database(source, directory / "monitor.db")
    manifest = script("validate_checkpoint_export").verify(directory, "success")
    assert manifest["version"] == 1 and manifest["database"]["bytes"] > 100
    (directory / "monitor.db.gz").write_bytes(b"damaged exported state")
    with pytest.raises(ValueError):
        script("validate_checkpoint_export").verify(directory, "success")


def test_missing_export_cannot_hide_a_successful_collection_without_durable_state(tmp_path):
    gate = script("validate_checkpoint_export")
    with pytest.raises(ValueError, match="Successful collection"):
        gate.verify(tmp_path, "success")
    assert gate.verify(tmp_path, "failure") is None
    assert gate.verify(tmp_path, "skipped") is None


def test_capacity_profile_is_readonly_and_exposes_report_allocation(tmp_path):
    source = Database(tmp_path / "source.db")
    source.initialize()
    with source.connect() as conn:
        conn.execute("CREATE TABLE monitor_runs(id INTEGER PRIMARY KEY,report_json TEXT)")
        conn.execute("INSERT INTO monitor_runs(report_json) VALUES(?)", ('{"retained":"history"}',))
    path = Path(source.path)
    before = path.read_bytes()
    result = script("checkpoint_capacity").capacity(path)
    assert result["retained_reports"] == 1 and result["legacy_report_text_bytes"] == len('{"retained":"history"}')
    assert result["database_bytes"] > 0 and path.read_bytes() == before
    assert script("checkpoint_capacity").capacity(tmp_path / "missing.db") == {"database_exists": False}


def test_original_history_is_verified_before_publication_permission(tmp_path, monkeypatch):
    source = Database(tmp_path / "source.db")
    source.initialize()
    store = ResearchStore(source)
    store.initialize()
    store.save_run({"completed_at": "2026-10-17T01:00:00+00:00", "status": "degraded", "evidence": "retained"})
    before = history_receipts(Path(source.path))
    directory = tmp_path / "work" / "checkpoint"
    checkpoint_database(source, directory / "monitor.db")
    assert verify_history(before, directory / "monitor.db")["preceding_reports_verified"] == 1
    baseline = tmp_path / "work" / "checkpoint-baseline.json"
    altered = [dict(before[0], sha256="0" * 64)]
    baseline.write_text(json.dumps(altered), encoding="utf-8")
    output = tmp_path / "github-output"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("COLLECTION_OUTCOME", "success")
    with pytest.raises(ValueError, match="original retained report"):
        runpy.run_path(str(ROOT / "scripts" / "validate_checkpoint_export.py"), run_name="__main__")
    assert not output.exists()
    baseline.write_text(json.dumps(before), encoding="utf-8")
    runpy.run_path(str(ROOT / "scripts" / "validate_checkpoint_export.py"), run_name="__main__")
    assert output.read_text() == "valid=true\n"


def test_history_verification_respects_existing_365_day_expiry(tmp_path):
    source = Database(tmp_path / "source.db")
    source.initialize()
    store = ResearchStore(source)
    store.initialize()
    store.save_run({"completed_at": "2025-10-15T01:00:00+00:00", "status": "ok"})
    before = history_receipts(Path(source.path))
    store.save_run({"completed_at": "2026-10-17T01:00:00+00:00", "status": "ok"})
    result = verify_history(before, Path(source.path))
    assert result == {"preceding_reports_verified": 0, "expired_under_existing_365_day_policy": 1, "reports_in_export": 1}
