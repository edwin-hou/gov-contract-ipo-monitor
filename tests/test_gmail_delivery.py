import base64
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from types import SimpleNamespace

import httpx
import pytest

from contract_ipo_monitor.gmail_delivery import (
    DefinitiveDeliveryFailure, GmailOAuthTransport, MIME_TREE_VERSION, UnknownDelivery, message_content_sha256,
    _message_id,
)

SENDER = "sender@example.com"
RECIPIENT = "recipient@example.com"
IDENTITY = "<sealed-123@ipo-monitor.local>"
NOW = datetime(2026, 10, 6, tzinfo=UTC)


def mime(*, recipient=RECIPIENT, subject="Research — verified", body="One line\nTwo lines\n", attachment=b"exact attachment"):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = SENDER
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = IDENTITY
    message.set_content(body)
    message.add_attachment(attachment, maintype="application", subtype="octet-stream", filename="report.bin")
    return message.as_bytes()


def presentation_mime():
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"] = SENDER, RECIPIENT
    message["Subject"], message["Message-ID"] = "Conditional research", IDENTITY
    message.set_content("Entry reference 100. No fill assumed.\n")
    message.add_alternative("<html><body><p>Entry reference 100.</p></body></html>\n", subtype="html")
    # Transport integrity tests treat the PDF as opaque bytes; rendering has
    # separate document tests and must not be inferred from this fixture.
    message.add_attachment(b"%PDF-1.4\nopaque offline PDF fixture\n", maintype="application", subtype="pdf", filename="research-report.pdf")
    message.add_attachment(b'{"holdings":[],"trade_ideas":[]}', maintype="application", subtype="json", filename="research-report.json")
    return message.as_bytes()


def credentials(scopes=None):
    return SimpleNamespace(token="test-token", scopes=scopes or [
        "https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"],
        granted_scopes=None)


def transport(handler, **kwargs):
    return GmailOAuthTransport(credentials_loader=lambda: credentials(), expected_sender=SENDER,
                               allowed_recipient=RECIPIENT,
                               client=httpx.Client(transport=httpx.MockTransport(handler)),
                               now=lambda: NOW, **kwargs)


def received(raw, labels=None):
    return {"id": "gmail123", "threadId": "thread456", "labelIds": labels or ["SENT"],
            "raw": base64.urlsafe_b64encode(b"Received: from google\r\nDKIM-Signature: added\r\n" + raw).decode(),
            "internalDate": str(int(NOW.timestamp() * 1000))}


def common(request):
    if request.url.path.endswith("/profile"):
        return httpx.Response(200, json={"emailAddress": SENDER})
    if request.url.path.endswith("/messages"):
        return httpx.Response(200, json={"messages": []})
    return None


def test_verified_send_preserves_semantic_content_despite_delivery_headers_and_is_idempotent():
    raw = mime()
    calls = []
    def handler(request):
        calls.append(request.method)
        result = common(request)
        if result:
            return result
        if request.method == "POST":
            return httpx.Response(200, json={"id": "gmail123", "threadId": "thread456"})
        return httpx.Response(200, json=received(raw))
    sender = transport(handler)
    result = sender.deliver(raw, RECIPIENT, IDENTITY)
    assert result["gmail_message_id"] == "gmail123"
    assert result["content_sha256"] == message_content_sha256(raw)
    assert result["content_sha256_version"] == MIME_TREE_VERSION
    assert result["recipient"] == RECIPIENT and result["delivered_label"] == "SENT"
    assert sender.deliver(raw, RECIPIENT, IDENTITY) == result
    assert calls.count("POST") == 1


@pytest.mark.parametrize("failure", ["timeout", "server", "invalid_ack"])
def test_uncertain_post_is_never_retried_when_sent_search_is_empty(failure):
    posts = []
    def handler(request):
        result = common(request)
        if result:
            return result
        posts.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("response missing", request=request)
        if failure == "server":
            return httpx.Response(503)
        return httpx.Response(200, json={})
    sender = transport(handler)
    raw = mime()
    with pytest.raises(UnknownDelivery):
        sender.deliver(raw, RECIPIENT, IDENTITY)
    with pytest.raises(UnknownDelivery):
        sender.deliver(raw, RECIPIENT, IDENTITY)
    assert len(posts) == 1


