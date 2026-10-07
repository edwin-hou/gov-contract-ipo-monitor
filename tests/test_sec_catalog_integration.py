"""Exercise real SEC catalogue/collector/service boundaries without network access."""
import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.health import HealthRegistry
from contract_ipo_monitor.service import MonitorService, SECSource
from contract_ipo_monitor.sources.http import TransientHTTPError
from contract_ipo_monitor.sources.sec import SECCollector
from contract_ipo_monitor.sources.sec_catalog import parse_master_index
from contract_ipo_monitor.tracking import IPOTracker


NOW = datetime(2026, 10, 6, 15, tzinfo=UTC)
DAY = date(2026, 10, 5)
FIXTURES = Path(__file__).parent / "fixtures"


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
            return json.dumps({"directory": {"name": path.removeprefix("/Archives/edgar/") + "/", "item": [
                {"name": f"master.{day:%Y%m%d}.idx", "type": "file"} for day in days]}})
        if parsed.path.endswith(".idx"):
            day = datetime.strptime(parsed.path.rsplit("/", 1)[-1], "master.%Y%m%d.idx").date()
            rows = [row for row in self.indexed if row["day"] == day]
            return "\n".join(["Description: Daily Index of EDGAR Dissemination Feed",
                "CIK|Company Name|Form Type|Date Filed|File Name", "----------", *[
                    f"42|{row['issuer_name']}|{row['form_type']}|{day:%Y%m%d}|edgar/data/42/{row['accession']}.txt" for row in rows]])
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


def review_collector(tmp_path, *, index_html, document):
    db = Database(tmp_path / "review.db")
    db.initialize()
    collector = SECCollector(None, db=db, request_interval=0)
    calls = []
    async def fetch(url, **kwargs):
        calls.append(url)
        return index_html if url.endswith("-index.htm") else document()
    collector._text = fetch
    return collector, calls


def review_entry(number=1, *, form="S-1", cik="42", issuer_name="Example Corp."):
    value = filing(number, form)
    return {"accession": value["accession"], "form_type": form, "cik": cik, "issuer_name": issuer_name,
            "filed_at": datetime.combine(DAY, datetime.min.time(), UTC), "filed_at_precision": "date",
            "source_url": value["source_url"].replace("/data/42/", f"/data/{int(cik)}/")}


def synthetic_filer(cik, name):
    return (f'<div class="companyInfo"><span class="companyName">{name} (Filer) CIK: '
            f'<a href="/cgi-bin/browse-edgar?action=getcompany&amp;CIK={cik}">{cik} '
            '(see all company filings)</a></span></div>')


def ambiguous_registration_index():
    return ("<html>" + synthetic_filer("42", "Example Corp.") + synthetic_filer("43", "Co-filer Ltd")
            + '<table><tr><td>1</td><td>Primary filing</td><td><a href="registration.htm">filing</a></td>'
              '<td>S-1</td></tr></table></html>')


def resolved_registration_document():
    return '''<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:dei="http://xbrl.sec.gov/dei/2026"><body>
        <xbrli:context id="base"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">43</xbrli:identifier>
        </xbrli:entity><xbrli:period><xbrli:instant>2026-10-05</xbrli:instant></xbrli:period></xbrli:context>
        <ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="base">43</ix:nonNumeric>
        <ix:nonNumeric name="dei:EntityRegistrantName" contextRef="base">Co-filer Ltd</ix:nonNumeric>
        <ix:nonNumeric name="dei:DocumentType" contextRef="base">S-1</ix:nonNumeric>
        <p>Registration No. 333-12345. This is our initial public offering. We are offering shares of common stock.
        We intend to list our shares on Nasdaq under the symbol "COF". The initial public offering price is $4.00.</p>
        </body></html>'''


@pytest.mark.asyncio
async def test_real_shared_8k_resolves_base_registrant_without_using_first_filer_or_archive_folder(tmp_path):
    index = (FIXTURES / "sec_joint_corteva_eidp_8k_index_20260930.htm").read_text(encoding="utf-8")
    body = (FIXTURES / "sec_joint_corteva_eidp_8k_primary_20260930.htm").read_text(encoding="utf-8")
    collector, calls = review_collector(tmp_path, index_html=index, document=lambda: body)
    entries = parse_master_index((FIXTURES / "sec_daily_master_20260930_excerpt.idx").read_text(encoding="utf-8"),
                                 day=date(2026, 9, 30), enabled_forms=("8-K",))
    joint = next(entry for entry in entries if len(entry["catalogue_filers"]) == 2)
    # Deliberately represent EIDP, which is first in the index and in the
    # document URL, while authoritative undimensioned DEI identifies Corteva.
    joint.update(joint["catalogue_filers"][1])
    collector.catalog.capture([joint], observed_at=NOW)
    pending = collector.catalog.pending_entries("8-K")[0]
    event, signal = await collector.collect_entry(pending)
    assert event is None and signal is None  # This actual document is an ordinary 8-K.
    collector.mark_processed(joint["accession"], observed_at=NOW)
    with collector.db.connect() as conn:
        saved = json.loads(conn.execute("SELECT entry_json FROM sec_pending_filings").fetchone()[0])
    assert saved["issuer_review"]["status"] == "resolved"
    assert saved["issuer_review"]["resolved_issuer"]["cik"] == "0001755672"
    assert saved["issuer_review"]["resolved_issuer"]["issuer_name"] == "Corteva, Inc."
    assert len(saved["catalogue_filers"]) == 2 and collector.is_processed(joint["accession"])
    assert collector.catalog.coverage()["issuer_review_count"] == 0
    assert len(calls) == 2 and "/data/30554/" in calls[0] and "/data/30554/" in calls[1]


