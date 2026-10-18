"""Lossless, bounded storage for original monitor-report bytes.

The explicit payload table distinguishes compressed reports from legacy JSON.
It never normalizes or parses a legacy payload during migration: even an invalid
historical JSON receipt remains available with its exact original bytes.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import sqlite3
import zlib
from collections.abc import Iterator, Mapping
from typing import Any

from .report_packs import initialize_packs, insert_packed_report, load_pack, migrate_gzip_packs, pack_member


MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_COMPRESSED_REPORT_BYTES = MAX_REPORT_BYTES + 1024 * 1024
_PAYLOAD_SCHEMA = """CREATE TABLE IF NOT EXISTS monitor_run_payloads(
 run_id INTEGER PRIMARY KEY REFERENCES monitor_runs(id) ON DELETE CASCADE,
 encoding TEXT NOT NULL CHECK(encoding='gzip'),
 sha256 TEXT NOT NULL, original_bytes INTEGER NOT NULL CHECK(original_bytes>=0),
 payload_gzip BLOB NOT NULL)"""


def legacy_report_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise ValueError("Saved report payload is neither UTF-8 text nor bytes")
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Saved report exceeds the original-byte limit")
    return raw


def decompress_report(*, encoding: Any, sha256: Any, original_bytes: Any, payload_gzip: Any) -> bytes:
    """Validate one gzip member without allocating more than its bounded size."""
    if encoding != "gzip":
        raise ValueError("Saved report has an unsupported payload encoding")
    if (not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or type(original_bytes) is not int or not 0 <= original_bytes <= MAX_REPORT_BYTES):
        raise ValueError("Saved report has invalid hash or original-byte metadata")
    if not isinstance(payload_gzip, bytes) or len(payload_gzip) > MAX_COMPRESSED_REPORT_BYTES:
        raise ValueError("Saved report has an invalid or oversized compressed payload")
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(payload_gzip, original_bytes + 1)
    except zlib.error as exc:
        raise ValueError("Saved report gzip payload is malformed") from exc
    if (len(raw) != original_bytes or not decoder.eof
            or decoder.unconsumed_tail or decoder.unused_data):
        raise ValueError("Saved report gzip length, completeness or member count does not match its receipt")
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("Saved report payload hash does not match its receipt")
    return raw


def compress_report(raw: bytes) -> tuple[str, str, int, bytes]:
    raw = legacy_report_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    payload = gzip.compress(raw, compresslevel=6, mtime=0)
    # A failed encoder or storage adapter must not authorize removal of the
    # original JSON. Verify the exact bytes before the migration clears them.
    restored = decompress_report(encoding="gzip", sha256=digest, original_bytes=len(raw), payload_gzip=payload)
    if restored != raw:
        raise ValueError("Saved report compression did not preserve its original bytes")
    return "gzip", digest, len(raw), payload


def migrate_report_history(conn: sqlite3.Connection) -> None:
    """Run within the caller's write transaction; failure preserves all rows."""
    if not conn.in_transaction:
        raise ValueError("Report history migration requires a transaction")
    conn.execute(_PAYLOAD_SCHEMA)
    initialize_packs(conn)
    pending = conn.execute("""SELECT r.id,r.report_json,p.run_id,p.encoding,p.sha256,
        p.original_bytes,p.payload_gzip,q.run_id AS pack_run_id,q.pack_id,q.byte_offset,
        q.original_bytes AS pack_original_bytes,q.sha256 AS pack_sha256 FROM monitor_runs r
        LEFT JOIN monitor_run_payloads p ON p.run_id=r.id
        LEFT JOIN monitor_run_pack_refs q ON q.run_id=r.id
        WHERE (p.run_id IS NULL AND q.run_id IS NULL) OR r.report_json<>''
           OR (p.run_id IS NOT NULL AND q.run_id IS NOT NULL) ORDER BY r.id""")
    for row in pending:
        raw = legacy_report_bytes(row["report_json"])
        packed = None
        if row["pack_run_id"] is not None:
            packed = pack_member(load_pack(conn, row["pack_id"]), byte_offset=row["byte_offset"],
                                 original_bytes=row["pack_original_bytes"], sha256=row["pack_sha256"])
            if row["report_json"] != "" and packed != raw:
                raise ValueError("Saved report has conflicting raw and packed payloads")
        if row["run_id"] is not None:
            restored = decompress_report(encoding=row["encoding"], sha256=row["sha256"],
                                         original_bytes=row["original_bytes"], payload_gzip=row["payload_gzip"])
            if packed is not None and restored != packed:
                raise ValueError("Saved report has conflicting gzip and packed payloads")
            if row["report_json"] != "" and restored != raw:
                raise ValueError("Saved report has conflicting raw and compressed payloads")
            if packed is not None:
                conn.execute("DELETE FROM monitor_run_payloads WHERE run_id=?", (row["id"],))
        elif row["pack_run_id"] is None:
            conn.execute("""INSERT INTO monitor_run_payloads
                (run_id,encoding,sha256,original_bytes,payload_gzip) VALUES(?,?,?,?,?)""",
                         (row["id"], *compress_report(raw)))
        conn.execute("UPDATE monitor_runs SET report_json='' WHERE id=?", (row["id"],))
    migrate_gzip_packs(conn)


