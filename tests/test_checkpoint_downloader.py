"""The standalone downloader and portable restore accept the same manifest bound."""
import io
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import zipfile

import httpx
import pytest

from contract_ipo_monitor import checkpoint_archive as portable
from contract_ipo_monitor.db import Database


HELPER = Path(__file__).resolve().parents[1] / "deploy/hermes/github_ops.py"


def run_download(tmp_path, monkeypatch, raw, destination):
    monkeypatch.setattr(sys, "argv", [str(HELPER), "download", "123", str(destination)])
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="password=offline-fixture\n"))
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: httpx.Response(
        200, content=raw, request=httpx.Request("GET", url)))
    return runpy.run_path(str(HELPER), run_name="__main__")


def report_zip(*, body=b"fresh report body", malformed=False):
    output = io.BytesIO()
    report = b"invalid-json" if malformed else json.dumps({"completed_at": "2026-10-18T15:00:00Z",
        "status": "degraded", "counts": {}, "ipo_summary": {}}).encode()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("latest.json", report)
        bundle.writestr("latest.md", body)
    return output.getvalue()


@pytest.mark.parametrize("rejection", ["oversized_second_member", "corrupt_second_member", "malformed_report"])
def test_rejected_download_preserves_every_file_in_previous_verified_generation(tmp_path, monkeypatch, rejection):
    destination = tmp_path / "download"
    destination.mkdir()
    before = {"latest.json": b"previous exact verified bytes", "latest.md": b"previous report",
              "update-notice.md": b"retain unrelated local audit"}
    for name, value in before.items():
        (destination / name).write_bytes(value)
    if rejection == "oversized_second_member":
        raw = report_zip(body=b"x" * 30_000_001)
        error = ValueError
    elif rejection == "corrupt_second_member":
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
            bundle.writestr("latest.json", b'{"completed_at":"fresh","status":"ok","counts":{},"ipo_summary":{}}')
            bundle.writestr("latest.md", b"unique uncompressed body")
        raw = output.getvalue().replace(b"unique uncompressed body", b"broken uncompressed body")
        error = zipfile.BadZipFile
    else:
        raw, error = report_zip(malformed=True), ValueError
    with pytest.raises(error):
        run_download(tmp_path, monkeypatch, raw, destination)
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before


@pytest.mark.parametrize("existing", [False, True])
def test_download_install_failure_rolls_back_all_members_and_preserves_audit_files(tmp_path, monkeypatch, existing):
    destination = tmp_path / "download"
    destination.mkdir()
    before = {"update-notice.md": b"unrelated audit"}
    if existing:
        before.update({"latest.json": b"original sealed JSON", "latest.md": b"original markdown"})
    for name, value in before.items():
        (destination / name).write_bytes(value)
    original_replace = Path.replace
    failed = False

    def fail_second_install(path, target):
        nonlocal failed
        if not failed and path.name == "latest.json" and Path(target).parent == destination:
            failed = True
            raise OSError("simulated interrupted artifact installation")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_install)
    with pytest.raises(OSError, match="interrupted artifact"):
        run_download(tmp_path, monkeypatch, report_zip(), destination)
    assert failed
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["download"]


def test_downloader_preserves_exact_valid_report_bytes_and_unrelated_audit_files(tmp_path, monkeypatch):
    destination = tmp_path / "download"
    destination.mkdir()
    (destination / "update-notice.md").write_bytes(b"retain local receipt")
    raw = report_zip()
    run_download(tmp_path, monkeypatch, raw, destination)
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        for name in bundle.namelist():
            assert (destination / name).read_bytes() == bundle.read(name)
    assert (destination / "update-notice.md").read_bytes() == b"retain local receipt"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["download"]


