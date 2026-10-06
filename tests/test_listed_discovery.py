import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.fundamentals import normalize_companyfacts
from contract_ipo_monitor.market_monitor import MarketMonitor
from contract_ipo_monitor.research import checkpoint_database, report_markdown
from contract_ipo_monitor.sources.http import ResilientClient
from contract_ipo_monitor.sources.listed_discovery import ListedDiscoveryBatch, ListedDiscoveryCollector, ticker_directory

NOW = datetime(2026, 10, 6, 0, 30, tzinfo=UTC)


def entry(cik=123, *, accession=None, filed=NOW-timedelta(hours=5)):
    accession = accession or f"{cik:010d}-26-000001"
    return {"cik": str(cik).zfill(10), "accession": accession, "issuer_name": f"Issuer {cik}",
            "filed_at": filed, "form_type": "10-Q", "source_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.htm"}


def facts(cik=123, *, growth=.5, profit=30, annual=False, currency="USD", wrong_cik=False):
    accession = f"{cik:010d}-26-000001"
    def row(start, end, value):
        return {"start": start, "end": end, "val": value, "filed": "2026-08-05", "accn": accession, "form": "10-K" if annual else "10-Q"}
    start, prior = ("2025-07-01", "2024-07-01") if annual else ("2026-04-01", "2025-04-01")
    return {"cik": 999 if wrong_cik else cik, "entityName": f"Issuer {cik}", "facts": {"us-gaap": {
        "Revenues": {"units": {currency: [row(start, "2026-06-30", 100*(1+growth)), row(prior, "2025-06-30", 100)]}},
        "NetIncomeLoss": {"units": {currency: [row(start, "2026-06-30", profit)]}}}}}


def discovery_handler(*, values=None, directory_fault=False, failed_cik=None):
    def handle(request):
        if request.url.path.endswith("company_tickers_exchange.json"):
            payload = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[n, f"Issuer {n}", f"T{n}", "NYSE"] for n in range(123, 133)]}
            if directory_fault: payload["fields"] = ["cik", "ticker"]
            return httpx.Response(200, json=payload)
        cik = int(request.url.path.rsplit("CIK", 1)[1].removesuffix(".json"))
        return httpx.Response(503 if cik == failed_cik else 200, json=values.get(cik, facts(cik)) if values else facts(cik))
    return handle


async def collector(handler, entries=None, **kwargs):
    connection = ResilientClient(transport=httpx.MockTransport(handler), max_attempts=1, max_response_bytes=8_000_000)
    source = ListedDiscoveryCollector(connection, request_interval=.15, **kwargs)
    async def current(form, **kw):
        return list(entries or [entry()]) if form == "10-Q" else []
    source.sec.current_entries = current
    return source, connection


@pytest.mark.asyncio
async def test_primary_quarter_qualified_is_review_only_and_exact_context_restores():
    source, connection = await collector(discovery_handler())
    try: batch = await source.collect(observed_at=NOW)
    finally: await connection.aclose()
    value = batch.candidates[0]
    assert value["status"] == "qualified_review" and value["financial_eligible"]
    assert value["trading_currency"] is None and value["reporting_currency"] == "USD"
    assert value["financials"]["reported_at"] == "2026-08-05"
    rebuilt = normalize_companyfacts(value["companyfacts_context_receipt"], cik=value["cik"], symbol=value["symbol"], now=NOW)
    assert rebuilt.to_dict() == value["financials"]
    assert len(value["facts_sha256"]) == 64 and "WAIT" in value["reasons"][-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,state", [(facts(growth=.09), "rejected"), (facts(profit=0), "rejected"),
    (facts(annual=True), "rejected"), ({"cik":123,"facts":{}}, "wait"), (facts(currency="EUR"), "qualified_review")])
async def test_screens_do_not_invent_growth_profit_quarter_or_trading_currency(payload, state):
    source, connection = await collector(discovery_handler(values={123:payload}))
    try: value = (await source.collect(observed_at=NOW)).candidates[0]
    finally: await connection.aclose()
    assert value["status"] == state and value["trading_currency"] is None


@pytest.mark.asyncio
async def test_bounds_dedup_processed_curated_and_old_filings():
    records = [entry(n) for n in range(123,133)] + [entry(123)]
    source, connection = await collector(discovery_handler(), records, max_new_ciks=2)
    try:
        batch = await source.collect(observed_at=NOW, excluded_ciks=("123",), processed_accessions=(entry(124)["accession"],),
                                     latest_filed_at={"0000000125": NOW.isoformat()})
    finally: await connection.aclose()
    assert [item["cik"] for item in batch.candidates] == ["0000000126", "0000000127"]
    assert sum(item.source.startswith("listed_discovery:companyfacts:") for item in batch.coverage) == 2


