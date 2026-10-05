from __future__ import annotations

import smtplib
import ssl
import hashlib
import json
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Callable

from .db import Database


class PartialSMTPDelivery(RuntimeError):
    def __init__(self, accepted: tuple[str, ...], refused: tuple[str, ...]):
        super().__init__(f"SMTP accepted {len(accepted)} recipients and refused {len(refused)} recipients")
        self.accepted = accepted
        self.refused = refused


class SMTPTransport:
    def __init__(self, *, host: str, port: int, username: str | None, password: str | None, sender: str, recipients: tuple[str, ...], security: str = "starttls", timeout: float = 10.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.recipients = recipients
        self.security = security
        self.timeout = timeout
        if security not in {"starttls", "ssl", "none"}:
            raise ValueError("unsupported SMTP security mode")
        if security == "none" and username:
            raise ValueError("SMTP authentication requires TLS")

    def __call__(self, row: dict[str, Any]) -> str | None:
        recipients = tuple(json.loads(row["pending_recipients_json"])) if row.get("pending_recipients_json") is not None else self.recipients
        if not recipients:
            raise ValueError("SMTP message has no pending recipients")
        message = EmailMessage()
        message["Subject"] = row["subject"]
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        identity = hashlib.sha256(f"{row['id']}|{row['created_at']}|{row['text_body']}".encode()).hexdigest()
        message["Message-ID"] = f"<{identity}@ipo-monitor.local>"
        message.set_content(row["text_body"])
        message.add_alternative(row["html_body"], subtype="html")
        context = ssl.create_default_context()
        if self.security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        try:
            client.ehlo()
            if self.security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if self.username:
                client.login(self.username, self.password or "")
            refused = client.send_message(message, from_addr=self.sender, to_addrs=list(recipients))
            if refused:
                accepted = tuple(address for address in recipients if address not in refused)
                raise PartialSMTPDelivery(accepted, tuple(refused))
        finally:
            # A QUIT failure after DATA acceptance must not trigger a duplicate send.
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                pass
            finally:
                client.close()
        return message.get("Message-ID")


class SMTPWorker:
    def __init__(
        self,
        db: Database,
        *,
        transport: Callable[[dict[str, Any]], str | None],
        now: Callable[[], datetime] | None = None,
        max_attempts: int = 5,
        retry_base: timedelta = timedelta(seconds=30),
        lease_for: timedelta = timedelta(minutes=5),
    ):
        self.db = db
        self.transport = transport
        self.now = now or (lambda: datetime.now(UTC))
        self.max_attempts = max_attempts
        self.retry_base = retry_base
        self.lease_for = lease_for
        if max_attempts < 1 or retry_base <= timedelta(0) or lease_for <= timedelta(0):
            raise ValueError("SMTP retry and lease settings must be positive")

    def run_once(self) -> bool:
        current = self.now()
        row = self.db.lease_outbox(now=current, lease_for=self.lease_for)
        if row is None:
            return False
        try:
            smtp_id = self.transport(row)
        except Exception as exc:
            attempts_after = int(row["attempts"]) + 1
            delay = self.retry_base * (2 ** max(0, attempts_after - 1))
            self.db.mark_outbox_failure(
                int(row["id"]), failed_at=current, error=f"{type(exc).__name__}: {exc}",
                max_attempts=self.max_attempts, next_attempt_at=self.now() + delay,
                lease_until=row["lease_until"],
                refused_recipients=exc.refused if isinstance(exc, PartialSMTPDelivery) else None,
                accepted_recipients=exc.accepted if isinstance(exc, PartialSMTPDelivery) else (),
            )
            return False
        self.db.mark_outbox_sent(int(row["id"]), sent_at=self.now(), smtp_message_id=smtp_id, lease_until=row["lease_until"])
        return True
