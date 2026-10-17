"""Restore only the expected SQLite checkpoint; never extract arbitrary ZIP paths."""
import sqlite3
from pathlib import Path

from contract_ipo_monitor.checkpoint import (
    CORE_SCHEMA, MAX_ARCHIVE_BYTES, MAX_CHECKPOINT_BYTES, MAX_LEGACY_BYTES,
    SQLITE_HEADER, validate_database,
)
from contract_ipo_monitor.checkpoint_archive import restore_archive


def restore(archive: Path, target: Path) -> bool:
    return restore_archive(archive, target)


if __name__ == "__main__":
    print("Restored prior collection history." if restore(Path("work/checkpoint.zip"), Path("data/monitor.db")) else "Starting initial collection history.")
