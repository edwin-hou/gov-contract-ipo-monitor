from dataclasses import replace
from datetime import date

import httpx
import pytest

from contract_ipo_monitor.fundamentals import FinancialFact, SECCompanyFactsCollector, financial_fact_from_dict, normalize_companyfacts
from contract_ipo_monitor.sources.http import ResilientClient
from contract_ipo_monitor.universe import benchmark_instruments, default_fundamentals, default_universe, rank_universe, select_watchlist, universe_metadata


NOW = date(2026, 10, 5)
ACCN = "0000000123-26-000001"


def result(start, end, value, *, filed="2026-08-05", currency="USD", accession=ACCN, form="10-Q"):
    return {"start": start, "end": end, "val": value, "filed": filed, "accn": accession, "form": form}


def companyfacts(revenue=None, income=None, *, income_currency="USD"):
    revenue = revenue if revenue is not None else [result("2026-04-01", "2026-06-30", 150), result("2025-04-01", "2025-06-30", 100)]
    income = income if income is not None else [result("2026-04-01", "2026-06-30", 30)]
    return {"cik": 123, "facts": {"us-gaap": {"Revenues": {"units": {"USD": revenue}}, "NetIncomeLoss": {"units": {income_currency: income}}}}}


def test_snapshot_uses_same_currency_reported_profit_and_honest_global_scope():
    facts = default_fundamentals()
    instruments = {row.symbol: row for row in default_universe()}
    assert len(instruments) == 10
    assert set(facts) == set(instruments)
    assert facts["TSM"].currency == "TWD"
    assert instruments["TSM"].currency == "USD"
    assert instruments["TSM"].listing_kind == "ADR"
    assert instruments["ASML"].listing_kind == "ordinary"
    assert facts["ASML"].currency == "EUR"
    assert facts["ASML"].accounting_standard == "US GAAP"
    assert facts["0700.HK"].currency == "CNY"
    assert instruments["0700.HK"].currency == "HKD"
    assert instruments["0700.HK"].nasdaq_symbol is None
    assert "unverified" in " ".join(facts["0700.HK"].limitations)
    assert "not an exhaustive" in universe_metadata()["methodology"]
    assert benchmark_instruments()[0].listing_kind == "etf"
    assert all(fact.is_fresh(NOW) and fact.net_income > 0 for fact in facts.values())


def test_micron_is_quarterly_gaap_column_not_annual_or_adjusted():
    fact = default_fundamentals()["MU"]
    assert fact.period_type == "quarter"
    assert fact.period_end == date(2026, 9, 3)
    assert fact.revenue == 54_229_000_000
    assert fact.prior_revenue == 11_315_000_000
    assert fact.net_income == 37_701_000_000
    assert fact.growth == pytest.approx(54_229 / 11_315 - 1)
    assert fact.net_margin == pytest.approx(37_701 / 54_229)
    assert "14 weeks" in " ".join(fact.limitations)
    assert financial_fact_from_dict(fact.to_dict()) == fact


def test_future_expired_lossmaking_or_mismatched_issuer_cannot_qualify():
    facts = default_fundamentals()
    facts["MU"] = replace(facts["MU"], reported_at=date(2026, 10, 6))
    facts["NVDA"] = replace(facts["NVDA"], net_income=-1)
    facts["PLTR"] = replace(facts["PLTR"], prior_revenue=facts["PLTR"].revenue)
    facts["ASML"] = replace(facts["ASML"], symbol="OTHER")
    selected = {row.symbol for row in select_watchlist(facts, now=NOW)}
    assert not selected & {"MU", "NVDA", "PLTR", "ASML"}
    assert select_watchlist(now=date(2027, 2, 1)) == ()
    assert not replace(default_fundamentals()["ASML"], currency="USD").currency == "EUR"
    bad = dict(default_fundamentals())
    bad["ASML"] = replace(bad["ASML"], currency="USD")
    assert not next(row for row in rank_universe(bad, now=NOW) if row.instrument.symbol == "ASML").eligible


def test_ratios_are_fractions_and_growth_order_is_transparent():
    rows = rank_universe(now=NOW)
    assert rows[0].instrument.symbol == "MU"
    assert all(left.fact.growth >= right.fact.growth for left, right in zip(rows, rows[1:]))
    assert default_fundamentals()["MSFT"].revenue_growth_percent == pytest.approx(default_fundamentals()["MSFT"].growth * 100)


def test_recent_restatement_does_not_make_ancient_financial_period_fresh():
    fact = FinancialFact(symbol="TEST", source_url="https://www.sec.gov/example", period_end=date(2024, 6, 30), reported_at=NOW, revenue=150, prior_revenue=100, net_income=30, currency="USD")
    assert not fact.is_fresh(NOW)
    assert not replace(fact, period_type="annual").is_fresh(NOW)
    assert replace(fact, period_type="annual", period_end=date(2025, 9, 30)).is_fresh(NOW)
    assert not replace(fact, period_end=date(2025, 9, 30)).is_fresh(NOW)


@pytest.mark.parametrize("field,value", [("revenue", float("inf")), ("net_income", float("nan")), ("prior_revenue", 0), ("revenue", True)])
def test_nonfinite_or_invalid_amounts_rejected(field, value):
    with pytest.raises(ValueError):
        replace(default_fundamentals()["NVDA"], **{field: value})


