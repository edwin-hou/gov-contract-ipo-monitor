"""Report compression preserves original receipts, retention and fail-closed reads."""
import gzip
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from contract_ipo_monitor import report_history
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.report_history import original_report_bytes, report_history_rows
from contract_ipo_monitor.research import ResearchStore


NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
LEGACY_SCHEMA = """CREATE TABLE monitor_runs(
    id INTEGER PRIMARY KEY,completed_at TEXT NOT NULL,status TEXT NOT NULL,report_json TEXT NOT NULL)"""


def report(**extra):
    return {"completed_at": NOW.isoformat(), "status": "degraded", **extra}


def legacy_database(tmp_path, payloads):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    with db.connect() as conn:
        conn.execute(LEGACY_SCHEMA)
        for index, payload in enumerate(payloads):
            conn.execute("INSERT INTO monitor_runs VALUES(?,?,?,?)",
                         (7+index*11, (NOW+timedelta(hours=index)).isoformat(), "degraded", payload))
    return db


def stored_reports(db):
    with db.connect() as conn:
        return [(row["id"], row["completed_at"], row["status"], original_report_bytes(row))
                for row in report_history_rows(conn)]


def test_legacy_migration_preserves_ids_exact_json_bytes_and_invalid_payloads_on_restart(tmp_path):
    first = '{\r\n "unicode": "café 東京", "float": 1.00, "report": [true, null]\r\n}\n '
    invalid = "invalid JSON\x00: keep the original receipt"
    invalid_utf8 = b"invalid UTF-8 JSON: \xff\xfe"
    last = json.dumps(report(nested={"value": ["same", 3]}), indent=2)
    originals = [first, invalid, invalid_utf8, last]
    db = legacy_database(tmp_path, originals)
    before = stored_reports(db)
    store = ResearchStore(db)
    store.initialize()
    assert stored_reports(db) == before
    with db.connect() as conn:
        assert [row[0] for row in conn.execute("SELECT report_json FROM monitor_runs ORDER BY id")] == [""]*4
        compressed = [tuple(row) for row in conn.execute("SELECT * FROM monitor_run_payloads ORDER BY run_id")]
        for row, original in zip(compressed, originals):
            raw = original.encode("utf-8") if isinstance(original, str) else original
            assert row[1] == "gzip" and row[2] == hashlib.sha256(raw).hexdigest()
            assert row[3] == len(raw) and gzip.decompress(row[4]) == raw
    restarted = ResearchStore(Database(db.path))
    restarted.initialize()
    restarted.initialize()
    assert restarted.latest_run() == json.loads(last)
    assert stored_reports(db) == before
    with db.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM monitor_run_payloads ORDER BY run_id")] == compressed


def test_backward_and_mixed_storage_reads_preserve_latest_by_id(tmp_path):
    old = report(marker="legacy")
    db = legacy_database(tmp_path, [json.dumps(old)])
    assert ResearchStore(db).latest_run() == old
    store = ResearchStore(db)
    store.initialize()
    store.save_run(report(marker="compressed"))
    newest_legacy = report(marker="new legacy writer")
    with db.connect() as conn:
        conn.execute("INSERT INTO monitor_runs(completed_at,status,report_json) VALUES(?,?,?)",
                     (NOW.isoformat(), "degraded", json.dumps(newest_legacy)))
    assert store.latest_run() == newest_legacy
    before = stored_reports(db)
    store.initialize()
    assert stored_reports(db) == before
    assert store.latest_run() == newest_legacy


