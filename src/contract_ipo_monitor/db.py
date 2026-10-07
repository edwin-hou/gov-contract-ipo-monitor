from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
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
CREATE INDEX IF NOT EXISTS contract_evidence_source_record ON contract_evidence(source_record_id);
CREATE INDEX IF NOT EXISTS contract_evidence_source_identity_version ON contract_evidence(
 json_extract(version_json, '$.source'), json_extract(version_json, '$.source_record_id'), id DESC);
CREATE INDEX IF NOT EXISTS listing_signals_source_record ON listing_signals(source_record_id);
CREATE INDEX IF NOT EXISTS listing_signals_source_identity_version ON listing_signals(
 signal_id, json_extract(version_json, '$.source'), id DESC);
CREATE INDEX IF NOT EXISTS candidate_matches_fingerprint ON candidate_matches(fingerprint);
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


class ClosingConnection(sqlite3.Connection):
    """SQLite's normal context manager commits but does not close its handle."""

    def __exit__(self, *args: Any) -> bool:
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(outbox_messages)")}
            for name in ("pending_recipients_json", "accepted_recipients_json", "correction_key"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE outbox_messages ADD COLUMN {name} TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS outbox_correction_key ON outbox_messages(correction_key) WHERE correction_key IS NOT NULL")
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)", (datetime.now(UTC).isoformat(),))
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)", (datetime.now(UTC).isoformat(),))
            self._migrate_legacy_sec_feed_gaps(conn)

    @staticmethod
    def _migrate_legacy_sec_feed_gaps(conn: sqlite3.Connection) -> None:
        """Carry explicit old SEC truncation receipts into unresolved coverage state."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone():
                conn.execute("COMMIT")
                return
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_runs'").fetchone():
                rows = conn.execute(
                    """SELECT id,completed_at,
                       CASE WHEN json_valid(report_json) THEN json_extract(report_json, '$.health.collectors.sec.error') END AS sec_error
                       FROM monitor_runs ORDER BY id"""
                )
                known_forms = {"S-1", "S-1/A", "F-1", "F-1/A", "1-A", "1-A/A", "8-K", "8-K/A", "6-K", "6-K/A", "RW", "AW", "EFFECT", "424B4"}
                pattern = re.compile(r"(?:^|[;:]\s+)([A-Z0-9][A-Z0-9/-]*):\s*feed page limit reached;\s*older filings may be missing")
                for row in rows:
                    error = row["sec_error"]
                    if not isinstance(error, str) or "feed page limit reached; older filings may be missing" not in error:
                        continue
                    forms = {match.group(1) for match in pattern.finditer(error) if match.group(1) in known_forms}
                    for form in sorted(forms) if forms else ("legacy",):
                        conn.execute(
                            """INSERT OR IGNORE INTO collector_state(name,cursor,last_success_at,last_error,disabled,updated_at)
                               VALUES(?,?,NULL,?,0,?)""",
                            (f"sec_feed_gap:{form}", _json({"unresolved": True, "legacy_run_id": row["id"]}), error, row["completed_at"]),
                        )
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(3,?)", (datetime.now(UTC).isoformat(),))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
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
            if payload is not None:
                # The write transaction fences classification changes during
                # an awaited quote lookup. Recheck authority before queuing.
                if not self.listing_is_current_authorized(candidate.listing):
                    return False, False
            if payload is not None and conn.execute("SELECT 1 FROM alerts WHERE fingerprint=?", (payload.fingerprint,)).fetchone():
                return False, True
            trace_fingerprint = payload.fingerprint if payload else self._candidate_trace_fingerprint(candidate, decisions)
            if conn.execute("SELECT 1 FROM candidate_matches WHERE fingerprint=?", (trace_fingerprint,)).fetchone():
                return False, False
            cursor = conn.execute(
                "INSERT INTO candidate_matches(fingerprint, contract_json, listing_json, market_json, created_at) VALUES(?,?,?,?,?)",
                (
                    trace_fingerprint,
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
    def _candidate_trace_fingerprint(candidate: Candidate, decisions: Sequence[GateDecision]) -> str:
        normalized = candidate.model_dump(mode="json")
        normalized["contract"].pop("retrieved_at", None)
        normalized["contract"].pop("published_at", None)
        raw = _json({"candidate": normalized, "decisions": [item.model_dump(mode="json") for item in decisions]})
        return hashlib.sha256(raw.encode()).hexdigest()

    def lease_outbox(self, *, now: datetime, lease_for: Any = None) -> dict[str, Any] | None:
        from datetime import timedelta
        lease_for = lease_for or timedelta(seconds=30)
        lease_until = now + lease_for
        with self.transaction() as conn:
            # An interrupted worker may have submitted DATA before its receipt
            # was saved. Expiry cannot establish that the provider rejected it.
            conn.execute(
                """UPDATE outbox_messages SET status='unknown', lease_until=NULL,
                   last_error='smtp_interrupted_send'
                   WHERE status='leased' AND (lease_until IS NULL OR julianday(lease_until) IS NULL
                     OR julianday(lease_until) <= julianday(?))""", (now.isoformat(),),
            )
            while True:
                row = conn.execute(
                """
                SELECT message.*,alert.signal_id,trace.listing_json FROM outbox_messages message
                JOIN alerts alert ON alert.id=message.alert_id
                LEFT JOIN candidate_matches trace ON trace.fingerprint=alert.fingerprint
                WHERE message.status='pending'
                  AND julianday(message.next_attempt_at) <= julianday(?)
                  AND (message.lease_until IS NULL OR julianday(message.lease_until) <= julianday(?))
                ORDER BY message.id LIMIT 1
                """,
                (now.isoformat(), now.isoformat()),
                ).fetchone()
                if row is None:
                    return None
                # Corrections describe withdrawn evidence; they do not endorse
                # its original trade claim and retain their independent lease.
                if row["correction_key"] is not None:
                    break
                if row["listing_json"] is not None:
                    from .models import ListingSignal
                    try:
                        authorized = self.listing_is_current_authorized(ListingSignal.model_validate_json(row["listing_json"]))
                    except (ValueError, TypeError):
                        authorized = False
                else:
                    stored = [item for item in self.all_listing_signals() if item.signal_id == row["signal_id"]]
                    authorized = (not stored or any(item.signal_id == row["signal_id"] and item.active
                        for item in self.load_listing_signals())) and row["signal_id"] not in self.sec_issuer_reviews()
                if authorized:
                    break
                conn.execute("UPDATE outbox_messages SET status='cancelled',lease_until=NULL,last_error='listing_authority_withheld' WHERE id=?", (row["id"],))
                conn.execute("UPDATE alerts SET status='withheld' WHERE id=? AND status='queued'", (row["alert_id"],))
            conn.execute(
                "UPDATE outbox_messages SET status='leased', lease_until=? WHERE id=?",
                (lease_until.isoformat(), row["id"]),
            )
            result = dict(row)
            result["status"] = "leased"
            result["lease_until"] = lease_until.isoformat()
            return result

    def mark_outbox_unknown(self, message_id: int, *, failed_at: datetime, lease_until: str, error: str = "smtp_delivery_unknown") -> bool:
        """Fence an uncertain provider outcome; readback/manual review is required."""
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM outbox_messages WHERE id=?", (message_id,)).fetchone()
            if row is None or row["status"] != "leased" or row["lease_until"] != lease_until:
                return False
            conn.execute("UPDATE outbox_messages SET status='unknown', attempts=attempts+1, lease_until=NULL, last_error=? WHERE id=?",
                         (error, message_id))
            conn.execute(
                "INSERT INTO dead_letters(component, source, external_id, payload_json, error, created_at) VALUES(?,?,?,?,?,?)",
                ("smtp", "outbox", str(message_id), _json({"status": "unknown", "subject": row["subject"]}), error, failed_at.isoformat()),
            )
            return True

    def mark_outbox_sent(self, message_id: int, *, sent_at: datetime, smtp_message_id: str | None, lease_until: str | None = None) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT status, lease_until FROM outbox_messages WHERE id=?", (message_id,)).fetchone()
            if row is None or row["status"] != "leased" or (lease_until is not None and row["lease_until"] != lease_until):
                raise RuntimeError("outbox lease is no longer owned by this worker")
            conn.execute(
                "UPDATE outbox_messages SET status='sent', sent_at=?, smtp_message_id=?, lease_until=NULL, last_error=NULL WHERE id=?",
                (sent_at.isoformat(), smtp_message_id, message_id),
            )
            conn.execute(
                "UPDATE alerts SET status='sent' WHERE status != 'corrected' AND id=(SELECT alert_id FROM outbox_messages WHERE id=?)",
                (message_id,),
            )

    def mark_outbox_failure(self, message_id: int, *, failed_at: datetime, error: str, max_attempts: int, next_attempt_at: datetime, lease_until: str | None = None, refused_recipients: Sequence[str] | None = None, accepted_recipients: Sequence[str] = ()) -> bool:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM outbox_messages WHERE id=?", (message_id,)).fetchone()
            if row is None or row["status"] != "leased" or (lease_until is not None and row["lease_until"] != lease_until):
                return False
            attempts = int(row["attempts"]) + 1
            dead = attempts >= max_attempts
            status = "dead" if dead else "pending"
            conn.execute(
                "UPDATE outbox_messages SET status=?, attempts=?, next_attempt_at=?, lease_until=NULL, last_error=? WHERE id=?",
                (status, attempts, next_attempt_at.isoformat(), error[:2000], message_id),
            )
            if refused_recipients is not None:
                accepted = set(json.loads(row["accepted_recipients_json"] or "[]")) | set(accepted_recipients)
                conn.execute("UPDATE outbox_messages SET pending_recipients_json=?, accepted_recipients_json=? WHERE id=?", (_json(list(refused_recipients)), _json(sorted(accepted)), message_id))
            if dead:
                conn.execute(
                    "INSERT INTO dead_letters(component, source, external_id, payload_json, error, created_at) VALUES(?,?,?,?,?,?)",
                    ("smtp", "outbox", str(message_id), _json({"subject": row["subject"], "text_body": row["text_body"]}), error[:2000], failed_at.isoformat()),
                )
            return dead

    def evidence_record_exists(self, table: str, source_record_id: int | None) -> bool:
        if table not in {"contract_evidence", "listing_signals"}:
            raise ValueError("unsupported evidence table")
        with self.connect() as conn:
            return conn.execute(f"SELECT 1 FROM {table} WHERE source_record_id=?", (source_record_id,)).fetchone() is not None

    def store_contract_evidence(self, evidence: Any, *, source_record_id: int | None = None, created_at: datetime) -> int:
        with self.transaction() as conn:
            if source_record_id is not None:
                existing = conn.execute("SELECT id FROM contract_evidence WHERE source_record_id=?", (source_record_id,)).fetchone()
                if existing:
                    return int(existing["id"])
            previous = conn.execute("SELECT id FROM contract_evidence WHERE json_extract(version_json, '$.source')=? AND json_extract(version_json, '$.source_record_id')=? ORDER BY id DESC LIMIT 1", (evidence.source, evidence.source_record_id)).fetchone()
            cursor = conn.execute(
                "INSERT INTO contract_evidence(source_record_id, award_id, version_json, created_at, supersedes_id) VALUES(?,?,?,?,?)",
                (source_record_id, evidence.award_id, _json(evidence), created_at.isoformat(), previous["id"] if previous else None),
            )
            return int(cursor.lastrowid)

    def store_listing_signal(self, signal: Any, *, source_record_id: int | None = None, created_at: datetime) -> int:
        with self.transaction() as conn:
            if source_record_id is not None:
                existing = conn.execute("SELECT id FROM listing_signals WHERE source_record_id=?", (source_record_id,)).fetchone()
                if existing:
                    return int(existing["id"])
            previous = conn.execute("SELECT id FROM listing_signals WHERE signal_id=? AND json_extract(version_json, '$.source')=? ORDER BY id DESC LIMIT 1", (signal.signal_id, signal.source)).fetchone()
            cursor = conn.execute(
                "INSERT INTO listing_signals(source_record_id, signal_id, version_json, created_at, supersedes_id) VALUES(?,?,?,?,?)",
                (source_record_id, signal.signal_id, _json(signal), created_at.isoformat(), previous["id"] if previous else None),
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
            key = (item.source, item.source_record_id)
            if key not in seen:
                seen.add(key)
                result.append(item)
        latest: dict[str, Any] = {}
        for item in result:
            previous = latest.get(item.award_id)
            if previous is None or (item.published_at or item.retrieved_at) > (previous.published_at or previous.retrieved_at):
                latest[item.award_id] = item
        cancelled_awards = {key for key, item in latest.items() if item.cancelled or item.status.strip().lower() in {"cancelled", "rescinded"}}
        return [item for item in result if item.award_id not in cancelled_awards or item.cancelled or item.deleted or item.status.strip().lower() in {"cancelled", "deleted", "rescinded"}]

    def load_listing_signals(self) -> list[Any]:
        result = self.all_listing_signals()
        reviews = self.sec_issuer_reviews()
        held = {item.signal_id: reason for item in result
                if (reason := self.sec_issuer_hold_reason(item, reviews))}
        held_scopes = {(item.cik, item.registration_id) for item in result
                       if item.signal_id in held and item.cik and item.registration_id}
        held_scopes.update(self.sec_issuer_held_registrations(reviews))
        # Retain the notice and its registration as inactive audit evidence.
        # Dropping an uncertain withdrawal would resurrect the earlier filing.
        result = [item.model_copy(update={
            "active": False, "status": "issuer_unresolved",
            "risk_findings": (*item.risk_findings, held.get(item.signal_id,
                "A notice for this registration is awaiting SEC issuer attribution.")),
        }) if item.signal_id in held or (item.cik, item.registration_id) in held_scopes
            or any(other.related_signal_id == item.signal_id and other.signal_id in held for other in result)
            else item for item in result]
        return [item for item in result if not any(self.listing_is_superseded(item, other) for other in result)]

    def listing_is_current_authorized(self, signal: Any) -> bool:
        if not signal.active or signal.status.strip().lower() in {"withdrawn", "terminated", "abandoned", "rejected", "closed"}:
            return False
        reviews = self.sec_issuer_reviews()
        if (self.sec_issuer_hold_reason(signal, reviews)
                or (signal.cik, signal.registration_id) in self.sec_issuer_held_registrations(reviews)):
            return False
        stored = [item for item in self.all_listing_signals() if item.signal_id == signal.signal_id]
        return not stored or any(item == signal and item.active for item in self.load_listing_signals())

    def sec_issuer_reviews(self) -> dict[str, dict[str, Any]]:
        """Read classification authority without altering original evidence."""
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sec_pending_filings'").fetchone():
                return {}
            rows = conn.execute("""SELECT accession,entry_json FROM sec_pending_filings
                WHERE json_extract(entry_json,'$.issuer_review.status') IS NOT NULL""").fetchall()
        return {row["accession"]: json.loads(row["entry_json"])["issuer_review"] for row in rows}

    def sec_issuer_held_registrations(self, reviews: dict[str, dict[str, Any]], *, evidence_kind: str = "listing") -> dict[tuple[str, str], str]:
        if evidence_kind not in {"listing", "ipo"}:
            raise ValueError("Unknown SEC issuer authority context")
        result = {}
        for accession, review in reviews.items():
            if not review.get("affected_registrations"):
                continue
            if review.get("status") == "resolved":
                # Keep paired scopes fenced until corrected evidence has been
                # ingested, not merely until the source worker wrote a review.
                with self.connect() as conn:
                    event_payload = signal_payload = None
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ipo_evidence'").fetchone():
                        row = conn.execute("SELECT evidence_json FROM ipo_evidence WHERE source='sec' AND event_id=? ORDER BY id DESC LIMIT 1", (accession,)).fetchone()
                        if row:
                            event_payload = json.loads(row[0])
                    row = conn.execute("SELECT version_json FROM listing_signals WHERE signal_id=? AND json_extract(version_json,'$.source')='sec' ORDER BY id DESC LIMIT 1", (accession,)).fetchone()
                    if row:
                        signal_payload = json.loads(row[0])
                # Each projection must replace its own historical assertion.
                # A context with no assertion may use the other source receipt.
                payload = (event_payload if event_payload is not None else signal_payload) if evidence_kind == "ipo" else (signal_payload if signal_payload is not None else event_payload)
                if payload is not None and self._sec_resolution_matches(payload, review):
                    continue
            for scope in review.get("affected_registrations", []):
                if scope.get("cik") and scope.get("registration_id"):
                    result[(scope["cik"], scope["registration_id"])] = str(review.get("reason")
                        or "SEC primary issuer attribution is awaiting review.")[:1000]
        return result

    @staticmethod
    def _sec_resolution_matches(payload: dict[str, Any], review: dict[str, Any]) -> bool:
        issuer = review.get("resolved_issuer", {})
        return bool(issuer.get("cik") and issuer.get("issuer_name") and "registration_id" in issuer
                    and payload.get("cik") == issuer["cik"]
                    and payload.get("issuer_name") == issuer["issuer_name"]
                    and payload.get("registration_id") == issuer["registration_id"]
                    and review.get("raw_payload_hash")
                    and payload.get("raw_payload_hash") == review["raw_payload_hash"])

    @staticmethod
    def sec_issuer_hold_reason(item: Any, reviews: dict[str, dict[str, Any]]) -> str | None:
        if item.source != "sec":
            return None
        accession = getattr(item, "event_id", None) or item.signal_id
        review = reviews.get(accession)
        if review is None:
            return None
        if review.get("status") == "resolved":
            # Resolution precedes downstream ingestion. Bind authority to the
            # exact document and issuer so an interrupted replay cannot revive
            # the previous version attributed to a different company.
            if Database._sec_resolution_matches(item.model_dump(), review):
                return None
            return "Stored SEC attribution has not yet been replaced by evidence matching the resolved primary issuer and document."
        return str(review.get("reason") or "SEC primary issuer attribution is awaiting review.")[:1000]

    def all_listing_signals(self) -> list[Any]:
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

    @staticmethod
    def listing_is_superseded(item: Any, other: Any) -> bool:
        inactive = not other.active or other.status.strip().lower() in {"withdrawn", "terminated", "abandoned", "rejected", "closed"}
        if not inactive or not item.active or other.filed_at < item.filed_at:
            return False
        if other.related_signal_id == item.signal_id:
            return True
        return bool(other.cik and item.cik and other.cik.lstrip("0") == item.cik.lstrip("0") and other.registration_id and other.registration_id == item.registration_id)

    def enqueue_correction(self, *, signal_id: str | None = None, award_id: str | None = None, company_name: str | None = None, contract_record_id: str | None = None, contract_source: str | None = None, reason: str, source_url: str, created_at: datetime) -> int:
        if not signal_id and not award_id:
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
            if contract_record_id is not None:
                clauses.append("EXISTS (SELECT 1 FROM candidate_matches AS cm WHERE cm.fingerprint=alerts.fingerprint AND json_extract(cm.contract_json, '$.source_record_id')=? AND json_extract(cm.contract_json, '$.source')=?)")
                params.extend((contract_record_id, contract_source))
            rows = conn.execute(f"SELECT * FROM alerts WHERE {' AND '.join(clauses)}", params).fetchall()
            inserted = 0
            for row in rows:
                marker = hashlib.sha256(_json([row["id"], reason, source_url]).encode()).hexdigest()
                duplicate = conn.execute("SELECT 1 FROM outbox_messages WHERE correction_key=?", (marker,)).fetchone()
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
                    "INSERT INTO outbox_messages(alert_id, subject, text_body, html_body, next_attempt_at, created_at, correction_key) VALUES(?,?,?,?,?,?,?)",
                    (row["id"], subject, body, f"<html><body><pre>{escape(body)}</pre></body></html>", created_at.isoformat(), created_at.isoformat(), marker),
                )
                conn.execute("UPDATE outbox_messages SET status='cancelled', lease_until=NULL WHERE alert_id=? AND correction_key IS NULL AND status='pending'", (row["id"],))
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
