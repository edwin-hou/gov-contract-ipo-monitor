from datetime import UTC, datetime
import json

import httpx
import pytest

from contract_ipo_monitor.models import ListingRoute
from contract_ipo_monitor.sources.http import PermanentHTTPError, ResilientClient
from contract_ipo_monitor.sources.market import TwelveDataNormalizer
from contract_ipo_monitor.sources.sam import SAMNormalizer
from contract_ipo_monitor.sources.sec import SECNormalizer
from contract_ipo_monitor.sources.state_local import AdapterInventory
from contract_ipo_monitor.sources.usaspending import (
    USAspendingCollector,
    USAspendingNormalizer,
    USAspendingRecipientResolver,
)

NOW = datetime(2026, 7, 24, 18, 0, tzinfo=UTC)


def test_usaspending_normalizes_prime_contract():
    row = {
        "generated_unique_award_id": "CONT_AWD_ABC_0",
        "Award ID": "ABC-123",
        "Recipient Name": "Acme Quantum, Inc.",
        "Recipient UEI": "UEI123456789",
        "Award Amount": 12_000_000,
        "Total Obligation": 10_000_000,
        "Awarding Agency": "Department of Energy",
        "Awarding Sub Agency": "Office of Science",
        "Start Date": "2026-07-23",
        "End Date": "2027-07-23",
        "Contract Description": "Quantum sensors",
        "Contract Award Type": "Definitive Contract",
    }
    record = USAspendingNormalizer().normalize(row, observed_at=NOW)
    assert record.award_id == "ABC-123"
    assert record.recipient_uei == "UEI123456789"
    assert record.obligated_amount == 10_000_000
    assert record.description == "Quantum sensors"
    assert record.evidence_class.value == "A"


def test_sam_normalizes_nested_contract_award():
    row = {
        "contractId": {"piid": "ABC-123", "modificationNumber": "0", "transactionNumber": "0", "subtier": {"name": "DOE"}},
        "awardDetails": {
            "awardeeData": {
                "awardeeHeader": {"awardeeName": "ACME QUANTUM, INC."},
                "awardeeUEIInformation": {"uniqueEntityId": "UEI123456789", "cageCode": "1AB23"},
            },
            "dollarsObligated": 10000000,
            "totalDollarsObligated": 10000000,
            "baseAndAllOptionsValue": 20000000,
            "dateSigned": "07/23/2026",
            "descriptionOfContractRequirement": "Quantum sensors",
            "awardOrIDVTypeName": "DEFINITIVE CONTRACT",
        },
    }
    record = SAMNormalizer().normalize(row, observed_at=NOW)
    assert record.award_id == "ABC-123"
    assert record.recipient_cage == "1AB23"
    assert record.ceiling_amount == 20_000_000


def test_sec_classifier_requires_initial_listing_language():
    initial = SECNormalizer().classify_document(
        form_type="S-1", accession="0001", issuer_name="Acme Quantum, Inc.", cik="1",
        filed_at=NOW, source_url="https://sec.gov/1", text="This is our initial public offering. We applied to list our common stock on Nasdaq under the symbol ACME. The price is expected to be between $4.00 and $4.50.",
    )
    resale = SECNormalizer().classify_document(
        form_type="S-1", accession="0002", issuer_name="Acme Quantum, Inc.", cik="1",
        filed_at=NOW, source_url="https://sec.gov/2", text="This prospectus relates solely to the resale by selling stockholders of previously issued shares.",
    )
    assert initial is not None
    assert initial.route == ListingRoute.S1
    assert initial.is_initial_listing is True
    assert initial.expected_exchange == "NASDAQ"
    assert initial.proposed_price == 4.25
    assert initial.ticker == "ACME"
    assert resale is None


def test_sec_classifier_recognizes_primary_ipo_without_literal_initial_public_offering_phrase():
    text = (
        "We are offering 8,000,000 shares of our common stock. Prior to this offering there has been no public market for our common stock. "
        "We have applied to list our common stock on the Nasdaq Capital Market under the symbol QNTA. "
        "The public offering price is expected to be between $3.50 and $4.50. "
        "The maximum aggregate offering price is $40,000,000. "
        "Shares of our common stock outstanding immediately after this offering will be 50,000,000."
    )
    signal = SECNormalizer().classify_document(
        form_type="S-1", accession="0004", issuer_name="Quanta Systems, Inc.", cik="4",
        filed_at=NOW, source_url="https://sec.gov/4", text=text,
    )
    assert signal is not None
    assert signal.expected_exchange == "NASDAQ CAPITAL MARKET"
    assert signal.ticker == "QNTA"
    assert signal.proposed_price == 4.0
    assert signal.max_offering_size == 40_000_000
    assert signal.proposed_valuation == 200_000_000


def test_sec_classifier_recognizes_withdrawal_request():
    signal = SECNormalizer().classify_document(
        form_type="RW", accession="0003", issuer_name="Acme Quantum, Inc.", cik="1",
        filed_at=NOW, source_url="https://sec.gov/3", text="The registrant hereby requests withdrawal of Registration Statement No. 333-123456.",
    )
    assert signal is not None
    assert signal.active is False
    assert signal.status == "withdrawn"


