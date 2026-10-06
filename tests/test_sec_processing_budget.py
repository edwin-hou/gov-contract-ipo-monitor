"""Durable discovery precedes bounded document work; hard failures stay errors."""
import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest

from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.health import HealthRegistry
from contract_ipo_monitor.service import MonitorService, SECSource
from contract_ipo_monitor.sources.http import TransientHTTPError
from contract_ipo_monitor.sources.sec_catalog import SECFilingCatalog


NOW = datetime(2026, 10, 6, 15, tzinfo=UTC)
FORMS = ("S-1", "F-1", "RW")


class FakeClock:
    def __init__(self):
        self.seconds = 0

    def __call__(self):
        return self.seconds


def filing(number, form):
    accession = f"0000000042-26-{number:06d}"
    return {"accession": accession, "issuer_name": "Example Corp.", "cik": "42",
            "form_type": form, "filed_at": NOW, "filed_at_precision": "second",
            "source_url": f"https://www.sec.gov/Archives/edgar/data/42/{accession.replace('-', '')}/{accession}-index.htm"}


class QueuedCollector:
    """Real catalogue/database, controlled discovery and document durations."""
    max_pages = 1
    last_feed_truncated = False

    def __init__(self, db, clock, *, discovery_seconds=0, document_seconds=11):
        self.db = db
        self.catalog = SECFilingCatalog(db)
        self.clock = clock
        self.discovery_seconds = discovery_seconds
        self.document_seconds = document_seconds
        self.operations = []
        self.completed = set()
        self.fail_document = False
        self.block_document = None
        self.fail_form = None

    async def _text(self, url):
        path = urlparse(url).path.removesuffix("/index.json")
        return json.dumps({"directory": {"name": path, "item": []}})

    def requeue_invalid_catalog_receipts(self):
        return 0

    async def current_entries(self, form, *, count):
        self.operations.append(("discover", form))
        self.clock.seconds += self.discovery_seconds
        if form == self.fail_form:
            raise TransientHTTPError("HTTP 503 from configured current feed")
        entries = [filing(FORMS.index(form)+1, form)]
        if self.catalog is not None:
            self.catalog.capture(entries, observed_at=NOW)
        return entries

    def is_processed(self, accession):
        return accession in self.completed

    async def collect_entry(self, entry):
        self.operations.append(("document", entry["form_type"]))
        if entry["form_type"] == self.block_document:
            await asyncio.Event().wait()
        if self.fail_document:
            raise TransientHTTPError("HTTP 503 from filing document")
        self.clock.seconds += self.document_seconds
        return None, None

    def mark_processed(self, accession, *, observed_at):
        self.completed.add(accession)
        self.catalog.note_processed(accession, observed_at=observed_at)


class EmptyContracts:
    async def collect(self, *, observed_at):
        return []


def setup_source(tmp_path, *, budget=10, forms=FORMS, **kwargs):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    clock = FakeClock()
    collector = QueuedCollector(db, clock, **kwargs)
    source = SECSource(collector, forms, processing_budget_seconds=budget, clock=clock)
    return db, collector, source


def setup_monitor(db, source, *, timeout=1):
    settings = Settings(sec_user_agent="SourceAudit audit@example.invalid", database_path=db.path,
                        markets_enabled=False, discourse_enabled=False, enabled_sec_forms=source.forms,
                        source_timeout_seconds=timeout)
    return MonitorService(settings=settings, db=db, health=HealthRegistry(), sec_source=source,
                          usaspending_source=EmptyContracts(), smtp_worker=None, now=lambda: NOW)