def test_repeated_saves_keep_each_report_and_reduce_large_history_without_changing_payloads(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    store = ResearchStore(db)
    store.initialize()
    data = report(evidence=[{"text": "A retained source observation. "*100, "id": index} for index in range(900)])
    expected = json.dumps(data).encode("utf-8")
    for _ in range(3):
        store.save_run(data)
    receipts = stored_reports(db)
    assert [row[0] for row in receipts] == [1, 2, 3]
    assert [row[3] for row in receipts] == [expected]*3
    assert store.latest_run() == data
    with db.connect() as conn:
        assert conn.execute("SELECT SUM(length(report_json)) FROM monitor_runs").fetchone()[0] == 0
        compressed_bytes = conn.execute("""SELECT COALESCE((SELECT SUM(length(payload_gzip)) FROM monitor_run_payloads),0)
            +COALESCE((SELECT SUM(length(payload_xz)) FROM monitor_report_packs),0)""").fetchone()[0]
        assert compressed_bytes < len(expected)*3*.05


def test_existing_365_day_retention_is_unchanged_and_payloads_cascade(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    store = ResearchStore(db)
    store.initialize()
    expired = report(completed_at=(NOW-timedelta(days=366)).isoformat(), marker="expired")
    boundary = report(completed_at=(NOW-timedelta(days=365)).isoformat(), marker="boundary")
    store.save_run(expired)
    store.save_run(boundary)
    store.save_run(report(marker="current"))
    with db.connect() as conn:
        assert [row[0] for row in conn.execute("SELECT id FROM monitor_runs ORDER BY id")] == [2, 3]
        assert [row[0] for row in conn.execute("SELECT run_id FROM monitor_run_payloads ORDER BY run_id")] == [2, 3]
    assert json.loads(stored_reports(db)[0][3]) == boundary


def test_mid_migration_failure_rolls_back_every_report_and_can_retry(tmp_path, monkeypatch):
    db = legacy_database(tmp_path, [json.dumps(report(marker="first")), "invalid but preserved", json.dumps(report(marker="last"))])
    before = stored_reports(db)
    real_compress = report_history.compress_report
    calls = 0

    def interrupted(raw):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted codec")
        return real_compress(raw)

    monkeypatch.setattr(report_history, "compress_report", interrupted)
    with pytest.raises(RuntimeError, match="interrupted codec"):
        ResearchStore(db).initialize()
    assert stored_reports(db) == before
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='monitor_run_payloads'").fetchone() is None
        assert all(row[0] for row in conn.execute("SELECT report_json FROM monitor_runs"))
    monkeypatch.setattr(report_history, "compress_report", real_compress)
    ResearchStore(db).initialize()
    assert stored_reports(db) == before


def test_failed_new_save_rolls_back_insert_and_retention(tmp_path, monkeypatch):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    store = ResearchStore(db)
    store.initialize()
    store.save_run(report(completed_at=(NOW-timedelta(days=400)).isoformat(), marker="must survive failed save"))
    before = stored_reports(db)
    monkeypatch.setattr(report_history.gzip, "compress", lambda *args, **kwargs: b"not gzip")
    with pytest.raises(ValueError, match="malformed"):
        store.save_run(report())
    assert stored_reports(db) == before
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitor_run_payloads").fetchone()[0] == 1


@pytest.mark.parametrize("change, expected", [
    ({"sha256": "0"*64}, "hash does not match"),
    ({"sha256": "bad"}, "invalid hash"),
    ({"original_bytes": -1}, "original-byte metadata"),
    ({"original_bytes": report_history.MAX_REPORT_BYTES+1}, "original-byte metadata"),
    ({"original_bytes": 1}, "length, completeness"),
    ({"encoding": "unknown"}, "unsupported"),
    ({"payload_gzip": b"malformed"}, "malformed"),
])
def test_bounded_decoder_rejects_corruption_and_invalid_receipts(change, expected):
    raw = b'{"retained": "original report"}'
    values = dict(zip(("encoding", "sha256", "original_bytes", "payload_gzip"), report_history.compress_report(raw)))
    values.update(change)
    with pytest.raises(ValueError, match=expected):
        report_history.decompress_report(**values)


@pytest.mark.parametrize("change", [lambda payload: payload[:-4], lambda payload: payload+b"trailing", lambda payload: payload+gzip.compress(b"second member")])
def test_gzip_must_be_complete_and_have_exactly_one_member(change):
    raw = b'{"retained": "original report"}'
    encoding, digest, size, payload = report_history.compress_report(raw)
    with pytest.raises(ValueError, match="length, completeness or member count"):
        report_history.decompress_report(encoding=encoding, sha256=digest, original_bytes=size, payload_gzip=change(payload))


def test_declared_small_size_limits_decompression_even_for_large_expansion():
    payload = gzip.compress(b"x"*(5*1024*1024))
    with pytest.raises(ValueError, match="length, completeness"):
        report_history.decompress_report(encoding="gzip", sha256=hashlib.sha256(b"x").hexdigest(), original_bytes=1, payload_gzip=payload)


def test_oversized_legacy_receipt_is_preserved_if_migration_cannot_encode_it(tmp_path, monkeypatch):
    raw = json.dumps(report(large="x"*100))
    db = legacy_database(tmp_path, [raw])
    monkeypatch.setattr(report_history, "MAX_REPORT_BYTES", 64)
    with pytest.raises(ValueError, match="original-byte limit"):
        ResearchStore(db).initialize()
    with db.connect() as conn:
        assert conn.execute("SELECT report_json FROM monitor_runs").fetchone()[0] == raw
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='monitor_run_payloads'").fetchone() is None


def test_corrupt_latest_payload_fails_without_falling_back_to_older_report(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    store = ResearchStore(db)
    store.initialize()
    store.save_run(report(marker="older"))
    store.save_run(report(marker="latest"))
    with db.connect() as conn:
        conn.execute("UPDATE monitor_run_payloads SET sha256=? WHERE run_id=2", ("0"*64,))
    with pytest.raises(ValueError, match="hash does not match"):
        store.latest_run()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitor_runs").fetchone()[0] == 2


def test_invalid_legacy_json_is_preserved_but_not_returned_as_valid_report(tmp_path):
    db = legacy_database(tmp_path, ["invalid JSON"])
    store = ResearchStore(db)
    store.initialize()
    with pytest.raises(json.JSONDecodeError):
        store.latest_run()
    assert stored_reports(db)[0][3] == b"invalid JSON"


def test_conflicting_raw_and_compressed_payload_rolls_back_earlier_migration_work(tmp_path):
    db = legacy_database(tmp_path, [json.dumps(report(marker="first")), json.dumps(report(marker="second"))])
    store = ResearchStore(db)
    store.initialize()
    with db.connect() as conn:
        conn.execute("DELETE FROM monitor_run_payloads WHERE run_id=7")
        conn.execute("UPDATE monitor_runs SET report_json=? WHERE id=7", (json.dumps(report(marker="first")),))
        conn.execute("UPDATE monitor_runs SET report_json=? WHERE id=18", (json.dumps(report(marker="conflict")),))
    with pytest.raises(ValueError, match="conflicting raw and compressed"):
        store.initialize()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitor_run_payloads WHERE run_id=7").fetchone()[0] == 0
        assert conn.execute("SELECT report_json FROM monitor_runs WHERE id=7").fetchone()[0] == json.dumps(report(marker="first"))


def test_historical_sec_gap_recovery_reads_compressed_and_mixed_legacy_reports(tmp_path):
    error = "Partial SEC collection: 8-K: feed page limit reached; older filings may be missing"
    raw = json.dumps(report(health={"collectors": {"sec": {"error": error}}}), indent=2)
    db = legacy_database(tmp_path, ["invalid legacy JSON", raw])
    store = ResearchStore(db)
    store.initialize()
    with db.connect() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version=3")
        conn.execute("INSERT INTO monitor_runs(completed_at,status,report_json) VALUES(?,?,?)",
                     (NOW.isoformat(), "degraded", json.dumps(report(health={"collectors": {"sec": {"error": "EFFECT: feed page limit reached; older filings may be missing"}}}))))
    db.initialize()
    with db.connect() as conn:
        rows = conn.execute("SELECT name,cursor,last_error FROM collector_state WHERE name LIKE 'sec_feed_gap:%' ORDER BY name").fetchall()
        assert [row["name"] for row in rows] == ["sec_feed_gap:8-K", "sec_feed_gap:EFFECT"]
        assert json.loads(rows[0]["cursor"])["legacy_run_id"] == 18
        assert rows[0]["last_error"] == error
    assert stored_reports(db)[1][3] == raw.encode("utf-8")


def test_corrupt_compressed_gap_history_rolls_back_coverage_migration(tmp_path):
    error = "8-K: feed page limit reached; older filings may be missing"
    db = legacy_database(tmp_path, [json.dumps(report(health={"collectors": {"sec": {"error": error}}})), json.dumps(report())])
    ResearchStore(db).initialize()
    with db.connect() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version=3")
        conn.execute("UPDATE monitor_run_payloads SET sha256=? WHERE run_id=18", ("0"*64,))
    with pytest.raises(ValueError, match="hash does not match"):
        db.initialize()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM collector_state WHERE name LIKE 'sec_feed_gap:%'").fetchone()[0] == 0
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone() is None
