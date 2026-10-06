"""Bounded daily market histories with explicit provider and unit provenance.

Public Nasdaq/Yahoo site endpoints are conveniences without an API SLA.
Unavailable, malformed, mismatched, or short histories never become quotes.
Daily observations describe past sessions, not executable intraday prices.
"""
from __future__ import annotations

import asyncio
import csv
import io
import math
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .sources.http import ResilientClient


class InstrumentLike(Protocol):
    symbol: str
    exchange: str
    currency: str
    provider_symbol: str
    listing_kind: str


@dataclass(frozen=True)
class DailyBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if not isinstance(self.date, date) or isinstance(self.date, datetime):
            raise ValueError("Daily bar requires a calendar date")
        for name in ("open", "high", "low", "close", "volume"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Daily {name} must be finite numeric data")
            if value < 0 or name != "volume" and value == 0:
                raise ValueError(f"Daily {name} is outside its valid range")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("Daily OHLC prices are inconsistent")


@dataclass(frozen=True)
class PriceHistory:
    symbol: str
    bars: tuple[DailyBar, ...]
    source: str
    provider_symbol: str
    source_url: str
    currency: str
    exchange: str
    observed_at: datetime
    status: str = "ok"
    error: str | None = None
    limitations: tuple[str, ...] = ()
    declared_exchange: str | None = None
    exchange_timezone: str | None = None
    adjustment_status: str = "unknown"
    metadata_source_url: str | None = None

    def __post_init__(self) -> None:
        _aware(self.observed_at)
        if any(not isinstance(bar, DailyBar) for bar in self.bars):
            raise ValueError("Price history contains an invalid daily bar")
        dates = [bar.date for bar in self.bars]
        if dates != sorted(set(dates)):
            raise ValueError("Daily dates must be strictly increasing and unique")
        cutoff = _local_date(self.observed_at, self.exchange_timezone) if self.exchange_timezone else self.observed_at.date()
        if any(value > cutoff for value in dates):
            raise ValueError("Daily history cannot contain future dates")
        if self.status == "ok" and not self.bars:
            raise ValueError("Successful price history must contain bars")

    @property
    def as_of(self) -> date | None:
        return self.bars[-1].date if self.bars else None


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Observation timestamp must include a timezone")


def _local_date(value: datetime, timezone: str | None) -> date:
    if not timezone:
        raise ValueError("Provider exchange timezone is missing")
    try:
        return value.astimezone(ZoneInfo(timezone)).date()
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Provider exchange timezone is invalid or unavailable") from exc


def _symbol(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9^._-]{1,32}", value):
        raise ValueError("An explicit, valid provider symbol is required")
    return value


def _number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing or invalid numeric provider field")
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", value):
            raise ValueError("Missing or invalid numeric provider field")
        value = value.removeprefix("$").replace(",", "")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid numeric provider field") from exc
    if not math.isfinite(number):
        raise ValueError("Nonfinite numeric provider field")
    return number


def _venue_group(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", value.upper())
    if normalized in {"NASDAQ", "NASDAQGS", "NASDAQGM", "NASDAQCM", "NMS", "NGM", "NCM"}:
        return "NASDAQ"
    if normalized in {"NYSE", "NYQ", "NEWYORKSTOCKEXCHANGE"}:
        return "NYSE"
    if normalized in {"NYSEARCA", "ARCA", "PCX"}:
        return "NYSE_ARCA"
    if normalized in {"NYSEAMERICAN", "AMEX", "ASE"}:
        return "NYSE_AMERICAN"
    if normalized in {"HKEX", "HKSE", "HKG", "HONGKONGSTOCKEXCHANGE"}:
        return "HKEX"
    return normalized


def _validate_venue(actual: str, declared: str) -> None:
    if not actual or not declared or _venue_group(actual) != _venue_group(declared):
        raise ValueError("Provider exchange does not match the declared listing venue")


def _validated_bars(values: list[DailyBar], *, observed_at: datetime, target: int,
                    timezone: str | None = None, exclude_today: bool = True) -> tuple[DailyBar, ...]:
    today = _local_date(observed_at, timezone) if timezone else observed_at.date()
    dates = [bar.date for bar in values]
    if len(set(dates)) != len(dates):
        raise ValueError("Provider history contains duplicate daily dates")
    if any(day > today for day in dates):
        raise ValueError("Provider history contains a future daily date")
    completed = [bar for bar in values if bar.date < today or not exclude_today]
    return tuple(sorted(completed, key=lambda bar: bar.date)[-target:])


def price_history_from_dict(value: dict[str, Any]) -> PriceHistory:
    """Validate serialized histories again when restoring durable evidence."""
    bars = tuple(DailyBar(date.fromisoformat(item["date"]) if isinstance(item["date"], str) else item["date"],
                          *(_number(item[key]) for key in ("open", "high", "low", "close", "volume")))
                 for item in value.get("bars", ()))
    observed = value["observed_at"]
    if isinstance(observed, str):
        observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    return PriceHistory(symbol=value["symbol"], bars=bars, source=value["source"],
                        provider_symbol=value["provider_symbol"], source_url=value["source_url"],
                        currency=value["currency"], exchange=value["exchange"], observed_at=observed,
                        status=value.get("status", "ok"), error=value.get("error"),
                        limitations=tuple(value.get("limitations", ())), declared_exchange=value.get("declared_exchange"),
                        exchange_timezone=value.get("exchange_timezone"), adjustment_status=value.get("adjustment_status", "unknown"),
                        metadata_source_url=value.get("metadata_source_url"))


class DailyPriceCollector:
    def __init__(self, client: ResilientClient | None = None, *, api_key: str = "", target_bars: int = 100,
                 min_bars: int = 60, provider_order: tuple[str, ...] | None = None,
                 min_request_interval: float = 0.5):
        if not 60 <= min_bars <= target_bars <= 500 or not 0 <= min_request_interval <= 10:
            raise ValueError("Daily history bounds or request interval are invalid")
        self.client = client or ResilientClient(timeout=20, max_attempts=2, max_response_bytes=2_000_000,
                                              headers={"User-Agent": "global-market-research-monitor/0.4 (public-data research)"})
        self._owns_client = client is None
        self.api_key = api_key
        self.target_bars = target_bars
        self.min_bars = min_bars
        self.provider_order = provider_order or (("twelve_data", "nasdaq", "yahoo", "stooq") if api_key else ("nasdaq", "yahoo", "stooq"))
        if len(set(self.provider_order)) != len(self.provider_order) or any(provider not in {"nasdaq", "yahoo", "stooq", "twelve_data"} for provider in self.provider_order):
            raise ValueError("Unknown daily market provider")
        self.min_request_interval = min_request_interval
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    async def _pace(self) -> None:
        async with self._request_lock:
            delay = self.min_request_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()

    async def _json(self, url: str, params: dict[str, Any]) -> Any:
        await self._pace()
        return await self.client.request_json("GET", url, params=params)

    async def collect(self, instrument: InstrumentLike, *, observed_at: datetime) -> PriceHistory:
        _aware(observed_at)
        observed_at = observed_at.astimezone(UTC)
        failures: list[str] = []
        short_history: PriceHistory | None = None
        for provider in self.provider_order:
            if provider == "twelve_data" and not self.api_key:
                continue
            try:
                result = await getattr(self, f"_{provider}")(instrument, observed_at)
                if result is None:
                    continue
                if len(result.bars) >= self.min_bars:
                    if failures:
                        result = replace(result, limitations=result.limitations + tuple(failures))
                    return result
                short_history = replace(result, status="insufficient", error=f"Fewer than {self.min_bars} completed daily bars")
                failures.append(f"{provider}: fewer than {self.min_bars} completed daily bars")
            except Exception as exc:
                # Never include a provider request URL or API credential in errors.
                detail = str(exc).replace(self.api_key, "[redacted]") if self.api_key else str(exc)
                failures.append(f"{provider}: {type(exc).__name__}: {detail[:240]}")
        if short_history is not None:
            return replace(short_history, limitations=short_history.limitations + tuple(failures))
        return PriceHistory(instrument.symbol, (), "unavailable", "", "", instrument.currency, instrument.exchange,
                            observed_at, status="unavailable", error="; ".join(failures) or "No explicit provider mapping is configured",
                            limitations=("No price or trading conclusion can be established from unavailable daily data.",),
                            declared_exchange=instrument.exchange)

    async def _nasdaq(self, instrument: InstrumentLike, observed_at: datetime) -> PriceHistory | None:
        mapped = getattr(instrument, "nasdaq_symbol", None)
        if not mapped:
            return None
        symbol = _symbol(mapped).upper()
        if instrument.currency != "USD" or _venue_group(instrument.exchange) not in {"NASDAQ", "NYSE", "NYSE_ARCA", "NYSE_AMERICAN"}:
            raise ValueError("Nasdaq public histories require an explicitly USD U.S.-listed instrument")
        assetclass = "etf" if instrument.listing_kind == "etf" else "stocks"
        base = f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/"
        params = {"assetclass": assetclass, "limit": self.target_bars + 30,
                  "fromdate": (observed_at.date() - timedelta(days=max(240, self.target_bars * 2))).isoformat(),
                  "todate": observed_at.date().isoformat()}
        payload = await self._json(base + "historical", params)
        info = await self._json(base + "info", {"assetclass": assetclass})
        history = (payload or {}).get("data")
        metadata = (info or {}).get("data")
        for response in (payload, info):
            code = (response.get("status") or {}).get("rCode") if isinstance(response, dict) else None
            if code is not None and code != 200:
                raise ValueError("Nasdaq provider status did not indicate success")
        if not isinstance(history, dict) or not isinstance(metadata, dict):
            raise ValueError("Nasdaq did not return historical data and listing metadata")
        if history.get("symbol") != symbol or metadata.get("symbol") != symbol:
            raise ValueError("Nasdaq returned a different instrument symbol")
        exchange = metadata.get("exchange") or ""
        _validate_venue(exchange, instrument.exchange)
        actual_currency = (metadata.get("primaryData") or {}).get("currency")
        if actual_currency is not None and actual_currency != "USD":
            raise ValueError("Nasdaq currency metadata does not match the declared USD unit")
        rows = (history.get("tradesTable") or {}).get("rows")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("Nasdaq historical rows are missing")
        values = [DailyBar(datetime.strptime(row["date"], "%m/%d/%Y").date(),
                           *(_number(row.get(key)) for key in ("open", "high", "low", "close", "volume"))) for row in rows]
        from .trades import completed_session_date
        cutoff = completed_session_date(instrument.exchange, observed_at)
        today = _local_date(observed_at, "America/New_York")
        # Calendar close alone is insufficient if the provider still reports
        # an open session or omits session-state metadata.
        if cutoff == today and metadata.get("marketStatus") != "Closed":
            cutoff = completed_session_date(instrument.exchange, observed_at.astimezone(ZoneInfo("America/New_York")).replace(hour=0, minute=0, second=0, microsecond=0))
        # A historical-table row fetched after its regular session completed
        # can retain that date. The reviewed holiday/early-close calendar and
        # 15-minute buffer exclude open sessions even across UTC midnight.
        validated = _validated_bars(values, observed_at=observed_at, target=1000,
                                    timezone="America/New_York", exclude_today=False)
        bars = tuple(bar for bar in validated if bar.date <= cutoff)[-self.target_bars:]
        return PriceHistory(instrument.symbol, bars, "nasdaq", symbol, base + "historical?" + urlencode(params),
                            "USD", exchange, observed_at, declared_exchange=instrument.exchange,
                            exchange_timezone="America/New_York", metadata_source_url=base + "info?" + urlencode({"assetclass": assetclass}),
                            limitations=("Public Nasdaq site endpoint is undocumented and has no API SLA.",
                                         "USD unit is declared by the U.S. listing catalogue; the public response may omit currency metadata.",
                                         "Historical rows are limited to completed scheduled U.S. regular sessions plus a 15-minute buffer; same-day rows also require provider Closed status. Unscheduled halts/closures and provider finality remain unverified; prices are not executable quotes.",
                                         "Corporate-action and split adjustment status is unknown; raw price moves are not guaranteed total returns."))

    async def _yahoo(self, instrument: InstrumentLike, observed_at: datetime) -> PriceHistory | None:
        mapped = getattr(instrument, "yahoo_symbol", None)
        if not mapped:
            return None
        symbol = _symbol(mapped)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
        params = {"interval": "1d", "range": "1y", "events": "splits,div"}
        payload = await self._json(url, params)
        chart = (payload or {}).get("chart") or {}
        results = chart.get("result")
        if chart.get("error") or not isinstance(results, list) or len(results) != 1:
            raise ValueError("Yahoo did not return one successful chart")
        result = results[0]
        meta = result.get("meta") or {}
        if meta.get("symbol") != symbol or meta.get("currency") != instrument.currency:
            raise ValueError("Yahoo symbol or currency metadata does not match the declared instrument")
        exchange = meta.get("fullExchangeName") or meta.get("exchangeName") or ""
        # Full display names vary; the exchange code is compared first.
        _validate_venue(meta.get("exchangeName") or exchange, instrument.exchange)
        timezone = meta.get("exchangeTimezoneName")
        today = _local_date(observed_at, timezone)
        regular_time = _number(meta.get("regularMarketTime"))
        if regular_time <= 0 or regular_time > observed_at.timestamp() + 300:
            raise ValueError("Yahoo market timestamp is in the future")
        regular_end = _number(((meta.get("currentTradingPeriod") or {}).get("regular") or {}).get("end"))
        if regular_end <= 0:
            raise ValueError("Yahoo regular session end timestamp is invalid")
        session_day = _local_date(datetime.fromtimestamp(regular_end, UTC), timezone)
        quote_rows = (result.get("indicators") or {}).get("quote") or []
        timestamps = result.get("timestamp")
        if len(quote_rows) != 1 or not isinstance(timestamps, list) or len(timestamps) > 1000:
            raise ValueError("Yahoo OHLC arrays are missing")
        fields = quote_rows[0]
        if any(not isinstance(fields.get(key), list) or len(fields[key]) != len(timestamps) for key in ("open", "high", "low", "close", "volume")):
            raise ValueError("Yahoo OHLC arrays have different lengths")
        values = []
        for index, stamp in enumerate(timestamps):
            stamp = _number(stamp)
            if stamp <= 0 or stamp > observed_at.timestamp():
                raise ValueError("Yahoo daily bar timestamp is in the future")
            day = _local_date(datetime.fromtimestamp(stamp, UTC), timezone)
            if day == today and (session_day != day or observed_at.timestamp() < regular_end + 900 or regular_time < regular_end - 300):
                continue
            values.append(DailyBar(day, *(_number(fields[key][index]) for key in ("open", "high", "low", "close", "volume"))))
        bars = _validated_bars(values, observed_at=observed_at, target=self.target_bars, timezone=timezone, exclude_today=False)
        return PriceHistory(instrument.symbol, bars, "yahoo", symbol, url + "?" + urlencode(params), instrument.currency,
                            exchange, observed_at, declared_exchange=instrument.exchange, exchange_timezone=timezone,
                            limitations=("Public Yahoo Finance chart endpoint is undocumented, unstable, and has no API SLA.",
                                         "Daily bars exclude an open or recently closed session; prices are not executable intraday quotes.",
                                         "Adjusted-close and corporate-action metadata are not used as a total-return series; split adjustment is unknown."))

    async def _stooq(self, instrument: InstrumentLike, observed_at: datetime) -> PriceHistory | None:
        mapped = getattr(instrument, "provider_symbol", None)
        if not mapped:
            return None
        symbol = _symbol(mapped)
        url = "https://stooq.com/q/d/l/"
        params = {"s": symbol.casefold(), "i": "d", "d1": (observed_at.date() - timedelta(days=max(240, self.target_bars * 2))).strftime("%Y%m%d"),
                  "d2": observed_at.date().strftime("%Y%m%d")}
        await self._pace()
        text = await self.client.request_text("GET", url, params=params)
        rows = csv.DictReader(io.StringIO(text))
        if rows.fieldnames != ["Date", "Open", "High", "Low", "Close", "Volume"]:
            raise ValueError("Stooq did not return the expected daily CSV columns")
        values = []
        for index, row in enumerate(rows):
            if index >= 1000:
                raise ValueError("Stooq daily row limit exceeded")
            values.append(DailyBar(date.fromisoformat(row["Date"]), *(_number(row[key]) for key in ("Open", "High", "Low", "Close", "Volume"))))
        bars = _validated_bars(values, observed_at=observed_at, target=self.target_bars)
        return PriceHistory(instrument.symbol, bars, "stooq", symbol, url + "?" + urlencode(params), instrument.currency,
                            instrument.exchange, observed_at, declared_exchange=instrument.exchange,
                            limitations=("Stooq CSV supplies no symbol/currency/exchange metadata; mapping and units come from the explicit catalogue.",
                                         "Current UTC-day bars are excluded; daily sessions are not executable intraday quotes.",
                                         "Split and corporate-action adjustment status is unknown; raw price moves are not guaranteed total returns."))

    async def _twelve_data(self, instrument: InstrumentLike, observed_at: datetime) -> PriceHistory | None:
        mapped = getattr(instrument, "twelve_data_symbol", None)
        declared_exchange = getattr(instrument, "twelve_data_exchange", None)
        if not mapped or not declared_exchange:
            return None
        symbol = _symbol(mapped)
        url = "https://api.twelvedata.com/time_series"
        public_params = {"symbol": symbol, "exchange": declared_exchange, "interval": "1day",
                         "outputsize": self.target_bars + 10, "order": "ASC"}
        payload = await self._json(url, {**public_params, "apikey": self.api_key})
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ValueError("Twelve Data did not return a successful daily history")
        meta = payload.get("meta") or {}
        if meta.get("symbol") != symbol or meta.get("currency") != instrument.currency or meta.get("interval") != "1day":
            raise ValueError("Twelve Data symbol, currency, or interval metadata does not match the declared instrument")
        exchange = meta.get("exchange") or ""
        _validate_venue(exchange, instrument.exchange)
        timezone = meta.get("exchange_timezone")
        rows = payload.get("values")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("Twelve Data daily rows are missing or outside the row bound")
        values = [DailyBar(date.fromisoformat(row["datetime"]), *(_number(row.get(key)) for key in ("open", "high", "low", "close", "volume"))) for row in rows]
        bars = _validated_bars(values, observed_at=observed_at, target=self.target_bars, timezone=timezone)
        return PriceHistory(instrument.symbol, bars, "twelve_data", symbol, url + "?" + urlencode(public_params), instrument.currency,
                            exchange, observed_at, declared_exchange=instrument.exchange, exchange_timezone=timezone,
                            limitations=("Daily dates use the provider's exchange-local timezone; current-day bars are excluded.",
                                         "Plan-dependent exchange coverage and delays apply; these are not executable intraday quotes.",
                                         "Corporate-action adjustment has not been independently reconciled; the series is not guaranteed total returns."))

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
