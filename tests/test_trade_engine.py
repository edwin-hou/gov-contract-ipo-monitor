"""Independent behavior tests using market scenarios and mismatched evidence."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from contract_ipo_monitor.fundamentals import FinancialFact
from contract_ipo_monitor.market import DailyBar, PriceHistory
from contract_ipo_monitor.trades import assess_trade
from contract_ipo_monitor.universe import Instrument
from contract_ipo_monitor.worldnews import WorldEvent


NOW = datetime(2026, 10, 5, 15, tzinfo=UTC)


def instrument():
    return Instrument(symbol="TEST", name="Test issuer", exchange="NASDAQ", currency="USD", provider_symbol="test.us", aliases=("Test issuer",), macro_exposures=("export_controls", "rates"), reporting_currency="USD", benchmark_symbol="ACWI")


def financials():
    return FinancialFact(symbol="TEST", source_url="https://www.sec.gov/Archives/example", period_end=date(2026, 6, 30), period_start=date(2026, 4, 1), prior_period_end=date(2025, 6, 30), prior_period_start=date(2025, 4, 1), reported_at=date(2026, 8, 1), revenue=150_000_000, prior_revenue=100_000_000, net_income=30_000_000, currency="USD", source_kind="sec")


def trading_dates(end=date(2026, 10, 2), count=65):
    days = []
    while len(days) < count:
        if end.weekday() < 5:
            days.append(end)
        end -= timedelta(days=1)
    return list(reversed(days))


def history(*, symbol="TEST", end=date(2026, 10, 2), step=.4, spread=1.0, observed_at=NOW, currency="USD"):
    prices = [80 + step * index for index in range(65)]
    bars = tuple(DailyBar(day, price-.2, price+spread, price-spread, price, 1_000_000) for day, price in zip(trading_dates(end), prices))
    return PriceHistory(symbol=symbol, bars=bars, source="nasdaq", provider_symbol=symbol, source_url=f"https://api.nasdaq.com/api/quote/{symbol}/historical", currency=currency, exchange="NASDAQ-GS", observed_at=observed_at, declared_exchange="NASDAQ", exchange_timezone="America/New_York", adjustment_status="unknown")


def assess(**changes):
    options = dict(instrument=instrument(), fact=financials(), history=history(), benchmark=history(symbol="ACWI", step=.1), sentiment={"label":"positive", "score":.3, "scored_count":4, "independent_origins":3}, events=(), now=NOW, world_coverage_ok=True)
    options.update(changes)
    return assess_trade(**options)


def test_valid_uptrend_produces_reviewable_ordered_levels_not_an_order():
    idea = assess()
    assert idea["action"] == "conditional_buy"
    assert 0 < idea["invalidation"] < idea["entry"] < idea["target"]
    actual_rr = (idea["target"] - idea["entry"]) / (idea["entry"] - idea["invalidation"])
    assert actual_rr == pytest.approx(idea["risk_reward"], abs=.01)
    assert "fresh quote" in " ".join(idea["conditions"])
    assert idea["confidence"].endswith("predictive_accuracy_unmeasured")
    assert "quantity" not in idea and "probability" not in idea
    assert financials().source_url in idea["evidence_urls"]


@pytest.mark.parametrize("changes", [
    {"symbol":"OTHER"}, {"currency":"EUR"}, {"source_kind":"commentary"},
    {"accounting_standard":"non-GAAP"},
])
def test_wrong_issuer_currency_or_unverified_profit_cannot_generate_buy(changes):
    idea = assess(fact=replace(financials(), **changes))
    assert idea["action"] == "wait"
    assert idea["entry"] is None


@pytest.mark.parametrize("changes", [
    {"symbol":"OTHER"}, {"currency":"EUR"}, {"exchange":"NYSE"},
])
def test_wrong_market_identity_currency_or_venue_cannot_generate_buy(changes):
    assert assess(history=replace(history(), **changes))["action"] == "wait"


def test_missing_short_stale_or_future_prices_wait():
    assert assess(history=None)["action"] == "wait"
    assert assess(history=replace(history(), bars=history().bars[-20:]))["action"] == "wait"
    assert assess(history=history(end=date(2026, 9, 30)))["action"] == "wait"
    future = history(end=date(2026, 10, 6), observed_at=NOW+timedelta(days=2))
    assert assess(history=future, benchmark=history(symbol="ACWI", end=date(2026, 10, 6), observed_at=NOW+timedelta(days=2)))["action"] == "wait"


def test_fresh_filing_cannot_relabel_ancient_quarter_and_future_reports_wait():
    assert assess(fact=replace(financials(), reported_at=date(2026, 10, 6)))["action"] == "wait"
    # A fresh report of a result from more than 180 days ago is still an old quarter.
    old = replace(financials(), period_end=date(2025, 6, 30), period_start=date(2025, 4, 1), prior_period_end=date(2024, 6, 30), prior_period_start=date(2024, 4, 1), reported_at=date(2026, 10, 4))
    assert assess(fact=old)["action"] == "wait"
    old_report = replace(financials(), period_end=date(2026, 5, 31), period_start=date(2026, 3, 1), prior_period_end=date(2025, 5, 31), prior_period_start=date(2025, 3, 1), reported_at=date(2026, 6, 1))
    assert assess(fact=old_report)["action"] == "wait"
    assert assess(now=datetime(2026, 12, 15, 15, tzinfo=UTC))["action"] == "wait"


@pytest.mark.parametrize("changes", [{"symbol":"SPY"}, {"currency":"EUR"}])
def test_benchmark_identity_and_currency_must_match_declared_comparison(changes):
    assert assess(benchmark=replace(history(symbol="ACWI", step=.1), **changes))["action"] == "wait"


def test_benchmark_missing_stale_or_different_closing_date_waits():
    assert assess(benchmark=None)["action"] == "wait"
    assert assess(benchmark=history(symbol="ACWI", end=date(2026, 9, 30)))["action"] == "wait"
    assert assess(benchmark=history(symbol="ACWI", end=date(2026, 10, 1)))["action"] == "wait"


def test_return_comparison_requires_matching_twenty_session_start_dates():
    benchmark = history(symbol="ACWI", step=.1)
    # One missing session shifts the 20-session start while leaving both final dates equal.
    changed = benchmark.bars[:50] + benchmark.bars[51:]
    assert assess(benchmark=replace(benchmark, bars=changed))["action"] == "wait"


def test_missing_world_coverage_and_negative_commentary_block_new_buys():
    assert assess(world_coverage_ok=False)["action"] == "wait"
    assert assess(sentiment={"label":"negative","scored_count":4})["action"] == "wait"
    assert assess(sentiment={"label":"unknown","scored_count":0})["action"] == "conditional_buy"


def test_downtrend_reduces_only_if_owned_even_with_negative_commentary():
    idea = assess(history=history(step=-.3), sentiment={"label":"negative","scored_count":4})
    assert idea["action"] == "reduce_if_owned"
    assert idea["entry"] is None and idea["target"] is None
    assert "already own" in " ".join(idea["conditions"])
    assert "short-sale" in " ".join(idea["conditions"])


def risk_news(publisher, *, age=timedelta(hours=1)):
    stamp = NOW-age
    return WorldEvent(event_id=publisher, title="New export restrictions announced", text="New export restrictions create policy uncertainty", source_url=f"https://www.bbc.com/news/{publisher}", publisher=publisher, published_at=stamp, observed_at=max(NOW,stamp), themes=("export_controls",), bias_flags=())


def test_two_exposed_risk_publishers_tighten_entry_and_can_change_buy_to_wait():
    rising = history(step=1.1)
    baseline = assess(history=rising)
    assert baseline["action"] == "conditional_buy"
    idea = assess(history=rising, events=(risk_news("publisher-a"),risk_news("publisher-b")))
    assert idea["macro_adjustment"]["tighter_entry_limits"]
    assert idea["action"] == "wait"
    assert all("unverified" in row["interpretation"] for row in idea["world_context"])
    assert not assess(history=rising, events=(risk_news("same"),risk_news("same")))["macro_adjustment"]["tighter_entry_limits"]


def test_stale_and_future_headlines_never_supply_current_trade_context():
    idea = assess(events=(risk_news("old",age=timedelta(days=3)),risk_news("future",age=timedelta(hours=-1))))
    assert idea["world_context"] == []
    assert not idea["macro_adjustment"]["tighter_entry_limits"]


def test_large_unadjusted_move_cannot_become_false_momentum_buy():
    original = history()
    bars = tuple(replace(bar, open=bar.open*2,high=bar.high*2,low=bar.low*2,close=bar.close*2) if index>=40 else bar for index,bar in enumerate(original.bars))
    # The jump is outside ATR14 and SMA20; those indicators alone would accept it.
    jump = replace(original, bars=bars)
    idea = assess(history=jump)
    assert idea["action"] == "wait"
    assert any("corporate" in item.casefold() or "30%" in item for item in idea["conditions"]+idea["risks"])


def test_new_reviewed_snapshot_advances_older_cache_without_downgrading_newer_cache(tmp_path):
    from contract_ipo_monitor.config import Settings
    from contract_ipo_monitor.db import Database
    from contract_ipo_monitor.market_monitor import MarketMonitor
    from contract_ipo_monitor.market_research import MarketResearchStore
    from contract_ipo_monitor.universe import default_fundamentals, default_universe

    db = Database(tmp_path / "cache.db")
    db.initialize()
    store = MarketResearchStore(db)
    store.initialize()
    old = FinancialFact(symbol="MU", source_url="https://www.sec.gov/Archives/old", period_end=date(2026, 5, 28), reported_at=date(2026, 6, 25), revenue=150, prior_revenue=100, net_income=30, currency="USD")
    store.record("financials", "MU", old, observed_at=NOW-timedelta(days=1))
    item = next(row for row in default_universe() if row.symbol == "MU")
    settings = Settings(markets_enabled=True)
    MarketMonitor(db, settings, now=lambda:NOW, instruments=(item,), benchmarks=())
    assert store.latest("financials")["MU"]["period_end"] == default_fundamentals()["MU"].period_end.isoformat()
    newer = FinancialFact(symbol="MU", source_url="https://www.sec.gov/Archives/new", period_end=date(2026, 11, 26), reported_at=date(2026, 12, 23), revenue=200, prior_revenue=100, net_income=60, currency="USD")
    later = datetime(2026, 12, 24, tzinfo=UTC)
    store.record("financials", "MU", newer, observed_at=later)
    MarketMonitor(db, settings, now=lambda:later, instruments=(item,), benchmarks=())
    assert store.latest("financials")["MU"]["period_end"] == "2026-11-26"
