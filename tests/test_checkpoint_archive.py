"""Actual SQLite/gzip/ZIP round trips and failure preservation, with no network."""
from contextlib import closing
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import zipfile

import pytest

from contract_ipo_monitor import checkpoint_archive as portable
from contract_ipo_monitor.checkpoint import MAX_ARCHIVE_BYTES, MAX_CHECKPOINT_BYTES, MAX_LEGACY_BYTES, validate_export
from contract_ipo_monitor.db import Database


def source_database(tmp_path):
    db = Database(tmp_path / 'source.db')
    db.initialize()
    db.update_collector_state('sec', cursor='retained checkpoint cursor')
    with db.connect() as conn:
        conn.execute('CREATE TABLE primary_bytes(sha256 TEXT PRIMARY KEY,payload BLOB NOT NULL)')
        raw = b'actual raw source\x00\xff and index bytes' * 100
        conn.execute('INSERT INTO primary_bytes VALUES(?,?)', (hashlib.sha256(raw).hexdigest(), raw))
    return db


def export(tmp_path):
    db = source_database(tmp_path)
    destination = tmp_path / 'export' / 'monitor.db'
    record = portable.export_database(db, destination)
    return db, destination, record


def bundle(tmp_path, directory, *, record=None, payload=None, extra=(), manifest_raw=None):
    archive = tmp_path / 'checkpoint.zip'
    if record is None:
        record = json.loads((directory / portable.MANIFEST_NAME).read_bytes())
    if payload is None:
        payload = (directory / portable.PAYLOAD_NAME).read_bytes()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as output:
        output.writestr(portable.MANIFEST_NAME, manifest_raw if manifest_raw is not None else json.dumps(record).encode())
        output.writestr(portable.PAYLOAD_NAME, payload)
        for name, content in extra:
            output.writestr(name, content)
    return archive


def preserved_restore(tmp_path, archive):
    target = tmp_path / 'restored.db'
    target.write_bytes(b'previous complete database')
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        portable.restore_archive(archive, target)
    assert target.read_bytes() == b'previous complete database'
    assert not target.with_suffix('.restore').exists()


def test_exact_portable_roundtrip_preserves_cursor_and_raw_source_bytes(tmp_path):
    db, destination, record = export(tmp_path)
    before_source = Path(db.path).read_bytes()
    assert record['database']['bytes'] == destination.stat().st_size
    assert record['database']['sha256'] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert record['payload']['sha256'] == hashlib.sha256((destination.parent / portable.PAYLOAD_NAME).read_bytes()).hexdigest()
    assert validate_export(destination.parent) == record
    assert Path(db.path).read_bytes() == before_source
    restored = tmp_path / 'restored.db'
    assert portable.restore_archive(bundle(tmp_path, destination.parent), restored)
    assert restored.read_bytes() == destination.read_bytes()
    with Database(restored).connect() as conn:
        assert conn.execute('SELECT cursor FROM collector_state WHERE name=?', ('sec',)).fetchone()[0] == 'retained checkpoint cursor'
        row = conn.execute('SELECT sha256,payload FROM primary_bytes').fetchone()
        assert hashlib.sha256(row[1]).hexdigest() == row[0]


def test_only_copied_snapshot_is_compacted_and_diagnostics_explain_freelist(tmp_path):
    db = source_database(tmp_path)
    with db.connect() as conn:
        conn.execute('CREATE TABLE temporary_work(payload BLOB)')
        conn.execute('INSERT INTO temporary_work VALUES(zeroblob(2000000))')
        conn.execute('DELETE FROM temporary_work')
        before_pages = conn.execute('PRAGMA page_count').fetchone()[0]
        before_free = conn.execute('PRAGMA freelist_count').fetchone()[0]
    before = Path(db.path).read_bytes()
    record = portable.export_database(db, tmp_path / 'export' / 'monitor.db')
    assert before_free > 0
    assert Path(db.path).read_bytes() == before
    stats = record['checkpoint_size']
    assert stats['before_compaction']['page_count'] == before_pages
    assert stats['before_compaction']['freelist_count'] == before_free
    assert stats['after_compaction']['freelist_count'] == 0
    assert stats['after_compaction']['page_count'] < before_pages
    assert stats['database_bytes'] < len(before)
    assert type(stats['dbstat_available']) is bool
    if stats['dbstat_available']:
        assert any(row['name'] == 'primary_bytes' and row['payload_bytes'] > 0 for row in stats['table_allocation'])


def test_committed_wal_content_is_in_the_portable_snapshot(tmp_path):
    db = source_database(tmp_path)
    with db.connect() as writer:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('INSERT INTO primary_bytes VALUES(?,?)', ('wal-only', b'committed WAL observation'))
        assert Path(db.path + '-wal').stat().st_size > 0
        destination = tmp_path / 'export' / 'monitor.db'
        portable.export_database(db, destination)
        restored = tmp_path / 'restored.db'
        portable.restore_archive(bundle(tmp_path, destination.parent), restored)
        with Database(restored).connect() as conn:
            assert conn.execute('SELECT payload FROM primary_bytes WHERE sha256=?', ('wal-only',)).fetchone()[0] == b'committed WAL observation'