@pytest.mark.asyncio
async def test_all_live_forms_discovered_before_slow_documents_and_checkpoint_preserves_later_queue(tmp_path):
    db, collector, source = setup_source(tmp_path)
    service = setup_monitor(db, source)
    await service._poll_sec()
    assert collector.operations == [("discover", form) for form in FORMS]+[("document", "S-1")]
    assert len(source.processed_entries) == 1 and source.parse_errors == []
    assert source.document_processing_budget_exhausted is True
    assert service.health.snapshot()["collectors"]["sec"]["ok"] is True
    coverage = collector.catalog.coverage()
    assert coverage["status"] == "pending" and coverage["pending_filings"] == 2
    report = service.create_report()
    assert report["status"] == "degraded"
    assert report["sec_collection"]["document_processing_budget_exhausted"] is True
    assert report["sec_collection"]["documents_processed_this_poll"] == 1
    with db.connect() as connection, sqlite3.connect(tmp_path / "checkpoint.db") as target:
        connection.backup(target)
    restored = SECFilingCatalog(Database(tmp_path / "checkpoint.db"))
    assert len(restored.pending_entries("F-1")) == len(restored.pending_entries("RW")) == 1
    # A following collection deduplicates the completed accession and drains
    # the next durable form rather than repeating the slow first document.
    collector.operations.clear()
    await service._poll_sec()
    assert collector.operations == [("discover", form) for form in FORMS]+[("document", "F-1")]
    assert collector.catalog.coverage()["pending_filings"] == 1


@pytest.mark.asyncio
async def test_discovery_time_consumes_document_budget_but_later_forms_are_still_captured(tmp_path):
    _, collector, source = setup_source(tmp_path, discovery_seconds=4)
    assert await source.collect(observed_at=NOW) == []
    assert collector.operations == [("discover", form) for form in FORMS]
    assert source.document_processing_budget_exhausted is True
    assert collector.catalog.coverage()["pending_filings"] == 3
    assert source.processed_entries == source.parse_errors == []


@pytest.mark.asyncio
async def test_real_document_failure_is_not_relabelled_as_normal_pending_work(tmp_path):
    db, collector, source = setup_source(tmp_path)
    collector.fail_document = True
    service = setup_monitor(db, source)
    with pytest.raises(RuntimeError, match="HTTP 503 from filing document"):
        await service._poll_sec()
    assert len(source.parse_errors) == 3 and source.processed_entries == []
    assert source.document_processing_budget_exhausted is False
    assert collector.catalog.coverage()["pending_filings"] == 3
    with db.connect() as connection:
        assert connection.execute("SELECT last_success_at FROM collector_state WHERE name='sec'").fetchone() is None


@pytest.mark.asyncio
async def test_inflight_hard_deadline_preserves_completed_and_later_form_discovery(tmp_path):
    db, collector, source = setup_source(tmp_path, budget=10, document_seconds=0)
    collector.block_document = "F-1"
    service = setup_monitor(db, source)
    # Settings use whole seconds in production; this isolated caller deadline
    # exercises cancellation without a long or external HTTP request.
    service.settings.source_timeout_seconds = .03
    with pytest.raises(TimeoutError):
        await service._poll_sec()
    assert collector.operations == [("discover", form) for form in FORMS]+[("document", "S-1"), ("document", "F-1")]
    assert source.document_processing_budget_exhausted is False
    assert len(source.processed_entries) == 1
    assert collector.completed == {filing(1, "S-1")["accession"]}
    assert collector.catalog.coverage()["pending_filings"] == 2


@pytest.mark.asyncio
async def test_document_budget_without_durable_queue_retains_incomplete_error(tmp_path):
    _, collector, source = setup_source(tmp_path)
    collector.catalog = None
    await source.collect(observed_at=NOW)
    assert collector.operations == [("discover", form) for form in FORMS]+[("document", "S-1")]
    assert len(source.processed_entries) == 1
    assert source.document_processing_budget_exhausted is True
    assert any("without a durable pending queue" in value for value in source.parse_errors)


@pytest.mark.asyncio
async def test_feed_failure_remains_error_even_when_document_budget_is_used(tmp_path):
    db, collector, source = setup_source(tmp_path, discovery_seconds=4)
    collector.fail_form = "F-1"
    service = setup_monitor(db, source)
    with pytest.raises(RuntimeError, match="HTTP 503 from configured current feed"):
        await service._poll_sec()
    assert collector.operations == [("discover", form) for form in FORMS]
    assert source.document_processing_budget_exhausted is True
    assert collector.catalog.coverage()["pending_filings"] == 2


@pytest.mark.parametrize("budget", [True, 0, -1, 3601, float("inf"), float("nan")])
def test_document_processing_budget_rejects_invalid_or_unbounded_values(tmp_path, budget):
    with pytest.raises(ValueError, match="bounded positive"):
        setup_source(tmp_path, budget=budget)
