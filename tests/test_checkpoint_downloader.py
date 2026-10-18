"""The standalone downloader and portable restore accept the same manifest bound."""
import io
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
