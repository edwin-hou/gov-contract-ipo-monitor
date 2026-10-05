import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from xml.sax.saxutils import escape

import httpx
import pytest

from contract_ipo_monitor.market import DailyBar, DailyPriceCollector, PriceHistory, price_history_from_dict
from contract_ipo_monitor.research import serializable
from contract_ipo_monitor.sources.http import ResilientClient
from contract_ipo_monitor.worldnews import (
    WorldEvent, WorldFeed, WorldNewsCollector, associate_events,
    match_macro_themes, world_event_from_dict,
)


NOW = datetime(2026, 10, 5, 22, tzinfo=UTC)


def instrument(**changes):
    values = dict(symbol="MSFT", name="Microsoft", currency="USD", exchange="NASDAQ", provider_symbol="msft.us",
                  nasdaq_symbol="MSFT", yahoo_symbol="MSFT", twelve_data_symbol="MSFT", twelve_data_exchange="NASDAQ",
                  listing_kind="ordinary")
    return SimpleNamespace(**(values | changes))


def dates(count=130):
    days, current = [], NOW.date() - timedelta(days=1)
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    return days


def nasdaq_rows(count=130):
    return [{"date": day.strftime("%m/%d/%Y"), "open": "$100.00", "high": "$102.00",
             "low": "$99.00", "close": "$101.00", "volume": "100,000"} for day in dates(count)]


def nasdaq_handler(rows=None, *, symbol="MSFT", exchange="NASDAQ-GS", currency=None, code=200):
    def handle(request):
        if request.url.path.endswith("/info"):
            data = {"symbol": symbol, "exchange": exchange, "primaryData": {"currency": currency}}
        else:
            data = {"symbol": symbol, "tradesTable": {"rows": nasdaq_rows() if rows is None else rows}}
        return httpx.Response(200, json={"data": data, "status": {"rCode": code}})
    return handle


def client(handler, **kwargs):
    return ResilientClient(transport=httpx.MockTransport(handler), max_attempts=1,
                           max_response_bytes=2_000_000, **kwargs)


@pytest.mark.asyncio
async def test_nasdaq_history_validates_listing_and_retains_explicit_provenance():
    connection = client(nasdaq_handler())
    async with connection.client:
        result = await DailyPriceCollector(connection, provider_order=("nasdaq",), min_request_interval=0).collect(instrument(), observed_at=NOW)
    assert result.status == "ok" and len(result.bars) == 100
    assert result.as_of == date(2026, 10, 2)
    assert [bar.date for bar in result.bars] == sorted(set(bar.date for bar in result.bars))
    assert result.exchange == "NASDAQ-GS" and result.declared_exchange == "NASDAQ"
    assert result.currency == "USD" and result.provider_symbol == "MSFT"
    assert "/historical?" in result.source_url and "/info?" in result.metadata_source_url
    assert any("omit currency" in limitation for limitation in result.limitations)
    assert result.adjustment_status == "unknown"
    assert price_history_from_dict(serializable(asdict(result))) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("symbol", "currency", "exchange", "duplicate", "future", "nan", "negative_volume", "ohlc", "status"))
async def test_invalid_or_mismatched_nasdaq_data_never_becomes_a_quote(fault):
    rows = nasdaq_rows()
    settings = {}
    if fault == "symbol": settings["symbol"] = "OTHER"
    elif fault == "currency": settings["currency"] = "CAD"
    elif fault == "exchange": settings["exchange"] = "NYSE"
    elif fault == "duplicate": rows.append(deepcopy(rows[0]))
    elif fault == "future": rows[0]["date"] = (NOW.date() + timedelta(days=1)).strftime("%m/%d/%Y")
    elif fault == "nan": rows[0]["close"] = "NaN"
    elif fault == "negative_volume": rows[0]["volume"] = "-1"
    elif fault == "ohlc": rows[0]["high"] = "$98.00"
    elif fault == "status": settings["code"] = 500
    connection = client(nasdaq_handler(rows, **settings))
    async with connection.client:
        result = await DailyPriceCollector(connection, provider_order=("nasdaq",), min_request_interval=0).collect(instrument(), observed_at=NOW)
    assert result.status == "unavailable" and result.bars == () and result.error


@pytest.mark.asyncio
async def test_short_history_excludes_current_session_and_remains_insufficient():
    rows = nasdaq_rows(59)
    rows.insert(0, dict(rows[0], date=NOW.strftime("%m/%d/%Y")))
    connection = client(nasdaq_handler(rows))
    async with connection.client:
        result = await DailyPriceCollector(connection, provider_order=("nasdaq",), min_request_interval=0).collect(instrument(), observed_at=NOW)
    assert result.status == "insufficient" and len(result.bars) == 59
    assert result.as_of < NOW.date() and "60" in result.error


