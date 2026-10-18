"""Continuous assembly must not turn minute clocks into unbounded history."""
import asyncio
from datetime import timedelta

import pytest

from contract_ipo_monitor.config import Settings
from contract_ipo_monitor import research
from test_research_integration import NOW, service


def prepared(tmp_path, monkeypatch, *, interval=None):
    monitor = service(tmp_path)
    if interval is not None:
        monitor.settings = Settings(sec_user_agent="Monitor test@example.org",
                                    database_path=tmp_path / "monitor.db",
                                    report_interval_seconds=interval)
    clock = [NOW]
    monitor.now = lambda: clock[0]
    published = []
    monkeypatch.setattr(research, "write_report", lambda report, directory: published.append(report))

    def assemble(counts=None):
        # Re-evaluating unchanged source data renews assembly clocks and quote
        # ages. Those minute evaluations previously grew both history tables.
        return {"completed_at": clock[0].isoformat(), "status": "degraded", "counts": counts or {},
                "trade_ideas": [{"symbol": "TEST", "action": "wait", "generated_at": clock[0].isoformat(),
                                 "current_quote": {"observed_at": NOW.isoformat(),
                                                   "observation_age_seconds": (clock[0]-NOW).total_seconds()}}]}

    monitor.create_report = assemble
    return monitor, clock, published


async def simulated_hour(monitor, clock, monkeypatch):
    timeouts = []

    async def advance(awaitable, *, timeout):
        awaitable.close()
        timeouts.append(timeout)
        if clock[0] + timedelta(seconds=timeout) > NOW + timedelta(hours=1):
            monitor.stop_event.set()
            return True
        clock[0] += timedelta(seconds=timeout)
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", advance)
    await monitor._report_loop()
    return timeouts


def history_sizes(monitor):
    with monitor.db.connect() as connection:
        return tuple(connection.execute("SELECT (SELECT COUNT(*) FROM monitor_runs),"
                                        "(SELECT COUNT(*) FROM trade_idea_versions)").fetchone())


@pytest.mark.asyncio
async def test_default_hour_stores_prompt_initial_report_without_minute_history_growth(tmp_path, monkeypatch):
    monitor, clock, published = prepared(tmp_path, monkeypatch)
    timeouts = await simulated_hour(monitor, clock, monkeypatch)
    assert history_sizes(monitor) == (1, 1)
    assert timeouts == [60, 3600]
    assert len(published) == 1
    assert monitor.research.latest_run()["completed_at"] == (NOW+timedelta(minutes=1)).isoformat()


@pytest.mark.asyncio
async def test_configured_cadence_saves_every_evaluated_report_without_deduplication(tmp_path, monkeypatch):
    monitor, clock, published = prepared(tmp_path, monkeypatch, interval=1200)
    timeouts = await simulated_hour(monitor, clock, monkeypatch)
    assert history_sizes(monitor) == (3, 3)
    assert timeouts == [60, 1200, 1200, 1200]
    assert [item["completed_at"] for item in published] == [
        (NOW+timedelta(minutes=minute)).isoformat() for minute in (1, 21, 41)]


@pytest.mark.asyncio
async def test_cancellation_during_report_wait_creates_no_synthetic_receipt(tmp_path, monkeypatch):
    monitor, _, published = prepared(tmp_path, monkeypatch)
    task = asyncio.create_task(monitor._report_loop())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert history_sizes(monitor) == (0, 0) and published == []


@pytest.mark.asyncio
async def test_run_once_preserves_each_real_collection_inside_continuous_interval(tmp_path, monkeypatch):
    monitor, clock, _ = prepared(tmp_path, monkeypatch)
    await monitor.run_once()
    clock[0] += timedelta(seconds=30)
    await monitor.run_once()
    assert history_sizes(monitor) == (2, 2)
    with monitor.db.connect() as connection:
        assert [row[0] for row in connection.execute("SELECT completed_at FROM monitor_runs ORDER BY id")] == [
            NOW.isoformat(), (NOW+timedelta(seconds=30)).isoformat()]


def test_report_interval_environment_and_runtime_validation(monkeypatch):
    monkeypatch.setenv("REPORT_INTERVAL_SECONDS", "1200")
    assert Settings.from_env(None).report_interval_seconds == 1200
    assert any("REPORT_INTERVAL_SECONDS" in error for error in
               Settings(sec_user_agent="Monitor test@example.org", report_interval_seconds=0).runtime_errors())