def test_annual_duration_cannot_be_declared_quarter_or_compared_with_ytd():
    fact = default_fundamentals()["MSFT"]
    with pytest.raises(ValueError, match="duration"):
        replace(fact, period_start=date(2025, 7, 1))
    with pytest.raises(ValueError, match="durations"):
        replace(fact, prior_period_start=date(2025, 1, 1))


def test_sec_same_filing_same_currency_prior_year_context_and_provenance():
    fact = normalize_companyfacts(companyfacts(), cik="0000000123", symbol="TEST", now=NOW)
    assert fact.growth == pytest.approx(0.5)
    assert fact.net_margin == pytest.approx(0.2)
    assert fact.period_type == "quarter"
    assert fact.revenue_tag == "Revenues"
    assert fact.accession == ACCN
    assert fact.source_kind == "sec"
    assert "000000012326000001" in fact.source_url
    assert normalize_companyfacts(companyfacts(income_currency="EUR"), cik="123", symbol="TEST", now=NOW) is None
    assert normalize_companyfacts(companyfacts(income=[result("2026-04-01", "2026-06-30", 30, accession="0000000123-26-000002")]), cik="123", symbol="TEST", now=NOW) is None


def test_sec_rejects_future_filing_ytd_prior_quarter_and_mixed_annual_context():
    examples = [
        [result("2026-04-01", "2026-06-30", 150, filed="2026-11-01"), result("2025-04-01", "2025-06-30", 100, filed="2026-11-01")],
        [result("2026-01-01", "2026-06-30", 150), result("2025-01-01", "2025-06-30", 100)],
        [result("2026-04-01", "2026-06-30", 150), result("2026-01-01", "2026-03-31", 100)],
        [result("2026-04-01", "2026-06-30", 150), result("2024-07-01", "2025-06-30", 100)],
    ]
    for revenue in examples:
        assert normalize_companyfacts(companyfacts(revenue), cik="123", symbol="TEST", now=NOW) is None


def test_sec_prior_value_from_different_accession_not_substituted():
    revenue = [result("2026-04-01", "2026-06-30", 150), result("2025-04-01", "2025-06-30", 100, accession="0000000123-25-000001")]
    assert normalize_companyfacts(companyfacts(revenue), cik="123", symbol="TEST", now=NOW) is None


def test_sec_conflicting_contexts_fail_closed_and_reported_losses_preserved():
    revenue = companyfacts()["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    revenue.append(result("2026-04-01", "2026-06-30", 155))
    assert normalize_companyfacts(companyfacts(revenue), cik="123", symbol="TEST", now=NOW) is None
    data = companyfacts(income=[result("2026-04-01", "2026-06-30", -30)])
    assert normalize_companyfacts(data, cik="123", symbol="TEST", now=NOW).net_income == -30
    data["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"].append(result("2026-04-01", "2026-06-30", 35))
    assert normalize_companyfacts(data, cik="123", symbol="TEST", now=NOW) is None


def test_sec_consolidated_and_parent_income_are_distinct_not_conflicting_duplicates():
    data = companyfacts()
    data["facts"]["us-gaap"]["ProfitLoss"] = {"units": {"USD": [result("2026-04-01", "2026-06-30", 31)]}}
    fact = normalize_companyfacts(data, cik="123", symbol="TEST", now=NOW)
    assert fact.net_income == 31
    assert fact.net_income_tag == "ProfitLoss"
    assert fact.net_income_label == "consolidated net income"
    del data["facts"]["us-gaap"]["ProfitLoss"]
    assert normalize_companyfacts(data, cik="123", symbol="TEST", now=NOW).net_income_label == "net income attributable to parent"


def test_sec_annual_fallback_explicit_and_custom_or_partial_revenue_not_invented():
    rows = [result("2025-07-01", "2026-06-30", 150, form="10-K"), result("2024-07-01", "2025-06-30", 100, form="10-K")]
    fact = normalize_companyfacts(companyfacts(rows, [result("2025-07-01", "2026-06-30", 30, form="10-K")]), cik="123", symbol="TEST", now=NOW)
    assert fact.period_type == "annual"
    assert "not quarterly growth" in " ".join(fact.limitations)
    data = companyfacts()
    data["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"] = data["facts"]["us-gaap"].pop("Revenues")
    assert normalize_companyfacts(data, cik="123", symbol="MELI", now=NOW) is None


async def test_collector_issuer_identity_and_bounded_existing_http_client():
    async def handler(request):
        assert request.url.path.endswith("CIK0000000123.json")
        return httpx.Response(200, json=companyfacts())
    client = ResilientClient(transport=httpx.MockTransport(handler), allowed_hosts=("data.sec.gov",))
    try:
        fact = await SECCompanyFactsCollector(client).latest("123", "TEST", NOW)
        assert fact.symbol == "TEST"
        with pytest.raises(ValueError):
            await SECCompanyFactsCollector(client).latest("../123", "TEST", NOW)
    finally:
        await client.aclose()
    with pytest.raises(ValueError, match="issuer"):
        normalize_companyfacts(companyfacts(), cik="124", symbol="TEST", now=NOW)
    malformed_identity = companyfacts()
    malformed_identity["cik"] = 123.5
    with pytest.raises(ValueError, match="issuer"):
        normalize_companyfacts(malformed_identity, cik="123", symbol="TEST", now=NOW)