def insert_report_payload(conn: sqlite3.Connection, run_id: int, raw: bytes) -> None:
    raw = legacy_report_bytes(raw)
    if insert_packed_report(conn, run_id, raw):
        return
    conn.execute("""INSERT INTO monitor_run_payloads
        (run_id,encoding,sha256,original_bytes,payload_gzip) VALUES(?,?,?,?,?)""",
                 (run_id, *compress_report(raw)))


def report_history_rows(conn: sqlite3.Connection, *, newest_first: bool = False,
                        limit: int | None = None) -> Iterator[Mapping[str, Any]]:
    """Select either storage generation, including mixed upgrade checkpoints."""
    has_payloads = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_run_payloads'").fetchone()
    has_packs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_run_pack_refs'").fetchone()
    if has_payloads:
        query = """SELECT r.id,r.completed_at,r.status,r.report_json,p.run_id AS payload_run_id,
            p.encoding AS payload_encoding,p.sha256 AS payload_sha256,
            p.original_bytes AS payload_original_bytes,p.payload_gzip
            FROM monitor_runs r LEFT JOIN monitor_run_payloads p ON p.run_id=r.id"""
    else:
        query = """SELECT id,completed_at,status,report_json,NULL AS payload_run_id,
            NULL AS payload_encoding,NULL AS payload_sha256,NULL AS payload_original_bytes,
            NULL AS payload_gzip FROM monitor_runs"""
    if has_packs:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_report_packs'").fetchone():
            raise ValueError("Saved report pack storage is incomplete")
        # Join only metadata; a pack is read and expanded once per contiguous
        # group, never copied into every SQL result row.
        query = "SELECT h.*,q.run_id AS pack_run_id,q.pack_id,q.byte_offset,q.original_bytes AS pack_original_bytes,q.sha256 AS pack_sha256 FROM (" + query + ") h LEFT JOIN monitor_run_pack_refs q ON q.run_id=h.id"
    query += " ORDER BY id " + ("DESC" if newest_first else "ASC")
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise ValueError("Report history limit must be positive")
        query += " LIMIT ?"
    cached_id, cached_raw = None, None
    for row in conn.execute(query, (limit,) if limit is not None else ()):
        if has_packs and row["pack_run_id"] is not None:
            if row["pack_id"] != cached_id:
                cached_raw = load_pack(conn, row["pack_id"])
                cached_id = row["pack_id"]
            member = pack_member(cached_raw, byte_offset=row["byte_offset"],
                                 original_bytes=row["pack_original_bytes"], sha256=row["pack_sha256"])
            # Expose only this report's bytes, not a cache reference that could
            # keep a complete old pack alive after the iterator advances.
            yield {**dict(row), "packed_original_bytes": member}
        else:
            yield row


def original_report_bytes(row: Mapping[str, Any] | sqlite3.Row) -> bytes:
    packed = row["pack_run_id"] is not None if "pack_run_id" in row.keys() else False
    raw = row["packed_original_bytes"] if packed else None
    if row["payload_run_id"] is not None:
        gzip_raw = decompress_report(encoding=row["payload_encoding"], sha256=row["payload_sha256"],
                                     original_bytes=row["payload_original_bytes"], payload_gzip=row["payload_gzip"])
        if packed and raw != gzip_raw:
            raise ValueError("Saved report has conflicting gzip and packed payloads")
        raw = gzip_raw
    if raw is None:
        return legacy_report_bytes(row["report_json"])
    if row["report_json"] != "" and legacy_report_bytes(row["report_json"]) != raw:
        raise ValueError("Saved report has conflicting raw and compressed payloads")
    return raw
