"""Bounded immutable XZ packs share dictionaries across original report receipts."""
from __future__ import annotations

import hashlib
import lzma
import re
import sqlite3
from typing import Any


MIN_PACK_REPORT_BYTES = 64 * 1024
MAX_PACK_BYTES = 32 * 1024 * 1024
MAX_COMPRESSED_PACK_BYTES = MAX_PACK_BYTES + 1024 * 1024
MAX_DECODER_MEMORY_BYTES = 64 * 1024 * 1024
PACK_PRESET = 5  # 8 MiB dictionary; bounded decompression independently checks it.

_PACK_SCHEMA = """CREATE TABLE IF NOT EXISTS monitor_report_packs(
 id INTEGER PRIMARY KEY,encoding TEXT NOT NULL CHECK(encoding='xz'),
 sha256 TEXT NOT NULL,original_bytes INTEGER NOT NULL CHECK(original_bytes>0),
 payload_xz BLOB NOT NULL)"""
_REF_SCHEMA = """CREATE TABLE IF NOT EXISTS monitor_run_pack_refs(
 run_id INTEGER PRIMARY KEY REFERENCES monitor_runs(id) ON DELETE CASCADE,
 pack_id INTEGER NOT NULL REFERENCES monitor_report_packs(id),
 byte_offset INTEGER NOT NULL CHECK(byte_offset>=0),
 original_bytes INTEGER NOT NULL CHECK(original_bytes>=0),sha256 TEXT NOT NULL)"""


def initialize_packs(conn: sqlite3.Connection) -> None:
    conn.execute(_PACK_SCHEMA)
    conn.execute(_REF_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS monitor_pack_members ON monitor_run_pack_refs(pack_id)")


def _digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def decode_pack(*, encoding: Any, sha256: Any, original_bytes: Any, payload_xz: Any) -> bytes:
    if encoding != "xz":
        raise ValueError("Saved report pack has an unsupported encoding")
    if (not _digest(sha256) or type(original_bytes) is not int
            or not 1 <= original_bytes <= MAX_PACK_BYTES):
        raise ValueError("Saved report pack has invalid hash or original-byte metadata")
    if not isinstance(payload_xz, bytes) or not 1 <= len(payload_xz) <= MAX_COMPRESSED_PACK_BYTES:
        raise ValueError("Saved report pack has an invalid or oversized compressed payload")
    try:
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=MAX_DECODER_MEMORY_BYTES)
        raw = decoder.decompress(payload_xz, max_length=original_bytes + 1)
    except lzma.LZMAError as exc:
        raise ValueError("Saved report pack XZ payload is malformed or exceeds its decoder memory bound") from exc
    if len(raw) != original_bytes or not decoder.eof or decoder.unused_data:
        raise ValueError("Saved report pack length, completeness or member count does not match its receipt")
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("Saved report pack hash does not match its receipt")
    return raw


def compress_pack(raw: bytes) -> tuple[str, str, int, bytes]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_PACK_BYTES:
        raise ValueError("Saved report pack exceeds its original-byte bound")
    payload = lzma.compress(raw, format=lzma.FORMAT_XZ, preset=PACK_PRESET)
    values = ("xz", hashlib.sha256(raw).hexdigest(), len(raw), payload)
    if decode_pack(**dict(zip(("encoding", "sha256", "original_bytes", "payload_xz"), values))) != raw:
        raise ValueError("Saved report pack did not preserve its original bytes")
    return values


def load_pack(conn: sqlite3.Connection, pack_id: Any) -> bytes:
    if type(pack_id) is not int or pack_id < 1:
        raise ValueError("Saved report has an invalid pack identity")
    row = conn.execute("SELECT encoding,sha256,original_bytes,payload_xz FROM monitor_report_packs WHERE id=?", (pack_id,)).fetchone()
    if row is None:
        raise ValueError("Saved report refers to a missing pack")
    return decode_pack(encoding=row[0], sha256=row[1], original_bytes=row[2], payload_xz=row[3])


def pack_member(raw: bytes, *, byte_offset: Any, original_bytes: Any, sha256: Any) -> bytes:
    if (type(byte_offset) is not int or byte_offset < 0 or type(original_bytes) is not int
            or not 0 <= original_bytes <= MAX_PACK_BYTES
            or byte_offset + original_bytes > len(raw) or not _digest(sha256)):
        raise ValueError("Saved report has invalid pack member bounds or hash metadata")
    member = raw[byte_offset:byte_offset + original_bytes]
    if hashlib.sha256(member).hexdigest() != sha256:
        raise ValueError("Saved report pack member hash does not match its receipt")
    return member


def _create_pack(conn: sqlite3.Connection, raw: bytes) -> int:
    values = compress_pack(raw)
    # Do not allow a faulty encoder/adapter to authorize deletion or retargeting
    # of the preceding verified generation.
    restored = decode_pack(**dict(zip(("encoding", "sha256", "original_bytes", "payload_xz"), values)))
    if restored != raw:
        raise ValueError("Saved report pack did not preserve its original bytes")
    return int(conn.execute("INSERT INTO monitor_report_packs(encoding,sha256,original_bytes,payload_xz) VALUES(?,?,?,?)", values).lastrowid)


