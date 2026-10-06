from pathlib import Path
from contextlib import closing
import sqlite3

import pytest

from contract_ipo_monitor.checkpoint import SQLITE_HEADER, validate_database
from contract_ipo_monitor.db import Database
from contract_ipo_monitor import research

def test_oversized_backup_cannot_publish_or_replace_a_usable_checkpoint(tmp_path, monkeypatch):
    db = Database(tmp_path/'source.db')
    db.initialize()
    destination = tmp_path/'snapshot.db'
    destination.write_bytes(b'preceding usable snapshot')
    monkeypatch.setattr(research, 'MAX_CHECKPOINT_BYTES', 100)
    with pytest.raises(ValueError, match='portable'):
        research.checkpoint_database(db, destination)
    assert destination.read_bytes() == b'preceding usable snapshot'
    assert not destination.with_suffix('.tmp').exists()


def valid_checkpoint(tmp_path):
    db = Database(tmp_path / 'previous.db')
    db.initialize()
    db.update_collector_state('sec', cursor='retained progress')
    destination = tmp_path / 'snapshot.db'
    research.checkpoint_database(db, destination)
    return destination, destination.read_bytes()


@pytest.mark.parametrize('mutation', ['uninitialized', 'unrelated', 'missing_table', 'missing_column', 'view'])
def test_backup_of_wrong_schema_preserves_preceding_checkpoint(tmp_path, mutation):
    destination, before = valid_checkpoint(tmp_path)
    source = Database(tmp_path / 'source.db')
    if mutation not in {'uninitialized', 'unrelated'}:
        source.initialize()
    with source.connect() as conn:
        if mutation == 'unrelated':
            conn.execute('CREATE TABLE unrelated(id INTEGER PRIMARY KEY)')
        elif mutation == 'missing_table':
            conn.execute('DROP TABLE collector_state')
        elif mutation == 'missing_column':
            conn.execute('ALTER TABLE collector_state DROP COLUMN cursor')
        elif mutation == 'view':
            conn.execute('DROP TABLE collector_state')
            conn.execute('CREATE VIEW collector_state AS SELECT 1 AS name')

    with pytest.raises(ValueError, match='monitor schema'):
        research.checkpoint_database(source, destination)
    assert destination.read_bytes() == before
    assert not destination.with_suffix('.tmp').exists()
    with Database(destination).connect() as conn:
        assert conn.execute('SELECT cursor FROM collector_state WHERE name=?', ('sec',)).fetchone()[0] == 'retained progress'


@pytest.mark.parametrize('payload', [b'', b'not SQLite' * 50, SQLITE_HEADER + b'\x00' * 496])
def test_damaged_backup_bytes_never_replace_preceding_checkpoint(tmp_path, monkeypatch, payload):
    destination, before = valid_checkpoint(tmp_path)
    source = Database(tmp_path / 'source.db')
    source.initialize()
    temporary = destination.with_suffix('.tmp')
    original_connect = sqlite3.connect

    # Model corruption between the real SQLite backup closing and publication.
    # The shared validator still opens the actual completed bytes read-only.
    class DamagedBackupConnection(sqlite3.Connection):
        def close(self):
            super().close()
            temporary.write_bytes(payload)

    def connect(path, *args, **kwargs):
        if not kwargs.get('uri') and str(path) == str(temporary):
            kwargs['factory'] = DamagedBackupConnection
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    with pytest.raises(ValueError):
        research.checkpoint_database(source, destination)
    assert destination.read_bytes() == before
    assert not temporary.exists()


def test_original_schema_backup_is_validated_readonly_without_optional_migrations(tmp_path, monkeypatch):
    from contract_ipo_monitor.db import SCHEMA

    source = tmp_path / 'original.db'
    with closing(sqlite3.connect(source)) as conn:
        conn.executescript(SCHEMA)
        conn.execute('INSERT INTO schema_migrations VALUES(1,?)', ('2026-10-05T00:00:00+00:00',))
        conn.commit()
    before = source.read_bytes()
    destination = tmp_path / 'snapshot.db'
    original_connect = sqlite3.connect
    readonly_connections = []

    def connect(path, *args, **kwargs):
        if kwargs.get('uri'):
            readonly_connections.append((path, kwargs))
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    research.checkpoint_database(Database(source), destination)
    assert len(readonly_connections) == 1
    assert readonly_connections[0][0].endswith('.tmp?mode=ro')
    assert readonly_connections[0][1] == {'uri': True}
    assert source.read_bytes() == before
    validate_database(destination)
    assert not destination.with_suffix('.tmp').exists()

def test_failing_backup_preserves_target_and_removes_partial_file(tmp_path, monkeypatch):
    db = Database(tmp_path/'source.db')
    db.initialize()
    destination = tmp_path/'snapshot.db'
    destination.write_bytes(b'preceding usable snapshot')
    original = db.connect
    class BadSource:
        def backup(self, target):
            raise RuntimeError('simulated interrupted backup')
        def close(self):
            pass
    monkeypatch.setattr(db, 'connect', lambda: BadSource())
    with pytest.raises(RuntimeError, match='interrupted'):
        research.checkpoint_database(db, destination)
    assert destination.read_bytes() == b'preceding usable snapshot'
    assert not destination.with_suffix('.tmp').exists()
