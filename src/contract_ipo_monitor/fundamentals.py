"""Comparable, sourced company results; every ratio uses one reporting currency."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime
from typing import Any, Mapping
from urllib.parse import urlparse

from .sources.http import ResilientClient


def _day(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _cik(value: Any) -> int:
    if not re.fullmatch(r"\d{1,10}", str(value)) or int(value) <= 0:
        raise ValueError("invalid SEC company identifier")
    return int(value)


@dataclass(frozen=True)
class FinancialFact:
    symbol: str
    source_url: str
    period_end: date
    reported_at: date
    revenue: float
    prior_revenue: float
    net_income: float
    currency: str
    accounting_standard: str = "US GAAP"
    period_type: str = "quarter"
    period_start: date | None = None
    prior_period_end: date | None = None
    prior_period_start: date | None = None
    source_kind: str = "issuer"
    period_label: str = ""
    supporting_urls: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    revenue_label: str = "total revenue"
    net_income_label: str = "consolidated net income"
    accession: str | None = None
    revenue_tag: str | None = None
    net_income_tag: str | None = None

    def __post_init__(self) -> None:
        for name in ("period_end", "reported_at", "period_start", "prior_period_end", "prior_period_start"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _day(value))
        for name in ("revenue", "prior_revenue", "net_income"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number in reporting-currency units")
        if self.revenue <= 0 or self.prior_revenue <= 0:
            raise ValueError("comparable revenues must be positive")
        if not math.isfinite(self.revenue / self.prior_revenue) or not math.isfinite(self.net_income / self.revenue):
            raise ValueError("financial ratios must be finite")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("reporting currency must be a three-letter currency code")
        if self.period_type not in {"quarter", "annual"}:
            raise ValueError("only comparable quarter or annual results are supported")
        if self.reported_at < self.period_end:
            raise ValueError("results cannot be reported before the period ends")
        if self.period_start and self.period_start > self.period_end:
            raise ValueError("period start follows period end")
        if self.period_start:
            days = (self.period_end - self.period_start).days + 1
            lower, upper = (70, 110) if self.period_type == "quarter" else (330, 380)
            if not lower <= days <= upper:
                raise ValueError("financial period duration does not match its quarter/annual label")
        if self.prior_period_end and not 350 <= (self.period_end - self.prior_period_end).days <= 380:
            raise ValueError("comparison must be the corresponding prior-year period")
        if self.prior_period_start and (not self.prior_period_end or self.prior_period_start > self.prior_period_end):
            raise ValueError("invalid prior comparison period")
        if self.period_start and self.prior_period_start and self.prior_period_end:
            current_days = (self.period_end - self.period_start).days + 1
            prior_days = (self.prior_period_end - self.prior_period_start).days + 1
            if abs(current_days - prior_days) > 14:
                raise ValueError("comparison periods have materially different durations")
        for url in (self.source_url, *self.supporting_urls):
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("financial evidence requires a public HTTPS source URL")
        object.__setattr__(self, "supporting_urls", tuple(self.supporting_urls))
        object.__setattr__(self, "limitations", tuple(self.limitations))

    @property
    def growth(self) -> float:
        """Year-over-year revenue growth as a fraction (0.10 means ten percent)."""
        return self.revenue / self.prior_revenue - 1.0

    @property
    def net_margin(self) -> float:
        """Reported net income / revenue as a fraction; never an adjusted margin."""
        return self.net_income / self.revenue

    @property
    def revenue_growth_percent(self) -> float:
        return self.growth * 100.0

    @property
    def net_margin_percent(self) -> float:
        return self.net_margin * 100.0

    def is_fresh(self, now: date | datetime | None = None, *, max_age_days: int = 120) -> bool:
        today = _day(now or datetime.now(UTC))
        if max_age_days < 1:
            raise ValueError("financial result expiry must be positive")
        period_limit = 180 if self.period_type == "quarter" else 450
        return 0 <= (today - self.period_end).days <= period_limit and 0 <= (today - self.reported_at).days <= max_age_days

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for name in ("period_end", "reported_at", "period_start", "prior_period_end", "prior_period_start"):
            result[name] = getattr(self, name).isoformat() if getattr(self, name) else None
        return result


def financial_fact_from_dict(value: Mapping[str, Any]) -> FinancialFact:
    """Restore stored evidence while recomputing ratios from the underlying amounts."""
    allowed = {field.name for field in fields(FinancialFact)}
    return FinancialFact(**{key: item for key, item in value.items() if key in allowed})


@dataclass(frozen=True)
class _Context:
    value: float
    start: date
    end: date
    filed: date
    accession: str
    form: str
    currency: str
    tag: str
    standard: str

    @property
    def period_type(self) -> str | None:
        duration = (self.end - self.start).days + 1
        if 70 <= duration <= 110:
            return "quarter"
        if 330 <= duration <= 380:
            return "annual"
        return None


_REVENUE_TAGS = (
    ("us-gaap", "Revenues"),
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    ("us-gaap", "SalesRevenueNet"),
    ("ifrs-full", "Revenue"),
)
_INCOME_TAGS = (("us-gaap", "ProfitLoss"), ("us-gaap", "NetIncomeLoss"), ("ifrs-full", "ProfitLoss"))
_RESULT_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "6-K", "6-K/A"}


def _contexts(data: Mapping[str, Any], tags: tuple[tuple[str, str], ...], today: date) -> list[_Context]:
    output: list[_Context] = []
    facts = data.get("facts", {})
    for taxonomy, tag in tags:
        item = facts.get(taxonomy, {}).get(tag, {})
        for unit, values in item.get("units", {}).items():
            # Ratios, shares, dollars/share, and converted auxiliary units cannot be combined.
            if not re.fullmatch(r"[A-Z]{3}", unit):
                continue
            for row in values:
                try:
                    value = row["val"]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        continue
                    start, end, filed = _day(row["start"]), _day(row["end"]), _day(row["filed"])
                    accession = row["accn"]
                    form = row["form"]
                    if form not in _RESULT_FORMS or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                        continue
                    if start > end or end > filed or filed > today:
                        continue
                    context = _Context(float(value), start, end, filed, accession, form, unit, tag, taxonomy)
                    if context.period_type:
                        output.append(context)
                except (KeyError, TypeError, ValueError):
                    continue
    return output


def normalize_companyfacts(data: Mapping[str, Any], *, cik: str, symbol: str, now: date | datetime | None = None) -> FinancialFact | None:
    """Use an entity-wide XBRL total, same filing, currency and prior-year duration.

    The API excludes custom tags. If a complete total is unavailable, keep the reviewed
    issuer snapshot rather than inventing a number from components or YTD differences.
    """
    requested_cik = _cik(cik)
    try:
        if _cik(data.get("cik")) != requested_cik:
            raise ValueError("SEC company facts do not match the requested issuer")
    except (TypeError, OverflowError, ValueError) as exc:
        raise ValueError("invalid SEC response issuer") from exc
    today = _day(now or datetime.now(UTC))
    revenues = _contexts(data, _REVENUE_TAGS, today)
    # MELI's total includes financial income. Customer-contract revenue alone is incomplete.
    if symbol.upper() == "MELI":
        revenues = [row for row in revenues if row.tag == "Revenues"]
    incomes = _contexts(data, _INCOME_TAGS, today)
    candidates: list[tuple[tuple[Any, ...], FinancialFact]] = []
    tag_order = {tag: index for index, (_, tag) in enumerate(_REVENUE_TAGS)}
    for current in revenues:
        if current.value <= 0:
            continue
        context_values = {row.value for row in revenues if (row.start, row.end, row.currency, row.accession, row.tag, row.standard) == (current.start, current.end, current.currency, current.accession, current.tag, current.standard)}
        if len(context_values) != 1:
            continue
        same_income = [row for row in incomes if (row.start, row.end, row.currency, row.accession, row.standard) == (current.start, current.end, current.currency, current.accession, current.standard)]
        prior = [row for row in revenues if row.value > 0 and row.tag == current.tag and row.standard == current.standard and row.currency == current.currency and row.accession == current.accession and row.period_type == current.period_type and 350 <= (current.end - row.end).days <= 380 and abs((current.end - current.start).days - (row.end - row.start).days) <= 14]
        if not same_income or not prior:
            continue
        # Parent-attributable income and consolidated income are distinct concepts.
        # Prefer the consolidated concept when both exist, retaining its exact label.
        income_tag = "ProfitLoss" if any(row.tag == "ProfitLoss" for row in same_income) else "NetIncomeLoss"
        same_income = [row for row in same_income if row.tag == income_tag]
        # Multiple unequal values for an identical concept/context are ambiguous.
        if len({row.value for row in same_income}) != 1 or len({(row.start, row.end, row.value) for row in prior}) != 1:
            continue
        previous, income = prior[0], same_income[0]
        if current.period_type == "annual":
            limitations = ("Annual comparison: a complete comparable quarterly SEC context was unavailable; this is not quarterly growth.",)
        else:
            limitations = ()
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{current.accession.replace('-', '')}/{current.accession}-index.htm"
        income_label = "consolidated net income" if income.tag == "ProfitLoss" else "net income attributable to parent"
        fact = FinancialFact(symbol=symbol, source_url=url, period_end=current.end, reported_at=current.filed, revenue=current.value, prior_revenue=previous.value, net_income=income.value, currency=current.currency, accounting_standard="US GAAP" if current.standard == "us-gaap" else "IFRS", period_type=current.period_type or "quarter", period_start=current.start, prior_period_start=previous.start, prior_period_end=previous.end, source_kind="sec", period_label=f"{current.period_type} ended {current.end.isoformat()}", supporting_urls=(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json",), limitations=limitations, net_income_label=income_label, accession=current.accession, revenue_tag=current.tag, net_income_tag=income.tag)
        key = (current.end, current.period_type == "quarter", current.filed, -tag_order[current.tag])
        candidates.append((key, fact))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


class SECCompanyFactsCollector:
    """The caller owns the rate-limited client and durable refresh cache."""

    def __init__(self, client: ResilientClient):
        self.client = client

    async def latest(self, cik: str, symbol: str, now: date | datetime | None = None) -> FinancialFact | None:
        requested_cik = _cik(cik)
        data = await self.client.request_json("GET", f"https://data.sec.gov/api/xbrl/companyfacts/CIK{requested_cik:010d}.json")
        fact = normalize_companyfacts(data, cik=str(cik), symbol=symbol, now=now)
        return fact if fact and fact.is_fresh(now) else None
