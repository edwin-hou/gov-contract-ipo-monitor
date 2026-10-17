"""Self-contained, streamed SQLite checkpoints with an explicit commit manifest."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from contextlib import closing
from pathlib import Path
from typing import BinaryIO

from .checkpoint import MAX_ARCHIVE_BYTES, MAX_CHECKPOINT_BYTES, MAX_LEGACY_BYTES, validate_database


FORMAT = "contract-ipo-monitor-checkpoint"
VERSION = 1
MANIFEST_NAME = "checkpoint.json"
PAYLOAD_NAME = "monitor.db.gz"
MAX_MANIFEST_BYTES = 64 * 1024
# Keep room for the manifest and the outer ZIP, even with no ZIP compression.
MAX_PAYLOAD_BYTES = MAX_ARCHIVE_BYTES - MAX_MANIFEST_BYTES - 1024 * 1024
_CHUNK = 1024 * 1024
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def _integer(value, *, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Checkpoint manifest has duplicate keys")
        result[key] = value
    return result


def _invalid_number(value):
    raise ValueError("Checkpoint manifest contains an invalid JSON number")


def parse_manifest(raw: bytes) -> dict:
    if not 1 <= len(raw) <= MAX_MANIFEST_BYTES:
        raise ValueError("Checkpoint manifest exceeds its byte bound")
    try:
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid_number)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Checkpoint manifest is not valid UTF-8 JSON") from exc
    if (not isinstance(record, dict)
            or set(record) != {"format", "version", "codec", "database", "payload", "checkpoint_size"}
            or record["format"] != FORMAT or type(record["version"]) is not int
            or record["version"] != VERSION or record["codec"] != "gzip"
            or not isinstance(record["checkpoint_size"], dict)):
        raise ValueError("Unsupported checkpoint manifest format")
    for name, filename, minimum, maximum in (
        ("database", "monitor.db", 100, MAX_CHECKPOINT_BYTES),
        ("payload", PAYLOAD_NAME, 1, MAX_PAYLOAD_BYTES),
    ):
        item = record[name]
        if (not isinstance(item, dict) or set(item) != {"file", "bytes", "sha256"}
                or item["file"] != filename
                or not _integer(item["bytes"], minimum=minimum, maximum=maximum)
                or not isinstance(item["sha256"], str) or not _DIGEST.fullmatch(item["sha256"])):
            raise ValueError(f"Invalid checkpoint {name} identity, size or hash")
    return record


def _free_space(directory: Path, required: int) -> None:
    if shutil.disk_usage(directory).free < required:
        raise ValueError(f"Checkpoint requires {required} free bytes for bounded staging; destination disk capacity is insufficient")


def _temporary(directory: Path, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".checkpoint-", suffix=suffix, dir=directory)
    os.close(descriptor)
    return Path(name)


def _page_stats(conn) -> dict:
    return {name: int(conn.execute(f"PRAGMA {name}").fetchone()[0])
            for name in ("page_size", "page_count", "freelist_count")}


def _diagnostics(path: Path, before: dict) -> dict:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        result = {"before_compaction": before, "after_compaction": _page_stats(conn),
                  "database_bytes": path.stat().st_size}
        for table, key in (("monitor_runs", "retained_report_count"),
                           ("monitor_run_payloads", "compressed_report_count")):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                result[key] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            else:
                result[key] = 0
        try:
            result["table_allocation"] = [dict(zip(("name", "page_bytes", "payload_bytes", "unused_bytes"), row))
                for row in conn.execute("""SELECT name,SUM(pgsize),SUM(payload),SUM(unused)
                    FROM dbstat GROUP BY name ORDER BY SUM(pgsize) DESC,name LIMIT 64""")]
            result["dbstat_available"] = True
        except sqlite3.OperationalError:
            result.update(dbstat_available=False, table_allocation=[])
        return result


class _BoundedWriter:
    def __init__(self, output: BinaryIO, limit: int):
        self.output = output
        self.limit = limit
        self.count = 0
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        if self.count + len(data) > self.limit:
            raise ValueError("Compressed checkpoint exceeds the portable archive transport bound")
        written = self.output.write(data)
        if written != len(data):
            raise OSError("Checkpoint payload write was incomplete")
        self.count += written
        self.digest.update(data)
        return written

    def flush(self) -> None:
        self.output.flush()


def _compress(snapshot: Path, payload: Path, *, max_payload_bytes: int) -> tuple[dict, dict]:
    digest = hashlib.sha256()
    count = 0
    with snapshot.open("rb") as source, payload.open("wb") as output:
        bounded = _BoundedWriter(output, max_payload_bytes)
        with gzip.GzipFile(filename="", fileobj=bounded, mode="wb", compresslevel=6, mtime=0) as compressed:
            while chunk := source.read(_CHUNK):
                count += len(chunk)
                digest.update(chunk)
                compressed.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    return ({"file": "monitor.db", "bytes": count, "sha256": digest.hexdigest()},
            {"file": PAYLOAD_NAME, "bytes": bounded.count, "sha256": bounded.digest.hexdigest()})


def decode_payload(source: BinaryIO, record: dict, output: BinaryIO | None = None) -> None:
    """Read one gzip member using bounded input and output buffers; never extract paths."""
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    encoded_hash, decoded_hash = hashlib.sha256(), hashlib.sha256()
    encoded_count = decoded_count = 0
    expected_payload, expected_database = record["payload"], record["database"]
    try:
        while chunk := source.read(_CHUNK):
            encoded_count += len(chunk)
            if encoded_count > expected_payload["bytes"] or encoded_count > MAX_PAYLOAD_BYTES:
                raise ValueError("Checkpoint payload exceeds its declared compressed size")
            encoded_hash.update(chunk)
            if decoder.eof:
                raise ValueError("Checkpoint gzip contains trailing data or concatenated members")
            pending = chunk
            while pending:
                data = decoder.decompress(pending, min(_CHUNK, expected_database["bytes"] - decoded_count + 1))
                decoded_count += len(data)
                if decoded_count > expected_database["bytes"] or decoded_count > MAX_CHECKPOINT_BYTES:
                    raise ValueError("Checkpoint expands beyond its declared database size")
                decoded_hash.update(data)
                if output is not None:
                    output.write(data)
                pending = decoder.unconsumed_tail
                if decoder.eof:
                    if decoder.unused_data or pending:
                        raise ValueError("Checkpoint gzip contains trailing data or concatenated members")
                    break
    except zlib.error as exc:
        raise ValueError("Checkpoint gzip is corrupt") from exc
    if (not decoder.eof or encoded_count != expected_payload["bytes"]
            or encoded_hash.hexdigest() != expected_payload["sha256"]
            or decoded_count != expected_database["bytes"]
            or decoded_hash.hexdigest() != expected_database["sha256"]):
        raise ValueError("Checkpoint payload is truncated or its sizes/hashes do not match")


def validate_export(directory: Path) -> dict:
    directory = Path(directory)
    manifest, payload = directory / MANIFEST_NAME, directory / PAYLOAD_NAME
    if manifest.is_symlink() or payload.is_symlink():
        raise ValueError("Checkpoint export files must not be symbolic links")
    if manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Checkpoint manifest exceeds its byte bound")
    record = parse_manifest(manifest.read_bytes())
    if payload.stat().st_size != record["payload"]["bytes"]:
        raise ValueError("Checkpoint gzip size does not match its manifest")
    _free_space(directory, record["database"]["bytes"] + _CHUNK)
    temporary = _temporary(directory, ".verify.db")
    try:
        with payload.open("rb") as source, temporary.open("wb") as output:
            decode_payload(source, record, output)
        validate_database(temporary, max_bytes=MAX_CHECKPOINT_BYTES)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def restore_archive(archive: Path, target: Path) -> bool:
    """Restore one legacy database or the exact two-file portable generation."""
    archive, target = Path(archive), Path(target)
    if not archive.exists():
        return False
    if not 1 <= archive.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError("Checkpoint ZIP exceeds the transport byte bound")
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        legacy = len(entries) == 1 and entries[0].filename == "monitor.db"
        if legacy:
            if entries[0].is_dir() or not 100 <= entries[0].file_size <= MAX_LEGACY_BYTES:
                raise ValueError("Unexpected or oversized checkpoint artifact")
            expanded_bytes = entries[0].file_size
        else:
            if (len(entries) != 2 or {entry.filename for entry in entries} != {MANIFEST_NAME, PAYLOAD_NAME}
                    or any(entry.is_dir() for entry in entries)):
                raise ValueError("Unexpected checkpoint artifact inventory")
            manifest_info, payload_info = bundle.getinfo(MANIFEST_NAME), bundle.getinfo(PAYLOAD_NAME)
            if (not 1 <= manifest_info.file_size <= MAX_MANIFEST_BYTES
                    or not 1 <= payload_info.file_size <= MAX_PAYLOAD_BYTES):
                raise ValueError("Checkpoint archive member exceeds its byte bound")
            with bundle.open(manifest_info) as source:
                record = parse_manifest(source.read(MAX_MANIFEST_BYTES + 1))
            if payload_info.file_size != record["payload"]["bytes"]:
                raise ValueError("Checkpoint gzip size does not match its manifest")
            expanded_bytes = record["database"]["bytes"]
        target.parent.mkdir(parents=True, exist_ok=True)
        _free_space(target.parent, expanded_bytes + _CHUNK)
        temporary = target.with_suffix(".restore")
        try:
            if legacy:
                written = 0
                with bundle.open(entries[0]) as source, temporary.open("wb") as output:
                    while chunk := source.read(_CHUNK):
                        written += len(chunk)
                        if written > expanded_bytes or written > MAX_LEGACY_BYTES:
                            raise ValueError("Checkpoint exceeds the uncompressed legacy byte limit")
                        output.write(chunk)
                if written != expanded_bytes:
                    raise ValueError("Checkpoint archive size does not match its contents")
                validate_database(temporary, max_bytes=MAX_LEGACY_BYTES)
            else:
                with bundle.open(payload_info) as source, temporary.open("wb") as output:
                    decode_payload(source, record, output)
                validate_database(temporary, max_bytes=MAX_CHECKPOINT_BYTES)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return True


def _publish(snapshot: Path, payload: Path, manifest: Path, destination: Path) -> None:
    """Manifest-last commit; roll back all prior files on an ordinary I/O failure."""
    manifest_target, payload_target = destination.parent / MANIFEST_NAME, destination.parent / PAYLOAD_NAME
    targets = (manifest_target, payload_target, destination)
    for target in targets:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError("Checkpoint publication target must be a regular file")
    backups = {}
    installed = set()
    try:
        # Invalidate the old commit marker before any old payload can be moved.
        for target in targets:
            if target.exists() or target.is_symlink():
                backup = _temporary(destination.parent, ".previous")
                backup.unlink()
                target.replace(backup)
                backups[target] = backup
        for staged, target in ((snapshot, destination), (payload, payload_target), (manifest, manifest_target)):
            staged.replace(target)
            installed.add(target)
    except Exception:
        # A missing marker is safer than asserting a mismatched generation.
        if manifest_target in backups or manifest_target in installed:
            manifest_target.unlink(missing_ok=True)
        try:
            for target in (payload_target, destination):
                if target in backups:
                    backups[target].replace(target)
                elif target in installed:
                    target.unlink(missing_ok=True)
            if manifest_target in backups:
                backups[manifest_target].replace(manifest_target)
        except Exception as rollback_error:
            manifest_target.unlink(missing_ok=True)
            raise RuntimeError("Checkpoint publication rollback failed; no commit manifest is present and previous files are retained for recovery") from rollback_error
        raise
    finally:
        # Do not discard recovery files if rollback itself failed.
        for backup in backups.values():
            if backup.exists() and manifest_target.exists():
                backup.unlink()


def export_database(db, destination: Path, *, max_database_bytes: int = MAX_CHECKPOINT_BYTES,
                    max_payload_bytes: int = MAX_PAYLOAD_BYTES) -> dict:
    """Backup committed WAL data, compact the copy, and publish a complete gzip generation."""
    destination = Path(destination)
    if getattr(db, "path", None) is not None and Path(db.path).resolve() == destination.resolve():
        raise ValueError("Checkpoint destination must be separate from its live source database")
    if destination.name in {MANIFEST_NAME, PAYLOAD_NAME}:
        raise ValueError("Checkpoint database destination conflicts with portable export files")
    destination.parent.mkdir(parents=True, exist_ok=True)
    snapshot = destination.with_suffix(".tmp")
    staged = [snapshot]
    try:
        compact = _temporary(destination.parent, ".compact.db")
        staged.append(compact)
        payload = _temporary(destination.parent, ".payload.gz")
        staged.append(payload)
        manifest = _temporary(destination.parent, ".manifest.json")
        staged.append(manifest)
        compact.unlink()  # SQLite VACUUM INTO requires an absent or empty output.
        with closing(db.connect()) as source:
            before = _page_stats(source) if hasattr(source, "execute") else {}
            estimated = before.get("page_size", 0) * before.get("page_count", 0)
            if estimated > max_database_bytes:
                raise ValueError("Checkpoint source exceeds the expanded portable database resource bound")
            _free_space(destination.parent, 2 * estimated + min(estimated, max_payload_bytes) + 2 * _CHUNK)
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
        validate_database(snapshot, max_bytes=max_database_bytes)
        with closing(sqlite3.connect(snapshot)) as copied:
            copied.execute("VACUUM INTO ?", (str(compact),))
        validate_database(compact, max_bytes=max_database_bytes)
        snapshot.unlink()
        compact.replace(snapshot)
        database_identity, payload_identity = _compress(snapshot, payload, max_payload_bytes=max_payload_bytes)
        record = {"format": FORMAT, "version": VERSION, "codec": "gzip",
                  "database": database_identity, "payload": payload_identity,
                  "checkpoint_size": _diagnostics(snapshot, before)}
        raw = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        parse_manifest(raw)
        with manifest.open("wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        # Verify the staged gzip against the independent copied-database digest.
        with payload.open("rb") as source:
            decode_payload(source, record)
        _publish(snapshot, payload, manifest, destination)
        return record
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
