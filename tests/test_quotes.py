from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from contract_ipo_monitor.market import DailyPriceCollector
from contract_ipo_monitor.quotes import CurrentQuote, CurrentQuoteCollector, quote_freshness, quote_from_dict, quote_to_dict
from contract_ipo_monitor.sources.http import ResilientClient
from contract_ipo_monitor.sources.market import TwelveDataNormalizer


CLOSED = datetime(2026, 10, 6, 1, 55, tzinfo=UTC)
OPEN = datetime(2026, 10, 5, 16, tzinfo=UTC)


def instrument(**kwargs):
    return SimpleNamespace(**(dict(symbol="MSFT", currency="USD", exchange="NASDAQ", listing_kind="ordinary",
        nasdaq_symbol="MSFT", yahoo_symbol="MSFT", twelve_data_symbol="MSFT", twelve_data_exchange="NASDAQ") | kwargs))


def connection(handler):
    return ResilientClient(transport=httpx.MockTransport(handler), max_attempts=1, max_response_bytes=100_000)


def nasdaq_payload(stamp="Oct 5, 2026", state="Closed", realtime=False):
    return {"status": {"rCode": 200}, "data": {"symbol": "MSFT", "exchange": "NASDAQ-GS", "marketStatus": state,
        "primaryData": {"lastSalePrice": "$525.18", "lastTradeTimestamp": stamp, "isRealTime": realtime, "currency": None}}}


def yahoo_payload(at=None, *, delay=None, price=525.18):
    at = at or datetime(2026, 10, 5, 20, 0, 2, tzinfo=UTC)
    meta = {"symbol": "MSFT", "currency": "USD", "exchangeName": "NMS", "exchangeTimezoneName": "America/New_York",
            "regularMarketPrice": price, "regularMarketTime": int(at.timestamp()),
            "currentTradingPeriod": {"regular": {"start": int(datetime(2026,10,5,13,30,tzinfo=UTC).timestamp()),
                                                  "end": int(datetime(2026,10,5,20,tzinfo=UTC).timestamp())}}}
    if delay is not None:
        meta["exchangeDataDelayedBy"] = delay
    return {"chart": {"error": None, "result": [{"meta": meta, "indicators": {"quote": [{"close": [1.0]}]}}]}}


def live_quote(at=OPEN, delay=0, **updates):
    value = CurrentQuote("MSFT", "MSFT", "nasdaq", "https://api.nasdaq.com/api/quote/MSFT/info?assetclass=stocks",
        525.18, "USD", "NASDAQ", "America/New_York", at, OPEN, quote_type="live" if delay == 0 else "delayed",
        market_phase="regular", delay_seconds=delay, status="fresh", session_date=at.date(), timestamp_precision="second",
        provider_timestamp=at.isoformat(), timestamp_basis="last_trade", provider_market_status="Open")
    return replace(value, **updates)


def close_quote(day=date(2026,10,5), observed_at=CLOSED):
    return CurrentQuote("MSFT", "MSFT", "nasdaq", "https://api.nasdaq.com/api/quote/MSFT/info?assetclass=stocks",
        525.18, "USD", "NASDAQ", "America/New_York", None, observed_at, quote_type="session_close", market_phase="closed",
        session_date=day, timestamp_precision="session_date", provider_timestamp="Oct 5, 2026", provider_market_status="Closed")


@pytest.mark.asyncio
async def test_nasdaq_date_only_close_preserves_precision_without_fabricating_timestamp():
    client = connection(lambda request: httpx.Response(200, json=nasdaq_payload()))
    async with client.client:
        result = await CurrentQuoteCollector(client, provider_order=("nasdaq",), min_request_interval=0).collect(instrument(), observed_at=CLOSED)
    assert result.price == 525.18 and result.status == "fresh"
    assert result.quote_type == "session_close" and result.quote_at is None
    assert result.session_date == date(2026,10,5) and result.timestamp_precision == "session_date"
    assert result.provider_timestamp == "Oct 5, 2026"
    assert quote_freshness(result, CLOSED)["executable"] is False
    assert quote_from_dict(quote_to_dict(result) | {"archive_hash": "hash", "source_observed_at": CLOSED.isoformat(), "freshness": {}}) == result


