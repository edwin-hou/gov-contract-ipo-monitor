"""Versioned public-market evidence that travels with the monitor checkpoint."""
from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any

from .db import Database
from .research import serializable

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_evidence(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL,
 payload_hash TEXT NOT NULL, gzip_json BLOB NOT NULL,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 UNIQUE(kind,identity,payload_hash));
CREATE INDEX IF NOT EXISTS market_evidence_latest ON market_evidence(kind,identity,last_seen_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS market_coverage(
 source TEXT PRIMARY KEY, coverage_json TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trade_idea_versions(
 id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, payload_hash TEXT NOT NULL,
 idea_json TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 UNIQUE(symbol,payload_hash));
CREATE INDEX IF NOT EXISTS trade_idea_latest ON trade_idea_versions(symbol,last_seen_at DESC,id DESC);
"""


def payload(value: Any) -> dict:
    return serializable(asdict(value) if is_dataclass(value) else value)


class MarketResearchStore:
    def __init__(self, db: Database):
        self.db = db

    def initialize(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(SCHEMA)

    def record(self, kind: str, identity: str, value: Any, *, observed_at: datetime) -> bool:
        data = payload(value)
        # Quote freshness must retain the actual request observation. Reopening
        # an archive or recording the same quote later is not a fresh request.
        if kind == "current_quote":
            data.setdefault("source_observed_at", data.get("observed_at"))
            try:
                original = datetime.fromisoformat(data["source_observed_at"])
                if original.utcoffset() is None or original > observed_at:
                    raise ValueError("Invalid original quote observation")
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError("Quote archive requires the original aware request observation") from exc
        # Retrieval does not create a new financial observation or headline.
        stable = {k: v for k, v in data.items() if k not in {"observed_at", "retrieved_at", "generated_at"}}
        raw = json.dumps(stable, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > 5_000_000:
            raise ValueError("Market evidence exceeds the archive limit")
        digest = hashlib.sha256(raw).hexdigest()
        if observed_at.utcoffset() is None:
            raise ValueError("Market evidence requires an aware observation timestamp")
        stamp = observed_at.astimezone(UTC).isoformat()
        with self.db.transaction() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO market_evidence(kind,identity,payload_hash,gzip_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                (kind, identity, digest, gzip.compress(raw, mtime=0), stamp, stamp),
            ).rowcount == 1
            conn.execute("UPDATE market_evidence SET last_seen_at=MAX(last_seen_at,?) WHERE kind=? AND identity=? AND payload_hash=?",
                         (stamp, kind, identity, digest))
        return inserted

    def latest(self, kind: str) -> dict[str, dict]:
        with self.db.connect() as conn:
            rows = conn.execute("""SELECT m.* FROM market_evidence m
              WHERE m.kind=? AND m.id=(SELECT newest.id FROM market_evidence newest
                WHERE newest.kind=m.kind AND newest.identity=m.identity
                ORDER BY newest.last_seen_at DESC,newest.id DESC LIMIT 1)""", (kind,)).fetchall()
        result = {}
        for row in rows:
            if row["identity"] in result:
                continue
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = decoder.decompress(row["gzip_json"], 5_000_001)
            if not decoder.eof or decoder.unconsumed_tail or decoder.unused_data or len(raw) > 5_000_000:
                raise ValueError("Invalid market evidence archive")
            if hashlib.sha256(raw).hexdigest() != row["payload_hash"]:
                raise ValueError("Market evidence hash mismatch")
            item = json.loads(raw)
            item["observed_at"] = row["last_seen_at"]
            if kind == "current_quote":
                if not item.get("source_observed_at"):
                    raise ValueError("Quote archive has no original request observation")
                item["observed_at"] = item["source_observed_at"]
            item["archive_hash"] = row["payload_hash"]
            result[row["identity"]] = item
        return result

    def record_coverage(self, source: str, coverage: list[Any], *, observed_at: datetime) -> None:
        raw = json.dumps([payload(item) for item in coverage], allow_nan=False)
        with self.db.connect() as conn:
            conn.execute("INSERT INTO market_coverage VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET coverage_json=excluded.coverage_json,observed_at=excluded.observed_at",
                         (source, raw, observed_at.isoformat()))

    def coverage(self, source: str) -> list[dict]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT coverage_json FROM market_coverage WHERE source=?", (source,)).fetchone()
        return json.loads(row[0]) if row else []

    def save_ideas(self, ideas: list[dict], *, observed_at: datetime) -> int:
        inserted = 0
        with self.db.transaction() as conn:
            for idea in ideas:
                data = payload(idea)
                stable = {k: v for k, v in data.items() if k != "generated_at"}
                raw = json.dumps(stable, sort_keys=True, ensure_ascii=False, allow_nan=False)
                digest = hashlib.sha256(raw.encode()).hexdigest()
                if observed_at.utcoffset() is None:
                    raise ValueError("Trade ideas require an aware observation timestamp")
                stamp = observed_at.astimezone(UTC).isoformat()
                inserted += conn.execute("INSERT OR IGNORE INTO trade_idea_versions(symbol,payload_hash,idea_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                                         (data["symbol"], digest, raw, stamp, stamp)).rowcount
                conn.execute("UPDATE trade_idea_versions SET last_seen_at=MAX(last_seen_at,?) WHERE symbol=? AND payload_hash=?",
                             (stamp, data["symbol"], digest))
        return inserted