@pytest.mark.asyncio
async def test_explicit_etf_mapping_selects_etf_route_and_currency_provenance():
    requests = []
    def handle(request):
        requests.append(request)
        return nasdaq_handler(symbol="ACWI", exchange="NASDAQ-GM")(request)
    connection = client(handle)
    async with connection.client:
        result = await DailyPriceCollector(connection, provider_order=("nasdaq",), min_request_interval=0).collect(
            instrument(symbol="ACWI", nasdaq_symbol="ACWI", listing_kind="etf"), observed_at=NOW)
    assert result.status == "ok"
    assert all(request.url.params["assetclass"] == "etf" for request in requests)


@pytest.mark.asyncio
async def test_stooq_uses_only_explicit_symbol_mapping_and_keeps_unit_limitations():
    requests = []
    text = "Date,Open,High,Low,Close,Volume\n" + "\n".join(f"{day},100,102,99,101,100000" for day in reversed(dates(100)))
    def handle(request):
        requests.append(request)
        return httpx.Response(200, text=text)
    connection = client(handle)
    async with connection.client:
        result = await DailyPriceCollector(connection, provider_order=("stooq",), min_request_interval=0).collect(
            instrument(symbol="SAP.DE", provider_symbol="EXPLICIT.DE", exchange="XETRA", currency="EUR"), observed_at=NOW)
    assert requests[0].url.params["s"] == "explicit.de"
    assert result.status == "ok" and result.currency == "EUR"
    assert any("no symbol/currency/exchange metadata" in value for value in result.limitations)


@pytest.mark.asyncio
async def test_stooq_challenge_html_and_streamed_oversize_are_not_quotes():
    for response in (httpx.Response(200, text="<html>Verify your browser</html>"),
                     httpx.Response(200, content=b"x" * 3000)):
        connection = ResilientClient(transport=httpx.MockTransport(lambda request: response),
                                     max_attempts=1, max_response_bytes=1024)
        async with connection.client:
            result = await DailyPriceCollector(connection, provider_order=("stooq",), min_request_interval=0).collect(instrument(), observed_at=NOW)
        assert result.status == "unavailable" and not result.bars


@pytest.mark.asyncio
async def test_twelve_data_validates_metadata_and_never_persists_key_in_source_url():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "meta": {"symbol": "MSFT", "currency": "USD", "exchange": "NASDAQ",
            "exchange_timezone": "America/New_York", "interval": "1day"},
            "values": [{"datetime": day.isoformat(), "open": "100", "high": "102", "low": "99", "close": "101", "volume": "100000"} for day in dates(100)]})
    connection = client(handle)
    async with connection.client:
        result = await DailyPriceCollector(connection, api_key="private-key", provider_order=("twelve_data",), min_request_interval=0).collect(instrument(), observed_at=NOW)
    assert requests[0].url.params["apikey"] == "private-key"
    assert result.status == "ok" and result.source == "twelve_data"
    assert "apikey" not in result.source_url and "private-key" not in str(asdict(result))