@pytest.mark.asyncio
async def test_ambiguous_registration_is_archived_once_and_requires_visible_review_until_deliberate_replay(tmp_path):
    document = ["<html><body>Registration No. 333-12345. This is our initial public offering. "
                "We intend to list our common stock on Nasdaq under the symbol COF.</body></html>"]
    collector, calls = review_collector(tmp_path, index_html=ambiguous_registration_index(), document=lambda: document[0])
    entry = review_entry()
    collector.catalog.capture([entry], observed_at=NOW)
    event, signal = await collector.collect_entry(collector.catalog.pending_entries("S-1")[0])
    assert event is None and signal is None
    collector.mark_processed(entry["accession"], observed_at=NOW)
    assert collector.is_processed(entry["accession"]) and collector.catalog.pending_entries("S-1") == []
    held = collector.catalog.coverage()
    assert held["status"] == "review_required" and held["issuer_review_count"] == 1
    assert held["pending_filings"] == 0 and held["catchup_complete"] is False
    assert "require issuer review" in held["limitations"][-1]
    with collector.db.connect() as conn:
        row = conn.execute("SELECT entry_json,processed_at FROM sec_pending_filings").fetchone()
        held_entry = json.loads(row["entry_json"])
        assert row["processed_at"] is not None
        assert conn.execute("SELECT COUNT(*) FROM sec_raw_documents").fetchone()[0] == 2
    # A normal feed refresh preserves the held classification and does not
    # repeat source processing. Explicit review replay retains original bytes
    # and identities, then can resolve newly authoritative primary metadata.
    collector.catalog.capture([entry], observed_at=NOW)
    assert collector.is_processed(entry["accession"]) and len(calls) == 2
    document[0] = resolved_registration_document()
    collector.catalog.capture([entry], observed_at=NOW, requeue=True)
    assert not collector.is_processed(entry["accession"])
    reopened = collector.catalog.pending_entries("S-1")[0]
    assert reopened["issuer_review"]["status"] == "retry_requested"
    event, signal = await collector.collect_entry(reopened)
    assert event.cik == signal.cik == "0000000043" and event.is_ipo is True and signal.active is True
    assert event.issuer_name == signal.issuer_name == "Co-filer Ltd"
    tracker = IPOTracker(collector.db)
    tracker.initialize()
    tracker.record(event, observed_at=NOW)
    collector.mark_processed(entry["accession"], observed_at=NOW)
    with collector.db.connect() as conn:
        resolved = json.loads(conn.execute("SELECT entry_json FROM sec_pending_filings").fetchone()[0])
        assert conn.execute("SELECT COUNT(*) FROM sec_raw_documents").fetchone()[0] == 3
    review = resolved["issuer_review"]
    assert review["status"] == "resolved" and review["resolved_issuer"]["registration_id"] == "333-12345"
    assert resolved["catalogue_filers"] == held_entry["catalogue_filers"]
    assert any(previous["status"] == "unresolved" for previous in resolved["issuer_review_history"])
    assert collector.is_processed(entry["accession"]) and collector.catalog.coverage()["issuer_review_count"] == 0


@pytest.mark.asyncio
async def test_unresolved_review_archive_corruption_requeues_and_repairs_actual_source_receipts(tmp_path):
    body = "<html><body>Our initial public offering</body></html>"
    collector, calls = review_collector(tmp_path, index_html=ambiguous_registration_index(), document=lambda: body)
    entry = review_entry()
    collector.catalog.capture([entry], observed_at=NOW)
    await collector.collect_entry(collector.catalog.pending_entries("S-1")[0])
    collector.mark_processed(entry["accession"], observed_at=NOW)
    with collector.db.connect() as conn:
        saved = json.loads(conn.execute("SELECT entry_json FROM sec_pending_filings").fetchone()[0])
        digest = saved["issuer_review"]["raw_payload_hash"]
        original = conn.execute("SELECT gzip_blob FROM sec_raw_documents WHERE sha256=?", (digest,)).fetchone()[0]
        conn.execute("UPDATE sec_raw_documents SET gzip_blob=? WHERE sha256=?", (gzip.compress(b"damaged review source"), digest))
    assert not collector.is_processed(entry["accession"])
    assert collector.requeue_invalid_catalog_receipts() == 1
    event, signal = await collector.collect_entry(collector.catalog.pending_entries("S-1")[0])
    assert event is None and signal is None
    collector.mark_processed(entry["accession"], observed_at=NOW)
    assert collector.is_processed(entry["accession"]) and len(calls) == 4
    with collector.db.connect() as conn:
        repaired = conn.execute("SELECT gzip_blob FROM sec_raw_documents WHERE sha256=?", (digest,)).fetchone()[0]
    assert repaired == original and collector.catalog.coverage()["issuer_review_count"] == 1