@pytest.mark.asyncio
async def test_yahoo_exact_last_trade_wins_over_date_only_close_and_ignores_daily_close():
    client = connection(lambda request: httpx.Response(200, json=nasdaq_payload() if request.url.host == "api.nasdaq.com" else yahoo_payload()))
    async with client.client:
        result = await CurrentQuoteCollector(client, min_request_interval=0).collect(instrument(), observed_at=CLOSED)
    assert result.source == "yahoo" and result.price == 525.18
    assert result.quote_at == datetime(2026,10,5,20,0,2,tzinfo=UTC)
    assert result.status == "fresh" and result.quote_type == "session_close"
    assert result.timestamp_basis == "last_trade"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["yahoo", "nasdaq"])
async def test_open_quote_during_request_uses_response_completion_clock_and_keeps_future_guard(provider):
    completion = OPEN+timedelta(minutes=1)
    for quote_time, expected in [(completion, "fresh"), (completion+timedelta(seconds=60), "unavailable")]:
        payload = yahoo_payload(quote_time) if provider == "yahoo" else nasdaq_payload(
            quote_time.astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %Y %I:%M:%S %p ET"), "Open", True)
        client = connection(lambda request:httpx.Response(200,json=payload))
        async with client.client:
            collector = CurrentQuoteCollector(client,provider_order=(provider,),min_request_interval=0,clock=lambda:completion)
            result = await collector.collect(instrument(),observed_at=OPEN)
        assert result.observed_at == completion and result.quote_at == quote_time
        assert result.status == expected and result.quote_at > OPEN
        if expected == "unavailable":
            assert quote_freshness(result,quote_time+timedelta(seconds=1))["status"] == "unavailable"


@pytest.mark.asyncio
async def test_each_sequential_quote_has_its_own_response_observation_not_batch_start():
    completions = iter((OPEN+timedelta(seconds=30), OPEN+timedelta(seconds=60)))
    times = [OPEN+timedelta(seconds=29), OPEN+timedelta(seconds=59)]
    count = 0
    def handler(request):
        nonlocal count
        payload = yahoo_payload(times[count])
        count += 1
        return httpx.Response(200,json=payload)
    client = connection(handler)
    async with client.client:
        collector = CurrentQuoteCollector(client,provider_order=("yahoo",),min_request_interval=0,clock=lambda:next(completions))
        first = await collector.collect(instrument(),observed_at=OPEN)
        second = await collector.collect(instrument(),observed_at=OPEN)
    assert first.status == second.status == "fresh"
    assert first.observed_at == OPEN+timedelta(seconds=30)
    assert second.observed_at == OPEN+timedelta(seconds=60)
    assert first.quote_at == times[0] and second.quote_at == times[1]
    assert quote_freshness(first,second.observed_at)["status"] == "fresh"


@pytest.mark.asyncio
async def test_daily_collector_passes_quote_completion_clock_to_live_adapter():
    completion = OPEN+timedelta(seconds=30)
    client = connection(lambda request:httpx.Response(200,json=yahoo_payload(completion)))
    async with client.client:
        value = await DailyPriceCollector(client,min_request_interval=0,quote_clock=lambda:completion).collect_quote(
            instrument(nasdaq_symbol=None), observed_at=OPEN)
    assert value.status == "fresh" and value.observed_at == completion and value.quote_at == completion


@pytest.mark.asyncio
async def test_market_monitor_archives_actual_response_observation_and_checks_after_collection(tmp_path):
    from contract_ipo_monitor.config import Settings
    from contract_ipo_monitor.db import Database
    from contract_ipo_monitor.market import PriceHistory
    from contract_ipo_monitor.market_monitor import MarketMonitor
    from contract_ipo_monitor.universe import default_universe
    issuer = next(item for item in default_universe() if item.symbol == "MU")
    current = [OPEN]
    completion = OPEN+timedelta(seconds=30)
    class Prices:
        async def collect(self, instrument, *, observed_at):
            return PriceHistory(instrument.symbol,(),"unavailable","","",instrument.currency,instrument.exchange,observed_at,status="unavailable")
        async def collect_quote(self, instrument, *, observed_at):
            assert observed_at == OPEN
            current[0] = completion+timedelta(seconds=2)
            return replace(live_quote(at=completion-timedelta(seconds=1)), symbol=instrument.symbol,
                provider_symbol=instrument.symbol, source_url="https://api.nasdaq.com/api/quote/MU/info?assetclass=stocks",
                observed_at=completion)
    db = Database(tmp_path/"monitor.db")
    db.initialize()
    subject = MarketMonitor(db,Settings(sec_user_agent="Personal research person@example.org"), price_source=Prices(),
        instruments=(issuer,),benchmarks=(),now=lambda:current[0])
    await subject.collect_prices()
    archived = subject.store.latest("current_quote")["MU"]
    coverage = subject.store.coverage("quotes")[0]
    assert archived["observed_at"] == completion.isoformat()
    assert archived["source_observed_at"] == completion.isoformat()
    assert coverage["observed_at"] == completion.isoformat() and coverage["status"] == "fresh"
    assert quote_freshness(quote_from_dict(archived),completion+timedelta(seconds=301))["status"] == "stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["symbol", "currency", "venue", "time", "future", "nan", "date_only_open", "missing_status", "ambiguous_zone"])
async def test_nasdaq_unverified_data_never_produces_a_fresh_current_quote(fault):
    payload = nasdaq_payload()
    data, primary = payload["data"], payload["data"]["primaryData"]
    if fault == "symbol": data["symbol"] = "WRONG"
    elif fault == "currency": primary["currency"] = "CAD"
    elif fault == "venue": data["exchange"] = "NYSE"
    elif fault == "time": primary.pop("lastTradeTimestamp")
    elif fault == "future": primary["lastTradeTimestamp"] = "Oct 6, 2026 4:00 PM ET"
    elif fault == "nan": primary["lastSalePrice"] = "NaN"
    elif fault == "date_only_open": data["marketStatus"] = "Open"
    elif fault == "ambiguous_zone": primary["lastTradeTimestamp"] = "Oct 5, 2026 4:00 PM"
    else: payload.pop("status")
    client = connection(lambda request: httpx.Response(200, json=payload))
    async with client.client:
        result = await CurrentQuoteCollector(client, provider_order=("nasdaq",), min_request_interval=0).collect(instrument(), observed_at=CLOSED)
    assert result.status != "fresh"


@pytest.mark.asyncio
async def test_provider_failure_does_not_reuse_a_previous_successful_quote():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=nasdaq_payload()) if calls == 1 else httpx.Response(503)
    client = connection(handler)
    async with client.client:
        collector = CurrentQuoteCollector(client, provider_order=("nasdaq",), min_request_interval=0)
        good = await collector.collect(instrument(), observed_at=CLOSED)
        failed = await collector.collect(instrument(), observed_at=CLOSED+timedelta(minutes=1))
    assert good.price == 525.18 and failed.price is None
    assert failed.status == "unavailable" and failed.source == "unavailable"
    assert failed.quote_at is None and failed.observed_at == CLOSED+timedelta(minutes=1)


