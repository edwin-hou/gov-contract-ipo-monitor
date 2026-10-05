from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.health import HealthRegistry, create_health_app
from contract_ipo_monitor.research import ResearchStore, checkpoint_database, write_report
from contract_ipo_monitor.service import MonitorService, SAMSource
from contract_ipo_monitor.sources.discourse import DiscourseBatch, DiscourseEvidence, SourceCoverage
from contract_ipo_monitor.tracking import IPOEvidence

NOW = datetime(2026, 10, 5, 19, tzinfo=UTC)


def database(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    return db


def evidence(text="Anduril is promising", at=NOW):
    return DiscourseEvidence("one", "reddit", "https://www.reddit.com/r/investing/one", "reddit:investing", "Anduril", text, "post", ("Anduril",), at)


def test_reobserved_content_and_reverted_version_use_latest_seen_time(tmp_path):
    store = ResearchStore(database(tmp_path))
    store.initialize()
    first = evidence()
    assert store.record_batch(DiscourseBatch((first,), ())) == 1
    assert store.record_batch(DiscourseBatch((replace(first, text="Anduril is risky", retrieved_at=NOW + timedelta(days=1)),), ())) == 1
    assert store.record_batch(DiscourseBatch((replace(first, retrieved_at=NOW + timedelta(days=30)),), ())) == 0
    latest = store.records()
    assert len(latest) == 1
    assert latest[0].text == first.text
    assert latest[0].retrieved_at == NOW + timedelta(days=30)


def test_coverage_only_describes_the_latest_batch(tmp_path):
    store = ResearchStore(database(tmp_path))
    store.initialize()
    old = SourceCoverage("news", "https://example.org/old", NOW, "error")
    current = SourceCoverage("news", "https://example.org/current", NOW + timedelta(hours=1), "ok")
    store.record_batch(DiscourseBatch((), (old,)))
    store.record_batch(DiscourseBatch((), (current,)))
    assert [row["source_url"] for row in store.coverage()] == [current.source_url]


def test_sqlite_checkpoint_preserves_wal_and_report_receipt(tmp_path):
    db = database(tmp_path)
    store = ResearchStore(db)
    store.initialize()
    store.record_batch(DiscourseBatch((evidence(),), ()))
    report = {"completed_at": NOW.isoformat(), "status": "degraded", "health": {}, "sentiment": [], "coverage": []}
    store.save_run(report)
    checkpoint = tmp_path / "checkpoint" / "monitor.db"
    checkpoint_database(db, checkpoint)
    restored = ResearchStore(Database(checkpoint))
    assert restored.latest_run() == report
    assert restored.records()[0].text == evidence().text
    write_report(report, tmp_path / "report")
    assert json.loads((tmp_path / "report/latest.json").read_text()) == report
    assert "degraded" in (tmp_path / "report/latest.md").read_text()


def test_smtp_is_opt_in_and_bad_intervals_fail_configuration():
    settings = Settings(sec_user_agent="IPO person@example.org")
    assert settings.smtp_enabled is False
    assert settings.runtime_errors() == []
    assert any("SMTP_HOST" in error for error in settings.model_copy(update={"smtp_enabled": True}).runtime_errors())
    assert any("SEC_INTERVAL_SECONDS" in error for error in settings.model_copy(update={"sec_interval_seconds": 0}).runtime_errors())


def legacy_receipts(tmp_path, reports):
    db = database(tmp_path)
    store = ResearchStore(db)
    store.initialize()
    with db.connect() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version=3")
    for report in reports:
        store.save_run(report)
    return db


def test_legacy_sec_truncation_remains_degraded_after_healthy_poll(tmp_path):
    error = "RuntimeError: Partial SEC collection (2 failures): 8-K: feed page limit reached; older filings may be missing; EFFECT: feed page limit reached; older filings may be missing"
    previous = {"completed_at": NOW.isoformat(), "status": "degraded", "health": {"collectors": {"sec": {"ok": False, "error": error}}}}
    healthy = {"completed_at": (NOW + timedelta(minutes=1)).isoformat(), "status": "ok", "health": {"collectors": {"sec": {"ok": True, "error": None}}}}
    db = legacy_receipts(tmp_path, (previous, healthy))
    db.initialize()
    db.update_collector_state("sec", success_at=NOW + timedelta(minutes=2), error=None)
    db.initialize()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM collector_state WHERE name LIKE 'sec_feed_gap:%' ORDER BY name").fetchall()
        assert [row["name"] for row in rows] == ["sec_feed_gap:8-K", "sec_feed_gap:EFFECT"]
        assert all(row["last_error"] == error and row["updated_at"] == NOW.isoformat() for row in rows)
        assert all(json.loads(row["cursor"])["unresolved"] for row in rows)
    monitor = service(tmp_path)
    monitor.health.set_database_ready(True)
    monitor.health.mark_success("sec")
    monitor.health.mark_success("usaspending")
    report = monitor.create_report()
    assert report["health"]["ready"] is True
    assert report["status"] == "degraded"
    assert len(report["historic_coverage_gaps"]) == 2


def test_legacy_sec_cap_without_explicit_form_uses_unresolved_legacy_scope(tmp_path):
    error = "SEC feed page limit reached; older filings may be missing"
    db = legacy_receipts(tmp_path, ({"completed_at": NOW.isoformat(), "status": "degraded", "health": {"collectors": {"sec": {"error": error}}}},))
    db.initialize()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM collector_state WHERE name LIKE 'sec_feed_gap:%'").fetchone()
        assert row["name"] == "sec_feed_gap:legacy"
        assert row["last_error"] == error and row["updated_at"] == NOW.isoformat()


def test_legacy_generic_or_other_source_failures_do_not_create_sec_caps(tmp_path):
    reports = [
        {"completed_at": NOW.isoformat(), "status": "degraded", "health": {"collectors": {"sec": {"error": "TimeoutError: filing request timed out"}}}},
        {"completed_at": NOW.isoformat(), "status": "degraded", "health": {"collectors": {"discourse": {"error": "8-K: feed page limit reached; older filings may be missing"}, "sec": {"error": None}}}},
    ]
    db = legacy_receipts(tmp_path, reports)
    with db.connect() as conn:
        conn.execute("INSERT INTO monitor_runs(completed_at,status,report_json) VALUES(?,?,?)", (NOW.isoformat(), "degraded", "invalid JSON"))
    db.initialize()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM collector_state WHERE name LIKE 'sec_feed_gap:%'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_each_sam_run_reconciles_active_and_deleted_records():
    calls = []
    class Collector:
        async def collect(self, **kwargs):
            calls.append(kwargs["deleted"])
            return [kwargs["deleted"]]
    source = SAMSource(Collector())
    assert await source.collect(observed_at=NOW) == [False, True]
    assert await SAMSource(Collector()).collect(observed_at=NOW) == [False, True]
    assert calls == [False, True, False, True]


class EmptySource:
    async def collect(self, *, observed_at):
        return []


def service(tmp_path, sec=None, discourse=None):
    settings = Settings(database_path=tmp_path / "monitor.db", sec_user_agent="IPO person@example.org", watch_companies=("Anduril",))
    return MonitorService(settings=settings, db=database(tmp_path), health=HealthRegistry(),
                          sec_source=sec or EmptySource(), usaspending_source=EmptySource(), smtp_worker=None,
                          discourse_source=discourse, now=lambda: NOW)


@pytest.mark.asyncio
async def test_partial_sec_records_are_saved_without_a_success_receipt(tmp_path):
    class PartialSEC:
        ipo_events = []
        partial_signals = []
        processed_entries = []
        async def collect(self, *, observed_at):
            self.ipo_events = [IPOEvidence(event_id="0000000001-26-000001", issuer_name="Large Company", cik="1", source="sec",
                source_kind="regulatory", source_url="https://www.sec.gov/Archives/edgar/data/1/ipo.htm", filed_at=NOW,
                event_type="registration", form_type="S-1", is_ipo=True, offering_kind="ipo")]
            raise TimeoutError("later filing timed out")
    monitor = service(tmp_path, sec=PartialSEC())
    result = await monitor.run_once()
    assert result["ipo_events"] == 1
    assert monitor.tracker.summary()["confirmed_ipos"] == 1
    assert monitor.last_report["status"] == "degraded"
    assert monitor.last_report["health"]["collectors"]["sec"]["ok"] is False
    assert monitor.research.latest_run()["status"] == "degraded"


@pytest.mark.asyncio
async def test_discourse_gaps_preserve_successful_records_and_error_state(tmp_path):
    class PartialDiscourse:
        async def collect(self, companies):
            return DiscourseBatch((evidence(),), (SourceCoverage("youtube_captions", "https://www.youtube.com/watch?v=0BE2AAOlYWI", NOW, "unavailable"),))
    monitor = service(tmp_path, discourse=PartialDiscourse())
    result = await monitor.run_once()
    assert result["discourse_records"] == 1
    assert len(monitor.research.records()) == 1
    assert monitor.last_report["status"] == "degraded"
    with monitor.db.connect() as conn:
        row = conn.execute("SELECT last_success_at,last_error FROM collector_state WHERE name='discourse'").fetchone()
    assert row["last_success_at"] is None and "source gaps" in row["last_error"]


@pytest.mark.asyncio
async def test_report_only_supervisor_keeps_running_until_stop(tmp_path):
    import asyncio
    monitor = service(tmp_path)
    task = asyncio.create_task(monitor.run_forever(serve_health=False))
    await asyncio.sleep(0.03)
    assert not task.done()
    monitor.stop_event.set()
    await asyncio.wait_for(task, timeout=1)


def test_health_uses_component_interval_and_snapshots_are_isolated():
    health = HealthRegistry(max_age_seconds=60, collector_max_ages={"sam": 43200})
    health.mark_success("sam")
    health._collectors["sam"]["last_success_at"] = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    snapshot = health.snapshot()
    assert snapshot["collectors"]["sam"]["ok"] is True
    snapshot["collectors"]["sam"]["ok"] = False
    assert health.snapshot()["collectors"]["sam"]["ok"] is True


def test_ipo_and_research_views_work_with_empty_database(tmp_path):
    client = TestClient(create_health_app(HealthRegistry(), database(tmp_path)))
    assert client.get("/api/ipos").json()["ipos"] == []
    assert client.get("/api/research").json()["status"] == "not_run"
    assert "Online sentiment sample" in client.get("/dashboard").text


@pytest.mark.asyncio
async def test_discourse_timeout_keeps_completed_sources(tmp_path):
    class InterruptedDiscourse:
        partial_batch = DiscourseBatch((evidence(),), (SourceCoverage("reddit", "https://reddit.com", NOW, "error", error="interrupted"),))
        async def collect(self, companies):
            raise TimeoutError("outer deadline")
    monitor = service(tmp_path, discourse=InterruptedDiscourse())
    result = await monitor.run_once()
    assert result["discourse_records"] == 1
    assert len(monitor.research.records()) == 1
    assert monitor.last_report["coverage"][0]["status"] == "error"


def test_checkpoint_restore_rejects_arbitrary_files_and_corruption(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("restore_checkpoint", Path(__file__).parents[1] / "scripts/restore_checkpoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = tmp_path / "checkpoint.zip"
    target = tmp_path / "restored.db"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../monitor.db", b"malicious")
    with pytest.raises(ValueError):
        module.restore(archive, target)
    assert not target.exists()
    source_db = database(tmp_path / "source")
    checkpoint = tmp_path / "monitor.db"
    checkpoint_database(source_db, checkpoint)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(checkpoint, "monitor.db")
    assert module.restore(archive, target)
    assert Database(target).count("alerts") == 0


def test_historic_feed_gaps_remain_visible_after_current_collection_recovers(tmp_path):
    monitor = service(tmp_path)
    monitor.db.update_collector_state("sec_feed_gap:8-K", error="Older filings missing")
    monitor.health.mark_success("sec")
    monitor.health.mark_success("usaspending")
    monitor.health.set_database_ready(True)
    report = monitor.create_report()
    assert report["health"]["ready"] is True
    assert report["status"] == "degraded"
    assert report["historic_coverage_gaps"][0]["name"] == "sec_feed_gap:8-K"