@pytest.mark.parametrize('mutation', ['payload_hash', 'database_hash', 'payload_size', 'database_size',
                                      'version', 'boolean_version', 'codec', 'database_path', 'payload_path',
                                      'extra_key', 'negative_size', 'over_expanded_bound', 'over_payload_bound'])
def test_invalid_manifest_never_replaces_previous_database(tmp_path, mutation):
    _, destination, record = export(tmp_path)
    record = deepcopy(record)
    if mutation == 'payload_hash': record['payload']['sha256'] = '0' * 64
    elif mutation == 'database_hash': record['database']['sha256'] = '0' * 64
    elif mutation == 'payload_size': record['payload']['bytes'] += 1
    elif mutation == 'database_size': record['database']['bytes'] += 1
    elif mutation == 'version': record['version'] = 2
    elif mutation == 'boolean_version': record['version'] = True
    elif mutation == 'codec': record['codec'] = 'zip'
    elif mutation == 'database_path': record['database']['file'] = '../monitor.db'
    elif mutation == 'payload_path': record['payload']['file'] = '../monitor.db.gz'
    elif mutation == 'extra_key': record['unexpected'] = True
    elif mutation == 'negative_size': record['database']['bytes'] = -1
    elif mutation == 'over_expanded_bound': record['database']['bytes'] = MAX_CHECKPOINT_BYTES + 1
    elif mutation == 'over_payload_bound': record['payload']['bytes'] = portable.MAX_PAYLOAD_BYTES + 1
    preserved_restore(tmp_path, bundle(tmp_path, destination.parent, record=record))


@pytest.mark.parametrize('mutation', ['truncated', 'garbage', 'concatenated', 'expanded_bomb', 'malformed_gzip'])
def test_corrupt_or_bomb_payload_is_bounded_and_preserves_target(tmp_path, mutation):
    _, destination, record = export(tmp_path)
    data = (destination.parent / portable.PAYLOAD_NAME).read_bytes()
    if mutation == 'truncated': data = data[:-8]
    elif mutation == 'garbage': data += b'trailing garbage'
    elif mutation == 'concatenated': data += gzip.compress(b'another member')
    elif mutation == 'expanded_bomb':
        data = gzip.compress(b'x' * (8 * 1024 * 1024))
        record['database']['bytes'] = 100
    else: data = b'invalid gzip bytes'
    # Keep the compressed manifest correct so inner stream validation is tested.
    record['payload'].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    preserved_restore(tmp_path, bundle(tmp_path, destination.parent, record=record, payload=data))


@pytest.mark.parametrize('raw', [b'{}', b'\xff', b'{"version":1,"version":1}',
                                b'{"version":NaN}', b'x' * (portable.MAX_MANIFEST_BYTES + 1)],
                         ids=['missing-fields', 'invalid-utf8', 'duplicate-keys', 'invalid-number', 'oversized'])
def test_malformed_manifest_is_rejected(tmp_path, raw):
    _, destination, _ = export(tmp_path)
    preserved_restore(tmp_path, bundle(tmp_path, destination.parent, manifest_raw=raw))


@pytest.mark.parametrize('extra', [('unexpected.txt', b'other'), ('../monitor.db', b'escape'), ('folder/', b'')])
def test_archive_inventory_is_exact_and_never_extracts_paths(tmp_path, extra):
    _, destination, _ = export(tmp_path)
    preserved_restore(tmp_path, bundle(tmp_path, destination.parent, extra=(extra,)))
    assert not (tmp_path.parent / 'monitor.db').exists()


def test_duplicate_zip_members_are_rejected(tmp_path):
    _, destination, _ = export(tmp_path)
    with pytest.warns(UserWarning, match='Duplicate name'):
        archive = bundle(tmp_path, destination.parent, extra=((portable.MANIFEST_NAME, b'{}'),))
    preserved_restore(tmp_path, archive)


