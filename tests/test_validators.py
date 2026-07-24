from datetime import UTC, datetime, timedelta

from contract_ipo_monitor.entity import EntityResolver
from contract_ipo_monitor.models import (
    Candidate,
    ContractEvidence,
    EvidenceClass,
    ListingRoute,
    ListingSignal,
    MarketSnapshot,
)
from contract_ipo_monitor.validators import (
    ContractValidator,
    ListingValidator,
    SmallCompanyValidator,
)

NOW = datetime(2026, 7, 24, 18, 0, tzinfo=UTC)


def contract(**overrides):
    data = dict(
        source="usaspending",
        source_url="https://www.usaspending.gov/award/CONT_AWD_ABC",
        source_record_id="CONT_AWD_ABC",
        retrieved_at=NOW,
        published_at=NOW - timedelta(minutes=2),
        award_id="ABC-123",
        modification_number="0",
        status="awarded",
        award_date=NOW.date(),
        agency="Department of Energy",
        subagency="Office of Science",
        office="Acquisitions",
        recipient_name="Acme Quantum, Inc.",
        recipient_uei="UEI123456789",
        recipient_cage="1AB23",
        recipient_address="1 Market St, San Francisco, CA 94105",
        prime=True,
        obligated_amount=12_000_000,
        current_value=12_000_000,
        ceiling_amount=30_000_000,
        award_type="definitive_contract",
        description="Quantum sensing systems",
        evidence_class=EvidenceClass.A,
        raw_payload_hash="abc123",
    )
    data.update(overrides)
    return ContractEvidence(**data)


def listing(**overrides):
    data = dict(
        source="sec",
        source_url="https://www.sec.gov/Archives/edgar/data/1/filing.htm",
        signal_id="0000000000-26-000001",
        issuer_name="Acme Quantum, Inc.",
        issuer_address="1 Market St, San Francisco, CA 94105",
        cik="0000000001",
        filed_at=NOW - timedelta(hours=1),
        active=True,
        route=ListingRoute.S1,
        form_type="S-1",
        is_initial_listing=True,
        intends_public_trading=True,
        expected_exchange="NASDAQ",
        proposed_price=4.25,
        proposed_valuation=220_000_000,
        max_offering_size=75_000_000,
        linked_ueis=("UEI123456789",),
        external_corroboration=True,
    )
    data.update(overrides)
    return ListingSignal(**data)


def test_contract_validator_accepts_structured_funded_award():
    result = ContractValidator().validate(contract())
    assert result.passed is True
    assert result.code == "actual_contract_confirmed"


def test_contract_validator_rejects_solicitation():
    result = ContractValidator().validate(contract(status="solicitation"))
    assert result.passed is False
    assert "solicitation" in result.reason.lower()


def test_contract_validator_rejects_zero_obligation_idv():
    result = ContractValidator().validate(
        contract(award_type="idv", obligated_amount=0, current_value=0)
    )
    assert result.passed is False
    assert "funded" in result.reason.lower()


def test_listing_validator_accepts_active_initial_s1():
    result = ListingValidator(now=NOW).validate(listing())
    assert result.passed is True


def test_listing_validator_rejects_resale_registration():
    result = ListingValidator(now=NOW).validate(listing(is_initial_listing=False))
    assert result.passed is False
    assert "initial" in result.reason.lower()


def test_listing_validator_accepts_option_b_two_factor_route():
    signal = listing(
        route=ListingRoute.OPTION_B,
        form_type=None,
        is_initial_listing=False,
        intends_public_trading=True,
        named_underwriter="Example Securities",
        expected_exchange="NYSE American",
        listing_application_announced=True,
        external_corroboration=True,
    )
    assert ListingValidator(now=NOW).validate(signal).passed is True


def test_listing_validator_rejects_option_b_without_external_support():
    signal = listing(
        route=ListingRoute.OPTION_B,
        form_type=None,
        is_initial_listing=False,
        named_underwriter="Example Securities",
        expected_exchange="NYSE American",
        listing_application_announced=False,
        external_corroboration=False,
    )
    assert ListingValidator(now=NOW).validate(signal).passed is False


def test_listing_validator_rejects_withdrawn_signal():
    assert ListingValidator(now=NOW).validate(listing(active=False, status="withdrawn")).passed is False


def test_entity_resolver_accepts_exact_uei_link():
    result = EntityResolver().resolve(contract(), listing())
    assert result.matched is True
    assert result.method == "uei"


def test_entity_resolver_rejects_fuzzy_name_only():
    result = EntityResolver().resolve(
        contract(recipient_name="Acme Quantum Holdings LLC", recipient_uei=None, recipient_address=None),
        listing(issuer_name="Acme Quantum Inc", linked_ueis=(), issuer_address=None),
    )
    assert result.matched is False


def test_public_small_company_requires_fresh_price_and_market_cap():
    quote = MarketSnapshot(
        symbol="ACME",
        venue="NASDAQ",
        quote_at=NOW - timedelta(minutes=15),
        price=4.75,
        market_cap=250_000_000,
        shares_outstanding=52_631_579,
        volume=100_000,
        source="twelve_data",
    )
    result = SmallCompanyValidator(now=NOW).validate(listing(ticker="ACME"), quote)
    assert result.passed is True


def test_public_small_company_rejects_stale_quote():
    quote = MarketSnapshot(
        symbol="ACME", venue="NASDAQ", quote_at=NOW - timedelta(days=2),
        price=4.0, market_cap=200_000_000, shares_outstanding=50_000_000,
        volume=1_000, source="twelve_data",
    )
    assert SmallCompanyValidator(now=NOW).validate(listing(ticker="ACME"), quote).passed is False


def test_private_small_company_accepts_primary_filing_valuation_proxy():
    result = SmallCompanyValidator(now=NOW).validate(listing(ticker=None), None)
    assert result.passed is True


def test_private_small_company_rejects_missing_valuation_proxy():
    result = SmallCompanyValidator(now=NOW).validate(
        listing(ticker=None, proposed_valuation=None, max_offering_size=None, transaction_value=None),
        None,
    )
    assert result.passed is False
