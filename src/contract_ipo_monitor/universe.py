"""A reviewed global-issuer shortlist, not an exhaustive world-market ranking."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from importlib.resources import files
from typing import Any, Mapping

from .fundamentals import FinancialFact, financial_fact_from_dict


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    exchange: str
    currency: str
    provider_symbol: str
    aliases: tuple[str, ...] = ()
    sector: str = ""
    macro_exposures: tuple[str, ...] = ()
    benchmark_symbol: str = "ACWI"
    cik: str | None = None
    listing_kind: str = "ordinary"
    reporting_currency: str | None = None
    twelve_data_symbol: str | None = None
    twelve_data_exchange: str | None = None
    yahoo_symbol: str | None = None
    nasdaq_symbol: str | None = None
    issuer_country: str = ""
    listing_source_url: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "macro_exposures", tuple(self.macro_exposures))
        if not self.symbol or not self.exchange or not self.currency:
            raise ValueError("instrument symbol, venue and trading currency are required")
        if self.listing_kind not in {"ordinary", "ADR", "etf"}:
            raise ValueError("unsupported listing kind")


@dataclass(frozen=True)
class RankedCompany:
    instrument: Instrument
    fact: FinancialFact | None
    eligible: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.instrument.symbol,
            "name": self.instrument.name,
            "exchange": self.instrument.exchange,
            "trading_currency": self.instrument.currency,
            "issuer_country": self.instrument.issuer_country,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "financials": self.fact.to_dict() if self.fact else None,
            "revenue_growth_percent": self.fact.revenue_growth_percent if self.fact else None,
            "net_margin_percent": self.fact.net_margin_percent if self.fact else None,
        }


def _snapshot() -> dict[str, Any]:
    # Package resources work in installed wheels and do not depend on the process cwd.
    return json.loads(files("contract_ipo_monitor").joinpath("data", "universe_snapshot.json").read_text(encoding="utf-8"))


def default_universe() -> tuple[Instrument, ...]:
    return tuple(Instrument(**row) for row in _snapshot()["instruments"])


def default_fundamentals() -> dict[str, FinancialFact]:
    return {row["symbol"]: financial_fact_from_dict(row) for row in _snapshot()["financials"]}


def benchmark_instruments() -> tuple[Instrument, ...]:
    return tuple(Instrument(**row) for row in _snapshot()["benchmarks"])


def universe_metadata() -> dict[str, Any]:
    """The research date, selection scope, exclusions, and limitations are user-visible."""
    snapshot = _snapshot()
    return {key: snapshot[key] for key in ("reviewed_at", "methodology", "limitations", "reviewed_exclusions")}


def rank_universe(
    facts: Mapping[str, FinancialFact] | None = None,
    *,
    now: date | datetime | None = None,
    instruments: tuple[Instrument, ...] | None = None,
    min_growth: float = 0.10,
    max_age_days: int = 120,
) -> tuple[RankedCompany, ...]:
    """Filter profitable growth; sort YoY growth, then net margin, then symbol.

    Growth and margin are dimensionless fractions. Ranking expresses the research
    filter only; price, liquidity, event risk and portfolio checks still gate trading.
    """
    if not math.isfinite(min_growth) or max_age_days < 1:
        raise ValueError("invalid fundamental selection thresholds")
    evidence = default_fundamentals() if facts is None else facts
    output = []
    for instrument in instruments if instruments is not None else default_universe():
        fact = evidence.get(instrument.symbol)
        reasons = []
        if fact is None:
            reasons.append("Comparable primary-source financial results are unavailable.")
        elif fact.symbol != instrument.symbol:
            reasons.append("Financial result issuer does not match the instrument.")
        else:
            if not fact.is_fresh(now, max_age_days=max_age_days):
                reasons.append(f"Financial evidence is future-dated, its report is older than {max_age_days} days, or its {fact.period_type} period exceeds the 180/450-day age limit.")
            if fact.source_kind not in {"issuer", "sec"}:
                reasons.append("Financial results do not have issuer or SEC provenance.")
            if fact.net_income <= 0:
                reasons.append("Reported GAAP/IFRS net income is not positive.")
            if fact.growth < min_growth:
                reasons.append(f"Total revenue growth is below {min_growth * 100:g}% year over year.")
            if instrument.reporting_currency and fact.currency != instrument.reporting_currency:
                reasons.append("Financial reporting currency does not match the reviewed issuer basis.")
            if fact.accounting_standard not in {"US GAAP", "IFRS", "Taiwan IFRS"}:
                reasons.append("Profit measure is not a supported reported GAAP/IFRS result.")
        eligible = not reasons
        if eligible and fact:
            reasons.append(f"{fact.period_type.capitalize()} revenue grew {fact.revenue_growth_percent:.1f}% year over year; reported net margin was {fact.net_margin_percent:.1f}%.")
        output.append(RankedCompany(instrument, fact, eligible, tuple(reasons)))
    output.sort(key=lambda row: (not row.eligible, -(row.fact.growth if row.fact else -math.inf), -(row.fact.net_margin if row.fact else -math.inf), row.instrument.symbol))
    return tuple(output)


def select_watchlist(facts: Mapping[str, FinancialFact] | None = None, *, now: date | datetime | None = None, limit: int = 10, **kwargs: Any) -> tuple[Instrument, ...]:
    if limit < 1:
        raise ValueError("watchlist limit must be positive")
    return tuple(row.instrument for row in rank_universe(facts, now=now, **kwargs) if row.eligible)[:limit]
