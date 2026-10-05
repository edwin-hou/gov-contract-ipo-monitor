from dataclasses import replace
from datetime import UTC, datetime, timedelta
import gzip
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.health import HealthRegistry, create_health_app
from contract_ipo_monitor.market_monitor import MarketMonitor
from contract_ipo_monitor.market_research import MarketResearchStore
from contract_ipo_monitor.research import checkpoint_database
from contract_ipo_monitor.service import MonitorService
from contract_ipo_monitor.sources.discourse import SourceCoverage
from contract_ipo_monitor.worldnews import WorldEvent, WorldNewsBatch
from test_trade_engine import financials, history, instrument

NOW = datetime(2026, 10, 5, 15, tzinfo=UTC)


def database(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    return db


def events():
    return tuple(WorldEvent(str(index), "Central bank discusses interest rates", "Interest rates remain uncertain",
                           f"https://example.org/{index}", publisher, NOW - timedelta(hours=1), NOW,
                           ("rates",), ("publisher_selection_bias",)) for index, publisher in enumerate(("BBC", "The Guardian")))


def world_batch(at=NOW):
    return WorldNewsBatch(events(), tuple(SourceCoverage("world_news:" + publisher, "https://example.org/feed",
                         at, "ok", 1) for publisher in ("BBC", "The Guardian")))


def monitor(db, **kwargs):
    return MarketMonitor(db, Settings(sec_user_agent="Research person@example.org"), now=lambda: NOW,
                         instruments=(instrument(),), benchmarks=(replace(instrument(), symbol="ACWI", cik=None),), **kwargs)


def seed(monitor):
    monitor.store.record("financials", "TEST", financials(), observed_at=NOW)
    monitor.store.record("prices", "TEST", history(), observed_at=NOW)
    monitor.store.record("prices", "ACWI", history(symbol="ACWI", step=.1), observed_at=NOW)
    monitor.save_world_batch(world_batch())


def test_world_coverage_contract_and_restored_evidence_produce_real_levels(tmp_path):
    subject = monitor(database(tmp_path))
    seed(subject)
    report = subject.report([])
    assert report["world_coverage_ready"] is True
    assert len(report["world_news"]) == 2
    assert report["trade_ideas"][0]["action"] == "conditional_buy"
    assert report["trade_ideas"][0]["entry"] > report["trade_ideas"][0]["invalidation"]
    subject.store.record_coverage("world", [replace(item, observed_at=NOW-timedelta(days=3)) for item in world_batch().coverage], observed_at=NOW)
    assert subject.report([])["trade_ideas"][0]["action"] == "wait"


def test_hash_versions_reobservation_timezone_and_corrupt_archive(tmp_path):
    store = MarketResearchStore(database(tmp_path))
    store.initialize()
    first = history()
    assert store.record("prices", "TEST", first, observed_at=NOW)
    assert not store.record("prices", "TEST", replace(first, observed_at=NOW+timedelta(hours=1)), observed_at=NOW+timedelta(hours=1))
    changed = replace(first, source="second")
    assert store.record("prices", "TEST", changed, observed_at=NOW+timedelta(hours=2))
    assert not store.record("prices", "TEST", first, observed_at=NOW+timedelta(hours=3))
    assert store.latest("prices")["TEST"]["source"] == first.source
    with store.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_evidence").fetchone()[0] == 2
        conn.execute("UPDATE market_evidence SET gzip_json=? WHERE last_seen_at=?", (gzip.compress(b'{}'), (NOW+timedelta(hours=3)).isoformat()))
    with pytest.raises(ValueError, match="hash mismatch"):
        store.latest("prices")


def test_newer_financial_result_survives_restart_and_checkpoint(tmp_path):
    db = database(tmp_path)
    subject = monitor(db)
    seed(subject)
    newer = replace(financials(), reported_at=financials().reported_at+timedelta(days=1), revenue=160_000_000)
    subject.store.record("financials", "TEST", newer, observed_at=NOW+timedelta(seconds=1))
    checkpoint = tmp_path / "backup" / "monitor.db"
    checkpoint_database(db, checkpoint)
    restored = monitor(Database(checkpoint))
    assert restored.report([])["listed_companies"][0]["financials"]["revenue"] == newer.revenue
    assert restored.report([])["trade_ideas"][0]["action"] == "conditional_buy"


@pytest.mark.asyncio
async def test_refresh_keeps_reviewed_quarter_over_annual_with_same_end(tmp_path, monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr("contract_ipo_monitor.market_monitor.asyncio.sleep", no_wait)
    annual = replace(financials(), period_type="annual", period_start=None, prior_period_start=None, reported_at=financials().reported_at+timedelta(days=1), revenue=900_000_000)
    class Facts:
        async def latest(self, *args, **kwargs):
            return annual
    subject = monitor(database(tmp_path), fundamentals_source=Facts())
    subject.instruments = (replace(instrument(), cik="1"),)
    seed(subject)
    assert (await subject.collect_prices())["financial_updates"] == 0
    assert subject.store.latest("financials")["TEST"]["revenue"] == financials().revenue
    # Seven-day cache prevents another request; the financial result's report
    # date is never changed to a collection date.
    assert not subject._refresh_due("TEST", NOW+timedelta(days=1))


@pytest.mark.asyncio
async def test_partial_price_deadline_preserves_completed_instruments_and_gaps(tmp_path):
    import asyncio
    class Prices:
        async def collect(self, item, *, observed_at):
            if item.symbol == "ACWI":
                await asyncio.Event().wait()
            return history()
    subject = monitor(database(tmp_path), price_source=Prices())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subject.collect_prices(), timeout=.05)
    assert subject.store.latest("prices")["TEST"]["bars"]
    assert subject.last_counts["price_histories"] == 1
    assert any(item["symbol"] == "ACWI" and item["status"] == "not_attempted" for item in subject.store.coverage("markets"))


@pytest.mark.asyncio
async def test_run_receipt_and_readonly_trade_api_match_collected_evidence(tmp_path):
    class Empty:
        async def collect(self, **kwargs):
            return []
    class Prices:
        async def collect(self, item, **kwargs):
            return history(symbol=item.symbol, step=.1 if item.symbol == "ACWI" else .4)
    class World:
        async def collect(self, **kwargs):
            return world_batch()
    db = database(tmp_path)
    service = MonitorService(settings=Settings(sec_user_agent="Research person@example.org"), db=db,
                             health=HealthRegistry(), sec_source=Empty(), usaspending_source=Empty(), smtp_worker=None,
                             price_source=Prices(), world_source=World(), instruments=(instrument(),),
                             benchmarks=(replace(instrument(), symbol="ACWI"),), now=lambda: NOW)
    service.markets.store.record("financials", "TEST", financials(), observed_at=NOW)
    counts = await service.run_once()
    assert counts["price_histories"] == 2 and counts["trade_ideas"] == 1
    report = service.research.latest_run()
    assert report["trade_ideas"][0]["action"] == "conditional_buy"
    assert report["world_coverage_ready"] is True
    client = TestClient(create_health_app(service.health, db))
    assert client.get("/api/trades").json()["trade_ideas"] == report["trade_ideas"]
    assert client.get("/api/companies").json()["companies"] == report["listed_companies"]
    assert len(client.get("/api/world-news").json()["events"]) == 2
    assert service.markets.store.save_ideas(report["trade_ideas"], observed_at=NOW+timedelta(minutes=1)) == 0


def test_unreviewed_symbols_and_invalid_windows_do_not_silently_pick_an_exchange():
    settings = Settings(sec_user_agent="Research person@example.org", watch_symbols=("INVENTED",))
    assert any("unreviewed" in item for item in settings.runtime_errors())
    assert any("PRICE_MAX_AGE" in item for item in settings.model_copy(update={"price_max_age_business_days": 20}).runtime_errors())
