"""Exercise real SEC catalogue/collector/service boundaries without network access."""
import gzip
import json
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

import pytest

from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.health import HealthRegistry
from contract_ipo_monitor.service import MonitorService, SECSource
from contract_ipo_monitor.sources.http import TransientHTTPError
from contract_ipo_monitor.sources.sec import SECCollector


NOW = datetime(2026, 10, 6, 15, tzinfo=UTC)
DAY = date(2026, 10, 5)


def filing(number, form="8-K", day=DAY):
    accession = f"0000000042-26-{number:06d}"
    return {"accession": accession, "cik": "42", "issuer_name": "Example Corp.", "form_type": form,
            "day": day, "source_url": f"https://www.sec.gov/Archives/edgar/data/42/{accession.replace('-', '')}/{accession}-index.htm"}


class OfficialSources:
    def __init__(self, *, indexed=(), current=()):
        self.indexed = list(indexed)
        self.current = list(current)
        self.calls = []
        self.fail_offset = None
        self.records = {row["accession"]: row for row in (*self.indexed, *self.current)}

    async def text(self, url, **kwargs):
        parsed = urlparse(url)
        assert parsed.scheme == "https" and parsed.hostname == "www.sec.gov"
        self.calls.append((url, kwargs))
        if url.endswith("/index.json"):
            path = parsed.path.removesuffix("/index.json")
            year, quarter = int(path.split("/")[-2]), int(path[-1])
            days = sorted({row["day"] for row in self.indexed
                           if row["day"].year == year and (row["day"].month - 1) // 3 + 1 == quarter})
            return json.dumps({"directory": {"name": path, "item": [
                {"name": f"master.{day:%Y%m%d}.idx", "type": "file"} for day in days]}})
        if parsed.path.endswith(".idx"):
            day = datetime.strptime(parsed.path.rsplit("/", 1)[-1], "master.%Y%m%d.idx").date()
            rows = [row for row in self.indexed if row["day"] == day]
            return "\n".join(["Description: Daily Index of EDGAR Dissemination Feed",
                "CIK|Company Name|Form Type|Date Filed|Filename", "----------", *[
                    f"42|{row['issuer_name']}|{row['form_type']}|{day}|edgar/data/42/{row['accession']}.txt" for row in rows]])
        if parsed.path == "/cgi-bin/browse-edgar":
            params = kwargs["params"]
            start = params["start"]
            if self.fail_offset is not None and start >= self.fail_offset:
                raise TransientHTTPError("HTTP 503 on later SEC Atom page")
            # EDGAR's form query is a prefix search; the collector must select
            # the exact configured form before it gains durable queue authority.
            selected = [row for row in self.current if row["form_type"].startswith(params["type"])]
            entries = []
            for row in selected[start:start + params["count"]]:
                entries.append(f"<entry><title>{row['form_type']} - Example Corp. (0000000042) (Filer)</title>"
                    f"<category term=\"{row['form_type']}\"/><updated>{(NOW-timedelta(minutes=10)).isoformat()}</updated>"
                    f"<id>urn:tag:sec.gov:{row['accession']}</id><link rel=\"alternate\" href=\"{row['source_url']}\"/></entry>")
            return '<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>"
        filename = parsed.path.rsplit("/", 1)[-1]
        accession = filename.removesuffix("-index.htm") if filename.endswith("-index.htm") else filename.removesuffix(".htm")
        row = self.records[accession]
        if filename.endswith("-index.htm"):
            return ('<html><table><tr><td>1</td><td>Primary filing</td>'
                    f'<td><a href="{accession}.htm">filing</a></td><td>{row["form_type"]}</td></tr></table></html>')
        if row["form_type"].startswith("S-1"):
            return ("<html><body>Registration No. 333-12345. This is our initial public offering. "
                    "We are offering shares of common stock. Prior to this offering there has been no public market "
                    "for our common stock. The initial public offering price is $4.00.</body></html>")
        return "<html><body>Item 2.02 Results of Operations and Financial Condition. Ordinary operating update.</body></html>"


class EmptyContracts:
    async def collect(self, *, observed_at):
        return []


def monitor(tmp_path, sources, *, forms=("8-K",), max_pages=1):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    collector = SECCollector(None, db=db, max_pages=max_pages, request_interval=0)
    collector._text = sources.text
    source = SECSource(collector, forms)
    settings = Settings(sec_user_agent="SourceAudit audit@example.invalid", database_path=tmp_path / "monitor.db",
                        markets_enabled=False, discourse_enabled=False, enabled_sec_forms=forms)
    service = MonitorService(settings=settings, db=db, health=HealthRegistry(), sec_source=source,
                             usaspending_source=EmptyContracts(), smtp_worker=None, now=lambda: NOW)
    return service, collector


@pytest.mark.asyncio
async def test_atom_prefix_amendment_is_not_queued_or_counted_when_disabled(tmp_path):
    sources = OfficialSources(current=[filing(1), filing(2, "8-K/A")])
    service, collector = monitor(tmp_path, sources)
    await service.run_once()
    with service.db.connect() as conn:
        rows = conn.execute("SELECT accession,form,processed_at FROM sec_pending_filings").fetchall()
    assert [(row["accession"], row["form"]) for row in rows] == [(filing(1)["accession"], "8-K")]
    assert rows[0]["processed_at"] is not None
    assert collector.catalog.coverage()["pending_filings"] == 0
    assert service.sec_source.parse_errors == []


@pytest.mark.asyncio
async def test_capped_atom_poll_drains_oldest_catalogue_work_and_remains_honestly_pending(tmp_path):
    sources = OfficialSources(indexed=[filing(number) for number in range(1, 181)],
                              current=[filing(number, day=NOW.date()) for number in range(181, 221)])
    service, collector = monitor(tmp_path, sources)
    await service.run_once()
    assert collector.last_feed_truncated is True
    assert service.sec_source.parse_errors == []
    assert service.last_report["health"]["collectors"]["sec"]["ok"] is True
    assert service.last_report["status"] == "degraded"
    coverage = service.last_report["sec_collection"]
    assert coverage["status"] == "pending" and coverage["pending_filings"] == 180
    with service.db.connect() as conn:
        processed = {row[0] for row in conn.execute("SELECT accession FROM sec_processed_filings")}
    assert processed == {filing(number)["accession"] for number in range(1, 41)}
    assert service.research.latest_run()["sec_collection"]["pending_filings"] == 180


@pytest.mark.asyncio
async def test_later_atom_http_failure_keeps_and_processes_preceding_page_without_success_receipt(tmp_path):
    sources = OfficialSources(current=[filing(number) for number in range(1, 81)])
    sources.fail_offset = 40
    service, collector = monitor(tmp_path, sources, max_pages=2)
    await service.run_once()
    assert any("TransientHTTPError" in error and "HTTP 503" in error for error in service.sec_source.parse_errors)
    assert service.last_report["health"]["collectors"]["sec"]["ok"] is False
    assert service.last_report["health"]["ready"] is False
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sec_pending_filings").fetchone()[0] == 40
        assert conn.execute("SELECT COUNT(*) FROM sec_processed_filings").fetchone()[0] == 40
        state = conn.execute("SELECT last_success_at,last_error FROM collector_state WHERE name='sec'").fetchone()
    assert state["last_success_at"] is None and "HTTP 503" in state["last_error"]
    assert collector.catalog.coverage()["pending_filings"] == 0


@pytest.mark.asyncio
async def test_corrupted_processed_archive_is_repaired_after_filing_leaves_atom_feed(tmp_path):
    value = filing(1, "S-1")
    sources = OfficialSources(indexed=[value], current=[value])
    service, collector = monitor(tmp_path, sources, forms=("S-1",))
    await service.run_once()
    assert service.tracker.summary()["confirmed_ipos"] == 1
    assert collector.is_processed(value["accession"]) is True
    with service.db.connect() as conn:
        document = conn.execute("SELECT document_sha256 FROM sec_raw_filing_archives WHERE accession=?", (value["accession"],)).fetchone()[0]
        original = conn.execute("SELECT gzip_blob FROM sec_raw_documents WHERE sha256=?", (document,)).fetchone()[0]
        conn.execute("UPDATE sec_raw_documents SET gzip_blob=? WHERE sha256=?", (gzip.compress(b"corrupted source"), document))
    assert collector.is_processed(value["accession"]) is False
    sources.current = []
    await service.run_once()
    assert collector.is_processed(value["accession"]) is True
    with service.db.connect() as conn:
        repaired = conn.execute("SELECT gzip_blob FROM sec_raw_documents WHERE sha256=?", (document,)).fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM sec_raw_documents").fetchone()[0] == 2
        assert conn.execute("SELECT processed_at FROM sec_pending_filings WHERE accession=?", (value["accession"],)).fetchone()[0] is not None
    assert repaired == original
    assert service.sec_source.parse_errors == []
    primary_calls = [url for url, _ in sources.calls if url.endswith(f"/{value['accession']}.htm")]
    assert len(primary_calls) == 2


@pytest.mark.asyncio
async def test_daily_index_date_precision_survives_actual_primary_document_classification(tmp_path):
    sources = OfficialSources(indexed=[filing(1, "S-1")])
    service, collector = monitor(tmp_path, sources, forms=("S-1",))
    await service.run_once()
    event = service.tracker.evidence()[0]
    assert event.filed_at_precision == "date"
    assert event.filed_at == datetime(2026, 10, 5, tzinfo=UTC)
    assert collector.is_processed(event.event_id) is True
    assert event.raw_archive_path.startswith("sqlite:sec_raw_documents/")


class TemporalOfficialSources(OfficialSources):
    async def text(self, url, **kwargs):
        html = await super().text(url, **kwargs)
        if url.endswith("-index.htm"):
            return html.replace("<html>", '<html><div class="infoHead">Filing Date</div><div class="info">2026-10-05</div><div class="infoHead">Accepted</div><div class="info">2026-10-02 20:15:00</div>')
        return html


@pytest.mark.asyncio
@pytest.mark.parametrize("with_atom", [False, True])
async def test_collected_acceptance_and_filing_date_remain_separate(with_atom, tmp_path):
    value = filing(1, "S-1")
    sources = TemporalOfficialSources(indexed=[value], current=[value] if with_atom else [])
    service, collector = monitor(tmp_path, sources, forms=("S-1",))
    await service.run_once()
    event = service.tracker.evidence()[0]
    assert event.accepted_at == datetime(2026, 10, 3, 0, 15, tzinfo=UTC)
    assert event.source_filing_date == DAY
    assert event.filed_at_precision == ("second" if with_atom else "date")
    assert event.filed_at == (NOW - timedelta(minutes=10) if with_atom else datetime(2026, 10, 5, tzinfo=UTC))
    assert collector.is_processed(value["accession"])
    assert service.sec_source.parse_errors == []


@pytest.mark.asyncio
async def test_full_excluded_prefix_window_is_a_visible_bound_not_false_complete_feed(tmp_path):
    sources = OfficialSources(current=[filing(number, "8-K/A") for number in range(1, 41)])
    service, collector = monitor(tmp_path, sources, forms=("8-K",))
    await service.run_once()
    assert collector.last_feed_truncated
    assert service.last_report["sec_collection"]["current_feed_truncated_forms"] == ["8-K"]
    assert collector.catalog.coverage()["pending_filings"] == 0
    assert service.last_report["health"]["collectors"]["sec"]["ok"]
    assert service.last_report["status"] == "degraded"
