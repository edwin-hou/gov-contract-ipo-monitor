import json
import smtplib
from datetime import timedelta

import httpx
import pytest

from contract_ipo_monitor.archive import EvidenceArchive
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.gate import AlertGate
from contract_ipo_monitor.models import MarketSnapshot
from contract_ipo_monitor.processor import EvidenceProcessor
from contract_ipo_monitor.smtp_worker import SMTPTransport, SMTPWorker
from contract_ipo_monitor.sources.http import PermanentHTTPError, ResilientClient
from contract_ipo_monitor.sources.sam import SAMCollector, SAMNormalizer
from contract_ipo_monitor.sources.usaspending import USAspendingCollector, USAspendingNormalizer
from contract_ipo_monitor.validators import ListingValidator, SmallCompanyValidator
from test_processor_service import NOW, contract, listing


def database(tmp_path):
    db = Database(tmp_path / "regression.db")
    db.initialize()
    return db


def test_precise_registration_withdrawal_blocks_later_contract_replay(tmp_path):
    db = database(tmp_path)
    processor = EvidenceProcessor(db, now=lambda: NOW)
    active = listing().model_copy(update={"registration_id": "333-123"})
    processor.ingest_contract(contract())
    processor.ingest_listing(active)
    withdrawal = active.model_copy(update={"signal_id": "RW-1", "filed_at": NOW, "active": False, "status": "withdrawn"})
    processor.ingest_listing(withdrawal)
    assert not any(item.active for item in db.load_listing_signals())
    assert db.fetch_outbox()[0]["status"] == "cancelled"
    assert db.fetch_outbox()[1]["subject"].startswith("[CORRECTION]")
    fresh = contract().model_copy(update={"source_record_id": "next", "award_id": "NEXT-AWARD"})
    assert processor.ingest_contract(fresh) == []
    assert db.count("alerts") == 1


def test_unscoped_withdrawal_does_not_corrupt_another_offering(tmp_path):
    db = database(tmp_path)
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    processor.ingest_listing(listing(active=False, status="withdrawn").model_copy(update={"signal_id": "RW-unlinked"}))
    assert db.count("outbox_messages") == 1
    assert any(item.active for item in db.load_listing_signals())


def test_correction_identity_is_intersection_and_html_is_escaped(tmp_path):
    db = database(tmp_path)
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    processor.ingest_listing(listing().model_copy(update={"signal_id": "OTHER"}))
    reason = '<script>alert("unsafe")</script> 10%_'
    assert db.enqueue_correction(signal_id="S1-1", company_name="Acme Quantum, Inc.", reason=reason, source_url="https://sec.gov/a", created_at=NOW) == 1
    assert db.enqueue_correction(signal_id="S1-1", company_name="Acme Quantum, Inc.", reason=reason, source_url="https://sec.gov/a", created_at=NOW) == 0
    correction = db.fetch_outbox()[-1]
    assert "<script>" not in correction["html_body"]
    assert "&lt;script&gt;" in correction["html_body"]
    assert db.fetch_outbox()[1]["status"] == "pending"


def test_cancelled_modification_suppresses_prior_active_award(tmp_path):
    db = database(tmp_path)
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    cancellation = contract(status="cancelled").model_copy(update={"modification_number": "1", "source_record_id": "cancel-1", "published_at": NOW + timedelta(minutes=1)})
    processor.ingest_contract(cancellation)
    assert not any(not item.cancelled for item in db.load_contracts())
    assert processor.ingest_listing(listing()) == []


def test_deleted_transaction_does_not_cancel_entire_award_or_unrelated_alert(tmp_path):
    db = database(tmp_path)
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    modification = contract().model_copy(update={"source_record_id": "mod-1", "modification_number": "1"})
    processor.ingest_contract(modification)
    processor.ingest_contract(modification.model_copy(update={"deleted": True, "status": "deleted", "published_at": NOW + timedelta(minutes=1)}))
    assert db.count("outbox_messages") == 1
    loaded = db.load_contracts()
    assert any(item.source_record_id == "a" and not item.deleted for item in loaded)
    assert not any(item.source_record_id == "mod-1" and not item.deleted for item in loaded)


def test_archive_failure_can_resume_same_source_record(tmp_path):
    db = database(tmp_path)

    class BrokenArchive:
        def write(self, *args, **kwargs):
            raise OSError("disk unavailable")

    processor = EvidenceProcessor(db, now=lambda: NOW, archive=BrokenArchive())
    with pytest.raises(OSError):
        processor.ingest_contract(contract())
    assert db.count("source_records") == 1
    assert db.count("contract_evidence") == 0
    processor.archive = EvidenceArchive(tmp_path / "archive")
    processor.ingest_contract(contract())
    assert db.count("source_records") == 1
    assert db.count("contract_evidence") == 1


