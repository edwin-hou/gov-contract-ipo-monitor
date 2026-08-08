from __future__ import annotations

import json
from html import escape
from typing import Any

from .db import Database


def recent_candidates(db: Database, *, limit: int = 50, rejected_only: bool = False) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with db.connect() as conn:
        rows = conn.execute(
            """
            WITH recent AS (
                SELECT * FROM candidate_matches ORDER BY id DESC LIMIT ?
            )
            SELECT r.id, r.fingerprint, r.contract_json, r.listing_json, r.created_at,
                   gd.gate, gd.passed, gd.code, gd.reason,
                   a.id AS alert_id, a.status AS alert_status
            FROM recent r
            LEFT JOIN gate_decisions gd ON gd.candidate_id = r.id
            LEFT JOIN alerts a ON a.fingerprint = r.fingerprint
            ORDER BY r.id DESC, gd.id ASC
            """,
            (limit,),
        ).fetchall()

    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        candidate_id = int(row["id"])
        item = grouped.get(candidate_id)
        if item is None:
            contract = json.loads(row["contract_json"])
            listing = json.loads(row["listing_json"])
            item = {
                "id": candidate_id,
                "created_at": row["created_at"],
                "company": listing.get("issuer_name"),
                "recipient": contract.get("recipient_name"),
                "award_id": contract.get("award_id"),
                "agency": contract.get("agency"),
                "signal_id": listing.get("signal_id"),
                "route": listing.get("route"),
                "ticker": listing.get("ticker"),
                "source_url": listing.get("source_url"),
                "alert_id": row["alert_id"],
                "alert_status": row["alert_status"],
                "decisions": [],
            }
            grouped[candidate_id] = item
        if row["gate"] is not None:
            item["decisions"].append(
                {
                    "gate": row["gate"],
                    "passed": bool(row["passed"]),
                    "code": row["code"],
                    "reason": row["reason"],
                }
            )

    result: list[dict[str, Any]] = []
    for item in grouped.values():
        failed = [d["gate"] for d in item["decisions"] if not d["passed"]]
        if item["alert_id"]:
            outcome = "alerted"
        elif failed:
            outcome = "rejected"
        else:
            outcome = "qualified_duplicate"
        item["outcome"] = outcome
        item["failed_gates"] = failed
        if not rejected_only or outcome == "rejected":
            result.append(item)
    return result


def collector_states(db: Database) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM collector_state ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def recent_alerts(db: Database, *, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, company_name, award_id, signal_id, subject, created_at, status FROM alerts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def dashboard_html(db: Database, *, limit: int = 50) -> str:
    candidates = recent_candidates(db, limit=limit)
    states = collector_states(db)
    alerts = recent_alerts(db, limit=10)

    collector_rows = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('name') or ''))}</td>"
        f"<td>{escape(str(row.get('last_success_at') or 'never'))}</td>"
        f"<td>{escape(str(row.get('last_error') or ''))}</td>"
        f"<td>{'yes' if row.get('disabled') else 'no'}</td>"
        "</tr>"
        for row in states
    ) or '<tr><td colspan="4">No collector state recorded yet.</td></tr>'

    candidate_rows = []
    for item in candidates:
        failed = ", ".join(item["failed_gates"]) or "—"
        decision_text = "; ".join(
            f"{d['gate']}: {d['code']}" for d in item["decisions"] if not d["passed"]
        ) or "all gates passed"
        candidate_rows.append(
            "<tr>"
            f"<td>{escape(str(item['created_at']))}</td>"
            f"<td>{escape(str(item['company'] or ''))}</td>"
            f"<td>{escape(str(item['award_id'] or ''))}</td>"
            f"<td>{escape(str(item['route'] or ''))}</td>"
            f"<td><span class='pill {escape(item['outcome'])}'>{escape(item['outcome'])}</span></td>"
            f"<td>{escape(failed)}</td>"
            f"<td>{escape(decision_text)}</td>"
            "</tr>"
        )
    candidate_html = "".join(candidate_rows) or '<tr><td colspan="7">No candidate evaluations yet.</td></tr>'

    alert_rows = "".join(
        "<tr>"
        f"<td>{escape(str(row['created_at']))}</td>"
        f"<td>{escape(str(row['company_name']))}</td>"
        f"<td>{escape(str(row['award_id']))}</td>"
        f"<td>{escape(str(row['status']))}</td>"
        "</tr>"
        for row in alerts
    ) or '<tr><td colspan="4">No alerts yet.</td></tr>'

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>IPO Monitor Dashboard</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;margin:32px;background:#f6f7f9;color:#15171a}}
main{{max-width:1400px;margin:auto}} h1,h2{{margin-bottom:8px}}
.card{{background:white;border:1px solid #ddd;border-radius:12px;padding:18px;margin:18px 0;overflow:auto}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{padding:9px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:white}} .pill{{padding:3px 7px;border-radius:999px;background:#eee}}
.alerted{{background:#d7f5df}} .rejected{{background:#ffe0e0}} .qualified_duplicate{{background:#fff1c7}}
small{{color:#666}}
</style></head><body><main>
<h1>Government Contract + IPO Monitor</h1><small>Read-only operational view. No trading recommendations.</small>
<div class="card"><h2>Collectors</h2><table><thead><tr><th>Collector</th><th>Last success</th><th>Last error</th><th>Disabled</th></tr></thead><tbody>{collector_rows}</tbody></table></div>
<div class="card"><h2>Recent candidate evaluations</h2><table><thead><tr><th>Observed</th><th>Company</th><th>Award</th><th>Route</th><th>Outcome</th><th>Failed gates</th><th>Reason</th></tr></thead><tbody>{candidate_html}</tbody></table></div>
<div class="card"><h2>Recent confirmed alerts</h2><table><thead><tr><th>Created</th><th>Company</th><th>Award</th><th>Status</th></tr></thead><tbody>{alert_rows}</tbody></table></div>
</main></body></html>"""
