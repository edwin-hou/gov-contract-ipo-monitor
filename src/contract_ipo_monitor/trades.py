"""Auditable conditional research setups; no orders or predicted probabilities."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from statistics import mean
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


# Published U.S. cash-equity calendar, not federal/bank holidays. In 2028
# January 1 falls on Saturday and has no Friday observance. Keep the reviewed
# range explicit; an unreviewed year must not silently become weekday-only.
# https://www.nasdaq.com/market-activity/stock-market-holiday-schedule
# https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/
_US_CLOSED = {
    2026: "01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25",
    2027: "01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06 11-25 12-24",
    2028: "01-17 02-21 04-14 05-29 06-19 07-04 09-04 11-23 12-25",
}
_US_EARLY = {2026: {"11-27", "12-24"}, 2027: {"11-26"}, 2028: {"07-03", "11-24"}}
_US_VENUES = {"NASDAQ", "NYSE", "NYSE_ARCA", "NYSE_AMERICAN"}


def _session_open(exchange: str, day: date) -> bool:
    from .market import _venue_group
    if _venue_group(exchange) not in _US_VENUES or day.year not in _US_CLOSED:
        raise ValueError("Reviewed regular-session calendar is unavailable for this venue/year")
    return day.weekday() < 5 and day.strftime("%m-%d") not in _US_CLOSED[day.year].split()


def completed_session_date(exchange: str, at: datetime) -> date:
    """Latest scheduled U.S. regular session past its close plus a 15-minute buffer.

    Overnight/extended-hours quotes do not complete a regular-session OHLC bar.
    Published holidays/early closes are known; unscheduled closures and halts
    still require provider/broker confirmation. Unknown calendars fail closed.
    """
    if at.utcoffset() is None:
        raise ValueError("Regular-session completion requires an aware timestamp")
    local = at.astimezone(ZoneInfo("America/New_York"))
    day = local.date()
    close = time(13, 15) if day.strftime("%m-%d") in _US_EARLY.get(day.year, set()) else time(16, 15)
    if _session_open(exchange, day) and local.time() >= close:
        return day
    day -= timedelta(days=1)
    while not _session_open(exchange, day):
        day -= timedelta(days=1)
    return day


def completed_sessions_since(day: date, at: datetime, exchange: str) -> int:
    """Completed regular sessions after a reference date; not elapsed weekdays."""
    last = completed_session_date(exchange, at)
    if day > last:
        return -1
    return session_count(day, last, exchange)


def session_count(start: date, end: date, exchange: str) -> int:
    """Scheduled regular sessions in (start, end], for labelled reference aging."""
    _session_open(exchange, start)
    _session_open(exchange, end)
    if abs((end-start).days) > 3660:
        raise ValueError("Session interval exceeds the reviewed counting bound")
    if end < start:
        return -1
    return sum(_session_open(exchange, start + timedelta(days=n)) for n in range(1, (end-start).days + 1))


def sessions_after(start: date, count: int, exchange: str) -> date:
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 500:
        raise ValueError("Session offset must be a bounded nonnegative integer")
    _session_open(exchange, start)
    day = start
    while count:
        day += timedelta(days=1)
        if _session_open(exchange, day):
            count -= 1
    return day


def business_days_since(day: date, today: date) -> int:
    if day > today:
        return -1
    return sum((day + timedelta(days=n)).weekday() < 5 for n in range(1, (today-day).days + 1))


def indicators(bars) -> dict[str, float]:
    if len(bars) < 60:
        raise ValueError("At least 60 completed daily bars are required")
    for bar in bars:
        prices = (bar.open, bar.high, bar.low, bar.close)
        if not all(math.isfinite(value) and value > 0 for value in prices) or not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError("Invalid OHLC prices")
        if not math.isfinite(bar.volume) or bar.volume < 0:
            raise ValueError("Invalid volume")
    if any(a.date >= b.date for a,b in zip(bars,bars[1:])):
        raise ValueError("Daily bars must have unique increasing dates")
    closes = [bar.close for bar in bars]
    tr = [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close)) for a,b in zip(bars,bars[1:])]
    values = {"last_close": closes[-1], "sma20": mean(closes[-20:]), "sma50": mean(closes[-50:]),
            "return20": closes[-1]/closes[-21]-1, "atr14": mean(tr[-14:]),
            "high20": max(b.high for b in bars[-20:]), "low20": min(b.low for b in bars[-20:]),
            "average_volume20": mean(b.volume for b in bars[-20:])}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("Derived price indicators must be finite")
    return values


def _source_host(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
            return None
        return parsed.hostname.casefold()
    except (TypeError, ValueError):
        return None


def _financial_source_reviewed(instrument, fact) -> bool:
    host = _source_host(fact.source_url)
    if fact.source_kind == "sec":
        return host in {"sec.gov", "www.sec.gov", "data.sec.gov"}
    if fact.source_kind != "issuer" or host is None:
        return False
    # The kind label alone cannot make a forum URL primary evidence. Reviewed
    # issuer snapshots and explicitly reviewed listing sources establish hosts;
    # the ratios still come from the dated amounts, never promotional prose.
    from .universe import default_fundamentals
    reviewed = default_fundamentals().get(instrument.symbol)
    hosts = {_source_host(instrument.listing_source_url)}
    if reviewed:
        hosts.add(_source_host(reviewed.source_url))
    return host in hosts


def _sentiment_context(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    label = value.get("label", "unknown")
    if label not in {"positive", "neutral", "negative", "unknown"}:
        label = "unknown"
    score = value.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not -1 <= score <= 1:
        score = None
    counts = {key: item if isinstance(item := value.get(key), int) and not isinstance(item, bool) and item >= 0 else 0
              for key in ("scored_count", "independent_origins")}
    flags = value.get("bias_flags", ())
    return {"label": label, "score": score, **counts,
            "bias_flags": [item for item in flags if isinstance(item, str)] if isinstance(flags, (list, tuple)) else [],
            "role": "Context only; commentary cannot establish issuer facts, trade direction, or predictive accuracy."}


def _base_strategy(currency: str) -> dict[str, Any]:
    return {
        "scope": "paper_reference_only", "state": "blocked", "holdings_status": "not_provided",
        "assumed_position": False, "price_unit": f"{currency} per traded share", "currency": currency,
        "entry_trigger": None, "maximum_entry": None, "stop": None, "target": None,
        "setup_expiry_sessions": 5,
        "time_exit": {"review_after_sessions": 5, "exit_after_sessions": 15,
                      "count_from": "Actual verified fill, or a separately labelled paper reference trigger; never report publication",
                      "policy": "Review after 5 completed trading sessions; exit by session 15 or sooner at the stop, target, or thesis invalidation"},
        "risk_budget": {"currency": currency, "quantity": None, "planned_risk_per_share": None,
                        "minimum_reward_risk": 1.5,
                        "required_inputs": ["portfolio equity converted to the trading currency", "chosen maximum loss fraction",
                                            "available cash in the trading currency", "estimated round-trip costs per share", "broker lot and tick rules"],
                        "budget_formula": "risk_budget = portfolio_equity_in_trading_currency * chosen_maximum_loss_fraction",
                        "quantity_formula": "floor(min(risk_budget / (actual_entry - stop + estimated_round_trip_cost_per_share), available_cash / (actual_entry + estimated_entry_cost_per_share))) rounded down to the broker lot size",
                        "loss_limit": "The budget limits planned price risk; gaps, execution costs and FX changes can exceed it."},
        "gap_policy": "Cancel a new entry above maximum_entry or below the invalidation level; never assume the trigger was filled. An owned position gapping through a stop requires fresh-quote review, with no guaranteed stop-price execution.",
        "daily_bar_policy": "A bar touching a trigger or exit is a paper reference observation, not proof of an executable quote or fill. If stop and target occur in one bar, their order is unknown.",
        "cancel_conditions": ["A required evidence gate fails", "Corporate actions are unreconciled", "The company or risk thesis changes",
                              "The setup remains untriggered for 5 completed sessions"],
    }


def _cent(value: float, rounding: str) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=rounding))


def _long_levels(values: dict[str, float]) -> tuple[float, float, float, float, float] | None:
    # Rounded references are not broker tick validation; the user must verify
    # the listing's actual tick/lot rules before considering a real order.
    try:
        entry = _cent(values["high20"] * 1.001, ROUND_CEILING)
        stop = _cent(entry - 2 * values["atr14"], ROUND_FLOOR)
        risk = entry - stop
        target = _cent(entry + 2 * risk, ROUND_CEILING)
        maximum_entry = _cent(min(entry + .25 * values["atr14"], (target + 1.5 * stop) / 2.5), ROUND_FLOOR)
    except (InvalidOperation, OverflowError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (entry, stop, risk, target, maximum_entry)) or not 0 < stop < entry <= maximum_entry < target:
        return None
    reward_risk = (target - entry) / risk
    return entry, stop, target, maximum_entry, reward_risk


def _price_constraints(history, expected, *, now: datetime, max_age: int, label: str) -> list[str]:
    from .market import _venue_group
    problems = []
    if history.symbol != expected.symbol or history.currency != expected.currency:
        problems.append(f"{label} symbol or trading currency does not match the reviewed instrument")
    if _venue_group(history.exchange) != _venue_group(expected.exchange):
        problems.append(f"{label} venue does not match the reviewed listing exchange")
    provider_hosts = {"nasdaq": {"api.nasdaq.com"}, "yahoo": {"query1.finance.yahoo.com", "query2.finance.yahoo.com"},
                      "twelve_data": {"api.twelvedata.com"}, "stooq": {"stooq.com", "stooq.pl"}}
    if _source_host(history.source_url) not in provider_hosts.get(history.source, set()):
        problems.append(f"{label} provider evidence URL is unverified")
    if history.observed_at > now:
        problems.append(f"{label} observation timestamp is in the future")
    try:
        from .market import _local_date
        if not history.exchange_timezone:
            raise ValueError("Provider exchange timezone is unavailable")
        _local_date(now, history.exchange_timezone)
        if _venue_group(expected.exchange) in _US_VENUES and history.exchange_timezone not in {"America/New_York", "US/Eastern"}:
            raise ValueError("Provider timezone does not match the reviewed U.S. venue")
        last = history.bars[-1].date
        if last > completed_session_date(expected.exchange, history.observed_at):
            problems.append(f"{label} contains an uncompleted regular-session daily bar")
        age = completed_sessions_since(last, now, expected.exchange)
        if age < 0 or age > max_age:
            problems.append(f"{label} completed daily prices are stale or dated in the future")
        if any(not _session_open(expected.exchange, bar.date) for bar in history.bars):
            problems.append(f"{label} contains a daily bar on a closed regular-session date")
    except (ValueError, KeyError) as exc:
        problems.append(f"{label} regular-session validation failed: {exc}")
    return problems


def assess_trade(instrument, fact, history, benchmark, sentiment: dict[str,Any] | None,
                 events, *, now: datetime, world_coverage_ok: bool,
                 max_price_age_business_days: int = 1, fundamental_max_age_days: int = 120,
                 benchmark_instrument=None) -> dict:
    if now.utcoffset() is None or not 0 <= max_price_age_business_days <= 5 or fundamental_max_age_days < 1:
        raise ValueError("Trade research requires an aware timestamp and valid freshness limits")
    result = {
        "symbol": instrument.symbol, "company": instrument.name, "exchange": instrument.exchange,
        "currency": instrument.currency, "listing_kind": instrument.listing_kind, "sector": instrument.sector,
        "action": "wait", "horizon": "5–15 trading sessions", "generated_at": now.isoformat(),
        "entry": None, "invalidation": None, "target": None, "risk_reward": None,
        "price_as_of": None, "conditions": [], "reasons": [], "risks": [],
        "sentiment": _sentiment_context(sentiment), "strategy": _base_strategy(instrument.currency),
        "fundamentals": None, "indicators": None, "world_context": [], "evidence_urls": [],
        "method": "growth_profit_momentum_v2; long setups or reduce-if-owned; unvalidated screening rules",
        "confidence": "insufficient_evidence",
        "limitations": ["No brokerage orders are placed. Prices are daily observations, not executable live quotes.",
                        "Signal scores are screening rules, not probabilities or tested investment returns.",
                        "Reported profit and growth do not establish fair valuation; valuation and portfolio suitability remain unverified.",
                        "An invalidation price is not a guaranteed exit; gaps and execution costs can worsen losses."],
    }
    blockers = []
    if fact is None:
        blockers.append("No comparable, sourced company results")
    else:
        result["fundamentals"] = {"period_end": fact.period_end.isoformat(), "reported_at": fact.reported_at.isoformat(),
             "revenue_growth_percent": round(fact.growth*100,2), "net_margin_percent": round(fact.net_margin*100,2),
             "net_income": fact.net_income, "reporting_currency": fact.currency, "period_type": fact.period_type,
             "accounting_standard": fact.accounting_standard, "source_url": fact.source_url}
        result["evidence_urls"].append(fact.source_url)
        result["risks"].extend(fact.limitations)
        if fact.symbol != instrument.symbol:
            blockers.append("Financial result issuer does not match the instrument")
        if fact.source_kind not in {"issuer", "sec"} or fact.accounting_standard not in {"US GAAP", "IFRS", "Taiwan IFRS"}:
            blockers.append("Comparable reported GAAP/IFRS profit from an issuer or SEC source is unavailable")
        elif not _financial_source_reviewed(instrument, fact):
            blockers.append("Financial evidence source is not the SEC or a reviewed issuer host")
        if instrument.reporting_currency and fact.currency != instrument.reporting_currency:
            blockers.append("Financial reporting currency does not match the reviewed issuer")
        if not fact.is_fresh(now, max_age_days=fundamental_max_age_days):
            blockers.append("Company results are stale or dated in the future")
        if fact.growth < .10 or fact.net_income <= 0 or fact.net_margin < .05:
            blockers.append("Company no longer meets the 10% revenue growth / positive profit / 5% net margin screen")
        result["reasons"].append(f"Reported revenue grew {fact.growth*100:.1f}% year over year with a {fact.net_margin*100:.1f}% net margin")
    if history is None or history.status != "ok" or len(history.bars) < 60:
        blockers.append("Usable verified price history is unavailable")
    else:
        result["price_as_of"] = history.bars[-1].date.isoformat()
        result["evidence_urls"].append(history.source_url)
        result["risks"].extend(history.limitations)
        blockers.extend(_price_constraints(history, instrument, now=now, max_age=max_price_age_business_days, label="Price"))
    related = []
    for event in events:
        if not event.published_at:
            continue
        stamp = event.published_at
        if stamp.tzinfo is None:
            continue
        if event.observed_at > now or _source_host(event.source_url) is None:
            continue
        if not timedelta(0) <= now-stamp <= timedelta(days=2):
            continue
        matches = sorted(set(event.themes).intersection(instrument.macro_exposures))
        if not matches:
            continue
        related.append(event)
        result["world_context"].append({"title": event.title, "source_url": event.source_url,
            "publisher": event.publisher, "published_at": stamp.isoformat(), "themes": matches,
            "interpretation": "Relevant exposure to these themes; direction and material impact are unverified."})
        result["evidence_urls"].append(event.source_url)
    result["world_context"] = result["world_context"][:8]
    risk_phrases = ("export ban", "export restrictions", "new sanctions", "supply disruption",
                    "blockade", "emergency rate", "rate hike", "inflation surge")
    risk_publishers = {event.publisher for event in related
                       if any(term in (event.title + " " + event.text).casefold() for term in risk_phrases)}
    macro_attention = len(risk_publishers) >= 2
    # Direction is not guessed from a headline. Multiple exposed risk reports
    # instead tighten the entry's volatility and extension limits.
    result["macro_adjustment"] = {"risk_reports_from_publishers": len(risk_publishers),
                                  "tighter_entry_limits": macro_attention,
                                  "max_atr_fraction": .06 if macro_attention else .08,
                                  "max_extension_fraction": .07 if macro_attention else .10}
    if macro_attention:
        result["risks"].append("Multiple relevant publishers report risk themes; entry volatility and price-extension limits are tightened without assuming price direction")
    if not world_coverage_ok:
        blockers.append("Fresh world-news coverage from at least two publishers is unavailable")
    if related:
        result["risks"].append("Current world headlines affect relevant exposures; they can already be priced in and do not establish a trade direction")
    else:
        result["risks"].append("No relevant theme was matched in the sampled world headlines; this does not establish that macro risk is absent")
    negative_commentary = result["sentiment"]["label"] == "negative" and result["sentiment"]["scored_count"] >= 3
    if negative_commentary:
        result["risks"].append("Accessible commentary is negative; a new long entry requires contradictory evidence or a better setup")
    elif result["sentiment"]["label"] == "unknown":
        result["risks"].append("Forum/commentary sentiment is unknown; it provides no positive confirmation")
    else:
        result["reasons"].append("Sampled commentary is considered as supporting context with capped influence")
    if benchmark is None or benchmark.status != "ok" or len(benchmark.bars) < 60:
        blockers.append("The global equity benchmark has insufficient price history")
    elif benchmark.symbol != instrument.benchmark_symbol:
        blockers.append("Benchmark identity does not match the reviewed reference instrument")
    elif benchmark.currency != instrument.currency:
        blockers.append("Benchmark and instrument use different currencies; comparable FX-adjusted strength is unavailable")
    else:
        if benchmark_instrument is None:
            from .universe import benchmark_instruments
            benchmark_instrument = next((item for item in benchmark_instruments() if item.symbol == instrument.benchmark_symbol), None)
        if benchmark_instrument is None or benchmark_instrument.symbol != instrument.benchmark_symbol:
            blockers.append("The benchmark listing venue and currency have not been reviewed")
        else:
            blockers.extend(_price_constraints(benchmark, benchmark_instrument, now=now, max_age=max_price_age_business_days, label="Benchmark"))
        if history is not None and history.bars and benchmark.bars[-1].date != history.bars[-1].date:
            blockers.append("Instrument and benchmark closing dates do not match")
        if history is not None and history.bars and [bar.date for bar in benchmark.bars[-21:]] != [bar.date for bar in history.bars[-21:]]:
            blockers.append("Instrument and benchmark 20-session dates do not match")
    if blockers:
        result["conditions"] = list(dict.fromkeys(blockers))
        result["risks"] = list(dict.fromkeys(result["risks"]))
        result["evidence_urls"] = list(dict.fromkeys(result["evidence_urls"]))
        return result
    try:
        values = indicators(history.bars)
        market = indicators(benchmark.bars)
    except (ValueError, TypeError, OverflowError) as exc:
        result["conditions"] = [f"Price validation failed: {exc}"]
        return result
    relative = values["return20"]-market["return20"]
    values["relative_return20"] = relative
    result["indicators"] = {k: round(v,6) for k,v in values.items()}
    result["confidence"] = "sourced_conditional_setup; predictive_accuracy_unmeasured"
    if any(abs(b.close/a.close-1) > .30 for series in (history.bars, benchmark.bars)
           for a,b in zip(series[-61:], series[-61:][1:])):
        result["conditions"] = ["A daily move exceeds 30%; review corporate actions and the source before using raw-price momentum"]
        return result
    strategy = result["strategy"]
    try:
        start = completed_session_date(instrument.exchange, now)
        valid_through = sessions_after(start, 5, instrument.exchange)
    except ValueError as exc:
        result["conditions"] = [f"A reviewed setup expiry could not be established: {exc}"]
        return result
    strategy["setup_start_session"] = start.isoformat()
    strategy["setup_valid_through"] = valid_through.isoformat()
    strategy["expiry_rule"] = "The fifth completed regular session ends setup validity; subsequent closes cannot trigger this unchanged reference plan"
    strategy["calendar_source_url"] = "https://www.nasdaq.com/market-activity/stock-market-holiday-schedule"
    result["metrics"] = {"close": values["last_close"], "currency": instrument.currency,
                         "price_as_of": history.as_of.isoformat(), "exchange": instrument.exchange,
                         "exchange_timezone": history.exchange_timezone}
    if values["last_close"] < values["sma50"] and values["return20"] < 0:
        result["action"] = "reduce_if_owned"
        result["invalidation"] = round(values["low20"],2)
        strategy.update(state="exit_review_if_owned", exit_trigger={"price": result["invalidation"], "currency": instrument.currency,
                        "verification": "A fresh regular-session quote confirms a break below the recent low; only applies to an explicitly verified existing position"})
        result["conditions"] = ["Only applies if you already own this instrument; review reducing exposure if a fresh quote confirms a break below the recent 20-session low", "No short-sale order is implied"]
        result["reasons"].append("Price is below its 50-session average with negative 20-session momentum")
    elif (values["last_close"] > values["sma20"] > values["sma50"] and values["return20"] > 0 and relative >= 0
          and values["average_volume20"] >= 100_000 and 0 < values["atr14"]/values["last_close"] <= result["macro_adjustment"]["max_atr_fraction"]):
        extension = result["macro_adjustment"]["max_extension_fraction"]
        if negative_commentary:
            result["conditions"] = ["Accessible commentary is negative; wait for contradictory evidence or a better setup"]
        elif values["last_close"]/values["sma20"]-1 > extension:
            result["conditions"] = [f"Price is more than {extension*100:.0f}% above its 20-session average; wait for consolidation rather than chasing"]
        else:
            levels = _long_levels(values)
            if levels is None:
                result["conditions"] = ["A positive, ordered risk plan could not be established"]
            else:
                entry, stop, target, maximum_entry, reward_risk = levels
                result.update(action="conditional_buy", entry=entry, invalidation=stop, target=target, risk_reward=round(reward_risk,6))
                strategy.update(state="awaiting_fresh_quote", maximum_entry=maximum_entry,
                    entry_trigger={"price": entry, "currency": instrument.currency, "verification": "A fresh regular-session broker quote confirms the breakout, acceptable spread and a fill no higher than maximum_entry"},
                    stop={"price": stop, "currency": instrument.currency, "verification": "Review exiting an explicitly owned position when a fresh quote reaches or crosses the invalidation level; stop-price execution is not guaranteed"},
                    target={"price": target, "currency": instrument.currency, "verification": "Review taking profit only on an explicitly owned position at an executable quote; a daily high does not prove a fill"})
                strategy["risk_budget"].update(planned_risk_per_share=round(entry-stop,6), maximum_entry_risk_per_share=round(maximum_entry-stop,6),
                                               reward_risk_at_reference=round(reward_risk,6), reward_risk_at_maximum_entry=round((target-maximum_entry)/(maximum_entry-stop),6))
                result["conditions"] = [f"Consider entry only if a fresh quote confirms a break above {instrument.currency} {entry:.2f}",
                    f"Cancel a new entry above {instrument.currency} {maximum_entry:.2f}; recheck reward/risk after costs and the actual fill",
                    "Review after 5 completed sessions from an actual fill; exit by session 15 or sooner at stop, target, or thesis invalidation",
                    "Review the linked company/world news and current spread before entry; cancel if the thesis changes",
                    "Risk sizing requires your portfolio value and acceptable loss; no quantity has been assumed"]
                result["reasons"].append(f"Positive 20/50-session trend and {relative*100:.1f} percentage-point strength versus {instrument.benchmark_symbol}")
    else:
        result["conditions"] = ["Wait: trend, relative strength, volume, or volatility does not meet the entry screen"]
    result["risks"] = list(dict.fromkeys(result["risks"]))
    result["evidence_urls"] = list(dict.fromkeys(result["evidence_urls"]))
    return result


def rank_ideas(ideas: list[dict]) -> list[dict]:
    order = {"conditional_buy":0,"reduce_if_owned":1,"wait":2}
    return sorted(ideas, key=lambda x: (order[x["action"]], -(x.get("fundamentals") or {}).get("revenue_growth_percent",0), x["symbol"]))
