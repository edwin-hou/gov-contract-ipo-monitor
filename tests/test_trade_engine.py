"""Independent behavior tests using market scenarios and mismatched evidence."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from contract_ipo_monitor.fundamentals import FinancialFact
from contract_ipo_monitor.market import DailyBar, PriceHistory
from contract_ipo_monitor.trades import assess_trade, completed_session_date, completed_sessions_since, session_count, sessions_after
from contract_ipo_monitor.universe import Instrument
from contract_ipo_monitor.worldnews import WorldEvent


NOW = datetime(2026, 10, 5, 15, tzinfo=UTC)


def instrument():
    return Instrument(symbol="TEST", name="Test issuer", exchange="NASDAQ", currency="USD", provider_symbol="test.us", aliases=("Test issuer",), macro_exposures=("export_controls", "rates"), reporting_currency="USD", benchmark_symbol="ACWI")


def financials():
    return FinancialFact(symbol="TEST", source_url="https://www.sec.gov/Archives/example", period_end=date(2026, 6, 30), period_start=date(2026, 4, 1), prior_period_end=date(2025, 6, 30), prior_period_start=date(2025, 4, 1), reported_at=date(2026, 8, 1), revenue=150_000_000, prior_revenue=100_000_000, net_income=30_000_000, currency="USD", source_kind="sec")


def trading_dates(end=date(2026, 10, 2), count=65):
    # Independent fixture calendar: weekends alone are not actual U.S. sessions.
    holidays_2026 = {date(2026, month, day) for month, day in ((1,1),(1,19),(2,16),(4,3),(5,25),(6,19),(7,3),(9,7),(11,26),(12,25))}
    days = []
    while len(days) < count:
        if end.weekday() < 5 and end not in holidays_2026:
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
    return WorldEvent(event_id=publisher, title="New export restrictions announced for Test issuer", text=f"{publisher} reports new export restrictions for Test issuer with policy uncertainty", source_url=f"https://www.bbc.com/news/{publisher}", publisher=publisher, published_at=stamp, observed_at=max(NOW,stamp), themes=("export_controls",), bias_flags=())


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


@pytest.mark.parametrize("affected", ["history", "benchmark"])
def test_exactly_sixty_bars_cannot_hide_a_large_corporate_action_jump(affected):
    original = replace(history(symbol="ACWI" if affected == "benchmark" else "TEST"), bars=history(symbol="ACWI" if affected == "benchmark" else "TEST").bars[-60:])
    changed = tuple(replace(bar, open=bar.open*2, high=bar.high*2, low=bar.low*2, close=bar.close*2) if index >= 15 else bar for index, bar in enumerate(original.bars))
    idea = assess(**{affected: replace(original, bars=changed)})
    assert idea["action"] == "wait"
    assert any("30%" in item for item in idea["conditions"])


@pytest.mark.parametrize("affected", ["history", "benchmark"])
def test_future_observation_of_old_completed_prices_cannot_pass(affected):
    item = history(symbol="ACWI" if affected == "benchmark" else "TEST", observed_at=NOW+timedelta(seconds=1))
    idea = assess(**{affected: item})
    assert idea["action"] == "wait"
    assert any("observation timestamp" in item for item in idea["conditions"])


def test_current_intraday_bar_waits_but_completed_same_day_is_eligible():
    intraday = history(end=date(2026,10,5))
    benchmark = history(symbol="ACWI", end=date(2026,10,5), step=.1)
    assert assess(history=intraday, benchmark=benchmark)["action"] == "wait"
    after_close = datetime(2026,10,5,20,15,tzinfo=UTC)
    valid = assess(history=replace(intraday, observed_at=after_close), benchmark=replace(benchmark, observed_at=after_close), now=after_close)
    assert valid["action"] == "conditional_buy"
    # Re-running later cannot retroactively turn an intraday observation into a final close.
    assert assess(history=intraday, benchmark=benchmark, now=after_close)["action"] == "wait"


@pytest.mark.parametrize("changes", [{"exchange":"NYSE"}, {"exchange_timezone":"UTC"}, {"exchange_timezone":None}])
def test_benchmark_venue_and_session_timezone_must_be_reviewed(changes):
    assert assess(benchmark=replace(history(symbol="ACWI", step=.1), **changes))["action"] == "wait"


def test_known_holiday_and_early_close_determine_completed_sessions():
    pre_open = datetime(2026,5,26,13,tzinfo=UTC)
    assert completed_session_date("NASDAQ", pre_open) == date(2026,5,22)
    assert completed_sessions_since(date(2026,5,22), pre_open, "NYSE") == 0
    assert session_count(date(2026,5,22), date(2026,5,26), "NYSE") == 1
    assert sessions_after(date(2026,5,22),1,"NASDAQ") == date(2026,5,26)
    assert completed_session_date("NASDAQ", datetime(2026,11,27,18,14,tzinfo=UTC)) == date(2026,11,25)
    assert completed_session_date("NASDAQ", datetime(2026,11,27,18,15,tzinfo=UTC)) == date(2026,11,27)
    # Sunday local time remains Friday's completed session even on UTC Monday.
    assert completed_session_date("NASDAQ", datetime(2026,10,5,1,tzinfo=UTC)) == date(2026,10,2)


@pytest.mark.parametrize("call", [
    lambda:session_count(date(2026,10,5),date(2026,10,5),"UNKNOWN"),
    lambda:session_count(date(2026,10,5),date(2026,10,4),"UNKNOWN"),
    lambda:sessions_after(date(2026,10,5),0,"UNKNOWN"),
    lambda:session_count(date(2029,10,5),date(2029,10,5),"NASDAQ"),
    lambda:sessions_after(date(2029,10,5),0,"NASDAQ"),
])
def test_zero_and_reversed_calendar_operations_still_fail_closed(call):
    with pytest.raises(ValueError, match="calendar"):
        call()


def test_entry_cap_rounded_risk_and_exit_strategy_do_not_assume_a_purchase():
    idea = assess(history=history(spread=.837))
    strategy = idea["strategy"]
    assert idea["action"] == "conditional_buy"
    assert strategy["scope"] == "paper_reference_only" and not strategy["assumed_position"]
    assert strategy["state"] == "awaiting_fresh_quote"
    assert strategy["risk_budget"]["quantity"] is None
    assert strategy["currency"] == strategy["risk_budget"]["currency"] == "USD"
    assert strategy["risk_budget"]["planned_risk_per_share"] == pytest.approx(idea["entry"]-idea["invalidation"])
    maximum = strategy["maximum_entry"]
    actual_rr = (idea["target"]-maximum)/(maximum-idea["invalidation"])
    assert actual_rr >= strategy["risk_budget"]["minimum_reward_risk"]
    assert strategy["risk_budget"]["reward_risk_at_maximum_entry"] == pytest.approx(actual_rr,abs=1e-6)
    assert strategy["stop"]["price"] == idea["invalidation"] and strategy["target"]["price"] == idea["target"]
    assert "actual_entry" in strategy["risk_budget"]["quantity_formula"] and "cost" in strategy["risk_budget"]["quantity_formula"]
    assert strategy["setup_start_session"] == "2026-10-02" and strategy["setup_valid_through"] == "2026-10-09"
    assert strategy["time_exit"]["review_after_sessions"] == 5 and strategy["time_exit"]["exit_after_sessions"] == 15
    assert "never report publication" in strategy["time_exit"]["count_from"]
    assert "order is unknown" in strategy["daily_bar_policy"]
    assert "Cancel" in strategy["gap_policy"]


def test_reduction_strategy_requires_verified_ownership_and_wait_assumes_none():
    reduction = assess(history=history(step=-.3))["strategy"]
    assert reduction["state"] == "exit_review_if_owned"
    assert not reduction["assumed_position"] and reduction["entry_trigger"] is None
    assert "existing position" in reduction["exit_trigger"]["verification"]
    waiting = assess(history=None)["strategy"]
    assert waiting["state"] == "blocked" and waiting["stop"] is None


def test_provenance_label_cannot_promote_a_forum_url_to_primary_results_or_prices():
    assert assess(fact=replace(financials(), source_url="https://example.org/forum"))["action"] == "wait"
    assert assess(history=replace(history(), source_url="https://example.org/forum"))["action"] == "wait"


def test_malformed_commentary_is_unknown_context_not_a_crash_or_buy_override():
    idea = assess(sentiment={"label":"negative", "score":float("nan"), "scored_count":None, "independent_origins":True})
    assert idea["sentiment"]["score"] is None and idea["sentiment"]["scored_count"] == 0
    assert idea["sentiment"]["independent_origins"] == 0
    blocked = assess(fact=None, sentiment={"label":"positive", "scored_count":999})
    assert blocked["action"] == "wait"


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