@pytest.mark.asyncio
async def test_failed_network_attempt_records_completion_without_inventing_quote_time():
    completion = OPEN+timedelta(seconds=30)
    client = connection(lambda request:httpx.Response(503))
    async with client.client:
        value = await CurrentQuoteCollector(client,provider_order=("nasdaq",),clock=lambda:completion,
            min_request_interval=0).collect(instrument(),observed_at=OPEN)
    assert value.status == "unavailable" and value.observed_at == completion
    assert value.price is None and value.quote_at is None


@pytest.mark.parametrize("at,day,status", [
    (datetime(2026,10,3,16,tzinfo=UTC), date(2026,10,2), "fresh"),
    (datetime(2026,10,5,12,tzinfo=UTC), date(2026,10,2), "fresh"),
    (datetime(2026,10,5,13,30,tzinfo=UTC), date(2026,10,2), "stale"),
    (datetime(2026,10,5,20,14,tzinfo=UTC), date(2026,10,5), "stale"),
    (datetime(2026,10,5,20,15,tzinfo=UTC), date(2026,10,5), "fresh"),
    (datetime(2026,10,5,22,tzinfo=UTC), date(2026,10,2), "stale"),
    (datetime(2026,4,3,16,tzinfo=UTC), date(2026,4,2), "fresh"),
    (datetime(2026,11,27,18,14,tzinfo=UTC), date(2026,11,27), "stale"),
    (datetime(2026,11,27,18,15,tzinfo=UTC), date(2026,11,27), "fresh"),
])
def test_closed_quote_freshness_uses_regular_calendar_weekends_holidays_and_early_close(at, day, status):
    result = quote_freshness(close_quote(day, at), at)
    assert result["status"] == status and not result["executable"]


