"""Durable report mail: publication, provider acceptance and readback are distinct."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
from pathlib import Path
from typing import Any

from .research import report_markdown, serializable


def _address(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "\r\n"):
        raise ValueError("A single confirmed email address is required")
    name, address = parseaddr(value)
    if name or address != value or address.count("@") != 1 or any(c.isspace() for c in address):
        raise ValueError("A single confirmed email address is required")
    return address


def report_message(report: dict, *, sender: str, recipient: str, event_key: str,
                   created_at: datetime, notice: str = "", test: bool = False) -> bytes:
    """Attach the exact report and give each setup a readable approximate strategy."""
    if created_at.utcoffset() is None or not event_key:
        raise ValueError("Message identity and aware creation time are required")
    report = serializable(report)
    identity = hashlib.sha256((event_key + "|" + _address(recipient)).encode()).hexdigest()
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = _address(sender)
    message["To"] = recipient
    message["Date"] = format_datetime(created_at.astimezone(UTC))
    message["Message-ID"] = f"<{identity}@ipo-monitor.local>"
    message["Subject"] = ("[PIPELINE TEST]" if test else "[Investment research]") + " Company, IPO and trade report"
    lines = ["Investment monitor report", "", f"Report collected: {report.get('completed_at', 'Unavailable')}",
             "Holdings on file: none. No purchase, fill, position or order is assumed.",
             "Horizon: approximately 5–15 trading sessions. These are conditional research plans, not execution instructions.", ""]
    if test:
        lines += ["This is an email pipeline verification. It does not announce a new buy or sell decision.", ""]
    if notice:
        lines += [notice, ""]
    for idea in report.get("trade_ideas", []):
        symbol, action = idea.get("symbol", "Unknown"), idea.get("action", "wait")
        lines += [f"{symbol} | {idea.get('exchange')} | {idea.get('currency')} | {action}",
                  f"Completed price date: {idea.get('price_as_of') or 'Unavailable'}",
                  f"Entry trigger: {idea.get('entry')}; invalidation: {idea.get('invalidation')}; target reference: {idea.get('target')}."]
        strategy = idea.get("strategy")
        if action == "conditional_buy" and isinstance(strategy, dict):
            trigger, stop, target = (strategy.get(key) or {} for key in ("entry_trigger", "stop", "target"))
            timing, risk = strategy.get("time_exit") or {}, strategy.get("risk_budget") or {}
            lines += ["Approximate strategy:",
                      f"- Entry: {trigger.get('verification') or 'Confirm a fresh quote and the entry trigger.'}",
                      f"- Maximum entry reference: {strategy.get('maximum_entry')}; setup valid through {strategy.get('setup_valid_through') or 'the next five sessions'}.",
                      f"- Invalidation: {stop.get('price', idea.get('invalidation'))}; {stop.get('gap_policy') or 'gaps can prevent an exit at this price'}.",
                      f"- Target reference: {target.get('price', idea.get('target'))}; confirm a fresh price before reviewing an exit.",
                      f"- Time review: after {timing.get('review_after_sessions', 5)} sessions; close or reassess by {timing.get('exit_after_sessions', 15)} sessions from an actual verified fill. None is assumed.",
                      f"- Risk per share at the entry reference: {risk.get('planned_risk_per_share')} {risk.get('currency', idea.get('currency'))}. For sizing, recompute actual entry minus stop plus estimated round-trip costs, divide your chosen loss budget by that amount, round down to the broker lot size and cap to available cash. No quantity is assumed; gaps and currency moves can increase losses."]
        elif action == "conditional_buy":
            lines += ["Approximate strategy: verify a fresh quote and breakout through the entry reference; skip a large gap or deteriorated evidence. The invalidation and target are risk references. Review after 5 sessions from a verified fill and close/reassess by 15; no fill is assumed. Position size requires your chosen loss budget and a same-currency quote."]
        elif action == "reduce_if_owned":
            lines += ["You report no holdings. This is a review reference for an owner; it is not a sell or short instruction for your current account."]
        else:
            lines += ["Wait: required evidence or trading conditions are incomplete. No entry is suggested."]
        lines += ["Reasons: " + "; ".join(str(x) for x in idea.get("reasons", [])),
                  "Conditions: " + "; ".join(str(x) for x in idea.get("conditions", [])),
                  "Material risks: " + "; ".join(str(x) for x in idea.get("risks", [])),
                  "Evidence: " + "\n".join(str(x) for x in idea.get("evidence_urls", [])), ""]
    lines += ["The attached Markdown and JSON retain world-news context, financial periods/currencies, source dates, commentary coverage and waits. Collection counters describe the source run; delivery is recorded separately.",
              "This is a bounded configured screen, with selection and source-access gaps. Sentiment and headline rules do not establish predictive probabilities. Stops cannot guarantee an exit price; gaps, currency exposure and costs can exceed a planned loss."]
    message.set_content("\n".join(lines))
    message.add_attachment(report_markdown(report).encode("utf-8"), maintype="text", subtype="markdown", filename="research-report.md")
    message.add_attachment(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"), maintype="application", subtype="json", filename="research-report.json")
    message.set_boundary("ipo-report-" + identity)
    result = message.as_bytes()
    if len(result) > 5_000_000:
        raise ValueError("Email report exceeds the supported message bound")
    return result


class EmailOutbox:
    """Never re-send an expired lease or an ambiguous provider outcome."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
              CREATE TABLE IF NOT EXISTS investment_mail(
                id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, recipient TEXT NOT NULL,
                rfc822_id TEXT NOT NULL UNIQUE, raw_message BLOB NOT NULL, raw_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
                lease_token TEXT, lease_until TEXT, error_code TEXT, receipt_json TEXT, sent_at TEXT);
              CREATE INDEX IF NOT EXISTS investment_mail_pending ON investment_mail(status,next_attempt_at);
              CREATE TABLE IF NOT EXISTS reference_plans(symbol TEXT PRIMARY KEY, plan_json TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS investment_mail_state(name TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            """)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(self, event_key: str, raw: bytes, recipient: str, *, now: datetime, expires_at: datetime) -> int:
        from email.parser import BytesParser
        _address(recipient)
        if not raw or len(raw) > 5_000_000 or now.utcoffset() is None or expires_at.utcoffset() is None:
            raise ValueError("Invalid bounded email publication")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        identity = message.get("Message-ID")
        if message.get_all("To") != [recipient] or message.get_all("Cc") or message.get_all("Bcc") or not identity:
            raise ValueError("Email recipient or message identity differs from the confirmed target")
        digest = hashlib.sha256(raw).hexdigest()
        stamp = now.astimezone(UTC).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM investment_mail WHERE event_key=?", (event_key,)).fetchone()
            if existing:
                # Immutable identity must not silently bind a different report or recipient.
                if existing["recipient"] != recipient or existing["raw_sha256"] != digest:
                    raise ValueError("Existing email event has a conflicting publication")
                return int(existing["id"])
            cursor = conn.execute("""INSERT INTO investment_mail
              (event_key,recipient,rfc822_id,raw_message,raw_sha256,created_at,expires_at,next_attempt_at)
              VALUES(?,?,?,?,?,?,?,?)""", (event_key, recipient, str(identity), raw, digest, stamp,
                                            expires_at.astimezone(UTC).isoformat(), stamp))
            return int(cursor.lastrowid)

    def get(self, identifier: int) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM investment_mail WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return dict(row)

    def states(self) -> dict[str, int]:
        with self.connect() as conn:
            return dict(conn.execute("SELECT status,COUNT(*) FROM investment_mail GROUP BY status"))

    def event(self, event_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM investment_mail WHERE event_key=?", (event_key,)).fetchone()
        return dict(row) if row else None

    def recover_interrupted(self, now: datetime) -> int:
        with self.connect() as conn:
            return conn.execute("UPDATE investment_mail SET status='unknown',lease_token=NULL,lease_until=NULL,error_code='interrupted_send' WHERE status='sending' AND lease_until<=?", (now.astimezone(UTC).isoformat(),)).rowcount

    def plans(self) -> dict[str, dict]:
        with self.connect() as conn:
            return {row["symbol"]: json.loads(row["plan_json"]) for row in conn.execute("SELECT * FROM reference_plans")}

    def save_plans(self, plans: dict[str, dict]) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for symbol, plan in plans.items():
                conn.execute("INSERT INTO reference_plans VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET plan_json=excluded.plan_json", (symbol, json.dumps(plan, sort_keys=True, allow_nan=False)))

    def state(self, name: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT value_json FROM investment_mail_state WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_state(self, name: str, value: dict) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO investment_mail_state VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json", (name, json.dumps(value, sort_keys=True, allow_nan=False)))

    def lease(self, now: datetime) -> dict | None:
        stamp, token = now.astimezone(UTC).isoformat(), uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # A crashed sender could have completed its POST. Recovery is read-only.
            conn.execute("UPDATE investment_mail SET status='unknown',lease_token=NULL,lease_until=NULL,error_code='interrupted_send' WHERE status='sending' AND lease_until<=?", (stamp,))
            conn.execute("UPDATE investment_mail SET status='expired',error_code='report_expired' WHERE status='pending' AND expires_at<=?", (stamp,))
            row = conn.execute("SELECT * FROM investment_mail WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT 1", (stamp,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE investment_mail SET status='sending',attempts=attempts+1,lease_token=?,lease_until=? WHERE id=?", (token, (now + timedelta(minutes=5)).astimezone(UTC).isoformat(), row["id"]))
            return dict(conn.execute("SELECT * FROM investment_mail WHERE id=?", (row["id"],)).fetchone())

    def _settle(self, row: dict, *, status: str, now: datetime, receipt: dict | None = None,
                error: str | None = None, next_attempt_at: datetime | None = None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owned = conn.execute("SELECT status,lease_token FROM investment_mail WHERE id=?", (row["id"],)).fetchone()
            if owned is None or owned["status"] != row["status"] or owned["lease_token"] != row.get("lease_token"):
                raise RuntimeError("The email delivery lease is no longer owned")
            conn.execute("""UPDATE investment_mail SET status=?,lease_token=NULL,lease_until=NULL,error_code=?,
              receipt_json=?,sent_at=?,next_attempt_at=? WHERE id=?""", (status, error,
                json.dumps(receipt, sort_keys=True, allow_nan=False) if receipt else None,
                now.astimezone(UTC).isoformat() if status == "sent" else None,
                (next_attempt_at or now).astimezone(UTC).isoformat(), row["id"]))

    def deliver_once(self, transport: Any, *, now: datetime) -> dict | None:
        from .gmail_delivery import DefinitiveDeliveryFailure, UnknownDelivery
        row = self.lease(now)
        if row is None:
            return None
        try:
            receipt = transport.deliver(bytes(row["raw_message"]), row["recipient"], row["rfc822_id"])
            if not self._receipt_matches(row, receipt):
                raise UnknownDelivery("Provider acceptance has no verified readback")
        except DefinitiveDeliveryFailure as error:
            retry = error.safe_to_retry and row["attempts"] < 5
            self._settle(row, status="pending" if retry else "rejected", now=now,
                         error=type(error).__name__, next_attempt_at=now + timedelta(minutes=min(60, 2 ** row["attempts"])))
        except UnknownDelivery as error:
            self._settle(row, status="unknown", now=now, error=type(error).__name__,
                         receipt=getattr(error, "partial_receipt", None))
        except Exception as error:
            # Even an unclassified failure after lease acquisition may follow a POST.
            self._settle(row, status="unknown", now=now, error=type(error).__name__)
        else:
            self._settle(row, status="sent", now=now, receipt=receipt)
        value = self.get(row["id"])
        return {key: value[key] for key in ("id", "event_key", "status", "attempts", "error_code", "receipt_json")}

    @staticmethod
    def _receipt_matches(row: dict, receipt: dict) -> bool:
        from .gmail_delivery import message_content_sha256
        from email.parser import BytesParser
        try:
            if not isinstance(receipt, dict):
                return False
            raw = bytes(row["raw_message"])
            sender = parseaddr(str(BytesParser(policy=policy.default).parsebytes(raw)["From"]))[1].casefold()
            accepted = datetime.fromisoformat(receipt["provider_accepted_at"].replace("Z", "+00:00"))
            verified = datetime.fromisoformat(receipt["verified_at"].replace("Z", "+00:00"))
            return bool(accepted.utcoffset() is not None and verified.utcoffset() is not None and accepted <= verified
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", receipt.get("gmail_message_id", ""))
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", receipt.get("thread_id", ""))
                        and receipt.get("delivered_label") in {"SENT", "INBOX"}
                        and receipt.get("recipient") == row["recipient"] and receipt.get("sender") == sender
                        and receipt.get("rfc822_id") == row["rfc822_id"]
                        and receipt.get("content_sha256") == message_content_sha256(raw)
                        and receipt.get("raw_content_sha256") == row["raw_sha256"] == hashlib.sha256(raw).hexdigest()
                        and re.fullmatch(r"[a-f0-9]{64}", receipt.get("readback_raw_sha256", "")))
        except (KeyError, ValueError, TypeError, AttributeError):
            return False

    def reconcile_unknown(self, transport: Any, *, now: datetime, limit: int = 10) -> list[dict]:
        self.recover_interrupted(now)
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM investment_mail WHERE status='unknown' ORDER BY id LIMIT ?", (max(0, min(limit, 10)),))]
        results = []
        for row in rows:
            try:
                partial = json.loads(row["receipt_json"] or "{}")
                provider_id = partial.get("gmail_message_id")
                linked = {"provider_message_id": provider_id} if provider_id else {}
                receipt = transport.reconcile(row["rfc822_id"], row["recipient"], bytes(row["raw_message"]), **linked)
                if receipt and self._receipt_matches(row, receipt):
                    self._settle(row, status="sent", now=now, receipt=receipt)
                    results.append({"id": row["id"], "status": "sent", "reconciled": True})
            except Exception:
                # Absence or a read failure cannot authorize another send.
                continue
        return results