@pytest.mark.asyncio
async def test_actual_shared_effect_atom_only_identity_reopens_legacy_last_filer_assertion(tmp_path):
    index = (FIXTURES / "sec_joint_cubebio_effect_index_20260930.htm").read_text(encoding="utf-8")
    body = (FIXTURES / "sec_joint_cubebio_effect_primary_20260930.xml").read_text(encoding="utf-8")
    collector, calls = review_collector(tmp_path, index_html=index, document=lambda: body)
    accession = "9999999995-26-003117"
    index_url = f"https://www.sec.gov/Archives/edgar/data/2058594/{accession.replace('-', '')}/{accession}-index.htm"
    document_url = collector.normalizer.primary_document_url(index_url, index, "EFFECT")
    entry = {"accession": accession, "form_type": "EFFECT", "cik": "2058594", "issuer_name": "Cubebio Co., Ltd",
             "filed_at": datetime(2026, 9, 30, 12, tzinfo=UTC), "filed_at_precision": "second", "source_url": index_url}
    collector.catalog.capture([entry], observed_at=NOW)
    reference = collector._archive_documents(accession=accession, index_url=index_url, index_html=index,
                                            document_url=document_url, document=body)
    old = collector.normalizer.tracking_document(**{key: entry[key] for key in ("form_type", "accession", "issuer_name", "cik", "filed_at")},
                                                source_url=document_url, text=body)
    old = old.model_copy(update={"raw_archive_path": reference, "registration_id": "333-298262-01"})
    tracker = IPOTracker(collector.db)
    tracker.initialize()
    tracker.record(old, observed_at=NOW)
    collector.mark_processed(accession, observed_at=NOW)
    assert not collector.is_processed(accession)
    assert collector.requeue_invalid_catalog_receipts() == 1
    reopened = collector.catalog.pending_entries("EFFECT")[0]
    assert reopened["issuer_review"]["status"] == "retry_requested"
    event, signal = await collector.collect_entry(reopened)
    assert event is None and signal is None
    collector.mark_processed(accession, observed_at=NOW)
    assert collector.is_processed(accession) and len(calls) == 2
    with collector.db.connect() as conn:
        saved = json.loads(conn.execute("SELECT entry_json FROM sec_pending_filings").fetchone()[0])
    assert len(saved["catalogue_filers"]) == 2
    assert saved["issuer_review"]["affected_registrations"] == [
        {"cik": "0002058261", "issuer_name": "CubeBio Holdings Ltd", "registration_id": "333-298262"},
        {"cik": "0002058594", "issuer_name": "Cubebio Co., Ltd", "registration_id": "333-298262-01"}]
    assert saved["issuer_review"]["status"] == "unresolved"
    # Original audit evidence remains, while the read-time gate withholds it.
    assert len(tracker.evidence()) == 1
    assert collector.catalog.coverage()["issuer_review_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_has_registration", [True, False])
async def test_resolved_shared_issuer_uses_fresh_primary_scope_and_discards_stale_or_index_co_filer_scope(primary_has_registration, tmp_path):
    body = resolved_registration_document()
    if not primary_has_registration:
        body = body.replace("Registration No. 333-12345.", "")
    index = ambiguous_registration_index().replace("</html>", "<p>File Number 333-99999</p></html>")
    collector, _calls = review_collector(tmp_path, index_html=index, document=lambda: body)
    entry = {**review_entry(), "registration_id": "333-11111"}
    collector.catalog.capture([entry], observed_at=NOW)
    event, signal = await collector.collect_entry(collector.catalog.pending_entries("S-1")[0])
    expected = "333-12345" if primary_has_registration else None
    assert event.cik == signal.cik == "0000000043"
    assert event.registration_id == signal.registration_id == expected
    with collector.db.connect() as conn:
        saved = json.loads(conn.execute("SELECT entry_json FROM sec_pending_filings").fetchone()[0])
    assert saved["issuer_review"]["resolved_issuer"]["registration_id"] == expected
    assert saved["issuer_review"]["raw_payload_hash"] == event.raw_payload_hash == signal.raw_payload_hash