def _reference(conn: sqlite3.Connection, run_id: int, pack_id: int, offset: int, raw: bytes) -> None:
    conn.execute("""INSERT INTO monitor_run_pack_refs(run_id,pack_id,byte_offset,original_bytes,sha256)
        VALUES(?,?,?,?,?)""", (run_id, pack_id, offset, len(raw), hashlib.sha256(raw).hexdigest()))


def pack_eligible(size: int) -> bool:
    return MIN_PACK_REPORT_BYTES <= size <= MAX_PACK_BYTES


def migrate_gzip_packs(conn: sqlite3.Connection) -> None:
    """Batch the old gzip generation; commit is owned by the caller's transaction."""
    from .report_history import decompress_report

    if not conn.in_transaction:
        raise ValueError("Report pack migration requires a transaction")
    identifiers = [row[0] for row in conn.execute("""SELECT p.run_id
        FROM monitor_run_payloads p LEFT JOIN monitor_run_pack_refs r ON r.run_id=p.run_id
        WHERE r.run_id IS NULL AND p.original_bytes>=? AND p.original_bytes<=? ORDER BY p.run_id""",
        (MIN_PACK_REPORT_BYTES, MAX_PACK_BYTES))]
    pending = bytearray()
    receipts = []

    def flush() -> None:
        if not receipts:
            return
        raw_pack = bytes(pending)
        pack_id = _create_pack(conn, raw_pack)
        for run_id, offset, size, digest in receipts:
            # The same per-report hash/length remains authoritative after the
            # storage change; malformed historical JSON is never normalized.
            member = pack_member(raw_pack, byte_offset=offset, original_bytes=size, sha256=digest)
            _reference(conn, run_id, pack_id, offset, member)
            conn.execute("DELETE FROM monitor_run_payloads WHERE run_id=?", (run_id,))
        pending.clear()
        receipts.clear()

    for run_id in identifiers:
        row = conn.execute("SELECT run_id,encoding,sha256,original_bytes,payload_gzip FROM monitor_run_payloads WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Saved report disappeared during pack migration")
        raw = decompress_report(encoding=row[1], sha256=row[2], original_bytes=row[3], payload_gzip=row[4])
        if pending and len(pending) + len(raw) > MAX_PACK_BYTES:
            flush()
        receipts.append((row[0], len(pending), len(raw), row[2]))
        pending.extend(raw)
    flush()
    prune_unreferenced_packs(conn)


def insert_packed_report(conn: sqlite3.Connection, run_id: int, raw: bytes) -> bool:
    if not pack_eligible(len(raw)):
        return False
    if not conn.in_transaction:
        raise ValueError("Report pack insertion requires a transaction")
    previous = conn.execute("SELECT pack_id FROM monitor_run_pack_refs ORDER BY run_id DESC LIMIT 1").fetchone()
    old_pack_id = previous[0] if previous else None
    existing = load_pack(conn, old_pack_id) if old_pack_id is not None else b""
    if len(existing) + len(raw) > MAX_PACK_BYTES:
        old_pack_id, existing = None, b""
    if old_pack_id is not None:
        for row in conn.execute("SELECT byte_offset,original_bytes,sha256 FROM monitor_run_pack_refs WHERE pack_id=?", (old_pack_id,)):
            pack_member(existing, byte_offset=row[0], original_bytes=row[1], sha256=row[2])
    pack_id = _create_pack(conn, existing + raw)
    if old_pack_id is not None:
        conn.execute("UPDATE monitor_run_pack_refs SET pack_id=? WHERE pack_id=?", (pack_id, old_pack_id))
    _reference(conn, run_id, pack_id, len(existing), raw)
    prune_unreferenced_packs(conn)
    return True


def prune_unreferenced_packs(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise ValueError("Report pack retention requires a transaction")
    conn.execute("""DELETE FROM monitor_report_packs WHERE NOT EXISTS(
        SELECT 1 FROM monitor_run_pack_refs WHERE pack_id=monitor_report_packs.id)""")
    # A partially expired hot pack must not carry old bytes into every future
    # rewrite indefinitely. Repack only its remaining verified members.
    partial = [row[0] for row in conn.execute("""SELECT p.id FROM monitor_report_packs p
        JOIN monitor_run_pack_refs r ON r.pack_id=p.id GROUP BY p.id
        HAVING SUM(r.original_bytes)<>p.original_bytes""")]
    for old_pack_id in partial:
        raw = load_pack(conn, old_pack_id)
        members = []
        pending = bytearray()
        for row in conn.execute("SELECT run_id,byte_offset,original_bytes,sha256 FROM monitor_run_pack_refs WHERE pack_id=? ORDER BY byte_offset,run_id", (old_pack_id,)):
            member = pack_member(raw, byte_offset=row[1], original_bytes=row[2], sha256=row[3])
            if len(pending) + len(member) > MAX_PACK_BYTES:
                raise ValueError("Saved report pack members exceed the bounded pack size")
            members.append((row[0], len(pending)))
            pending.extend(member)
        new_pack_id = _create_pack(conn, bytes(pending))
        for run_id, offset in members:
            conn.execute("UPDATE monitor_run_pack_refs SET pack_id=?,byte_offset=? WHERE run_id=?", (new_pack_id, offset, run_id))
        conn.execute("DELETE FROM monitor_report_packs WHERE id=?", (old_pack_id,))