def test_definitive_rate_rejection_can_be_safely_retried():
    attempts = []
    def handler(request):
        result = common(request)
        if result:
            return result
        attempts.append(request)
        return httpx.Response(429)
    sender = transport(handler)
    for _ in range(2):
        with pytest.raises(DefinitiveDeliveryFailure) as error:
            sender.deliver(mime(), RECIPIENT, IDENTITY)
        assert error.value.safe_to_retry
    assert len(attempts) == 2


def test_sent_reconciliation_prevents_a_second_post_and_requires_exact_attachment():
    raw = mime()
    posts = []
    def handler(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": SENDER})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "gmail123"}]})
        if request.method == "POST":
            posts.append(request)
        return httpx.Response(200, json=received(raw))
    sender = transport(handler)
    result = sender.deliver(raw, RECIPIENT, IDENTITY)
    assert result["gmail_message_id"] == "gmail123" and not posts
    with pytest.raises(UnknownDelivery):
        sender.reconcile(IDENTITY, RECIPIENT, mime(attachment=b"different bytes"))


@pytest.mark.parametrize("changed", ["recipient", "subject", "body", "label"])
def test_success_response_with_mismatched_readback_is_unknown(changed):
    raw = mime()
    altered = mime(recipient="wrong@example.com") if changed == "recipient" else (
        mime(subject="different") if changed == "subject" else mime(body="different") if changed == "body" else raw)
    def handler(request):
        result = common(request)
        if result:
            return result
        if request.method == "POST":
            return httpx.Response(200, json={"id": "gmail123", "threadId": "thread456"})
        return httpx.Response(200, json=received(altered, labels=["DRAFT"] if changed == "label" else None))
    sender = transport(handler)
    with pytest.raises(UnknownDelivery) as error:
        sender.deliver(raw, RECIPIENT, IDENTITY)
    assert error.value.partial_receipt["gmail_message_id"] == "gmail123"


def test_preflight_credentials_sender_and_recipient_validation_never_sends():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"emailAddress": "other@example.com"})
    sender = transport(handler)
    with pytest.raises(DefinitiveDeliveryFailure, match="authenticated_sender_mismatch"):
        sender.deliver(mime(), RECIPIENT, IDENTITY)
    assert all(request.method == "GET" for request in requests)
    requests.clear()
    with pytest.raises(DefinitiveDeliveryFailure, match="recipient_not_authorized"):
        sender.deliver(mime(), "someoneelse@example.com", IDENTITY)
    assert not requests
    sender.credentials_loader = lambda: credentials(["https://www.googleapis.com/auth/gmail.readonly"])
    with pytest.raises(DefinitiveDeliveryFailure, match="grants_required"):
        sender.deliver(mime(), RECIPIENT, IDENTITY)
    assert not requests


def test_bounded_readback_after_acceptance_preserves_unknown_receipt():
    def handler(request):
        result = common(request)
        if result:
            return result
        if request.method == "POST":
            return httpx.Response(200, json={"id": "gmail123"})
        return httpx.Response(200, content=b"x" * 2_001_000)
    sender = transport(handler, max_message_bytes=1000)
    with pytest.raises(UnknownDelivery) as error:
        sender.deliver(mime(), RECIPIENT, IDENTITY)
    assert error.value.partial_receipt["gmail_message_id"] == "gmail123"


def test_semantic_hash_accepts_transfer_encoding_and_line_endings_but_preserves_attachments():
    raw = mime()
    message = BytesParser(policy=policy.SMTP).parsebytes(raw)
    body = message.get_body(preferencelist=("plain",))
    text = body.get_content()
    body.set_content(text, cte="base64")
    reencoded = message.as_bytes().replace(b"\r\n", b"\n")
    assert message_content_sha256(reencoded) == message_content_sha256(raw)
    assert message_content_sha256(mime(attachment=b"altered")) != message_content_sha256(raw)


def test_mime_tree_digest_preserves_container_semantics_and_legacy_hash_is_explicit():
    raw = presentation_mime()
    retyped = raw.replace(b"multipart/alternative", b"multipart/mixed")
    assert retyped != raw and message_content_sha256(retyped) != message_content_sha256(raw)
    assert message_content_sha256(retyped, version="leaf-v1") == message_content_sha256(raw, version="leaf-v1")
    assert message_content_sha256(raw, version="mime-tree-v2") == message_content_sha256(raw)


@pytest.mark.parametrize("version", ["unknown", "", None, False, []])
def test_unrecognized_digest_versions_fail_closed(version):
    with pytest.raises(DefinitiveDeliveryFailure, match="unsupported_content_digest_version"):
        message_content_sha256(presentation_mime(), version=version)


