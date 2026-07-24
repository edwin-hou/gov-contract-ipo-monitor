from __future__ import annotations

import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Callable

from .db import Database


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

    def __call__(self, row: dict[str, Any]) -> str | None:
        message = EmailMessage()
        message["Subject"] = row["subject"]
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(row["text_body"])
        message.add_alternative(row["html_body"], subtype="html")
        context = ssl.create_default_context()
        if self.security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        with client:
            client.ehlo()
            if self.security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if self.username:
                client.login(self.username, self.password or "")
            refused = client.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
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
        lease_for: timedelta = timedelta(seconds=30),
    ):
        self.db = db
        self.transport = transport
        self.now = now or (lambda: datetime.now(UTC))
        self.max_attempts = max_attempts
        self.retry_base = retry_base
        self.lease_for = lease_for

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
                max_attempts=self.max_attempts, next_attempt_at=current + delay,
            )
            return False
        self.db.mark_outbox_sent(int(row["id"]), sent_at=self.now(), smtp_message_id=smtp_id)
        return True
