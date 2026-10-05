"""Auditable conditional research setups; no orders or predicted probabilities."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from statistics import mean
from typing import Any


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
    return {"last_close": closes[-1], "sma20": mean(closes[-20:]), "sma50": mean(closes[-50:]),
            "return20": closes[-1]/closes[-21]-1, "atr14": mean(tr[-14:]),
            "high20": max(b.high for b in bars[-20:]), "low20": min(b.low for b in bars[-20:]),
            "average_volume20": mean(b.volume for b in bars[-20:])}


def assess_trade(instrument, fact, history, benchmark, sentiment: dict[str,Any] | None,
                 events, *, now: datetime, world_coverage_ok: bool,
                 max_price_age_business_days: int = 1, fundamental_max_age_days: int = 120) -> dict:
    if now.utcoffset() is None or not 0 <= max_price_age_business_days <= 5 or fundamental_max_age_days < 1:
        raise ValueError("Trade research requires an aware timestamp and valid freshness limits")
    today = now.astimezone(UTC).date()
    result = {
        "symbol": instrument.symbol, "company": instrument.name, "exchange": instrument.exchange,
        "currency": instrument.currency, "listing_kind": instrument.listing_kind, "sector": instrument.sector,
        "action": "wait", "horizon": "5–15 trading sessions", "generated_at": now.isoformat(),
        "entry": None, "invalidation": None, "target": None, "risk_reward": None,
        "price_as_of": None, "conditions": [], "reasons": [], "risks": [],
        "sentiment": {k: (sentiment or {}).get(k) for k in ("label", "score", "scored_count", "independent_origins", "bias_flags")},
        "fundamentals": None, "indicators": None, "world_context": [], "evidence_urls": [],
        "method": "growth_profit_momentum_v1; long setups or reduce-if-owned; unvalidated screening rules",
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
        if history.symbol != instrument.symbol or history.currency != instrument.currency:
            blockers.append("Price symbol or trading currency does not match the instrument")
        from .market import _venue_group
        if _venue_group(history.exchange) != _venue_group(instrument.exchange):
            blockers.append("Price venue does not match the reviewed listing exchange")
        age = business_days_since(history.bars[-1].date, today)
        if age < 0 or age > max_price_age_business_days:
            blockers.append("Completed daily prices are stale or dated in the future")
    related = []
    for event in events:
        if not event.published_at:
            continue
        stamp = event.published_at
        if stamp.tzinfo is None:
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
    negative_commentary = (sentiment or {}).get("label") == "negative" and (sentiment or {}).get("scored_count",0) >= 3
    if negative_commentary:
        result["risks"].append("Accessible commentary is negative; a new long entry requires contradictory evidence or a better setup")
    elif (sentiment or {}).get("label", "unknown") == "unknown":
        result["risks"].append("Forum/commentary sentiment is unknown; it provides no positive confirmation")
    else:
        result["reasons"].append("Sampled commentary is considered as supporting context with capped influence")
    if benchmark is None or benchmark.status != "ok" or len(benchmark.bars) < 60:
        blockers.append("The global equity benchmark has insufficient price history")
    elif benchmark.symbol != instrument.benchmark_symbol:
        blockers.append("Benchmark identity does not match the reviewed reference instrument")
    elif benchmark.currency != instrument.currency:
        blockers.append("Benchmark and instrument use different currencies; comparable FX-adjusted strength is unavailable")
    elif business_days_since(benchmark.bars[-1].date,today) not in range(max_price_age_business_days+1):
        blockers.append("Benchmark prices are stale")
    elif history is not None and history.bars and benchmark.bars[-1].date != history.bars[-1].date:
        blockers.append("Instrument and benchmark closing dates do not match")
    elif history is not None and history.bars and [bar.date for bar in benchmark.bars[-21:]] != [bar.date for bar in history.bars[-21:]]:
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
    if (any(abs(b.close/a.close-1) > .30 for a,b in zip(history.bars[-61:],history.bars[-60:]))
            or any(abs(b.close/a.close-1) > .30 for a,b in zip(benchmark.bars[-61:],benchmark.bars[-60:]))):
        result["conditions"] = ["A daily move exceeds 30%; review corporate actions and the source before using raw-price momentum"]
        return result
    if values["last_close"] < values["sma50"] and values["return20"] < 0:
        result["action"] = "reduce_if_owned"
        result["invalidation"] = round(values["low20"],2)
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
            entry = round(values["high20"]*1.001,2)
            risk = 2*values["atr14"]
            stop, target = round(entry-risk,2), round(entry+2*risk,2)
            if not 0 < stop < entry < target:
                result["conditions"] = ["A positive, ordered risk plan could not be established"]
            else:
                result.update(action="conditional_buy", entry=entry, invalidation=stop, target=target, risk_reward=2.0)
                result["conditions"] = [f"Consider entry only if a fresh quote confirms a break above {instrument.currency} {entry:.2f}",
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