def test_market_failure_can_resume_without_new_filing(tmp_path):
    db = database(tmp_path)
    quotes = [None]
    processor = EvidenceProcessor(db, now=lambda: NOW, market_lookup=lambda _: quotes[0])
    processor.ingest_contract(contract())
    active = listing().model_copy(update={"ticker": "ACME"})
    assert not processor.ingest_listing(active)[0].alert_created
    quotes[0] = MarketSnapshot(symbol="ACME", quote_at=NOW, price=4, market_cap=200_000_000, source="test")
    assert processor.ingest_listing(active)[0].alert_created
    assert processor.ingest_listing(active)[0].duplicate
    assert db.count("listing_signals") == 1
    assert db.count("candidate_matches") == 2


def test_database_context_closes_connection(tmp_path):
    db = database(tmp_path)
    with db.connect() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(Exception, match="closed"):
        conn.execute("SELECT 1")


def test_expired_worker_cannot_overwrite_new_lease(tmp_path):
    db = database(tmp_path)
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    first = db.lease_outbox(now=NOW, lease_for=timedelta(seconds=1))
    second = db.lease_outbox(now=NOW + timedelta(seconds=2))
    with pytest.raises(RuntimeError, match="lease"):
        db.mark_outbox_sent(first["id"], sent_at=NOW, smtp_message_id="old", lease_until=first["lease_until"])
    db.mark_outbox_sent(second["id"], sent_at=NOW, smtp_message_id="new", lease_until=second["lease_until"])
    assert db.fetch_outbox()[0]["smtp_message_id"] == "new"


def test_outbox_compares_instants_across_timezone_offsets(tmp_path):
    from datetime import timezone

    db = database(tmp_path)
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    central = NOW.astimezone(timezone(timedelta(hours=-5)))
    assert db.lease_outbox(now=central) is not None


def test_evidence_retry_is_idempotent_and_versions_link_to_previous(tmp_path):
    db = database(tmp_path)
    first = db.insert_source_record("usaspending", "a", {"version": 1}, observed_at=NOW)
    evidence = contract()
    first_version = db.store_contract_evidence(evidence, source_record_id=first.row_id, created_at=NOW)
    assert db.store_contract_evidence(evidence, source_record_id=first.row_id, created_at=NOW) == first_version
    assert db.count("contract_evidence") == 1
    second = db.insert_source_record("usaspending", "a", {"version": 2}, observed_at=NOW)
    second_version = db.store_contract_evidence(evidence.model_copy(update={"obligated_amount": 20}), source_record_id=second.row_id, created_at=NOW)
    with db.connect() as conn:
        row = conn.execute("SELECT supersedes_id FROM contract_evidence WHERE id=?", (second_version,)).fetchone()
        assert row["supersedes_id"] == first_version


def test_partial_smtp_retries_only_refused_recipients_and_ignores_quit_error(tmp_path, monkeypatch):
    db = database(tmp_path)
    processor = EvidenceProcessor(db, now=lambda: NOW)
    processor.ingest_contract(contract())
    processor.ingest_listing(listing())
    envelopes, identifiers = [], []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def ehlo(self):
            pass

        def starttls(self, **kwargs):
            pass

        def send_message(self, message, *, from_addr, to_addrs):
            envelopes.append(to_addrs)
            identifiers.append(message["Message-ID"])
            return {"b@example.com": (450, b"try later")} if len(envelopes) == 1 else {}

        def quit(self):
            raise smtplib.SMTPResponseException(421, b"disconnect")

        def close(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    transport = SMTPTransport(host="smtp.example.com", port=587, username=None, password=None, sender="a@example.com", recipients=("a@example.com", "b@example.com"))
    current = [NOW]
    worker = SMTPWorker(db, transport=transport, now=lambda: current[0], retry_base=timedelta(seconds=1))
    assert not worker.run_once()
    assert json.loads(db.fetch_outbox()[0]["accepted_recipients_json"]) == ["a@example.com"]
    current[0] += timedelta(seconds=2)
    assert worker.run_once()
    assert envelopes == [["a@example.com", "b@example.com"], ["b@example.com"]]
    assert identifiers[0] == identifiers[1]
    assert db.fetch_outbox()[0]["status"] == "sent"


def test_archive_rejects_parent_path_source(tmp_path):
    with pytest.raises(ValueError):
        EvidenceArchive(tmp_path).write("..", "id", {}, observed_at=NOW)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -1, 0])
def test_private_financial_screen_rejects_invalid_values(number):
    active = listing().model_copy(update={"proposed_valuation": number})
    assert not SmallCompanyValidator(now=NOW).validate(active, None).passed


def test_market_future_timestamp_and_symbol_mismatch_fail_closed():
    active = listing().model_copy(update={"ticker": "ACME"})
    future = MarketSnapshot(symbol="ACME", quote_at=NOW + timedelta(days=1), price=4, market_cap=10, source="test")
    assert not SmallCompanyValidator(now=NOW).validate(active, future).passed
    wrong = future.model_copy(update={"symbol": "OTHER", "quote_at": NOW})
    assert not SmallCompanyValidator(now=NOW).validate(active, wrong).passed
    assert not ListingValidator(now=NOW).validate(active.model_copy(update={"filed_at": NOW + timedelta(days=1)})).passed


