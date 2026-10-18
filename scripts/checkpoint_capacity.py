"""Read-only capacity evidence; never expose source payloads or repair state."""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def capacity(path: Path) -> dict:
    if not path.exists():
        return {"database_exists": False}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {"database_exists": True, "database_bytes": path.stat().st_size,
                  "page_count": conn.execute("PRAGMA page_count").fetchone()[0],
                  "freelist_pages": conn.execute("PRAGMA freelist_count").fetchone()[0]}
        if "monitor_runs" in names:
            count, raw = conn.execute("SELECT COUNT(*),COALESCE(SUM(length(CAST(report_json AS BLOB))),0) FROM monitor_runs").fetchone()
            result.update(retained_reports=count, legacy_report_text_bytes=raw)
        if "monitor_run_payloads" in names:
            result["compressed_report_rows"] = conn.execute("SELECT COUNT(*) FROM monitor_run_payloads").fetchone()[0]
        if "monitor_run_pack_refs" in names:
            result["packed_report_rows"] = conn.execute("SELECT COUNT(*) FROM monitor_run_pack_refs").fetchone()[0]
            result["compressed_report_rows"] = result.get("compressed_report_rows", 0) + result["packed_report_rows"]
        if "monitor_report_packs" in names:
            count, compressed, expanded = conn.execute("SELECT COUNT(*),COALESCE(SUM(length(payload_xz)),0),COALESCE(SUM(original_bytes),0) FROM monitor_report_packs").fetchone()
            result.update(report_pack_count=count, report_pack_compressed_bytes=compressed,
                          report_pack_expanded_bytes=expanded)
        try:
            result["largest_tables_bytes"] = dict(conn.execute("SELECT name,SUM(pgsize) AS bytes FROM dbstat GROUP BY name ORDER BY bytes DESC LIMIT 10"))
        except sqlite3.OperationalError:
            result["table_allocation_available"] = False
    return result


if __name__ == "__main__":
    path = Path(sys.argv[1])
    print("Checkpoint capacity before collection: " + json.dumps(capacity(path), sort_keys=True))
    if len(sys.argv) == 4 and sys.argv[2] == "--baseline":
        from contract_ipo_monitor.checkpoint_history import history_receipts
        receipts = history_receipts(path)
        Path(sys.argv[3]).write_text(json.dumps(receipts), encoding="utf-8")
        print(f"Captured original-byte receipts for {len(receipts)} retained reports.")
