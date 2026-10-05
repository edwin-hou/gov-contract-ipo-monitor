"""Company IPO evidence, independent of contract awards or company size.

The filing's registration number scopes lifecycle events. An EFFECT or RW for
an unrelated resale registration must never change an issuer's IPO status.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import closing
from datetime import UTC, date, datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import Database


class IPOEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    issuer_name: str = Field(min_length=1)
    cik: str | None = None
    source: str
    source_url: str
    source_kind: Literal["regulatory", "issuer", "reporting", "commentary"] = "commentary"
    filed_at: datetime
    event_type: Literal[
        "registration", "amendment", "effective", "prospectus",
        "withdrawn", "amendment_withdrawn", "rumor",
    ]
    form_type: str | None = None
    underlying_form: str | None = None
    effective_date: date | None = None
    registration_id: str | None = None
    is_ipo: bool = False
    offering_kind: str = "unclassified"
    ticker: str | None = None
    exchange: str | None = None
    proposed_price: float | None = None
    evidence_excerpt: str = ""
    classification_reason: str = ""
    raw_payload_hash: str | None = None
    raw_archive_path: str | None = None

    @field_validator("filed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("evidence timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("cik")
    @classmethod
    def normalize_cik(cls, value: str | None) -> str | None:
        if not value:
            return None
        if not str(value).isdigit() or len(str(value)) > 10:
            raise ValueError("invalid SEC CIK")
        return f"{int(value):010d}"

    @property
    def issuer_key(self) -> str:
        if self.cik:
            return f"cik:{self.cik}"
        # Names are unresolved identities, never automatically joined to a CIK.
        normalized = re.sub(r"\s+", " ", self.issuer_name.strip().casefold())
        return "name:" + hashlib.sha256(normalized.encode()).hexdigest()[:24]

    @property
    def authoritative(self) -> bool:
        parsed = urlparse(self.source_url)
        return (
            self.source == "sec" and self.source_kind == "regulatory"
            and parsed.scheme == "https" and parsed.hostname in {"sec.gov", "www.sec.gov", "data.sec.gov"}
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS ipo_companies(
 issuer_key TEXT PRIMARY KEY, issuer_name TEXT NOT NULL, cik TEXT,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ipo_evidence(
 id INTEGER PRIMARY KEY, issuer_key TEXT NOT NULL REFERENCES ipo_companies(issuer_key),
 source TEXT NOT NULL, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
 registration_id TEXT, filed_at TEXT NOT NULL, observed_at TEXT NOT NULL,
 evidence_json TEXT NOT NULL, UNIQUE(source,event_id,payload_hash));
CREATE INDEX IF NOT EXISTS ipo_evidence_issuer ON ipo_evidence(issuer_key, filed_at);
CREATE INDEX IF NOT EXISTS ipo_evidence_registration ON ipo_evidence(issuer_key, registration_id);
"""