def test_self_consistent_hashes_do_not_replace_sqlite_schema_validation(tmp_path):
    _, destination, record = export(tmp_path)
    unrelated = tmp_path / 'unrelated.db'
    with closing(sqlite3.connect(unrelated)) as conn:
        conn.execute('CREATE TABLE other(id INTEGER PRIMARY KEY)')
    raw = unrelated.read_bytes()
    payload = gzip.compress(raw, mtime=0)
    record['database'].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    record['payload'].update(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    preserved_restore(tmp_path, bundle(tmp_path, destination.parent, record=record, payload=payload))


@pytest.mark.parametrize('stage', ['database', 'payload', 'manifest'])
def test_publication_failure_rolls_back_the_entire_prior_generation(tmp_path, monkeypatch, stage):
    db, destination, _ = export(tmp_path)
    paths = (destination, destination.parent / portable.PAYLOAD_NAME, destination.parent / portable.MANIFEST_NAME)
    before = {path: path.read_bytes() for path in paths}
    db.update_collector_state('sec', cursor='new progress that must not be partly published')
    replace = Path.replace
    failed = False

    def fail_once(self, target):
        nonlocal failed
        target = Path(target)
        matches = ((stage == 'database' and self == destination.with_suffix('.tmp') and target == destination)
                   or (stage == 'payload' and self.name.endswith('.payload.gz') and target == paths[1])
                   or (stage == 'manifest' and self.name.endswith('.manifest.json') and target == paths[2]))
        if matches and not failed:
            failed = True
            raise OSError('simulated publication interruption')
        return replace(self, target)

    monkeypatch.setattr(Path, 'replace', fail_once)
    with pytest.raises(OSError, match='publication interruption'):
        portable.export_database(db, destination)
    assert failed
    assert {path: path.read_bytes() for path in paths} == before
    validate_export(destination.parent)
    assert not list(destination.parent.glob('.checkpoint-*'))


@pytest.mark.parametrize('filename', ['checkpoint.json', 'monitor.db.gz', 'monitor.db'])
def test_failure_while_moving_any_old_file_preserves_the_prior_generation(tmp_path, monkeypatch, filename):
    db, destination, _ = export(tmp_path)
    paths = (destination, destination.parent / portable.PAYLOAD_NAME, destination.parent / portable.MANIFEST_NAME)
    before = {path: path.read_bytes() for path in paths}
    replace = Path.replace
    failed = False

    def fail_once(self, target):
        nonlocal failed
        if self == destination.parent / filename and str(target).endswith('.previous') and not failed:
            failed = True
            raise OSError('simulated backup-stage interruption')
        return replace(self, target)

    monkeypatch.setattr(Path, 'replace', fail_once)
    with pytest.raises(OSError, match='backup-stage interruption'):
        portable.export_database(db, destination)
    assert failed
    assert {path: path.read_bytes() for path in paths} == before
    validate_export(destination.parent)
    assert not list(destination.parent.glob('.checkpoint-*'))


def test_checkpoint_cannot_compact_or_replace_its_live_source(tmp_path):
    db = source_database(tmp_path)
    before = Path(db.path).read_bytes()
    with pytest.raises(ValueError, match='separate from its live source'):
        portable.export_database(db, Path(db.path))
    assert Path(db.path).read_bytes() == before


def test_transport_capacity_and_disk_capacity_fail_without_changing_prior_generation(tmp_path, monkeypatch):
    db, destination, _ = export(tmp_path)
    paths = (destination, destination.parent / portable.PAYLOAD_NAME, destination.parent / portable.MANIFEST_NAME)
    before = {path: path.read_bytes() for path in paths}
    with pytest.raises(ValueError, match='transport bound'):
        portable.export_database(db, destination, max_payload_bytes=20)
    assert {path: path.read_bytes() for path in paths} == before
    monkeypatch.setattr(shutil, 'disk_usage', lambda path: shutil._ntuple_diskusage(100, 100, 0))
    with pytest.raises(ValueError, match='disk capacity'):
        portable.export_database(db, destination)
    assert {path: path.read_bytes() for path in paths} == before
    assert not list(destination.parent.glob('.checkpoint-*'))


def test_export_validation_rejects_a_mixed_or_missing_generation(tmp_path):
    _, destination, _ = export(tmp_path)
    (destination.parent / portable.PAYLOAD_NAME).write_bytes(gzip.compress(b'other bytes'))
    with pytest.raises(ValueError):
        validate_export(destination.parent)
    (destination.parent / portable.MANIFEST_NAME).unlink()
    with pytest.raises(FileNotFoundError):
        validate_export(destination.parent)


def test_legacy_single_database_zip_is_still_supported_and_bounded_separately(tmp_path, monkeypatch):
    _, destination, _ = export(tmp_path)
    archive = tmp_path / 'legacy.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        output.write(destination, 'monitor.db')
    restored = tmp_path / 'restored.db'
    assert portable.restore_archive(archive, restored)
    assert restored.read_bytes() == destination.read_bytes()
    monkeypatch.setattr(portable, 'MAX_LEGACY_BYTES', 100)
    preserved_restore(tmp_path, archive)
    assert MAX_ARCHIVE_BYTES == MAX_LEGACY_BYTES < MAX_CHECKPOINT_BYTES


def test_database_over_former_250mb_raw_limit_roundtrips_without_losing_history(tmp_path):
    db = source_database(tmp_path)
    with db.connect() as conn:
        conn.execute('CREATE TABLE large_history(payload BLOB NOT NULL)')
        conn.execute('INSERT INTO large_history VALUES(zeroblob(?))', (MAX_LEGACY_BYTES,))
    destination = tmp_path / 'export' / 'monitor.db'
    record = portable.export_database(db, destination)
    assert record['database']['bytes'] > MAX_LEGACY_BYTES
    assert record['payload']['bytes'] < MAX_ARCHIVE_BYTES
    restored = tmp_path / 'restored.db'
    portable.restore_archive(bundle(tmp_path, destination.parent), restored)
    with Database(restored).connect() as conn:
        assert conn.execute('SELECT length(payload) FROM large_history').fetchone()[0] == MAX_LEGACY_BYTES
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == record['database']['sha256']
