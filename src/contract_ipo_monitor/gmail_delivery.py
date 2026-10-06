"""One-shot Gmail OAuth delivery with semantic MIME readback.

The caller must durably claim its outbox before calling ``deliver`` and retain
unknown outcomes across restarts. A timeout is never permission to send again.
Credentials are injected; this module neither locates nor refreshes token files.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from typing import Any, Callable

import httpx

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
SEND_SCOPES = {"https://mail.google.com/", "https://www.googleapis.com/auth/gmail.send",
               "https://www.googleapis.com/auth/gmail.modify"}
READ_SCOPES = {"https://mail.google.com/", "https://www.googleapis.com/auth/gmail.readonly",
               "https://www.googleapis.com/auth/gmail.modify"}
# RFC 5322 section 3.2.3: Gmail-generated IDs can include '=' and other atext.
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
RFC822_ID_PATTERN = rf"{_ATOM}(?:\.{_ATOM})*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
MIME_TREE_VERSION = "mime-tree-v2"
LEGACY_LEAF_VERSION = "leaf-v1"
CONTENT_DIGEST_VERSIONS = frozenset({MIME_TREE_VERSION, LEGACY_LEAF_VERSION})


class DefinitiveDeliveryFailure(RuntimeError):
    """No message was accepted; retry only after any stated precondition heals."""
    def __init__(self, code: str, *, safe_to_retry: bool = False):
        super().__init__(code)
        self.code = code
        self.safe_to_retry = safe_to_retry


class UnknownDelivery(RuntimeError):
    """Acceptance or exact readback is uncertain: reconcile; never blind resend."""
    def __init__(self, code: str, *, partial_receipt: dict | None = None):
        super().__init__(code)
        self.code = code
        self.partial_receipt = dict(partial_receipt or {})


def _address(value: str) -> str:
    if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
        raise DefinitiveDeliveryFailure("invalid_email_address")
    addresses = getaddresses([value])
    if len(addresses) != 1 or addresses[0][1] != value or not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", value):
        raise DefinitiveDeliveryFailure("invalid_email_address")
    return value.casefold()


def _message_id(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise DefinitiveDeliveryFailure("invalid_rfc822_message_id")
    text = str(value).strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]
    if not re.fullmatch(RFC822_ID_PATTERN, text) or len(text) > 250:
        raise DefinitiveDeliveryFailure("invalid_rfc822_message_id")
    return text


def _semantic(raw: bytes) -> dict:
    try:
        raw.decode("utf-8")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if any(part.defects for part in message.walk()):
            raise ValueError("Malformed MIME")
        for key in ("From", "To", "Subject", "Message-ID"):
            if len(message.get_all(key, [])) != 1:
                raise ValueError("Missing or duplicate identity header")
        if message.get_all("Cc") or message.get_all("Bcc"):
            raise ValueError("Unexpected recipients")
        addresses = {}
        for key in ("From", "To"):
            found = getaddresses([str(message[key])])
            if len(found) != 1:
                raise ValueError("Multiple addresses")
            addresses[key.lower()] = _address(found[0][1])
        parts = []
        for part in message.walk():
            if part.is_multipart():
                continue
            body = part.get_payload(decode=True)
            if body is None:
                raise ValueError("MIME payload missing")
            item = {"type": part.get_content_type(), "disposition": part.get_content_disposition(),
                    "filename": part.get_filename(), "content_id": str(part.get("Content-ID", ""))}
            if part.get_content_maintype() == "text" and part.get_content_disposition() != "attachment":
                text = body.decode(part.get_content_charset() or "ascii", errors="strict")
                # SMTP/Gmail may normalize wire line endings and transfer encoding.
                item["text"] = text.replace("\r\n", "\n").replace("\r", "\n")
            else:
                item["bytes_sha256"] = hashlib.sha256(body).hexdigest()
                item["bytes_length"] = len(body)
            parts.append(item)
        if not parts:
            raise ValueError("MIME has no payload")
        leaf_index = 0

        def mime_tree(part):
            nonlocal leaf_index
            node = {"type": part.get_content_type(), "disposition": part.get_content_disposition(),
                    "filename": part.get_filename(), "content_id": str(part.get("Content-ID", ""))}
            if part.is_multipart():
                node["children"] = [mime_tree(child) for child in part.get_payload()]
            else:
                node["part_index"] = leaf_index
                leaf_index += 1
            return node

        return {**addresses, "subject": str(message["Subject"]),
                "message_id": _message_id(str(message["Message-ID"])), "parts": parts,
                "mime_tree": mime_tree(message)}
    except DefinitiveDeliveryFailure:
        raise
    except (ValueError, UnicodeError, LookupError, TypeError):
        raise DefinitiveDeliveryFailure("invalid_message_payload") from None


def message_content_sha256(raw: bytes, *, version: str = MIME_TREE_VERSION) -> str:
    """Hash sender/recipient/subject/identity plus decoded bodies and attachments.

    Delivery-added headers, MIME boundary strings and transfer encodings are ignored.
    Attachment bytes and names, displayed body text, recipients, and the ordered
    MIME structure remain exact. ``leaf-v1`` exists only to verify old receipts.
    """
    if not isinstance(version, str) or version not in CONTENT_DIGEST_VERSIONS:
        raise DefinitiveDeliveryFailure("unsupported_content_digest_version")
    value = _semantic(raw)
    if version == LEGACY_LEAF_VERSION:
        value.pop("mime_tree")
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class GmailOAuthTransport:
    def __init__(self, *, credentials_loader: Callable[[], Any], expected_sender: str,
                 allowed_recipient: str, client: httpx.Client | None = None,
                 now: Callable[[], datetime] | None = None, timeout: float = 20,
                 max_message_bytes: int = 5_000_000):
        self.credentials_loader = credentials_loader
        self.expected_sender = _address(expected_sender)
        self.allowed_recipient = _address(allowed_recipient)
        if timeout <= 0 or not 1 <= max_message_bytes <= 25_000_000:
            raise ValueError("Invalid Gmail transport bounds")
        self.now = now or (lambda: datetime.now(UTC))
        self.max_message_bytes = max_message_bytes
        self.max_response_bytes = max_message_bytes * 4 // 3 + 2_000_000
        self.timeout = timeout
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._owns_client = client is None
        self._attempted: set[str] = set()
        self._verified: dict[str, dict] = {}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _time(self) -> datetime:
        stamp = self.now()
        if stamp.utcoffset() is None:
            raise ValueError("Gmail transport clock must be timezone aware")
        return stamp.astimezone(UTC)

    def _request(self, method: str, suffix: str, token: str, *, after_send: bool = False, **kwargs) -> dict:
        try:
            with self.client.stream(method, BASE + suffix,
                                    headers={"Authorization": "Bearer " + token},
                                    timeout=self.timeout, follow_redirects=False, **kwargs) as response:
                status = response.status_code
                if status < 200 or status >= 300:
                    if method == "POST" and (status < 400 or status >= 500 or status in {408, 409}):
                        raise UnknownDelivery("gmail_send_response_uncertain")
                    if after_send:
                        raise UnknownDelivery("gmail_readback_unavailable")
                    raise DefinitiveDeliveryFailure(f"gmail_http_{status}",
                                                    safe_to_retry=status in {401, 429} or status >= 500)
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > self.max_response_bytes:
                        raise ValueError("Response bound exceeded")
                    raw.extend(chunk)
                data = json.loads(raw)
                if not isinstance(data, dict) or "error" in data:
                    raise ValueError("Invalid response")
                return data
        except (UnknownDelivery, DefinitiveDeliveryFailure):
            raise
        except Exception:
            if method == "POST" or after_send:
                raise UnknownDelivery("gmail_response_uncertain") from None
            raise DefinitiveDeliveryFailure("gmail_preflight_unavailable", safe_to_retry=True) from None

    def _token(self) -> str:
        try:
            credentials = self.credentials_loader()
            scopes = set(getattr(credentials, "granted_scopes", None)
                         or getattr(credentials, "scopes", None) or ())
            token = getattr(credentials, "token", None)
            if not scopes.intersection(SEND_SCOPES) or not scopes.intersection(READ_SCOPES):
                raise DefinitiveDeliveryFailure("gmail_send_and_read_grants_required")
            if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n\x00"):
                raise DefinitiveDeliveryFailure("gmail_credentials_missing", safe_to_retry=True)
            profile = self._request("GET", "/profile", token)
            if _address(str(profile.get("emailAddress", ""))) != self.expected_sender:
                raise DefinitiveDeliveryFailure("gmail_authenticated_sender_mismatch")
            return token
        except DefinitiveDeliveryFailure:
            raise
        except Exception:
            raise DefinitiveDeliveryFailure("gmail_credentials_unavailable", safe_to_retry=True) from None

    def _expected(self, raw: bytes, recipient: str, rfc822_id: str) -> tuple[str, dict]:
        if not isinstance(raw, bytes) or not raw or len(raw) > self.max_message_bytes:
            raise DefinitiveDeliveryFailure("gmail_message_size_invalid")
        recipient = _address(recipient)
        if recipient != self.allowed_recipient:
            raise DefinitiveDeliveryFailure("gmail_recipient_not_authorized")
        identity = _message_id(rfc822_id)
        expected = _semantic(raw)
        if expected["to"] != recipient or expected["from"] != self.expected_sender or expected["message_id"] != identity:
            raise DefinitiveDeliveryFailure("gmail_message_identity_mismatch")
        return identity, expected

    def _readback(self, provider_id: str, token: str, expected: dict, raw: bytes,
                  *, accepted_at: str | None = None, acknowledged_id: bool = False) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", provider_id):
            raise UnknownDelivery("gmail_message_id_invalid")
        partial = {"gmail_message_id": provider_id, "provider_accepted_at": accepted_at}
        try:
            data = self._request("GET", "/messages/" + provider_id, token, after_send=True,
                                 params={"format": "raw"})
        except UnknownDelivery as exc:
            exc.partial_receipt = {**partial, **exc.partial_receipt}
            raise
        try:
            encoded = data["raw"]
            if not isinstance(encoded, str) or len(encoded) > self.max_response_bytes:
                raise ValueError("Raw message missing")
            actual_raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            if len(actual_raw) > self.max_message_bytes:
                raise ValueError("Readback bound exceeded")
            actual = _semantic(actual_raw)
            provider_identity = actual["message_id"]
            rewritten = provider_identity != expected["message_id"]
            # Gmail may replace the RFC822 Message-ID during submission. Only
            # our own acknowledged provider ID binds that rewrite to this send;
            # a search-only candidate must preserve the original identity.
            if rewritten and acknowledged_id:
                actual["message_id"] = expected["message_id"]
            labels = data.get("labelIds", [])
            sent_label = isinstance(labels, list) and "SENT" in labels
            self_inbox = isinstance(labels, list) and "INBOX" in labels and expected["to"] == self.expected_sender
            if data.get("id") != provider_id or actual != expected or not (sent_label or self_inbox):
                raise ValueError("Semantic readback mismatch")
            verified_at = self._time().isoformat()
            if accepted_at is None:
                accepted_at = verified_at
                milliseconds = str(data.get("internalDate", ""))
                if milliseconds.isdigit():
                    recorded = datetime.fromtimestamp(int(milliseconds) / 1000, UTC)
                    if recorded <= self._time() + timedelta(minutes=5):
                        accepted_at = recorded.isoformat()
            return {"gmail_message_id": provider_id, "thread_id": str(data.get("threadId", "")),
                    "provider_accepted_at": accepted_at, "verified_at": verified_at,
                    "delivered_label": "INBOX" if "INBOX" in labels and expected["to"] == self.expected_sender else "SENT",
                    "content_sha256": message_content_sha256(raw), "content_sha256_version": MIME_TREE_VERSION,
                    "raw_content_sha256": hashlib.sha256(raw).hexdigest(),
                    "recipient": expected["to"], "sender": self.expected_sender,
                    "rfc822_id": "<" + expected["message_id"] + ">",
                    "provider_rfc822_id": "<" + provider_identity + ">",
                    "rfc822_identity_status": "provider_rewritten" if rewritten else "preserved",
                    "readback_raw_sha256": hashlib.sha256(actual_raw).hexdigest()}
        except (ValueError, KeyError, UnicodeError, OverflowError, DefinitiveDeliveryFailure):
            raise UnknownDelivery("gmail_exact_readback_mismatch", partial_receipt=partial) from None

    def _reconcile(self, identity: str, expected: dict, raw: bytes, token: str,
                   *, after_send: bool = False) -> dict | None:
        data = self._request("GET", "/messages", token, after_send=after_send,
                             params={"q": "in:anywhere rfc822msgid:" + identity,
                                     "includeSpamTrash": "true", "maxResults": 10})
        found = data.get("messages", [])
        if not isinstance(found, list) or data.get("nextPageToken") or len(found) > 1:
            raise UnknownDelivery("gmail_reconciliation_ambiguous")
        if not found:
            return None
        if not isinstance(found[0], dict):
            raise UnknownDelivery("gmail_reconciliation_invalid")
        receipt = self._readback(str(found[0].get("id", "")), token, expected, raw)
        self._verified[identity] = receipt
        return dict(receipt)

    def reconcile(self, rfc822_id: str, recipient: str, expected_raw: bytes,
                  *, provider_message_id: str | None = None) -> dict | None:
        """Read only. ``None`` does not prove an uncertain POST was rejected.

        ``provider_message_id`` must come from this sealed outbox item's own
        persisted POST acknowledgement, never from a subject/body search.
        """
        identity, expected = self._expected(expected_raw, recipient, rfc822_id)
        try:
            token = self._token()
        except DefinitiveDeliveryFailure:
            raise UnknownDelivery("gmail_reconciliation_credentials_unavailable") from None
        if provider_message_id is not None:
            receipt = self._readback(provider_message_id, token, expected, expected_raw, acknowledged_id=True)
            self._verified[identity] = receipt
            return dict(receipt)
        return self._reconcile(identity, expected, expected_raw, token, after_send=True)

    def deliver(self, raw_message: bytes, recipient: str, rfc822_id: str) -> dict:
        """POST at most once per identity; caller persists uncertainty across restart."""
        identity, expected = self._expected(raw_message, recipient, rfc822_id)
        if identity in self._verified:
            known = self._verified[identity]
            if known["content_sha256"] != message_content_sha256(raw_message):
                raise DefinitiveDeliveryFailure("gmail_message_id_reused_for_different_content")
            return dict(known)
        try:
            token = self._token()
        except DefinitiveDeliveryFailure:
            if identity in self._attempted:
                raise UnknownDelivery("gmail_prior_attempt_credentials_unavailable") from None
            raise
        receipt = self._reconcile(identity, expected, raw_message, token, after_send=identity in self._attempted)
        if receipt:
            return receipt
        if identity in self._attempted:
            raise UnknownDelivery("gmail_prior_attempt_unresolved")
        # Fence BEFORE entering the network. A later timeout never clears it.
        self._attempted.add(identity)
        try:
            sent = self._request("POST", "/messages/send", token,
                                 json={"raw": base64.urlsafe_b64encode(raw_message).decode("ascii")})
        except DefinitiveDeliveryFailure:
            self._attempted.discard(identity)
            raise
        accepted_at = self._time().isoformat()
        provider_id = str(sent.get("id", ""))
        partial = {"gmail_message_id": provider_id, "thread_id": str(sent.get("threadId", "")),
                   "provider_accepted_at": accepted_at, "recipient": expected["to"],
                   "content_sha256": message_content_sha256(raw_message), "content_sha256_version": MIME_TREE_VERSION}
        try:
            receipt = self._readback(provider_id, token, expected, raw_message, accepted_at=accepted_at, acknowledged_id=True)
        except UnknownDelivery as exc:
            exc.partial_receipt = {**partial, **exc.partial_receipt}
            raise
        self._verified[identity] = receipt
        return dict(receipt)