class IPOTracker:
    def __init__(self, db: Database):
        self.db = db

    def initialize(self) -> None:
        with closing(self.db.connect()) as conn:
            conn.executescript(SCHEMA)

    def record(self, evidence: IPOEvidence, *, observed_at: datetime | None = None) -> bool:
        observed = observed_at or datetime.now(UTC)
        if observed.utcoffset() is None:
            raise ValueError("observation timestamp must include a timezone")
        observed = observed.astimezone(UTC)
        raw = evidence.model_dump_json()
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO ipo_companies(issuer_key,issuer_name,cik,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(issuer_key) DO UPDATE SET
                   issuer_name=excluded.issuer_name,last_seen_at=MAX(last_seen_at,excluded.last_seen_at)""",
                (evidence.issuer_key, evidence.issuer_name, evidence.cik, observed.isoformat(), observed.isoformat()),
            )
            cursor = conn.execute(
                """INSERT OR IGNORE INTO ipo_evidence
                   (issuer_key,source,event_id,payload_hash,registration_id,filed_at,observed_at,evidence_json)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (evidence.issuer_key, evidence.source, evidence.event_id, digest,
                 evidence.registration_id, evidence.filed_at.isoformat(), observed.isoformat(), raw),
            )
        return bool(cursor.rowcount)

    def evidence(self, *, issuer_key: str | None = None) -> list[IPOEvidence]:
        with closing(self.db.connect()) as conn:
            if issuer_key:
                rows = conn.execute(
                    "SELECT evidence_json FROM ipo_evidence WHERE issuer_key=? ORDER BY filed_at,id", (issuer_key,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT evidence_json FROM ipo_evidence ORDER BY filed_at,id").fetchall()
        return [IPOEvidence.model_validate_json(row["evidence_json"]) for row in rows]

    def candidates(self, *, limit: int = 1000) -> list[dict]:
        if limit < 1:
            return []
        groups: dict[tuple[str, str], list[IPOEvidence]] = {}
        for evidence in self.evidence():
            if evidence.event_type == "rumor" or not evidence.authoritative:
                scope = "unverified"
            elif evidence.registration_id:
                scope = evidence.registration_id
            else:
                # No issuer-wide lifecycle inference when SEC file number is absent.
                scope = f"unscoped:{evidence.event_id}"
            groups.setdefault((evidence.issuer_key, scope), []).append(evidence)

        result: list[dict] = []
        for (issuer_key, scope), events in groups.items():
            authoritative = [event for event in events if event.authoritative]
            registration_events = [event for event in authoritative if event.event_type in {"registration", "amendment", "prospectus"}]
            # Resale and generic lifecycle notices remain audit evidence, not IPO candidates.
            eligible = [event for event in registration_events if event.offering_kind not in {"resale", "follow_on"}]
            confirmed = [event for event in eligible if event.is_ipo]
            if authoritative and not eligible:
                continue
            latest = events[-1]
            current = eligible[-1] if eligible else latest
            status = "rumored" if not authoritative else "registration_observed"
            active = True
            confidence = "unverified" if not authoritative else "unclassified_registration"
            if confirmed:
                first_confirmation = confirmed[0]
                confidence = "primary_filing"
                status = "amended" if first_confirmation.event_type == "amendment" else "filed"
                # Only replay notices sharing this exact registration scope and issuer.
                for event in authoritative:
                    if event.filed_at < first_confirmation.filed_at:
                        continue
                    if event.event_type == "withdrawn":
                        status, active = "withdrawn", False
                    elif active and event.event_type == "effective":
                        status = "effective"
                    elif active and event.event_type == "prospectus":
                        status = "prospectus_filed"
                    elif active and event.event_type == "amendment" and status in {"filed", "amended"}:
                        status = "amended"
                # Effectiveness and final prospectus do not establish completed trading.
            elif eligible and scope != "unverified":
                for event in authoritative:
                    if event.event_type == "withdrawn" and event.filed_at >= eligible[0].filed_at:
                        status, active = "withdrawn_unclassified_registration", False

            urls = list(dict.fromkeys(event.source_url for event in events))
            result.append({
                "issuer_key": issuer_key, "issuer_name": current.issuer_name, "cik": current.cik,
                "registration_id": None if scope.startswith("unscoped:") or scope == "unverified" else scope,
                "status": status, "active": active, "ipo_confirmed": bool(confirmed),
                "confidence": confidence, "ticker": next((event.ticker for event in reversed(eligible) if event.ticker), None),
                "exchange": next((event.exchange for event in reversed(eligible) if event.exchange), None),
                "proposed_price": next((event.proposed_price for event in reversed(eligible) if event.proposed_price is not None), None),
                "offering_kind": current.offering_kind,
                "effective_date": next((event.effective_date.isoformat() for event in reversed(authoritative) if event.effective_date), None),
                "first_filed_at": events[0].filed_at.isoformat(), "last_filed_at": latest.filed_at.isoformat(),
                "evidence_count": len(events), "source_urls": urls,
                "classification_reason": current.classification_reason,
                "limitations": [
                    "Public filings do not prove the offering completed or shares began trading.",
                    *(["SEC registration file number missing; lifecycle cannot be linked safely."] if scope.startswith("unscoped:") else []),
                    *(["Commentary or reporting is unverified and cannot confirm an IPO."] if not authoritative else []),
                ],
            })
        result.sort(key=lambda candidate: candidate["last_filed_at"], reverse=True)
        return result[:limit]

    def summary(self) -> dict[str, int]:
        candidates = self.candidates(limit=2**31 - 1)
        with closing(self.db.connect()) as conn:
            evidence_count = conn.execute("SELECT COUNT(*) FROM ipo_evidence").fetchone()[0]
            company_count = conn.execute("SELECT COUNT(*) FROM ipo_companies").fetchone()[0]
        return {
            "companies": company_count, "evidence": evidence_count, "ipo_candidates": len(candidates),
            "confirmed_ipos": sum(candidate["ipo_confirmed"] for candidate in candidates),
            "active_ipos": sum(candidate["ipo_confirmed"] and candidate["active"] for candidate in candidates),
            "withdrawn_ipos": sum(candidate["ipo_confirmed"] and not candidate["active"] for candidate in candidates),
            "unverified_candidates": sum(candidate["status"] == "rumored" for candidate in candidates),
        }
