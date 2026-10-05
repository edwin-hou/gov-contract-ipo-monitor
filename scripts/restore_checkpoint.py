"""Restore only the expected SQLite checkpoint; never extract arbitrary ZIP paths."""
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path


def restore(archive: Path, target: Path) -> bool:
    if not archive.exists():
        return False
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) != 1 or entries[0].filename != "monitor.db" or entries[0].file_size > 250_000_000:
            raise ValueError("Unexpected or oversized checkpoint artifact")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".restore")
        with bundle.open(entries[0]) as source, temporary.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
    with closing(sqlite3.connect(temporary)) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Checkpoint SQLite integrity check failed")
    temporary.replace(target)
    return True


if __name__ == "__main__":
    print("Restored prior collection history." if restore(Path("work/checkpoint.zip"), Path("data/monitor.db")) else "Starting initial collection history.")