def test_open_quote_known_delay_boundary_and_no_freshness_renewal_on_readback():
    quote = live_quote(OPEN-timedelta(seconds=1200), delay=900)
    assert quote_freshness(quote, OPEN)["status"] == "fresh"
    assert quote_freshness(replace(quote, quote_at=quote.quote_at-timedelta(seconds=1)), OPEN)["status"] == "stale"
    assert quote_freshness(quote, OPEN+timedelta(minutes=28))["status"] == "stale"
    stored = quote_to_dict(quote)
    restored = quote_from_dict(stored)
    assert restored.observed_at == OPEN
    assert quote_freshness(restored, OPEN+timedelta(minutes=28))["status"] == "stale"


def test_intraday_quote_cannot_be_reclassified_as_a_completed_close_on_readback():
    value = replace(live_quote(), quote_type="session_close", provider_market_status="Closed", observed_at=CLOSED)
    assert quote_freshness(value, CLOSED)["status"] == "unavailable"


@pytest.mark.asyncio
async def test_yahoo_weekend_keeps_friday_exact_close_when_provider_advances_session_to_monday():
    saturday = datetime(2026,10,3,18,tzinfo=UTC)
    payload = yahoo_payload(datetime(2026,10,2,20,0,2,tzinfo=UTC))
    client = connection(lambda request:httpx.Response(200,json=payload))
    async with client.client:
        result = await CurrentQuoteCollector(client,provider_order=("yahoo",),min_request_interval=0).collect(instrument(),observed_at=saturday)
    assert result.status == "fresh" and result.quote_type == "session_close"
    assert result.session_date == date(2026,10,2) and result.quote_at.date() == date(2026,10,2)
    assert result.regular_session_start.date() == date(2026,10,5)


@pytest.mark.parametrize("change", [
    {"observed_at": OPEN+timedelta(seconds=1)},
    {"quote_at": OPEN+timedelta(seconds=1)},
    {"quote_type": "unknown", "delay_seconds": None},
    {"exchange_timezone": "UTC"},
    {"source_url": "https://forum.example/MSFT"},
    {"source_url": "https://api.nasdaq.com/api/quote/MSFT/info?apikey=private"},
    {"exchange": "UNKNOWN"},
])
def test_future_unverified_or_bad_provenance_quotes_fail_closed(change):
    assert quote_freshness(live_quote(**change), OPEN)["status"] == "unavailable"


@pytest.mark.asyncio
async def test_nasdaq_live_minute_timestamp_retains_eastern_timezone_and_real_time_claim():
    client = connection(lambda request: httpx.Response(200,json=nasdaq_payload("Oct 5, 2026 12:00 PM ET", "Open", True)))
    async with client.client:
        value = await CurrentQuoteCollector(client,provider_order=("nasdaq",),min_request_interval=0).collect(instrument(),observed_at=OPEN)
    assert value.status == "fresh" and value.quote_type == "live" and value.delay_seconds == 0
    assert value.quote_at == OPEN and value.timestamp_precision == "minute"


