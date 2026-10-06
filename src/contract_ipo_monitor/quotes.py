"""Timestamped informational quotes, independent of completed daily OHLC bars.

Public site endpoints have no API SLA. No value here proves a broker fill,
consolidated best bid/offer, or an executable price. Retrieval time never fills
in a missing provider timestamp.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, date, datetime, time as wall_time, timedelta
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

from .market import InstrumentLike, _aware, _number, _symbol, _validate_venue, _venue_group
from .sources.http import ResilientClient


YAHOO_DELAY_SOURCE = "https://help.yahoo.com/kb/finance/article-exchanges-data-delays-sln2310.html"
# Reviewed official provider exchange table, 2026-10-05. Do not infer other
# exchanges' delays or equate a listed venue with consolidated market coverage.
_YAHOO_REVIEWED_DELAYS = {"NASDAQ": 0, "HKEX": 900}
_US_VENUES = {"NASDAQ", "NYSE", "NYSE_ARCA", "NYSE_AMERICAN"}


@dataclass(frozen=True)
class CurrentQuote:
    symbol: str
    provider_symbol: str
    source: str
    source_url: str
    price: float | None
    currency: str
    exchange: str
    exchange_timezone: str | None
    quote_at: datetime | None
    observed_at: datetime
    quote_type: str = "unknown"
    market_phase: str = "unknown"
    delay_seconds: int | None = None
    status: str = "unavailable"
    session_date: date | None = None
    timestamp_precision: str = "unavailable"
    provider_timestamp: str | None = None
    timestamp_basis: str = "unknown"
    provider_market_status: str | None = None
    regular_session_start: datetime | None = None
    regular_session_end: datetime | None = None
    delay_source_url: str | None = None
    error: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self):
        _aware(self.observed_at)
        if self.quote_at is not None:
            _aware(self.quote_at)
        for stamp in (self.regular_session_start, self.regular_session_end):
            if stamp is not None:
                _aware(stamp)
        if self.price is not None and (isinstance(self.price, bool) or not isinstance(self.price, (int, float)) or not math.isfinite(self.price) or self.price <= 0):
            raise ValueError("Quote price must be finite and positive")
        if self.delay_seconds is not None and (isinstance(self.delay_seconds, bool) or not isinstance(self.delay_seconds, int) or not 0 <= self.delay_seconds <= 86400):
            raise ValueError("Quote delay must be a bounded integer in seconds")
        if self.quote_type not in {"live", "delayed", "session_close", "unknown"} or self.market_phase not in {"regular", "pre", "post", "closed", "unknown"}:
            raise ValueError("Unknown quote type or market phase")
        if self.status not in {"fresh", "stale", "unavailable"} or self.timestamp_precision not in {"second", "minute", "session_date", "unavailable"}:
            raise ValueError("Unknown quote status or timestamp precision")
        if self.session_date is not None and (not isinstance(self.session_date, date) or isinstance(self.session_date, datetime)):
            raise ValueError("Quote session requires a calendar date")
        if self.exchange_timezone:
            ZoneInfo(self.exchange_timezone)
        if self.quote_at is not None and self.exchange_timezone and self.session_date != self.quote_at.astimezone(ZoneInfo(self.exchange_timezone)).date():
            raise ValueError("Quote timestamp and exchange-local session date disagree")
        if (self.regular_session_start is None) != (self.regular_session_end is None):
            raise ValueError("Both provider regular-session boundaries are required")
        if self.regular_session_start is not None and not timedelta(0) < self.regular_session_end - self.regular_session_start <= timedelta(hours=12):
            raise ValueError("Provider regular-session interval is invalid")


def quote_to_dict(value: CurrentQuote) -> dict[str, Any]:
    result = asdict(value)
    for key, item in result.items():
        if isinstance(item, (datetime, date)):
            result[key] = item.isoformat()
        elif isinstance(item, tuple):
            result[key] = list(item)
    return result


def quote_from_dict(value: dict[str, Any]) -> CurrentQuote:
    data = {key: item for key, item in value.items() if key in {field.name for field in fields(CurrentQuote)}}
    for key in ("quote_at", "observed_at", "regular_session_start", "regular_session_end"):
        if isinstance(data.get(key), str):
            data[key] = datetime.fromisoformat(data[key].replace("Z", "+00:00"))
    if isinstance(data.get("session_date"), str):
        data["session_date"] = date.fromisoformat(data["session_date"])
    data["limitations"] = tuple(data.get("limitations", ()))
    return CurrentQuote(**data)


def _us_context(exchange: str, at: datetime):
    # Reuse the reviewed holiday/early-close calendar used by the trade engine.
    from .trades import _US_EARLY, _session_open, completed_session_date
    local = at.astimezone(ZoneInfo("America/New_York"))
    day = local.date()
    latest = completed_session_date(exchange, at)
    if not _session_open(exchange, day):
        return "closed", latest, None, None
    start = datetime.combine(day, wall_time(9, 30), ZoneInfo("America/New_York"))
    end = datetime.combine(day, wall_time(13 if day.strftime("%m-%d") in _US_EARLY[day.year] else 16), ZoneInfo("America/New_York"))
    phase = "regular" if start <= at < end else "pre" if local.time() >= wall_time(4) and at < start else "post" if end <= at and local.time() < wall_time(20) else "closed"
    return phase, latest, start, end


def _us_close(exchange: str, day: date) -> datetime:
    from .trades import _US_EARLY, _session_open
    if not _session_open(exchange, day):
        raise ValueError("Provider quote does not belong to a scheduled regular session")
    return datetime.combine(day, wall_time(13 if day.strftime("%m-%d") in _US_EARLY[day.year] else 16), ZoneInfo("America/New_York"))


def quote_freshness(value: CurrentQuote, at: datetime, *, max_open_age_seconds: int = 300,
                    max_observation_age_seconds: int = 300) -> dict[str, Any]:
    """Re-evaluate an archived quote at consumption, including retrieval age.

    A recent latest-session close is usable as a labelled reference while the
    regular market is shut. It never becomes a current intraday quote. During
    an open session the timestamp must be exact enough and within a documented
    delay plus the configured tolerance. Returned mapping is JSON-safe.
    """
    _aware(at)
    if any(isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 86400 for limit in (max_open_age_seconds, max_observation_age_seconds)):
        raise ValueError("Quote freshness tolerances must be bounded positive seconds")
    result = {"status": "unavailable", "reason": "Quote price or provider timestamp is unavailable",
              "market_phase": "unknown", "quote_age_seconds": None, "maximum_age_seconds": None,
              "observation_age_seconds": (at-value.observed_at).total_seconds(),
              "latest_completed_session": None, "executable": False}
    if value.observed_at > at or (value.quote_at is not None and (value.quote_at > at or value.quote_at > value.observed_at)):
        result["reason"] = "Quote observation or provider timestamp is in the future"
        return result
    if value.price is None or value.source == "unavailable" or value.session_date is None:
        result["reason"] = value.error or result["reason"]
        return result
    if result["observation_age_seconds"] > max_observation_age_seconds:
        result.update(status="stale", reason="Quote was not refreshed recently; collect again before evaluation")
        return result
    if not value.exchange_timezone:
        result["reason"] = "Exchange timezone is unavailable"
        return result
    expected_host = {"nasdaq": "api.nasdaq.com", "yahoo": "query1.finance.yahoo.com", "twelve_data": "api.twelvedata.com"}.get(value.source)
    try:
        parsed = urlparse(value.source_url)
        valid_url = parsed.scheme == "https" and parsed.hostname == expected_host and not parsed.username and not parsed.password and parsed.port in (None, 443) and "apikey" not in parsed.query.casefold()
    except (ValueError, TypeError):
        valid_url = False
    if not valid_url:
        result["reason"] = "Quote provenance URL does not identify a reviewed provider"
        return result
    if value.session_date > at.astimezone(ZoneInfo(value.exchange_timezone)).date():
        result["reason"] = "Provider session date is in the future"
        return result
    expected_tz = "America/New_York" if _venue_group(value.exchange) in _US_VENUES else "Asia/Hong_Kong" if _venue_group(value.exchange) == "HKEX" else None
    if expected_tz is None or value.exchange_timezone != expected_tz:
        result["reason"] = "Quote timezone or venue has not been reviewed"
        return result
    try:
        if _venue_group(value.exchange) in _US_VENUES:
            phase, latest, start, end = _us_context(value.exchange, at)
        else:
            # An exact quote inside a current provider-reported native session
            # establishes an open-session reference, not a holiday calendar.
            start, end = value.regular_session_start, value.regular_session_end
            if start is None or not start <= at < end:
                raise ValueError("Closed-session calendar is unavailable for this venue")
            phase, latest = "regular", None
        result.update(market_phase=phase, latest_completed_session=latest.isoformat() if latest else None)
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    if value.quote_at is not None:
        result["quote_age_seconds"] = (at-value.quote_at).total_seconds()
    if value.quote_type == "session_close":
        if phase == "regular":
            result.update(status="stale", reason="Previous session close is not a current quote while the regular market is open")
        elif latest is None or value.session_date != latest:
            result.update(status="stale", reason="Provider close does not belong to the latest completed regular session")
        elif value.provider_market_status not in {"Closed", "closed"}:
            result["reason"] = "Provider has not established a closed regular session"
        elif value.quote_at is not None and abs((value.quote_at-_us_close(value.exchange, value.session_date)).total_seconds()) > 300:
            result["reason"] = "Provider timestamp is not near the regular-session close"
        else:
            result.update(status="fresh", reason="Latest completed regular-session close; reference only, exact trade time may be unavailable")
        return result
    if value.quote_at is None or value.timestamp_precision not in {"second", "minute"}:
        result["reason"] = "Provider supplies no intraday timestamp"
        return result
    if value.quote_type not in {"live", "delayed"} or value.delay_seconds is None:
        result["reason"] = "Provider delay or real-time classification is unverified"
        return result
    result["maximum_age_seconds"] = value.delay_seconds + max_open_age_seconds
    if result["quote_age_seconds"] > result["maximum_age_seconds"]:
        result.update(status="stale", reason="Provider timestamp exceeds its declared delay and freshness tolerance")
    elif value.market_phase != phase or phase != "regular" or not start <= value.quote_at <= end:
        result.update(status="stale", reason="Quote does not belong to the current regular session; extended hours remain separate")
    else:
        result.update(status="fresh", reason="Recent timestamped informational quote within the declared provider delay")
    return result


def _epoch(value: Any) -> datetime:
    seconds = _number(value)
    if seconds <= 0:
        raise ValueError("Provider timestamp must be positive")
    return datetime.fromtimestamp(seconds, UTC)


def _nasdaq_time(raw: Any):
    if not isinstance(raw, str) or len(raw) > 100:
        raise ValueError("Nasdaq provider timestamp is missing")
    raw = raw.strip()
    try:
        return None, datetime.strptime(raw, "%b %d, %Y").date(), "session_date"
    except ValueError:
        pass
    found = re.fullmatch(r"(.+?)\s+(ET|EDT|EST)", raw)
    if not found:
        raise ValueError("Nasdaq intraday timestamp requires an explicit Eastern timezone")
    for form, precision in (("%b %d, %Y %I:%M:%S %p", "second"), ("%b %d, %Y %I:%M %p", "minute")):
        try:
            local = datetime.strptime(found[1], form).replace(tzinfo=ZoneInfo("America/New_York"))
        except ValueError:
            continue
        if found[2] in {"EDT", "EST"} and local.tzname() != found[2]:
            raise ValueError("Nasdaq timezone abbreviation disagrees with the session date")
        return local.astimezone(UTC), local.date(), precision
    raise ValueError("Nasdaq timestamp format is unrecognized")


class CurrentQuoteCollector:
    def __init__(self, client: ResilientClient | None = None, *, api_key: str = "",
                 provider_order: tuple[str, ...] | None = None, min_request_interval: float = .5,
                 clock: Callable[[], datetime] | None = None):
        self.client = client or ResilientClient(timeout=15, max_attempts=2, max_response_bytes=2_000_000,
            headers={"User-Agent": "global-market-research-monitor/0.5 (personal informational research)"})
        self._owns_client = client is None
        self.api_key = api_key
        self.provider_order = provider_order or (("twelve_data", "nasdaq", "yahoo") if api_key else ("nasdaq", "yahoo"))
        if not self.provider_order or len(set(self.provider_order)) != len(self.provider_order) or any(item not in {"nasdaq", "yahoo", "twelve_data"} for item in self.provider_order):
            raise ValueError("Unknown or duplicate current quote provider")
        if not 0 <= min_request_interval <= 10:
            raise ValueError("Quote request interval is outside its bound")
        self.min_request_interval = min_request_interval
        self.clock = clock
        self._lock, self._last_request = asyncio.Lock(), 0.0

    def _response_observed_at(self, started_at: datetime) -> datetime:
        completed_at = self.clock() if self.clock is not None else started_at
        _aware(completed_at)
        if completed_at < started_at:
            raise ValueError("Quote collection clock moved backwards")
        return completed_at.astimezone(UTC)

    async def _json(self, url: str, params: dict[str, Any]):
        async with self._lock:
            delay = self.min_request_interval - (time.monotonic()-self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()
        return await self.client.request_json("GET", url, params=params)

    async def collect(self, instrument: InstrumentLike, *, observed_at: datetime) -> CurrentQuote:
        _aware(observed_at)
        failures, best = [], None
        for provider in self.provider_order:
            if provider == "twelve_data" and not self.api_key:
                continue
            try:
                candidate = await getattr(self, f"_{provider}")(instrument, observed_at)
                if candidate is None:
                    continue
                freshness = quote_freshness(candidate, candidate.observed_at)
                candidate = replace(candidate, status=freshness["status"], error=None if freshness["status"] == "fresh" else freshness["reason"])
                rank = (candidate.status == "fresh", candidate.status == "stale", candidate.quote_at is not None)
                if best is None or rank > (best.status == "fresh", best.status == "stale", best.quote_at is not None):
                    best = candidate
                if candidate.status == "fresh" and candidate.quote_at is not None:
                    return replace(candidate, limitations=candidate.limitations+tuple(failures))
            except Exception as exc:
                # Request exceptions can contain URLs including API credentials.
                # Keep bounded provider/class evidence, never their raw message.
                failures.append(f"{provider}: {type(exc).__name__}; current quote could not be validated")
        if best is not None:
            return replace(best, limitations=best.limitations+tuple(failures))
        try:
            unavailable_at = self._response_observed_at(observed_at)
        except ValueError:
            unavailable_at = observed_at
            failures.append("clock: response completion observation could not be validated")
        return CurrentQuote(instrument.symbol, "", "unavailable", "", None, instrument.currency,
                            instrument.exchange, None, None, unavailable_at, error="; ".join(failures) or "No explicit quote provider mapping is configured",
                            limitations=("No previous quote or daily close is substituted when current collection fails.",))

    async def _nasdaq(self, instrument: InstrumentLike, observed_at: datetime) -> CurrentQuote | None:
        mapped = getattr(instrument, "nasdaq_symbol", None)
        if not mapped:
            return None
        symbol = _symbol(mapped).upper()
        if instrument.currency != "USD" or _venue_group(instrument.exchange) not in _US_VENUES:
            raise ValueError("Nasdaq public quotes require an explicitly USD U.S. listing")
        params = {"assetclass": "etf" if instrument.listing_kind == "etf" else "stocks"}
        url = f"https://api.nasdaq.com/api/quote/{quote(symbol, safe='')}/info"
        payload = await self._json(url, params)
        observed_at = self._response_observed_at(observed_at)
        if not isinstance(payload, dict) or (payload.get("status") or {}).get("rCode") != 200:
            raise ValueError("Nasdaq quote response is unsuccessful")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("symbol") != symbol:
            raise ValueError("Nasdaq quote symbol is mismatched")
        exchange, primary = data.get("exchange"), data.get("primaryData") or {}
        _validate_venue(exchange, instrument.exchange)
        if primary.get("currency") not in (None, "USD"):
            raise ValueError("Nasdaq quote currency disagrees with the listing")
        timestamp, day, precision = _nasdaq_time(primary.get("lastTradeTimestamp"))
        price = _number(primary.get("lastSalePrice"))
        phase, latest, _, _ = _us_context(exchange, observed_at)
        closed = data.get("marketStatus") == "Closed" and phase != "regular" and day <= latest
        # A timestamped value earlier in the session is not promoted to its
        # close just because collection happens after hours.
        if closed and timestamp is not None:
            local_close = _us_close(exchange, day)
            closed = abs((timestamp-local_close).total_seconds()) <= 300
        kind = "session_close" if closed else "live" if primary.get("isRealTime") is True else "unknown"
        return CurrentQuote(instrument.symbol, symbol, "nasdaq", url+"?"+urlencode(params), price, "USD", exchange,
                            "America/New_York", timestamp, observed_at, quote_type=kind, market_phase="closed" if closed else phase,
                            delay_seconds=0 if primary.get("isRealTime") is True else None, session_date=day,
                            timestamp_precision=precision, provider_timestamp=primary.get("lastTradeTimestamp"),
                            timestamp_basis="session_date" if timestamp is None else "last_trade", provider_market_status=data.get("marketStatus"),
                            limitations=("Public Nasdaq site endpoint is undocumented and has no API SLA; Nasdaq last-sale coverage is not a broker executable quote.",
                                         "USD is declared by the reviewed U.S. listing when provider currency metadata is absent.",
                                         "A date-only provider close has no exact trade timestamp; no retrieval time is substituted."))

    async def _yahoo(self, instrument: InstrumentLike, observed_at: datetime) -> CurrentQuote | None:
        mapped = getattr(instrument, "yahoo_symbol", None)
        if not mapped:
            return None
        symbol = _symbol(mapped)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
        params = {"interval": "1m", "range": "1d"}
        payload = await self._json(url, params)
        observed_at = self._response_observed_at(observed_at)
        chart = (payload or {}).get("chart") or {}
        results = chart.get("result")
        if chart.get("error") or not isinstance(results, list) or len(results) != 1:
            raise ValueError("Yahoo quote chart response is unsuccessful")
        meta = results[0].get("meta") or {}
        if meta.get("symbol") != symbol or meta.get("currency") != instrument.currency:
            raise ValueError("Yahoo quote symbol or currency is mismatched")
        exchange = meta.get("exchangeName") or ""
        _validate_venue(exchange, instrument.exchange)
        timezone = meta.get("exchangeTimezoneName")
        if not timezone:
            raise ValueError("Yahoo quote timezone is unavailable")
        timestamp = _epoch(meta.get("regularMarketTime"))
        day = timestamp.astimezone(ZoneInfo(timezone)).date()
        regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        start, end = _epoch(regular.get("start")), _epoch(regular.get("end"))
        delay = meta.get("exchangeDataDelayedBy")
        delay_source = url if delay is not None else YAHOO_DELAY_SOURCE
        if delay is None:
            delay = _YAHOO_REVIEWED_DELAYS.get(_venue_group(exchange))
        elif isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise ValueError("Yahoo delay metadata is invalid")
        else:
            delay *= 60
        phase = "regular" if start <= observed_at < end else "closed"
        kind = "live" if delay == 0 else "delayed" if delay is not None else "unknown"
        state = "Open" if phase == "regular" else "Closed"
        if _venue_group(exchange) in _US_VENUES:
            phase, latest, _, _ = _us_context(exchange, observed_at)
            # Yahoo may advance currentTradingPeriod to Monday on a weekend.
            # Preserve the exact last trade on Friday and verify that session's
            # reviewed calendar close instead of pretending it traded Monday.
            close = _us_close(exchange, day)
            if phase != "regular" and observed_at >= close+timedelta(minutes=15) and day == latest and abs((timestamp-close).total_seconds()) <= 300:
                kind, state = "session_close", "Closed"
        return CurrentQuote(instrument.symbol, symbol, "yahoo", url+"?"+urlencode(params), _number(meta.get("regularMarketPrice")),
                            instrument.currency, exchange, timezone, timestamp, observed_at, quote_type=kind, market_phase=phase,
                            delay_seconds=delay, session_date=day, timestamp_precision="second", provider_timestamp=str(meta.get("regularMarketTime")),
                            timestamp_basis="last_trade", provider_market_status=state, regular_session_start=start, regular_session_end=end,
                            delay_source_url=delay_source,
                            limitations=("Public Yahoo chart endpoint is undocumented and has no API SLA; informational use only, subject to provider terms and redistribution restrictions.",
                                         "Price is regularMarketPrice at exact regularMarketTime, never the last daily OHLC close; extended-hours prices are not substituted.",
                                         "Exchange delays are provider metadata or the reviewed official exchange table; an unknown delay remains unverified."))

    async def _twelve_data(self, instrument: InstrumentLike, observed_at: datetime) -> CurrentQuote | None:
        mapped, exchange_mapping = getattr(instrument, "twelve_data_symbol", None), getattr(instrument, "twelve_data_exchange", None)
        if not mapped or not exchange_mapping:
            return None
        symbol = _symbol(mapped)
        url = "https://api.twelvedata.com/quote"
        public = {"symbol": symbol, "exchange": exchange_mapping, "interval": "1min", "timezone": "UTC"}
        data = await self._json(url, {**public, "apikey": self.api_key})
        observed_at = self._response_observed_at(observed_at)
        if not isinstance(data, dict) or data.get("status") == "error" or data.get("symbol") != symbol or data.get("currency") != instrument.currency:
            raise ValueError("Twelve Data quote identity or currency is mismatched")
        exchange = data.get("exchange") or ""
        _validate_venue(exchange, instrument.exchange)
        timezone = data.get("exchange_timezone") or ("America/New_York" if _venue_group(exchange) in _US_VENUES else "Asia/Hong_Kong" if _venue_group(exchange) == "HKEX" else None)
        if not timezone:
            raise ValueError("Twelve Data exchange timezone is unavailable")
        # Official /quote docs: timestamp/datetime are candle OPEN times.
        # last_quote_at is the last minute candle, not an exact exchange trade.
        timestamp = _epoch(data.get("last_quote_at"))
        day = timestamp.astimezone(ZoneInfo(timezone)).date()
        phase, latest, _, _ = _us_context(exchange, observed_at)
        kind, state = "unknown", "Open" if data.get("is_market_open") is True else "Closed" if data.get("is_market_open") is False else None
        if phase != "regular" and state == "Closed" and day == latest:
            close = _us_close(exchange, day)
            if -120 <= (timestamp-close).total_seconds() <= 300:
                kind = "session_close"
        return CurrentQuote(instrument.symbol, symbol, "twelve_data", url+"?"+urlencode(public), _number(data.get("close")),
                            instrument.currency, exchange, timezone, timestamp, observed_at, quote_type=kind, market_phase=phase,
                            session_date=day, timestamp_precision="minute", provider_timestamp=str(data.get("last_quote_at")),
                            timestamp_basis="last_minute_candle", provider_market_status=state,
                            limitations=("Twelve Data last_quote_at is the last minute candle timestamp, not proof of an exact last trade or executable quote.",
                                         "Plan-dependent delay is unverified unless supplied explicitly; no API key is archived in the source URL."))

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