def test_twelve_data_normalizer_fails_closed_without_market_cap():
    normalizer = TwelveDataNormalizer()
    assert normalizer.normalize(
        symbol="ACME", quote={"close": "4.50", "datetime": "2026-07-24 17:45:00", "volume": "1000", "exchange": "NASDAQ"},
        statistics={}, observed_at=NOW,
    ) is None


def test_twelve_data_normalizer_builds_snapshot_from_quote_and_shares():
    snapshot = TwelveDataNormalizer().normalize(
        symbol="ACME", quote={"close": "4.50", "datetime": "2026-07-24 17:45:00", "volume": "1000", "exchange": "NASDAQ"},
        statistics={"shares_outstanding": "50000000"}, observed_at=NOW,
    )
    assert snapshot is not None
    assert snapshot.market_cap == 225_000_000
    assert snapshot.price == 4.5


def test_state_local_inventory_never_claims_unconfigured_nationwide_coverage():
    inventory = AdapterInventory()
    inventory.register("ca-sam", "California State Contract Register", enabled=True)
    report = inventory.report()
    assert report[0]["name"] == "ca-sam"
    assert inventory.nationwide_complete is False


@pytest.mark.asyncio
async def test_usaspending_collector_follows_all_pages():
    pages = []

    def award(number):
        return {
            "internal_id": number,
            "Award ID": f"AWD-{number}",
            "Recipient Name": f"Company {number}",
            "Award Amount": number * 1000,
            "Awarding Agency": "DOE",
            "Start Date": "2026-07-24",
            "Contract Award Type": "Definitive Contract",
            "Contract Description": "Test award",
        }

    def handler(request):
        payload = json.loads(request.content.decode())
        page = payload["page"]
        pages.append(page)
        if page == 1:
            return httpx.Response(200, json={"results": [award(1)], "page_metadata": {"page": 1, "next": 2, "hasNext": True}})
        return httpx.Response(200, json={"results": [award(2)], "page_metadata": {"page": 2, "next": None, "hasNext": False}})

    client = ResilientClient(transport=httpx.MockTransport(handler), max_attempts=1)
    records = await USAspendingCollector(client).collect(observed_at=NOW, limit=1)
    assert pages == [1, 2]
    assert [record.award_id for record in records] == ["AWD-1", "AWD-2"]
    await client.aclose()


@pytest.mark.asyncio
async def test_usaspending_pagination_refuses_silent_truncation():
    def handler(_request):
        return httpx.Response(200, json={
            "results": [{
                "internal_id": 1, "Award ID": "AWD-1", "Recipient Name": "Acme", "Award Amount": 1,
                "Awarding Agency": "DOE", "Start Date": "2026-07-24", "Contract Award Type": "Definitive Contract",
            }],
            "page_metadata": {"page": 1, "next": 2, "hasNext": True},
        })

    client = ResilientClient(transport=httpx.MockTransport(handler), max_attempts=1)
    with pytest.raises(RuntimeError, match="refusing to truncate silently"):
        await USAspendingCollector(client).collect(observed_at=NOW, max_pages=1)
    await client.aclose()


@pytest.mark.asyncio
async def test_recipient_resolver_returns_only_unique_exact_uei():
    responses = [
        {"results": [
            {"name": "Acme Quantum, Inc.", "uei": "UEI-ACME"},
            {"name": "Acme Quantum Research LLC", "uei": "UEI-OTHER"},
        ]},
        {"results": [
            {"name": "Duplicate Corp", "uei": "UEI-1"},
            {"name": "Duplicate Corporation", "uei": "UEI-2"},
        ]},
    ]

    def handler(_request):
        return httpx.Response(200, json=responses.pop(0))

    client = ResilientClient(transport=httpx.MockTransport(handler), max_attempts=1)
    resolver = USAspendingRecipientResolver(client)
    assert await resolver.resolve_uei("Acme Quantum, Inc.") == "UEI-ACME"
    assert await resolver.resolve_uei("Duplicate Corp") is None
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_retries_5xx_then_succeeds():
    calls = []
    responses = [httpx.Response(503, json={"error": "busy"}), httpx.Response(200, json={"ok": True})]

    def handler(request):
        calls.append(request)
        return responses.pop(0)

    async def no_sleep(_seconds):
        return None

    client = ResilientClient(transport=httpx.MockTransport(handler), sleeper=no_sleep, max_attempts=2)
    assert await client.request_json("GET", "https://example.test/data") == {"ok": True}
    assert len(calls) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_does_not_retry_permanent_4xx():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"error": "forbidden"})

    client = ResilientClient(transport=httpx.MockTransport(handler), max_attempts=3)
    with pytest.raises(PermanentHTTPError):
        await client.request_json("GET", "https://example.test/data")
    assert len(calls) == 1
    await client.aclose()