@pytest.mark.asyncio
async def test_yahoo_delay_metadata_is_distinct_and_currency_timezone_identity_required():
    value = yahoo_payload(OPEN-timedelta(minutes=15), delay=15)
    for fault in (None, "timestamp", "currency", "venue", "timezone"):
        payload = deepcopy(value)
        meta = payload["chart"]["result"][0]["meta"]
        if fault == "timestamp": meta.pop("regularMarketTime")
        elif fault == "currency": meta["currency"] = "GBp"
        elif fault == "venue": meta["exchangeName"] = "HKG"
        elif fault == "timezone": meta["exchangeTimezoneName"] = "UTC"
        client = connection(lambda request:httpx.Response(200,json=payload))
        async with client.client:
            result = await CurrentQuoteCollector(client,provider_order=("yahoo",),min_request_interval=0).collect(instrument(),observed_at=OPEN)
        if fault is None:
            assert result.status == "fresh" and result.quote_type == "delayed" and result.delay_seconds == 900
        else:
            assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_twelve_data_uses_last_minute_not_candle_open_and_redacts_credentials():
    requested = []
    payload = {"symbol":"MSFT","currency":"USD","exchange":"NASDAQ","exchange_timezone":"America/New_York",
        "datetime":"2026-10-05","timestamp":int(datetime(2026,10,5,4,tzinfo=UTC).timestamp()),
        "last_quote_at":int(datetime(2026,10,5,19,59,tzinfo=UTC).timestamp()),"close":"525.18","is_market_open":False}
    def handler(request):
        requested.append(request)
        return httpx.Response(200,json=payload)
    client = connection(handler)
    async with client.client:
        collector = CurrentQuoteCollector(client,api_key="private-key",provider_order=("twelve_data",),min_request_interval=0)
        result = await collector.collect(instrument(),observed_at=CLOSED)
        payload.pop("last_quote_at")
        missing = await collector.collect(instrument(),observed_at=CLOSED)
    assert result.status == "fresh" and result.quote_type == "session_close"
    assert result.quote_at == datetime(2026,10,5,19,59,tzinfo=UTC) and result.timestamp_basis == "last_minute_candle"
    assert requested[0].url.params["interval"] == "1min"
    assert "private-key" not in str(quote_to_dict(result)) and "apikey" not in result.source_url
    assert missing.status == "unavailable" and missing.quote_at is None


@pytest.mark.asyncio
async def test_exception_urls_never_leak_keys_and_daily_collector_exposes_quote_api():
    def handler(request):
        raise RuntimeError(str(request.url))
    client = connection(handler)
    async with client.client:
        result = await DailyPriceCollector(client,api_key="private-key",min_request_interval=0).collect_quote(instrument(),observed_at=CLOSED)
    assert result.status == "unavailable" and result.price is None
    assert "private-key" not in str(quote_to_dict(result)) and "apikey" not in str(quote_to_dict(result))


@pytest.mark.parametrize("invalid", [{}, {"datetime":"2026-10-05 12:00:00"}, {"timestamp":OPEN.timestamp()},
                                      {"last_quote_at":True}, {"last_quote_at":float("nan")}, {"last_quote_at":OPEN.timestamp()+1}])
def test_legacy_normalizer_never_invents_quote_time_or_uses_bar_open(invalid):
    quote = {"close":"4.5","exchange":"NASDAQ"} | invalid
    assert TwelveDataNormalizer().normalize(symbol="MSFT",quote=quote,statistics={"market_cap":100},observed_at=OPEN) is None


def test_legacy_normalizer_rejects_other_symbol_and_overflowed_implied_market_cap():
    quote = {"symbol":"OTHER","close":"4.5","last_quote_at":OPEN.timestamp()}
    assert TwelveDataNormalizer().normalize(symbol="MSFT",quote=quote,statistics={"market_cap":100},observed_at=OPEN) is None
    quote.update(symbol="MSFT",close="1e308")
    assert TwelveDataNormalizer().normalize(symbol="MSFT",quote=quote,statistics={"shares_outstanding":"1e308"},observed_at=OPEN) is None


@pytest.mark.parametrize("invalid", [{"price":float("nan")}, {"price":True}, {"delay_seconds":True},
                                      {"quote_at":datetime(2026,10,5)}, {"observed_at":datetime(2026,10,5)}])
def test_quote_model_rejects_nonfinite_and_ambiguous_values(invalid):
    with pytest.raises(ValueError):
        live_quote(**invalid)
