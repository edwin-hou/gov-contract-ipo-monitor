"""Verify retained report bytes across real checkpoint upgrades and restores."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from .report_history import original_report_bytes, report_history_rows


def history_receipts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_runs'").fetchone():
            return []
        return [{"id": row["id"], "completed_at": row["completed_at"], "status": row["status"],
                 "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                for row in report_history_rows(conn) for raw in [original_report_bytes(row)]]


def verify_history(before: list[dict], path: Path) -> dict:
    after = history_receipts(path)
    indexed = {row["id"]: row for row in after}
    if before and not after:
        raise ValueError("Checkpoint lost all retained report history")
    cutoff = datetime.fromisoformat(after[-1]["completed_at"]) - timedelta(days=365) if after else None
    verified = expired = 0
    for receipt in before:
        if cutoff is not None and datetime.fromisoformat(receipt["completed_at"]) < cutoff:
            expired += 1
            continue
        if indexed.get(receipt["id"]) != receipt:
            raise ValueError("Checkpoint changed or lost original retained report bytes or metadata")
        verified += 1
    return {"preceding_reports_verified": verified, "expired_under_existing_365_day_policy": expired,
            "reports_in_export": len(after)}