def test_sam_real_nested_api_shape_preserves_dates_totals_and_type():
    row = {
        "contractId": {"piid": "P1", "subtier": {"code": "9700", "name": "DOD"}},
        "coreData": {"awardOrIDV": "AWARD", "awardOrIDVType": {"name": "DELIVERY ORDER"}},
        "awardDetails": {
            "dates": {"dateSigned": "2026-07-23T00:00:00Z"},
            "dollars": {"actionObligation": "5", "baseAndAllOptionsValue": "25"},
            "totalContractDollars": {"totalActionObligation": "15", "totalBaseAndExercisedOptionsValue": "20", "totalBaseAndAllOptionsValue": "40"},
            "productOrServiceInformation": {"descriptionOfContractRequirement": "Real nested description"},
            "awardeeData": {"awardeeHeader": {"awardeeName": "Acme"}, "awardeeUEIInformation": {"uniqueEntityId": "UEI"}},
        },
    }
    item = SAMNormalizer().normalize(row, observed_at=NOW)
    assert item.award_date.isoformat() == "2026-07-23"
    assert (item.obligated_amount, item.current_value, item.ceiling_amount) == (15, 20, 40)
    assert item.award_type == "delivery_order"
    assert item.description == "Real nested description"
    assert item.published_at is None


def test_usaspending_real_identity_location_and_missing_date():
    row = {"generated_internal_id": "CONT_AWD_P1", "Award ID": "P1", "Start Date": "2026-07-23", "Recipient Name": "Acme", "Recipient Location": {"address_line1": "1 Main", "city_name": "Nashville", "state_code": "TN"}}
    item = USAspendingNormalizer().normalize(row, observed_at=NOW)
    assert item.source_url.endswith("CONT_AWD_P1")
    assert item.recipient_address == "1 Main, Nashville, TN"
    assert item.published_at is None
    del row["Start Date"]
    with pytest.raises(ValueError, match="date"):
        USAspendingNormalizer().normalize(row, observed_at=NOW)


@pytest.mark.asyncio
async def test_sam_pagination_uses_page_offset():
    offsets = []

    def handler(request):
        offsets.append(int(request.url.params["offset"]))
        row = {"contractId": {"piid": f"P{offsets[-1]}"}, "awardDetails": {"dateSigned": "07/23/2026"}}
        return httpx.Response(200, json={"awardSummary": [row], "totalRecords": "2"})

    client = ResilientClient(transport=httpx.MockTransport(handler))
    try:
        rows = await SAMCollector(client, "fake").collect(observed_at=NOW, last_modified_start=NOW.date(), limit=1)
        assert len(rows) == 2 and offsets == [0, 1]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_usaspending_only_requests_supported_fields():
    def handler(request):
        fields = json.loads(request.content)["fields"]
        assert "Recipient UEI" in fields and "Recipient Location" in fields
        assert not {"Potential Award Amount", "Awarding Office", "Signed Date", "Contract Description"}.intersection(fields)
        return httpx.Response(200, json={"results": [], "page_metadata": {"hasNext": False}})

    client = ResilientClient(transport=httpx.MockTransport(handler))
    try:
        assert await USAspendingCollector(client).collect(observed_at=NOW) == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_rejects_private_redirect_and_retries_text():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/secret"})

    client = ResilientClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PermanentHTTPError, match="private"):
            await client.request_text("GET", "https://public.example/start")
        assert calls == ["https://public.example/start"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_text_retries_transient_protocol_error():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("connection reset")
        return httpx.Response(200, text="recovered")

    async def no_sleep(_):
        pass

    client = ResilientClient(transport=httpx.MockTransport(handler), sleeper=no_sleep)
    try:
        assert await client.request_text("GET", "https://public.example/data") == "recovered"
        assert len(calls) == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_stops_streaming_oversized_body_without_retry():
    consumed = []
    closed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (b"1234", b"5678", b"9012", b"never read"):
                consumed.append(chunk)
                yield chunk

        async def aclose(self):
            closed.append(True)

    client = ResilientClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Body())), max_response_bytes=8)
    try:
        with pytest.raises(PermanentHTTPError, match="byte limit"):
            await client.request_text("GET", "https://public.example/huge")
        assert consumed == [b"1234", b"5678", b"9012"]
        assert closed == [True]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_caps_decoded_gzip_body():
    import gzip

    body = gzip.compress(b"a" * 1000)
    client = ResilientClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(body))), max_response_bytes=100)
    try:
        with pytest.raises(PermanentHTTPError, match="byte limit"):
            await client.request_text("GET", "https://public.example/compressed")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_decodes_small_gzip_and_deflate_responses():
    import gzip
    import zlib

    for encoding, body in (("gzip", gzip.compress(b'{"ok":true}')), ("deflate", zlib.compress(b'{"ok":true}'))):
        client = ResilientClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Encoding": encoding}, stream=httpx.ByteStream(body))), max_response_bytes=100)
        try:
            assert await client.request_json("GET", "https://public.example/compressed") == {"ok": True}
        finally:
            await client.aclose()
