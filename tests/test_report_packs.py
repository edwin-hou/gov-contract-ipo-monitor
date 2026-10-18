"""Hybrid report packs retain exact receipts through migration, expiry and failures."""
from __future__ import annotations

import hashlib
import json
import lzma
from datetime import UTC, datetime, timedelta

import pytest

from contract_ipo_monitor import report_history, report_packs as packs
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.research import ResearchStore


NOW = datetime(2026, 10, 18, 22, tzinfo=UTC)


def store_at(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    store = ResearchStore(db)
    store.initialize()
    return db, store


def data(marker="retained", *, at=NOW, size=70_000):
    return {"completed_at": at.isoformat(), "status": "degraded", "marker": marker, "source": "x" * size}


def history(db):
    with db.connect() as conn:
        return [(row["id"], row["completed_at"], row["status"], report_history.original_report_bytes(row))
                for row in report_history.report_history_rows(conn)]


def snapshot(db):
    with db.connect() as conn:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("monitor_runs", "monitor_run_payloads", "monitor_report_packs", "monitor_run_pack_refs")}


def old_gzip(db, run_id, raw, *, at=NOW):
    with db.transaction() as conn:
        conn.execute("INSERT INTO monitor_runs VALUES(?,?,?,'')", (run_id, at.isoformat(), "degraded"))
        conn.execute("INSERT INTO monitor_run_payloads VALUES(?,?,?,?,?)", (run_id, *report_history.compress_report(raw)))


def test_large_gzip_migration_preserves_exact_arbitrary_bytes_ids_metadata_and_restart(tmp_path):
    db, store = store_at(tmp_path)
    originals = [b"invalid JSON\xff\x00" * 6_000, json.dumps(data(), indent=2).encode() + b"\r\n  ", b"[" * 70_000]
    for identity, raw in zip((7, 18, 42), originals):
        old_gzip(db, identity, raw)
    before = history(db)
    store.initialize()
    assert history(db) == before
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitor_run_payloads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM monitor_report_packs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM monitor_run_pack_refs").fetchone()[0] == 3
    migrated = snapshot(db)
    store.initialize()
    store.initialize()
    assert snapshot(db) == migrated and history(db) == before


def test_hybrid_legacy_gzip_and_packs_are_explicit_and_newest_by_original_id(tmp_path):
    db, store = store_at(tmp_path)
    old_gzip(db, 7, json.dumps(data("old gzip")).encode())
    store.save_run(data("new pack"))
    with db.connect() as conn:
        conn.execute("INSERT INTO monitor_runs(completed_at,status,report_json) VALUES(?,?,?)",
                     (NOW.isoformat(), "degraded", json.dumps(data("newest legacy", size=1))))
    assert store.latest_run()["marker"] == "newest legacy"
    before = history(db)
    store.initialize()
    assert history(db) == before and store.latest_run()["marker"] == "newest legacy"


@pytest.mark.parametrize("size,packed", [(63, False), (64, True), (256, True), (257, False)])
def test_thresholds_keep_small_and_oversized_reports_as_independent_bounded_gzip(tmp_path, monkeypatch, size, packed):
    monkeypatch.setattr(packs, "MIN_PACK_REPORT_BYTES", 64)
    monkeypatch.setattr(packs, "MAX_PACK_BYTES", 256)
    db, _ = store_at(tmp_path)
    raw = bytes(range(32)) * (size // 32) + b"x" * (size % 32)
    with db.transaction() as conn:
        identity = conn.execute("INSERT INTO monitor_runs VALUES(1,?,'degraded','')", (NOW.isoformat(),)).lastrowid
        report_history.insert_report_payload(conn, identity, raw)
    assert history(db)[0][3] == raw
    with db.connect() as conn:
        assert bool(conn.execute("SELECT COUNT(*) FROM monitor_run_pack_refs").fetchone()[0]) == packed
        assert bool(conn.execute("SELECT COUNT(*) FROM monitor_run_payloads").fetchone()[0]) != packed


def test_pack_rollover_and_append_retarget_previous_members_without_changing_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "MIN_PACK_REPORT_BYTES", 64)
    monkeypatch.setattr(packs, "MAX_PACK_BYTES", 1024)
    db, store = store_at(tmp_path)
    for index in range(8):
        store.save_run(data(str(index), size=150))
    actual = history(db)
    assert [json.loads(item[3])["marker"] for item in actual] == [str(index) for index in range(8)]
    with db.connect() as conn:
        rows = conn.execute("SELECT id,original_bytes FROM monitor_report_packs ORDER BY id").fetchall()
        assert len(rows) == 2 and all(row[1] <= 1024 for row in rows)
        assert not conn.execute("SELECT id FROM monitor_report_packs p WHERE NOT EXISTS(SELECT 1 FROM monitor_run_pack_refs r WHERE r.pack_id=p.id)").fetchall()


