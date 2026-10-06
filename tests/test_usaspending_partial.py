"""An incomplete scan must retain observed records without claiming coverage."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from contract_ipo_monitor.sources.http import TransientHTTPError
from contract_ipo_monitor.sources.usaspending import (
    IncompleteUSAspendingCollection,
    USAspendingCollector,
)
from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.health import HealthRegistry
from contract_ipo_monitor.service import MonitorService


NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


def award(identity):
    return {
        "Award ID": f"AWARD-{identity}",
        "generated_internal_id": f"RECORD-{identity}",
        "Recipient Name": "Example Manufacturing Inc.",
        "Recipient UEI": "ABC123456789",
        "Award Amount": 10000,
        "Start Date": "2026-10-05",
        "Awarding Agency": "Example Department",
    }


def response(rows, *, more=False):
    return {"results": rows, "page_metadata": {"hasNext": more, "next": 2 if more else None}}


class Client:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    async def request_json(self, *_args, **_kwargs):
        value = next(self.outcomes)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_page_budget_retains_all_completed_records_and_incomplete_reason():
    collector = USAspendingCollector(Client([response([award(1), award(2)], more=True)]))
    with pytest.raises(IncompleteUSAspendingCollection, match="refusing to truncate silently") as raised:
        await collector.collect(observed_at=NOW, max_pages=1)
    assert [record.source_record_id for record in raised.value.records] == ["RECORD-1", "RECORD-2"]
    assert raised.value.page == 1
    assert raised.value.reason == str(raised.value)
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert len(collector.partial_records) == 2


@pytest.mark.asyncio
async def test_later_http_failure_retains_first_page_and_original_cause():
    failure = TransientHTTPError("HTTP 503: Service Unavailable")
    collector = USAspendingCollector(Client([response([award(1)], more=True), failure]))
    with pytest.raises(IncompleteUSAspendingCollection, match="HTTP 503") as raised:
        await collector.collect(observed_at=NOW)
    assert [record.award_id for record in raised.value.records] == ["AWARD-1"]
    assert raised.value.page == 2
    assert raised.value.__cause__ is failure


@pytest.mark.asyncio
async def test_invalid_later_row_retains_only_successfully_validated_evidence():
    invalid = award(2)
    del invalid["Start Date"]
    collector = USAspendingCollector(Client([response([award(1), invalid])]))
    with pytest.raises(IncompleteUSAspendingCollection, match="missing its source award date") as raised:
        await collector.collect(observed_at=NOW)
    assert [record.source_record_id for record in raised.value.records] == ["RECORD-1"]
    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_deadline_cancellation_retains_records_without_redefining_cancellation():
    class SlowClient:
        async def request_json(self, *_args, **kwargs):
            if kwargs["json"]["page"] == 1:
                return response([award(1)], more=True)
            await asyncio.Event().wait()

    collector = USAspendingCollector(SlowClient())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(collector.collect(observed_at=NOW), timeout=.05)
    assert [record.award_id for record in collector.partial_records] == ["AWARD-1"]


@pytest.mark.asyncio
async def test_next_attempt_resets_partial_records_without_mutating_previous_exception():
    collector = USAspendingCollector(Client([response([award(1)], more=True), response([])]))
    with pytest.raises(IncompleteUSAspendingCollection) as raised:
        await collector.collect(observed_at=NOW, max_pages=1)
    assert await collector.collect(observed_at=NOW) == []
    assert collector.partial_records == []
    assert [record.award_id for record in raised.value.records] == ["AWARD-1"]


@pytest.mark.asyncio
async def test_first_page_failure_keeps_original_error_type_and_clears_prior_partial():
    failure = TransientHTTPError("HTTP 503: Service Unavailable")
    collector = USAspendingCollector(Client([response([award(1)]), failure]))
    assert len(await collector.collect(observed_at=NOW)) == 1
    with pytest.raises(TransientHTTPError) as raised:
        await collector.collect(observed_at=NOW)
    assert raised.value is failure
    assert collector.partial_records == []


@pytest.mark.asyncio
async def test_duplicate_rows_do_not_create_duplicate_partial_observations():
    collector = USAspendingCollector(Client([response([award(1), award(1)], more=True)]))
    with pytest.raises(IncompleteUSAspendingCollection) as raised:
        await collector.collect(observed_at=NOW, max_pages=1)
    assert len(raised.value.records) == 1


def service(tmp_path, source, *, timeout=600):
    class EmptySEC:
        async def collect(self, **_kwargs):
            return []

    db = Database(tmp_path / "monitor.db")
    db.initialize()
    settings = Settings(
        database_path=tmp_path / "monitor.db",
        evidence_archive_path=tmp_path / "evidence",
        sec_user_agent="Test monitor test@example.invalid",
        markets_enabled=False,
        discourse_enabled=False,
    ).model_copy(update={"source_timeout_seconds": timeout})
    return MonitorService(
        settings=settings, db=db, health=HealthRegistry(), sec_source=EmptySEC(),
        usaspending_source=source, smtp_worker=None, now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_run_retains_partial_awards_and_preserves_previous_success_watermark(tmp_path):
    failure = TransientHTTPError("HTTP 503: Service Unavailable")
    source = USAspendingCollector(Client([response([award(1)], more=True), failure]))
    subject = service(tmp_path, source)
    previous = NOW - timedelta(days=2)
    subject.db.update_collector_state("usaspending", cursor="previous-complete-window", success_at=previous)
    counts = await subject.run_once()
    assert counts["contracts"] == 1
    assert subject.db.count("contract_evidence") == 1
    assert subject.last_report["history"]["contract_records"] == 1
    assert subject.last_report["health"]["collectors"]["usaspending"]["ok"] is False
    assert subject.last_report["health"]["ready"] is False
    with subject.db.connect() as conn:
        state = conn.execute("SELECT * FROM collector_state WHERE name='usaspending'").fetchone()
    assert state["last_success_at"] == previous.isoformat()
    assert state["cursor"] == "previous-complete-window"
    assert "HTTP 503" in state["last_error"]


@pytest.mark.asyncio
async def test_run_deadline_retains_completed_awards_but_never_marks_scan_success(tmp_path):
    class SlowClient:
        async def request_json(self, *_args, **kwargs):
            if kwargs["json"]["page"] == 1:
                return response([award(1)], more=True)
            await asyncio.Event().wait()

    subject = service(tmp_path, USAspendingCollector(SlowClient()), timeout=.05)
    counts = await subject.run_once()
    assert counts["contracts"] == 1
    assert subject.db.count("contract_evidence") == 1
    with subject.db.connect() as conn:
        state = conn.execute("SELECT * FROM collector_state WHERE name='usaspending'").fetchone()
    assert state["last_success_at"] is None
    assert state["cursor"] is None
    assert "TimeoutError" in state["last_error"]


@pytest.mark.asyncio
async def test_repeated_partial_run_versions_do_not_duplicate_retained_awards(tmp_path):
    failure = TransientHTTPError("HTTP 503: Service Unavailable")
    source = USAspendingCollector(Client([
        response([award(1)], more=True), failure,
        response([award(1)], more=True), failure,
    ]))
    subject = service(tmp_path, source)
    await subject.run_once()
    await subject.run_once()
    assert subject.db.count("contract_evidence") == 1
    assert subject.db.count("source_records") == 1
    assert subject.last_report["history"]["contract_records"] == 1