def test_failed_rollback_keeps_original_bytes_for_recovery_and_old_json_readable(tmp_path, monkeypatch):
    destination = tmp_path / "download"
    destination.mkdir()
    (destination / "latest.json").write_bytes(b"original sealed JSON")
    (destination / "latest.md").write_bytes(b"original markdown")
    original_replace = Path.replace

    def fail_install_and_rollback(path, target):
        if (path.name == "latest.json" or path.name == "previous-latest.md") and Path(target).parent == destination:
            raise OSError("simulated inaccessible destination")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_install_and_rollback)
    with pytest.raises(RuntimeError, match="previous files are retained"):
        run_download(tmp_path, monkeypatch, report_zip(), destination)
    assert (destination / "latest.json").read_bytes() == b"original sealed JSON"
    recovery = list(tmp_path.glob(".artifact-*/previous-latest.md"))
    assert len(recovery) == 1 and recovery[0].read_bytes() == b"original markdown"


def test_checkpoint_manifest_is_installed_after_complete_payload(tmp_path, monkeypatch):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("checkpoint.json", b"manifest bytes for downstream validation")
        bundle.writestr("monitor.db.gz", b"payload bytes for downstream validation")
    destination = tmp_path / "download"
    original_replace = Path.replace
    installed = []

    def record_install(path, target):
        if Path(target).parent == destination:
            installed.append(Path(target).name)
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", record_install)
    run_download(tmp_path, monkeypatch, output.getvalue(), destination)
    assert installed == ["monitor.db.gz", "checkpoint.json"]


def test_run_discovery_is_scoped_to_collector_and_current_main_commit(monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(HELPER), "runs", "a" * 40])
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="password=offline-fixture\n"))
    urls = []

    def get(url, **kwargs):
        urls.append(url)
        return httpx.Response(200, json={"workflow_runs": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    runpy.run_path(str(HELPER), run_name="__main__")
    assert urls == ["https://api.github.com/repos/edwin-hou/gov-contract-ipo-monitor/actions/workflows/monitor.yml/runs"
                    "?per_page=100&branch=main&head_sha=" + "a" * 40]


@pytest.mark.parametrize("overflow", [False, True], ids=["exact-bound", "over-bound"])
def test_downloaded_manifest_bound_matches_real_checkpoint_restore(tmp_path, monkeypatch, overflow):
    database = Database(tmp_path / "source.db")
    database.initialize()
    database.update_collector_state("sec", cursor="preserved progress")
    snapshot = tmp_path / "export/monitor.db"
    portable.export_database(database, snapshot)
    manifest = (snapshot.parent / portable.MANIFEST_NAME).read_bytes()
    manifest += b" " * (portable.MAX_MANIFEST_BYTES + int(overflow) - len(manifest))
    payload = (snapshot.parent / portable.PAYLOAD_NAME).read_bytes()
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        bundle.writestr(portable.MANIFEST_NAME, manifest)
        bundle.writestr(portable.PAYLOAD_NAME, payload)
    raw = data.getvalue()
    destination = tmp_path / "download"
    monkeypatch.setattr(sys, "argv", [str(HELPER), "download", "123", str(destination)])

    def credential_double(args, **kwargs):
        assert args[-2:] == ["credential", "fill"]
        return SimpleNamespace(stdout="password=offline-fixture\n")

    def download_double(url, **kwargs):
        assert url.endswith("/actions/artifacts/123/zip")
        return httpx.Response(200, content=raw, request=httpx.Request("GET", url))

    monkeypatch.setattr(subprocess, "run", credential_double)
    monkeypatch.setattr(httpx, "get", download_double)
    if overflow:
        with pytest.raises(ValueError, match="Unexpected artifact file"):
            runpy.run_path(str(HELPER), run_name="__main__")
        assert not (destination / portable.MANIFEST_NAME).exists()
        assert not (destination / portable.PAYLOAD_NAME).exists()
    else:
        runpy.run_path(str(HELPER), run_name="__main__")
        assert (destination / portable.MANIFEST_NAME).read_bytes() == manifest
        assert portable.validate_export(destination)["database"]["bytes"] == snapshot.stat().st_size
        archive = tmp_path / "downloaded.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for name in (portable.MANIFEST_NAME, portable.PAYLOAD_NAME):
                bundle.write(destination / name, name)
        restored = tmp_path / "restored.db"
        assert portable.restore_archive(archive, restored)
        assert restored.read_bytes() == snapshot.read_bytes()