def test_hot_pack_corruption_rolls_back_new_insert_and_retention(tmp_path, monkeypatch):
    db, store = store_at(tmp_path)
    store.save_run(data("old", at=NOW-timedelta(days=400)))
    before = snapshot(db)
    monkeypatch.setattr(packs.lzma, "compress", lambda *args, **kwargs: b"bad XZ")
    with pytest.raises(ValueError, match="malformed|length, completeness"):
        store.save_run(data("new"))
    assert snapshot(db) == before
    assert json.loads(history(db)[0][3])["marker"] == "old"


def test_interrupted_multi_pack_migration_rolls_back_original_gzip_and_all_new_refs(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "MAX_PACK_BYTES", 100_000)
    db, store = store_at(tmp_path)
    for index in range(3):
        old_gzip(db, index+1, json.dumps(data(str(index))).encode())
    before = snapshot(db)
    real = packs.compress_pack
    calls = 0
    def fail_second(raw):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted pack compression")
        return real(raw)
    monkeypatch.setattr(packs, "compress_pack", fail_second)
    with pytest.raises(RuntimeError, match="interrupted pack"):
        store.initialize()
    assert snapshot(db) == before
    monkeypatch.setattr(packs, "compress_pack", real)
    store.initialize()
    assert len(history(db)) == 3


@pytest.mark.parametrize("mutate", [lambda value: value[:-1], lambda value: value+b"trailing",
                                   lambda value: value+lzma.compress(b"second XZ member")])
def test_xz_requires_one_complete_member(mutate):
    raw = b"exact original bytes" * 4_000
    encoding, digest, size, payload = packs.compress_pack(raw)
    with pytest.raises(ValueError, match="length, completeness or member count|malformed"):
        packs.decode_pack(encoding=encoding, sha256=digest, original_bytes=size, payload_xz=mutate(payload))


@pytest.mark.parametrize("change,expected", [({"encoding": "gzip"}, "unsupported"),
    ({"sha256": "bad"}, "invalid hash"), ({"sha256": "0"*64}, "hash does not match"),
    ({"original_bytes": 1}, "length, completeness"), ({"original_bytes": True}, "invalid hash"),
    ({"payload_xz": b"corrupt"}, "malformed|length, completeness"), ({"payload_xz": "text"}, "invalid or oversized")])
def test_xz_metadata_and_expansion_are_bounded(change, expected):
    values = dict(zip(("encoding", "sha256", "original_bytes", "payload_xz"), packs.compress_pack(b"x" * 80_000)))
    values.update(change)
    with pytest.raises(ValueError, match=expected):
        packs.decode_pack(**values)


def test_xz_dictionary_memory_is_bounded_independently_of_expanded_bytes(monkeypatch):
    values = dict(zip(("encoding", "sha256", "original_bytes", "payload_xz"), packs.compress_pack(b"x" * 80_000)))
    monkeypatch.setattr(packs, "MAX_DECODER_MEMORY_BYTES", 1024 * 1024)
    with pytest.raises(ValueError, match="decoder memory bound"):
        packs.decode_pack(**values)


@pytest.mark.parametrize("column,value", [("byte_offset", 999_999), ("original_bytes", 0), ("sha256", "0"*64)])
def test_bad_member_receipt_fails_without_falling_back_to_older_report(tmp_path, column, value):
    db, store = store_at(tmp_path)
    store.save_run(data("older"))
    store.save_run(data("latest"))
    with db.connect() as conn:
        conn.execute(f"UPDATE monitor_run_pack_refs SET {column}=? WHERE run_id=2", (value,))
    with pytest.raises(ValueError, match="member"):
        store.latest_run()
    assert len(snapshot(db)["monitor_runs"]) == 2


def test_missing_pack_is_not_treated_as_legacy_empty_json(tmp_path):
    db, store = store_at(tmp_path)
    store.save_run(data())
    with db.connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM monitor_report_packs")
    with pytest.raises(ValueError, match="missing pack"):
        store.latest_run()


