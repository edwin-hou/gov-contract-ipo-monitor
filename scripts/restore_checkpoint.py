"""Restore only the expected SQLite checkpoint; never extract arbitrary ZIP paths."""
import sqlite3
import zipfile
from pathlib import Path

from contract_ipo_monitor.checkpoint import (
    CORE_SCHEMA, MAX_CHECKPOINT_BYTES, SQLITE_HEADER, validate_database,
)


def restore(archive: Path, target: Path) -> bool:
    if not archive.exists():
        return False
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if (len(entries) != 1 or entries[0].filename != "monitor.db"
                or not 100 <= entries[0].file_size <= MAX_CHECKPOINT_BYTES):
            raise ValueError("Unexpected or oversized checkpoint artifact")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".restore")
        try:
            written = 0
            with bundle.open(entries[0]) as source, temporary.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_CHECKPOINT_BYTES:
                        raise ValueError("Checkpoint exceeds the uncompressed byte limit")
                    output.write(chunk)
            if written != entries[0].file_size:
                raise ValueError("Checkpoint archive size does not match its contents")
            validate_database(temporary, max_bytes=MAX_CHECKPOINT_BYTES)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return True


if __name__ == "__main__":
    print("Restored prior collection history." if restore(Path("work/checkpoint.zip"), Path("data/monitor.db")) else "Starting initial collection history.")
