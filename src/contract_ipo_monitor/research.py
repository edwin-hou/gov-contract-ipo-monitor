"""Durable discourse evidence, collection receipts, and reviewable research reports."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .db import Database
from .sources.discourse import DiscourseBatch, DiscourseEvidence, evidence_from_dict


SCHEMA = """
CREATE TABLE IF NOT EXISTS discourse_evidence(
 id INTEGER PRIMARY KEY, evidence_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
 evidence_json TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 UNIQUE(evidence_id,payload_hash));
CREATE TABLE IF NOT EXISTS discourse_coverage(
 source TEXT NOT NULL, source_url TEXT NOT NULL, coverage_json TEXT NOT NULL,
 observed_at TEXT NOT NULL, PRIMARY KEY(source,source_url));
CREATE TABLE IF NOT EXISTS monitor_runs(
 id INTEGER PRIMARY KEY, completed_at TEXT NOT NULL, status TEXT NOT NULL, report_json TEXT NOT NULL);
"""


def serializable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item)))


class ResearchStore:
    def __init__(self, db: Database):
        self.db = db

    def initialize(self) -> None:
        with closing(self.db.connect()) as conn:
            conn.executescript(SCHEMA)

    def record_batch(self, batch: DiscourseBatch) -> int:
        inserted = 0
        with self.db.transaction() as conn:
            for evidence in batch.records:
                payload = serializable(asdict(evidence))
                # Retrieval time is an observation, never a new content version.
                content = {key: value for key, value in payload.items() if key != "retrieved_at"}
                digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
                stamp = evidence.retrieved_at.isoformat()
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO discourse_evidence(evidence_id,payload_hash,evidence_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    (evidence.evidence_id, digest, json.dumps(payload, sort_keys=True), stamp, stamp),
                )
                inserted += int(bool(cursor.rowcount))
                conn.execute("UPDATE discourse_evidence SET last_seen_at=MAX(last_seen_at,?) WHERE evidence_id=? AND payload_hash=?", (stamp, evidence.evidence_id, digest))
            conn.execute("DELETE FROM discourse_coverage")
            for coverage in batch.coverage:
                conn.execute(
                    "INSERT INTO discourse_coverage(source,source_url,coverage_json,observed_at) VALUES(?,?,?,?) ON CONFLICT(source,source_url) DO UPDATE SET coverage_json=excluded.coverage_json,observed_at=excluded.observed_at",
                    (coverage.source, coverage.source_url, json.dumps(serializable(asdict(coverage))), coverage.observed_at.isoformat()),
                )
        return inserted

    def records(self, *, limit: int = 10000) -> list[DiscourseEvidence]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT evidence_json,last_seen_at FROM discourse_evidence ORDER BY last_seen_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        seen = set()
        for row in rows:
            data = json.loads(row["evidence_json"])
            if data["evidence_id"] in seen:
                continue
            seen.add(data["evidence_id"])
            data["retrieved_at"] = row["last_seen_at"]
            result.append(evidence_from_dict(data))
        return result

    def coverage(self) -> list[dict[str, Any]]:
        with closing(self.db.connect()) as conn:
            return [json.loads(row[0]) for row in conn.execute("SELECT coverage_json FROM discourse_coverage ORDER BY source,source_url")]

    def save_run(self, report: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO monitor_runs(completed_at,status,report_json) VALUES(?,?,?)", (report["completed_at"], report["status"], json.dumps(serializable(report))))
            conn.execute("DELETE FROM monitor_runs WHERE completed_at < ?", ((datetime.fromisoformat(report["completed_at"]) - timedelta(days=365)).isoformat(),))

    def latest_run(self) -> dict[str, Any] | None:
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT report_json FROM monitor_runs ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None


def report_markdown(report: dict[str, Any]) -> str:
    def clean(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;").replace(">", "&gt;")
    lines = ["# IPO and sentiment monitor", "", f"Run: {clean(report['completed_at'])} · **{clean(report['status'])}**", "",
             "IPO status comes from regulatory evidence. Sentiment describes the collected sample and never confirms an IPO or predicts returns.", "",
             "## Collection", "", "| Source | Status | Details |", "|---|---|---|"]
    for name, state in report.get("health", {}).get("collectors", {}).items():
        lines.append(f"| {clean(name)} | {'ok' if state.get('ok') else 'degraded'} | {clean(state.get('error') or state.get('last_success_at') or '')} |")
    lines += ["", f"New evidence this run: {clean(report.get('counts', {}))}", "", "## IPO evidence", "", "| Company | Stage | Evidence confidence | Source |", "|---|---|---|---|"]
    for candidate in report.get("ipos", []):
        url = next(iter(candidate.get("source_urls", [])), "")
        lines.append(f"| {clean(candidate['issuer_name'])} | {clean(candidate['status'])} | {clean(candidate['confidence'])} | {clean(url)} |")
    if not report.get("ipos"):
        lines += ["", "No IPO evidence has been collected yet. Check collection coverage before interpreting an empty result."]
    lines += ["", "## Sentiment sample", ""]
    for company in report.get("sentiment", []):
        lines += [f"### {clean(company.get('company_name', company.get('company', 'Company')))}", "", "```json", json.dumps(company, indent=2, ensure_ascii=False), "```", ""]
    lines += ["## Source coverage", "", "| Source | Status | Items | Limitation or error |", "|---|---|---|---|"]
    for source in report.get("coverage", []):
        detail = source.get("error") or "; ".join(source.get("limitations", []))
        lines.append(f"| {clean(source['source'])} | {clean(source['status'])} | {source.get('collected_count', 0)} | {clean(detail)} |")
    lines += ["", "## Limitations", "", *[f"- {clean(item)}" for item in report.get("limitations", [])], ""]
    return "\n".join(lines)


def write_report(report: dict[str, Any], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in (("latest.json", json.dumps(serializable(report), indent=2, ensure_ascii=False)), ("latest.md", report_markdown(report))):
        target = directory / name
        temporary = directory / f".{name}.{os.getpid()}.tmp"
        temporary.write_text(content + "\n", encoding="utf-8")
        temporary.replace(target)


def checkpoint_database(db: Database, destination: Path) -> None:
    """SQLite backup includes committed WAL data; copying the .db alone does not."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    with closing(db.connect()) as source, closing(sqlite3.connect(temporary)) as target:
        source.backup(target)
    temporary.replace(destination)