def test_conflicting_legacy_or_gzip_authority_never_replaces_pack_receipts(tmp_path):
    db, store = store_at(tmp_path)
    store.save_run(data("packed"))
    with db.connect() as conn:
        conn.execute("UPDATE monitor_runs SET report_json=?", (json.dumps(data("conflicting")),))
    before = snapshot(db)
    with pytest.raises(ValueError, match="conflicting raw"):
        store.initialize()
    assert snapshot(db) == before
    with db.connect() as conn:
        conn.execute("UPDATE monitor_runs SET report_json=''")
        conn.execute("INSERT INTO monitor_run_payloads VALUES(?,?,?,?,?)", (1, *report_history.compress_report(json.dumps(data("conflicting")).encode())))
    with pytest.raises(ValueError, match="conflicting gzip"):
        store.latest_run()
    before = snapshot(db)
    with pytest.raises(ValueError, match="conflicting gzip"):
        store.initialize()
    assert snapshot(db) == before


def test_matching_dual_gzip_and_pack_authority_is_verified_before_deduplicating(tmp_path):
    db, store = store_at(tmp_path)
    value = data("same exact bytes")
    store.save_run(value)
    before = history(db)
    with db.connect() as conn:
        conn.execute("INSERT INTO monitor_run_payloads VALUES(?,?,?,?,?)", (1, *report_history.compress_report(json.dumps(value).encode())))
    assert history(db) == before
    store.initialize()
    assert history(db) == before
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitor_run_payloads").fetchone()[0] == 0


def test_history_iterator_reads_each_pack_once_and_does_not_expose_other_report_bytes(tmp_path, monkeypatch):
    db, store = store_at(tmp_path)
    for index in range(3):
        store.save_run(data(str(index)))
    actual_load = report_history.load_pack
    loaded = []
    def track(connection, identity):
        loaded.append(identity)
        return actual_load(connection, identity)
    monkeypatch.setattr(report_history, "load_pack", track)
    with db.connect() as conn:
        rows = list(report_history.report_history_rows(conn))
    assert len(loaded) == 1
    assert all(len(row["packed_original_bytes"]) == row["pack_original_bytes"] for row in rows)
    assert [json.loads(report_history.original_report_bytes(row))["marker"] for row in rows] == ["0", "1", "2"]


def test_expiry_preserves_boundary_rewrites_partial_packs_and_collects_unreferenced_bytes(tmp_path):
    db, store = store_at(tmp_path)
    store.save_run(data("expired", at=NOW-timedelta(days=366)))
    store.save_run(data("boundary", at=NOW-timedelta(days=365)))
    store.save_run(data("current"))
    expected = history(db)
    assert [json.loads(item[3])["marker"] for item in expected] == ["boundary", "current"]
    with db.connect() as conn:
        assert conn.execute("SELECT SUM(original_bytes) FROM monitor_report_packs").fetchone()[0] == sum(len(item[3]) for item in expected)
        assert [row[0] for row in conn.execute("SELECT run_id FROM monitor_run_pack_refs ORDER BY run_id")] == [2, 3]
    store.save_run(data("future", at=NOW+timedelta(days=366)))
    current = history(db)
    assert [json.loads(item[3])["marker"] for item in current] == ["future"]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitor_report_packs").fetchone()[0] == 1
        assert conn.execute("SELECT original_bytes FROM monitor_report_packs").fetchone()[0] == len(current[0][3])


def test_failed_partial_expiry_repack_rolls_back_retention_and_new_generation(tmp_path, monkeypatch):
    db, store = store_at(tmp_path)
    store.save_run(data("expired", at=NOW-timedelta(days=366)))
    store.save_run(data("boundary", at=NOW-timedelta(days=365)))
    before = snapshot(db)
    real = packs.compress_pack
    calls = 0
    def fail_retention(raw):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted expiry repack")
        return real(raw)
    monkeypatch.setattr(packs, "compress_pack", fail_retention)
    with pytest.raises(RuntimeError, match="expiry repack"):
        store.save_run(data("new"))
    assert snapshot(db) == before


def test_gc_rejects_overlapping_members_before_allocating_more_than_pack_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "MIN_PACK_REPORT_BYTES", 64)
    monkeypatch.setattr(packs, "MAX_PACK_BYTES", 1024)
    db, store = store_at(tmp_path)
    store.save_run(data("one", size=600))
    with db.transaction() as conn:
        conn.execute("INSERT INTO monitor_runs VALUES(2,?,'degraded','')", (NOW.isoformat(),))
        conn.execute("INSERT INTO monitor_run_pack_refs SELECT 2,pack_id,byte_offset,original_bytes,sha256 FROM monitor_run_pack_refs WHERE run_id=1")
    before = snapshot(db)
    with pytest.raises(ValueError, match="members exceed"):
        with db.transaction() as conn:
            packs.prune_unreferenced_packs(conn)
    assert snapshot(db) == before
