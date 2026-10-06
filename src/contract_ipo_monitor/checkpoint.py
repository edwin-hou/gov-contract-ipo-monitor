"""The portable SQLite contract shared by checkpoint publication and restore."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


MAX_CHECKPOINT_BYTES = 250_000_000
SQLITE_HEADER = b"SQLite format 3\x00"
# These tables and columns belong to the original monitor schema. Newer optional
# collectors are initialized after restoration, so they cannot be prerequisites.
CORE_SCHEMA = {
    "schema_migrations": {"version", "applied_at"},
    "source_records": {"id", "source", "external_id", "payload_hash", "payload_json", "observed_at"},
    "contract_evidence": {"id", "source_record_id", "award_id", "version_json", "supersedes_id", "created_at"},
    "listing_signals": {"id", "source_record_id", "signal_id", "version_json", "supersedes_id", "created_at"},
    "collector_state": {"name", "cursor", "last_success_at", "last_error", "disabled", "updated_at"},
}


def validate_database(path: Path, *, max_bytes: int = MAX_CHECKPOINT_BYTES) -> None:
    """Validate an existing backup without creating, migrating or repairing it."""
    if not 100 <= path.stat().st_size <= max_bytes:
        raise ValueError("Checkpoint is empty or exceeds the portable archive byte bound")
    # SQLite treats a zero-byte file as a new, valid empty database. Verify the
    # existing file before opening it, and never let validation create or repair it.
    with path.open("rb") as source:
        if source.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise ValueError("Checkpoint is not an existing SQLite database")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("Checkpoint SQLite integrity check failed")
            for table, expected in CORE_SCHEMA.items():
                if conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone() != ("table",):
                    raise ValueError(f"Checkpoint monitor schema is missing table {table}")
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                if not expected <= columns:
                    raise ValueError(f"Checkpoint monitor schema is incomplete for table {table}")
    except sqlite3.DatabaseError as exc:
        raise ValueError("Checkpoint SQLite integrity check failed") from exc