@pytest.mark.parametrize("changed", ["html", "pdf", "container_type", "container_removed"])
def test_html_pdf_or_container_tampering_is_unknown_and_never_reposts(changed):
    raw = presentation_mime()
    message = BytesParser(policy=policy.SMTP).parsebytes(raw)
    if changed == "html":
        html = message.get_body(preferencelist=("html",))
        html.set_content(html.get_content().replace("100", "999"), subtype="html")
    elif changed == "pdf":
        pdf = next(part for part in message.iter_attachments() if part.get_filename() == "research-report.pdf")
        pdf.set_payload(base64.b64encode(pdf.get_payload(decode=True) + b"TAMPER").decode())
    elif changed == "container_removed":
        alternative, *attachments = message.get_payload()
        message.set_payload([*alternative.get_payload(), *attachments])
    altered = (raw.replace(b"multipart/alternative", b"multipart/mixed") if changed == "container_type"
               else message.as_bytes())
    posts = []

    def handler(request):
        result = common(request)
        if result:
            return result
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"id": "gmail123", "threadId": "thread456"})
        return httpx.Response(200, json=received(altered))

    sender = transport(handler)
    with pytest.raises(UnknownDelivery, match="gmail_exact_readback_mismatch"):
        sender.deliver(raw, RECIPIENT, IDENTITY)
    with pytest.raises(UnknownDelivery):
        sender.deliver(raw, RECIPIENT, IDENTITY)
    assert len(posts) == 1


def test_html_pdf_transfer_encoding_boundary_and_line_ending_changes_allow_exact_readback():
    raw = presentation_mime()
    message = BytesParser(policy=policy.SMTP).parsebytes(raw)
    plain = message.get_body(preferencelist=("plain",))
    plain.set_content(plain.get_content(), cte="quoted-printable")
    html = message.get_body(preferencelist=("html",))
    html.set_content(html.get_content(), subtype="html", cte="base64")
    pdf = next(part for part in message.iter_attachments() if part.get_filename() == "research-report.pdf")
    pdf.set_content(pdf.get_payload(decode=True), maintype="application", subtype="pdf", filename="research-report.pdf", cte="quoted-printable")
    for index, part in enumerate(message.walk()):
        if part.is_multipart():
            part.set_boundary(f"normalized-boundary-{index}")
    normalized = message.as_bytes().replace(b"\r\n", b"\n")
    assert message_content_sha256(normalized) == message_content_sha256(raw)
    posts = []

    def handler(request):
        result = common(request)
        if result:
            return result
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"id": "gmail123", "threadId": "thread456"})
        return httpx.Response(200, json=received(normalized))

    receipt = transport(handler).deliver(raw, RECIPIENT, IDENTITY)
    assert receipt["content_sha256_version"] == MIME_TREE_VERSION
    assert receipt["content_sha256"] == message_content_sha256(raw) and len(posts) == 1


@pytest.mark.parametrize("result", [{"messages": [None]}, {"messages": [{"id": "one"}, {"id": "two"}]},
                                    {"messages": [{"id": "one"}], "nextPageToken": "more"}])
def test_malformed_or_ambiguous_reconciliation_never_sends(result):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": SENDER})
        return httpx.Response(200, json=result)
    with pytest.raises(UnknownDelivery):
        transport(handler).deliver(mime(), RECIPIENT, IDENTITY)
    assert "POST" not in calls


def test_external_recipient_inbox_label_cannot_falsely_prove_sent_delivery():
    raw = mime()
    def handler(request):
        result = common(request)
        if result:
            return result
        if request.method == "POST":
            return httpx.Response(200, json={"id": "gmail123"})
        return httpx.Response(200, json=received(raw, labels=["INBOX"]))
    with pytest.raises(UnknownDelivery, match="readback_mismatch"):
        transport(handler).deliver(raw, RECIPIENT, IDENTITY)


def test_unexpected_transport_failure_is_sanitized_and_cannot_enable_resend():
    posts = []
    def handler(request):
        result = common(request)
        if result:
            return result
        posts.append(request)
        raise OSError("sensitive provider detail")
    sender = transport(handler)
    for _ in range(2):
        with pytest.raises(UnknownDelivery) as failure:
            sender.deliver(mime(), RECIPIENT, IDENTITY)
        assert "sensitive" not in str(failure.value)
    assert len(posts) == 1