def yahoo_payload(*, include_today=True):
    days = list(reversed(dates(100))) + ([NOW.date()] if include_today else [])
    timestamps = [int(datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC).timestamp()) for day in days]
    return {"chart": {"error": None, "result": [{"meta": {"symbol": "MSFT", "currency": "USD", "exchangeName": "NMS",
        "fullExchangeName": "NasdaqGS", "exchangeTimezoneName": "America/New_York", "regularMarketTime": NOW.timestamp(),
        "currentTradingPeriod": {"regular": {"end": datetime(2026, 10, 5, 20, tzinfo=UTC).timestamp()}}},
        "timestamp": timestamps, "indicators": {"quote": [{key: [value] * len(days) for key, value in
            (("open", 100), ("high", 102), ("low", 99), ("close", 101), ("volume", 100000))}]}}]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("time,expected_asof", ((datetime(2026, 10, 5, 19, tzinfo=UTC), date(2026, 10, 2)),
                                                (datetime(2026, 10, 5, 20, 10, tzinfo=UTC), date(2026, 10, 2)),
                                                (NOW, date(2026, 10, 5))))
async def test_yahoo_excludes_open_and_recently_closed_sessions(time, expected_asof):
    payload = yahoo_payload()
    payload["chart"]["result"][0]["meta"]["regularMarketTime"] = min(time.timestamp(), datetime(2026, 10, 5, 20, tzinfo=UTC).timestamp())
    connection = client(lambda request: httpx.Response(200, json=payload))
    async with connection.client:
        result = await DailyPriceCollector(connection, provider_order=("yahoo",), min_request_interval=0).collect(instrument(), observed_at=time)
    assert result.status == "ok" and result.as_of == expected_asof


@pytest.mark.asyncio
async def test_yahoo_nonfuture_market_metadata_and_exact_units_are_required():
    for fault in ("currency", "symbol", "future_timestamp", "array_length", "zero_session_end"):
        payload = yahoo_payload()
        result = payload["chart"]["result"][0]
        if fault == "currency": result["meta"]["currency"] = "GBp"
        elif fault == "symbol": result["meta"]["symbol"] = "WRONG"
        elif fault == "future_timestamp": result["meta"]["regularMarketTime"] = NOW.timestamp() + 600
        elif fault == "zero_session_end": result["meta"]["currentTradingPeriod"]["regular"]["end"] = 0
        else: result["indicators"]["quote"][0]["close"].pop()
        connection = client(lambda request: httpx.Response(200, json=payload))
        async with connection.client:
            history = await DailyPriceCollector(connection, provider_order=("yahoo",), min_request_interval=0).collect(instrument(), observed_at=NOW)
        assert history.status == "unavailable" and not history.bars


@pytest.mark.asyncio
async def test_yahoo_verified_hong_kong_alias_retains_actual_currency_and_venue():
    payload = yahoo_payload(include_today=False)
    meta = payload["chart"]["result"][0]["meta"]
    meta.update(symbol="0700.HK", currency="HKD", exchangeName="HKG", fullExchangeName="HKSE", exchangeTimezoneName="Asia/Hong_Kong")
    connection = client(lambda request: httpx.Response(200, json=payload))
    async with connection.client:
        history = await DailyPriceCollector(connection, provider_order=("yahoo",), min_request_interval=0).collect(
            instrument(symbol="0700.HK", yahoo_symbol="0700.HK", currency="HKD", exchange="HKEX"), observed_at=NOW)
    assert history.status == "ok" and history.exchange == "HKSE" and history.declared_exchange == "HKEX"
    assert history.currency == "HKD" and history.provider_symbol == "0700.HK"


@pytest.mark.asyncio
async def test_yahoo_pence_are_never_silently_treated_as_pounds():
    payload = yahoo_payload(include_today=False)
    payload["chart"]["result"][0]["meta"].update(symbol="HSBA.L", currency="GBp", exchangeName="LSE")
    connection = client(lambda request: httpx.Response(200, json=payload))
    async with connection.client:
        history = await DailyPriceCollector(connection, provider_order=("yahoo",), min_request_interval=0).collect(
            instrument(symbol="HSBA.L", yahoo_symbol="HSBA.L", currency="GBP", exchange="LSE"), observed_at=NOW)
    assert history.status == "unavailable" and "currency" in history.error


def test_archive_hydration_rejects_duplicate_future_and_inconsistent_bars():
    bar = DailyBar(date(2026, 10, 2), 100, 102, 99, 101, 100000)
    history = PriceHistory("MSFT", (bar,), "nasdaq", "MSFT", "https://api.nasdaq.com/history", "USD", "NASDAQ", NOW)
    data = serializable(asdict(history))
    for fault in ("duplicate", "future", "ohlc"):
        altered = deepcopy(data)
        if fault == "duplicate": altered["bars"].append(altered["bars"][0])
        elif fault == "future": altered["bars"][0]["date"] = "2026-10-06"
        else: altered["bars"][0]["high"] = 98
        with pytest.raises(ValueError): price_history_from_dict(altered)


def rss(items):
    body = []
    for item in items:
        body.append("<item>" + "".join(f"<{name}>{escape(str(value))}</{name}>" for name, value in item.items()) + "</item>")
    return "<rss><channel>" + "".join(body) + "</channel></rss>"


def article(index=1, **changes):
    return dict(title=f"Central bank reviews interest rates {index}", link=f"https://www.bbc.com/news/{index}",
                description="<p>Inflation and energy supply remain uncertain.</p>", pubDate="Mon, 05 Oct 2026 12:00:00 GMT") | changes


FEED = WorldFeed("BBC", "https://feeds.bbci.co.uk/world.xml", ("bbc.com",))


@pytest.mark.asyncio
async def test_world_feed_preserves_reported_fact_separately_from_unknown_direction_and_theme_inference():
    connection = client(lambda request: httpx.Response(200, text=rss([article(description="<p>Inflation is high.</p><script>buy now</script>")])) )
    async with connection.client:
        batch = await WorldNewsCollector(connection, feeds=(FEED,)).collect(observed_at=NOW)
    event = batch.events[0]
    assert event.text == "Inflation is high." and "buy now" not in event.text
    assert event.themes == ("rates", "inflation") and "rates:interest rates" in event.matched_terms
    assert event.direction == "unknown" and event.fact_status == "publisher_reported_unverified"
    assert "inference" in event.interpretation and "publisher_selection_bias" in event.bias_flags
    assert event.content_hash and batch.coverage[0].status == "ok"
    assert world_event_from_dict(serializable(asdict(event))) == event


@pytest.mark.asyncio
async def test_world_feed_page_limit_dedup_and_future_offsite_rejections_are_visible():
    items = [article(), article(link="https://www.bbc.com/news/1?utm_source=tracker"),
             article(2, link="https://attacker.example/world"), article(3, pubDate="Tue, 06 Oct 2026 12:00:00 GMT")]
    items += [article(index) for index in range(4, 40)]
    connection = client(lambda request: httpx.Response(200, text=rss(items)))
    async with connection.client:
        batch = await WorldNewsCollector(connection, feeds=(FEED,)).collect(observed_at=NOW)
    assert len(batch.events) == 27 and len({event.event_id for event in batch.events}) == 27
    coverage = batch.coverage[0]
    assert coverage.status == "partial" and coverage.collected_count == 27
    assert any("Rejected 2" in item for item in coverage.limitations)
    assert any("duplicate" in item for item in coverage.limitations)
    assert any("first 30" in item for item in coverage.limitations)


@pytest.mark.asyncio
async def test_undated_news_is_preserved_but_cannot_be_a_fresh_catalyst():
    connection = client(lambda request: httpx.Response(200, text=rss([article(pubDate="bad date")])))
    async with connection.client:
        batch = await WorldNewsCollector(connection, feeds=(FEED,)).collect(observed_at=NOW)
    event = batch.events[0]
    assert event.published_at is None and "publication_date_missing_or_invalid" in event.bias_flags
    assert associate_events(("rates",), batch.events, now=NOW) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ("<html>Verify browser</html>", '<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel/></rss>'))
async def test_nonfeed_and_xml_entity_declarations_fail_with_coverage_receipt(body):
    connection = client(lambda request: httpx.Response(200, text=body))
    async with connection.client:
        batch = await WorldNewsCollector(connection, feeds=(FEED,)).collect(observed_at=NOW)
    assert not batch.events and batch.coverage[0].status == "error" and batch.coverage[0].error


@pytest.mark.asyncio
async def test_world_feed_interruption_retains_completed_evidence_and_reports_unattempted_sources():
    feeds = (FEED, WorldFeed("Second", "https://second.example/feed", ("second.example",)),
             WorldFeed("Third", "https://third.example/feed", ("third.example",)))
    class InterruptedClient:
        async def request_text(self, method, url):
            if url == FEED.url: return rss([article()])
            raise asyncio.CancelledError()
    collector = WorldNewsCollector(InterruptedClient(), feeds=feeds)
    with pytest.raises(asyncio.CancelledError):
        await collector.collect(observed_at=NOW)
    assert len(collector.partial_batch.events) == 1
    assert [item.status for item in collector.partial_batch.coverage] == ["ok", "error", "not_attempted"]


def test_macro_association_requires_exact_exposures_and_fresh_dated_evidence():
    assert match_macro_themes("Hardware improvements help spoiled food") == ((), ())
    themes, terms = match_macro_themes("Export controls restrict chips while shipping faces war risk")
    assert set(themes) == {"geopolitics", "export_controls", "supply_chain"}
    event = WorldEvent("one", "Export controls", "Shipping faces war risk", "https://www.bbc.com/news/one", "BBC",
                       NOW - timedelta(hours=1), NOW, themes, ("publisher_selection_bias",), matched_terms=terms)
    stale = replace(event, event_id="stale", published_at=NOW - timedelta(days=3))
    assert associate_events(("export_controls",), (event, event, stale), now=NOW) == (event,)
    assert associate_events(("inflation",), (event,), now=NOW) == ()
    with pytest.raises(ValueError): replace(event, direction="buy")
