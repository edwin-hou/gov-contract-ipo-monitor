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
    filed_at_precision: Literal["second", "date"] = "second"
    accepted_at: datetime | None = None
    source_filing_date: date | None = None
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

    @field_validator("accepted_at")
    @classmethod
    def require_accepted_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("SEC Accepted timestamp must include a timezone")
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


def _filing_order(left: IPOEvidence, right: IPOEvidence) -> int | None:
    if (left.source, left.event_id) == (right.source, right.event_id):
        return 0
    if left.accepted_at is not None and right.accepted_at is not None:
        return (left.accepted_at > right.accepted_at) - (left.accepted_at < right.accepted_at)
    if left.source_filing_date is not None and right.source_filing_date is not None:
        if left.source_filing_date == right.source_filing_date:
            return None
        return (left.source_filing_date > right.source_filing_date) - (left.source_filing_date < right.source_filing_date)
    if left.filed_at_precision == right.filed_at_precision == "second":
        return (left.filed_at > right.filed_at) - (left.filed_at < right.filed_at)
    # Atom updated timestamps and the SEC's legal Filing Date have different
    # meanings. After-hours acceptance can receive the next business day's
    # filing date, so their apparent calendar-day separation proves no order.
    return None


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
        # Keep all versions in evidence(), but derive lifecycle state from the
        # newest source accession receipt. A precise Accepted-time replay must
        # supersede its earlier date-only observation rather than coexist with
        # an ambiguous copy of the same filing.
        with closing(self.db.connect()) as conn:
            rows = conn.execute("""SELECT evidence_json FROM ipo_evidence current
                WHERE current.id=(SELECT MAX(newer.id) FROM ipo_evidence newer
                  WHERE newer.source=current.source AND newer.event_id=current.event_id)
                ORDER BY filed_at,id""").fetchall()
        for row in rows:
            evidence = IPOEvidence.model_validate_json(row["evidence_json"])
            if evidence.event_type == "rumor" or not evidence.authoritative:
                scope = "unverified"
            elif evidence.registration_id:
                scope = evidence.registration_id
            else:
                # No issuer-wide lifecycle inference when SEC file number is absent.
                scope = f"unscoped:{evidence.event_id}"
            groups.setdefault((evidence.issuer_key, scope), []).append(evidence)

        result: list[dict] = []
        reviews = self.db.sec_issuer_reviews()
        held_registrations = self.db.sec_issuer_held_registrations(reviews, evidence_kind="ipo")
        for (issuer_key, scope), events in groups.items():
            issuer_holds = [reason for event in events
                            if (reason := self.db.sec_issuer_hold_reason(event, reviews))]
            if (reason := held_registrations.get((events[0].cik, scope))):
                issuer_holds.append(reason)
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
            ambiguous_notices = []
            confidence = "unverified" if not authoritative else "unclassified_registration"
            if confirmed:
                first_confirmation = confirmed[0]
                confidence = "primary_filing"
                status = "amended" if first_confirmation.event_type == "amendment" else "filed"
                # Only replay notices sharing this exact registration scope and issuer.
                for event in authoritative:
                    order = _filing_order(event, first_confirmation)
                    if order is None and event.event_type in {"withdrawn", "effective", "prospectus"}:
                        ambiguous_notices.append(event)
                        continue
                    if order == -1:
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
                    if event.event_type == "withdrawn":
                        order = _filing_order(event, eligible[0])
                        if order is None:
                            ambiguous_notices.append(event)
                        elif order >= 0:
                            status, active = "withdrawn_unclassified_registration", False
            # A definitely applicable withdrawal is terminal regardless of any
            # other uncertain stage. Otherwise date-only lifecycle overlap
            # with the registration withholds an active-state assertion.
            if ambiguous_notices and active:
                status, active = "chronology_unresolved", False
            if issuer_holds:
                status, active, confidence = "issuer_unresolved", False, "issuer_unresolved"

            urls = list(dict.fromkeys(event.source_url for event in events))
            result.append({
                "issuer_key": issuer_key, "issuer_name": current.issuer_name, "cik": current.cik,
                "registration_id": None if scope.startswith("unscoped:") or scope == "unverified" else scope,
                "status": status, "active": active, "ipo_confirmed": bool(confirmed) and not issuer_holds,
                "confidence": confidence, "ticker": next((event.ticker for event in reversed(eligible) if event.ticker), None),
                "exchange": next((event.exchange for event in reversed(eligible) if event.exchange), None),
                "proposed_price": next((event.proposed_price for event in reversed(eligible) if event.proposed_price is not None), None),
                "offering_kind": current.offering_kind,
                "effective_date": next((event.effective_date.isoformat() for event in reversed(authoritative) if event.effective_date), None),
                "first_filed_at": events[0].filed_at.isoformat(), "last_filed_at": latest.filed_at.isoformat(),
                "first_filed_at_precision": events[0].filed_at_precision,
                "last_filed_at_precision": latest.filed_at_precision,
                "first_accepted_at": events[0].accepted_at.isoformat() if events[0].accepted_at else None,
                "last_accepted_at": latest.accepted_at.isoformat() if latest.accepted_at else None,
                "first_source_filing_date": events[0].source_filing_date.isoformat() if events[0].source_filing_date else None,
                "last_source_filing_date": latest.source_filing_date.isoformat() if latest.source_filing_date else None,
                "evidence_count": len(events), "source_urls": urls,
                "classification_reason": current.classification_reason,
                "limitations": [
                    "Public filings do not prove the offering completed or shares began trading.",
                    *list(dict.fromkeys(issuer_holds)),
                    *(["Date-only SEC filing evidence does not establish the order of a registration and its lifecycle notice; active status is withheld until official Accepted timestamps resolve it."] if status == "chronology_unresolved" else []),
                    *(["At least one source reports only a filing date; its midnight placeholder is not an actual filing time."] if any(event.filed_at_precision == "date" for event in events) else []),
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
            "withdrawn_ipos": sum(candidate["ipo_confirmed"] and candidate["status"] == "withdrawn" for candidate in candidates),
            "chronology_unresolved_candidates": sum(candidate["status"] == "chronology_unresolved" for candidate in candidates),
            "issuer_unresolved_candidates": sum(candidate["status"] == "issuer_unresolved" for candidate in candidates),
            "unverified_candidates": sum(candidate["status"] == "rumored" for candidate in candidates),
        }