def test_self_delivery_readback_receipt_records_actual_inbox_label():
    raw = mime(recipient=SENDER)
    def handler(request):
        result = common(request)
        if result:
            return result
        if request.method == "POST":
            return httpx.Response(200, json={"id": "gmail123"})
        return httpx.Response(200, json=received(raw, labels=["INBOX"]))
    sender = GmailOAuthTransport(credentials_loader=credentials, expected_sender=SENDER,
                                allowed_recipient=SENDER, client=httpx.Client(transport=httpx.MockTransport(handler)),
                                now=lambda: NOW)
    receipt = sender.deliver(raw, SENDER, IDENTITY)
    assert receipt["delivered_label"] == "INBOX" and receipt["recipient"] == SENDER


def test_reconcile_with_unavailable_credentials_never_claims_definitive_rejection():
    sender = transport(lambda request: httpx.Response(401))
    with pytest.raises(UnknownDelivery, match="credentials_unavailable"):
        sender.reconcile(IDENTITY, RECIPIENT, mime())


def test_own_post_acknowledgement_allows_provider_identity_rewrite_only_with_exact_other_content():
    raw = mime()
    rewritten = raw.replace(IDENTITY.encode(), b"<provider-rewrite@mail.gmail.com>")
    posts = []
    def handler(request):
        result = common(request)
        if result:
            return result
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"id": "gmail123", "threadId": "thread456"})
        return httpx.Response(200, json=received(rewritten))
    sender = transport(handler)
    receipt = sender.deliver(raw, RECIPIENT, IDENTITY)
    assert len(posts) == 1 and receipt["rfc822_id"] == IDENTITY
    assert receipt["provider_rfc822_id"] == "<provider-rewrite@mail.gmail.com>"
    assert receipt["rfc822_identity_status"] == "provider_rewritten"
    assert receipt["content_sha256"] == message_content_sha256(raw)
    assert sender.deliver(raw, RECIPIENT, IDENTITY) == receipt and len(posts) == 1


@pytest.mark.parametrize("provider_identity", ["<provider-rewrite@mail.gmail.com>", "<CAP0eM+r=qeU6D2_hDn=UTP9ttSXegRN8J+3iGchBegxtowAhEQ@mail.gmail.com>"])
def test_persisted_acknowledgement_can_reconcile_rewritten_identity_readonly(provider_identity):
    raw = mime()
    rewritten = raw.replace(IDENTITY.encode(), provider_identity.encode())
    paths = []
    def handler(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": SENDER})
        return httpx.Response(200, json=received(rewritten))
    sender = transport(handler)
    receipt = sender.reconcile(IDENTITY, RECIPIENT, raw, provider_message_id="gmail123")
    assert receipt["gmail_message_id"] == "gmail123" and receipt["provider_rfc822_id"] != IDENTITY
    assert not any(path.endswith("/messages") for path in paths)
    with pytest.raises(UnknownDelivery):
        sender.reconcile(IDENTITY, RECIPIENT, mime(attachment=b"changed"), provider_message_id="gmail123")


def test_search_candidate_with_rewritten_identity_cannot_be_bound_or_trigger_another_send():
    raw = mime()
    rewritten = raw.replace(IDENTITY.encode(), b"<different-original@mail.gmail.com>")
    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": SENDER})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "gmail123"}]})
        return httpx.Response(200, json=received(rewritten))
    sender = transport(handler)
    with pytest.raises(UnknownDelivery, match="readback_mismatch"):
        sender.deliver(raw, RECIPIENT, IDENTITY)


@pytest.mark.parametrize("local", ["valid=identity", "valid/identity", "valid?identity", "valid%identity", "valid~identity"])
def test_rfc_dot_atom_id_accepts_supported_provider_punctuation(local):
    assert _message_id(f"<{local}@mail.gmail.com>") == f"{local}@mail.gmail.com"


@pytest.mark.parametrize("identity", ["<bad..dots@mail.gmail.com>", "<.leading@mail.gmail.com>", "<trailing.@mail.gmail.com>",
                                      "<bad@mail..gmail.com>", "<bad\r\n@mail.gmail.com>", "\n<bad@mail.gmail.com>",
                                      "<bad\x00@mail.gmail.com>", "<bad name@mail.gmail.com>"])
def test_message_identity_rejects_invalid_dot_placement_whitespace_and_controls(identity):
    with pytest.raises(DefinitiveDeliveryFailure):
        _message_id(identity)