@pytest.mark.asyncio
async def test_partial_failure_retains_finished_candidate_and_retries_failed_issuer():
    source, connection = await collector(discovery_handler(failed_cik=124), [entry(123),entry(124)])
    try: batch = await source.collect(observed_at=NOW)
    finally: await connection.aclose()
    assert len(batch.candidates) == 1 and batch.candidates[0]["cik"] == "0000000123"
    assert batch == source.partial_batch
    assert any(item.status == "error" and "0000000124" in item.source for item in batch.coverage)


@pytest.mark.asyncio
async def test_identity_mismatch_and_bad_directory_fail_closed():
    for handler in (discovery_handler(values={123:facts(wrong_cik=True)}), discovery_handler(directory_fault=True)):
        source, connection = await collector(handler)
        try: batch = await source.collect(observed_at=NOW)
        finally: await connection.aclose()
        assert batch.candidates == () and any(item.status == "error" for item in batch.coverage)
        assert batch == source.partial_batch


@pytest.mark.asyncio
async def test_hour_budget_portable_checkpoint_preserves_financial_dates_and_cap(tmp_path):
    source, connection = await collector(discovery_handler(), [entry(n) for n in range(123,128)])
    db = Database(tmp_path/"monitor.db"); db.initialize()
    settings = Settings(listed_discovery_max_candidates=2)
    monitor = MarketMonitor(db,settings,now=lambda:NOW,discovery_source=source,instruments=(),benchmarks=())
    try:
        assert await monitor.collect_discovery() == 5
        assert await monitor.collect_discovery() == 0
        assert len(monitor.discovery_candidates()) == 2
        checkpoint_database(db,tmp_path/"restored.db")
        restored = MarketMonitor(Database(tmp_path/"restored.db"),settings,now=lambda:NOW+timedelta(days=130),instruments=(),benchmarks=())
        rows = restored.discovery_candidates()
        assert len(rows) == 2 and all(row["status"] == "wait" for row in rows)
        assert all(row["financials"]["reported_at"] == "2026-08-05" for row in rows)
        assert all(row["companyfacts_context_receipt"] for row in rows)
        assert len(restored.store.latest("listed_discovery_seen")) == 5
    finally: await connection.aclose()


@pytest.mark.asyncio
async def test_cancelled_source_keeps_finished_evidence_and_gap():
    source, connection = await collector(discovery_handler())
    async def cancelled(form, **kwargs): raise asyncio.CancelledError()
    source.sec.current_entries = cancelled
    try:
        with pytest.raises(asyncio.CancelledError): await source.collect(observed_at=NOW)
    finally: await connection.aclose()
    assert source.partial_batch.coverage[-1].status == "error"


@pytest.mark.asyncio
async def test_waiting_xbrl_lag_recovers_next_hour_without_renewing_discovery_date(tmp_path):
    values = {123:{"cik":123,"facts":{}}}
    source, connection = await collector(discovery_handler(values=values))
    db = Database(tmp_path/"monitor.db"); db.initialize()
    clock = [NOW]
    monitor = MarketMonitor(db,Settings(),now=lambda:clock[0],discovery_source=source,instruments=(),benchmarks=())
    try:
        await monitor.collect_discovery()
        assert monitor.discovery_candidates()[0]["status"] == "wait"
        values[123] = facts()
        clock[0] += timedelta(hours=1)
        await monitor.collect_discovery()
        value = monitor.discovery_candidates()[0]
        assert value["status"] == "qualified_review" and value["discovered_at"] == NOW.isoformat()
        assert value["financials"]["reported_at"] == "2026-08-05"
    finally: await connection.aclose()


def test_discovery_report_escapes_findings_and_strategy_is_reviewable():
    report = {"completed_at":NOW.isoformat(),"status":"degraded","listed_discovery":[{"name":"<script>alert(1)</script>","status":"qualified_review","reasons":["WAIT"],"financials":{},"listing_source_url":"javascript:alert(1)"}],
        "trade_ideas":[{"symbol":"TEST","action":"conditional_buy","conditions":["Verify quote"],"currency":"USD","strategy":{"maximum_entry":101,"setup_valid_through":"2026-10-12","risk_budget":{"planned_risk_per_share":5,"budget_formula":"risk_budget = equity * loss_fraction","quantity_formula":"floor(budget / risk)"}}}]}
    text = report_markdown(report)
    assert "<script>" not in text and "javascript:" not in text and "qualified_review" in text
    assert "101.00" in text and "2026-10-12" in text and "15 sessions from a verified actual fill" in text and "floor(budget / risk)" in text


def test_settings_bound_and_env(monkeypatch):
    monkeypatch.setenv("LISTED_DISCOVERY_ENABLED","false")
    assert Settings.from_env(None).listed_discovery_enabled is False
    assert any("discovery bounds" in item for item in Settings(listed_discovery_max_new_ciks=6).runtime_errors())
