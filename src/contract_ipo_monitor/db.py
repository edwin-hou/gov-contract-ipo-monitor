from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import AlertPayload, Candidate, GateDecision

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_records(
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL, observed_at TEXT NOT NULL, UNIQUE(source, external_id, payload_hash));
CREATE TABLE IF NOT EXISTS contract_evidence(id INTEGER PRIMARY KEY, source_record_id INTEGER, award_id TEXT, version_json TEXT NOT NULL, supersedes_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listing_signals(id INTEGER PRIMARY KEY, source_record_id INTEGER, signal_id TEXT, version_json TEXT NOT NULL, supersedes_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities(id INTEGER PRIMARY KEY, legal_name TEXT NOT NULL, cik TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entity_identifiers(id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, source_url TEXT, UNIQUE(kind,value));
CREATE TABLE IF NOT EXISTS entity_relationships(id INTEGER PRIMARY KEY, from_entity_id INTEGER, to_entity_id INTEGER, relationship_type TEXT, source_url TEXT, verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS market_snapshots(id INTEGER PRIMARY KEY, symbol TEXT, snapshot_json TEXT NOT NULL, quote_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_findings(id INTEGER PRIMARY KEY, candidate_fingerprint TEXT, category TEXT, severity TEXT, finding TEXT, source_url TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidate_matches(id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, contract_json TEXT NOT NULL, listing_json TEXT NOT NULL, market_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gate_decisions(id INTEGER PRIMARY KEY, candidate_id INTEGER NOT NULL, gate TEXT NOT NULL, passed INTEGER NOT NULL, code TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, company_name TEXT NOT NULL, award_id TEXT NOT NULL, signal_id TEXT NOT NULL, subject TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued');
CREATE TABLE IF NOT EXISTS outbox_messages(
 id INTEGER PRIMARY KEY, alert_id INTEGER NOT NULL, subject TEXT NOT NULL, text_body TEXT NOT NULL, html_body TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
 lease_until TEXT, last_error TEXT, sent_at TEXT, smtp_message_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collector_state(name TEXT PRIMARY KEY, cursor TEXT, last_success_at TEXT, last_error TEXT, disabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dead_letters(id INTEGER PRIMARY KEY, component TEXT NOT NULL, source TEXT, external_id TEXT, payload_json TEXT, error TEXT NOT NULL, created_at TEXT NOT NULL);
"""

TABLES = {
    "source_records", "contract_evidence", "listing_signals", "entities", "entity_identifiers",
    "entity_relationships", "market_snapshots", "risk_findings", "candidate_matches", "gate_decisions",
    "alerts", "outbox_messages", "collector_state", "dead_letters",
}


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class InsertResult:
    inserted: bool
    row_id: int | None


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)", (datetime.now(UTC).isoformat(),))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def insert_source_record(self, source: str, external_id: str, payload: dict[str, Any], *, observed_at: datetime) -> InsertResult:
        raw = _json(payload)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO source_records(source, external_id, payload_hash, payload_json, observed_at) VALUES(?,?,?,?,?)",
                (source, external_id, digest, raw, observed_at.isoformat()),
            )
            if cursor.rowcount:
                return InsertResult(True, cursor.lastrowid)
            row = conn.execute("SELECT id FROM source_records WHERE source=? AND external_id=? AND payload_hash=?", (source, external_id, digest)).fetchone()
            return InsertResult(False, row["id"] if row else None)

    def count(self, table: str) -> int:
        if table not in TABLES:
            raise ValueError("unknown table")
        with self.connect() as conn:
            return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

    def fetch_outbox(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM outbox_messages WHERE status=? ORDER BY id", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM outbox_messages ORDER BY id").fetchall()
            return [dict(row) for row in rows]

    def save_evaluation(self, candidate: Candidate, decisions: Sequence[GateDecision], payload: AlertPayload | None, *, created_at: datetime) -> tuple[bool, bool]:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO candidate_matches(fingerprint, contract_json, listing_json, market_json, created_at) VALUES(?,?,?,?,?)",
                (
                    payload.fingerprint if payload else self._candidate_trace_fingerprint(candidate, created_at),
                    _json(candidate.contract), _json(candidate.listing), _json(candidate.market) if candidate.market else None,
                    created_at.isoformat(),
                ),
            )
            candidate_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO gate_decisions(candidate_id, gate, passed, code, reason, created_at) VALUES(?,?,?,?,?,?)",
                [(candidate_id, d.gate, int(d.passed), d.code, d.reason, created_at.isoformat()) for d in decisions],
            )
            if payload is None:
                return False, False
            existing = conn.execute("SELECT id FROM alerts WHERE fingerprint=?", (payload.fingerprint,)).fetchone()
            if existing:
                return False, True
            alert = conn.execute(
                "INSERT INTO alerts(fingerprint, company_name, award_id, signal_id, subject, created_at) VALUES(?,?,?,?,?,?)",
                (payload.fingerprint, payload.company_name, payload.award_id, payload.signal_id, payload.subject, created_at.isoformat()),
            )
            conn.execute(
                "INSERT INTO outbox_messages(alert_id, subject, text_body, html_body, next_attempt_at, created_at) VALUES(?,?,?,?,?,?)",
                (alert.lastrowid, payload.subject, payload.text_body, payload.html_body, created_at.isoformat(), created_at.isoformat()),
            )
            return True, False

    @staticmethod
    def _candidate_trace_fingerprint(candidate: Candidate, created_at: datetime) -> str:
        raw = f"rejected|{candidate.contract.award_id}|{candidate.listing.signal_id}|{created_at.isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def lease_outbox(self, *, now: datetime, lease_for: Any = None) -> dict[str, Any] | None:
        from datetime import timedelta
        lease_for = lease_for or timedelta(seconds=30)
        lease_until = now + lease_for
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM outbox_messages
                WHERE status IN ('pending','leased')
                  AND next_attempt_at <= ?
                  AND (lease_until IS NULL OR lease_until <= ?)
                ORDER BY id LIMIT 1
                """,
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE outbox_messages SET status='leased', lease_until=? WHERE id=?",
                (lease_until.isoformat(), row["id"]),
            )
            result = dict(row)
            result["status"] = "leased"
            result["lease_until"] = lease_until.isoformat()
            return result

    def mark_outbox_sent(self, message_id: int, *, sent_at: datetime, smtp_message_id: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE outbox_messages SET status='sent', sent_at=?, smtp_message_id=?, lease_until=NULL, last_error=NULL WHERE id=?",
                (sent_at.isoformat(), smtp_message_id, message_id),
            )
            conn.execute(
                "UPDATE alerts SET status='sent' WHERE id=(SELECT alert_id FROM outbox_messages WHERE id=?)",
                (message_id,),
            )

    def mark_outbox_failure(self, message_id: int, *, failed_at: datetime, error: str, max_attempts: int, next_attempt_at: datetime) -> bool:
        with self.transaction() as conn:
            row = conn.execute("SELECT attempts, subject, text_body FROM outbox_messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"]) + 1
            dead = attempts >= max_attempts
            status = "dead" if dead else "pending"
            conn.execute(
                "UPDATE outbox_messages SET status=?, attempts=?, next_attempt_at=?, lease_until=NULL, last_error=? WHERE id=?",
                (status, attempts, next_attempt_at.isoformat(), error[:2000], message_id),
            )
            if dead:
                conn.execute(
                    "INSERT INTO dead_letters(component, source, external_id, payload_json, error, created_at) VALUES(?,?,?,?,?,?)",
                    ("smtp", "outbox", str(message_id), _json({"subject": row["subject"], "text_body": row["text_body"]}), error[:2000], failed_at.isoformat()),
                )
            return dead

    def store_contract_evidence(self, evidence: Any, *, source_record_id: int | None = None, created_at: datetime) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO contract_evidence(source_record_id, award_id, version_json, created_at) VALUES(?,?,?,?)",
                (source_record_id, evidence.award_id, _json(evidence), created_at.isoformat()),
            )
            return int(cursor.lastrowid)

    def store_listing_signal(self, signal: Any, *, source_record_id: int | None = None, created_at: datetime) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO listing_signals(source_record_id, signal_id, version_json, created_at) VALUES(?,?,?,?)",
                (source_record_id, signal.signal_id, _json(signal), created_at.isoformat()),
            )
            return int(cursor.lastrowid)

    def load_contracts(self) -> list[Any]:
        from .models import ContractEvidence
        with self.connect() as conn:
            rows = conn.execute("SELECT version_json FROM contract_evidence ORDER BY id DESC").fetchall()
        seen: set[tuple[str, str]] = set()
        result: list[Any] = []
        for row in rows:
            item = ContractEvidence.model_validate_json(row["version_json"])
            key = (item.award_id, item.modification_number)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    def load_listing_signals(self) -> list[Any]:
        from .models import ListingSignal
        with self.connect() as conn:
            rows = conn.execute("SELECT version_json FROM listing_signals ORDER BY id DESC").fetchall()
        seen: set[str] = set()
        result: list[Any] = []
        for row in rows:
            item = ListingSignal.model_validate_json(row["version_json"])
            if item.signal_id not in seen:
                seen.add(item.signal_id)
                result.append(item)
        return result

    def enqueue_correction(self, *, signal_id: str | None = None, award_id: str | None = None, company_name: str | None = None, reason: str, source_url: str, created_at: datetime) -> int:
        if not signal_id and not award_id and not company_name:
            return 0
        with self.transaction() as conn:
            clauses: list[str] = []
            params: list[Any] = []
            if signal_id:
                clauses.append("signal_id=?")
                params.append(signal_id)
            if award_id:
                clauses.append("award_id=?")
                params.append(award_id)
            if company_name:
                clauses.append("company_name=?")
                params.append(company_name)
            rows = conn.execute(f"SELECT * FROM alerts WHERE {' OR '.join(clauses)}", params).fetchall()
            inserted = 0
            for row in rows:
                marker = f"correction:{row['id']}:{reason}:{source_url}"
                duplicate = conn.execute("SELECT 1 FROM outbox_messages WHERE text_body LIKE ?", (f"%{marker}%",)).fetchone()
                if duplicate:
                    continue
                subject = f"[CORRECTION] {row['subject']}"
                body = (
                    f"Correction to confirmed alert for {row['company_name']} / award {row['award_id']}.\n\n"
                    f"What changed: {reason}\nPrimary source: {source_url}\nObserved: {created_at.isoformat()}\n\n"
                    "The original evidence trail is preserved. Reassess any investment thesis independently.\n"
                    f"Internal marker: {marker}"
                )
                conn.execute(
                    "INSERT INTO outbox_messages(alert_id, subject, text_body, html_body, next_attempt_at, created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], subject, body, f"<html><body><pre>{body}</pre></body></html>", created_at.isoformat(), created_at.isoformat()),
                )
                conn.execute("UPDATE alerts SET status='corrected' WHERE id=?", (row["id"],))
                inserted += 1
            return inserted


    def update_collector_state(self, name: str, *, cursor: str | None = None, success_at: datetime | None = None, error: str | None = None, disabled: bool = False) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO collector_state(name, cursor, last_success_at, last_error, disabled, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                  cursor=COALESCE(excluded.cursor, collector_state.cursor),
                  last_success_at=COALESCE(excluded.last_success_at, collector_state.last_success_at),
                  last_error=excluded.last_error, disabled=excluded.disabled, updated_at=excluded.updated_at""",
                (name, cursor, success_at.isoformat() if success_at else None, error, int(disabled), now),
            )

    def add_dead_letter(self, *, component: str, source: str | None, external_id: str | None, payload: Any, error: str, created_at: datetime) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO dead_letters(component, source, external_id, payload_json, error, created_at) VALUES(?,?,?,?,?,?)",
                (component, source, external_id, _json(payload), error[:2000], created_at.isoformat()),
            )
            return int(cursor.lastrowid)
