import gzip
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from contract_ipo_monitor.db import Database
from contract_ipo_monitor.research import checkpoint_database
from contract_ipo_monitor.sources.http import TransientHTTPError
from contract_ipo_monitor.sources.sec_catalog import SECFilingCatalog, parse_master_index


NOW = datetime(2026, 10, 6, 15, tzinfo=UTC)


def database(path):
    db = Database(path)
    db.initialize()
    return db


def filing(identity, *, day=date(2026, 10, 5), form="8-K"):
    accession = f"0000000042-26-{identity:06d}"
    return {"accession": accession, "cik": "0000000042", "issuer_name": "Example Corp.",
            "form_type": form, "filed_at": datetime.combine(day, datetime.min.time(), UTC),
            "filed_at_precision": "date",
            "source_url": f"https://www.sec.gov/Archives/edgar/data/42/{accession.replace('-', '')}/{accession}-index.htm"}


def master(entries):
    lines = ["Description: Daily Index of EDGAR Dissemination Feed", "CIK|Company Name|Form Type|Date Filed|Filename", "-----------"]
    for value in entries:
        lines.append(f"42|{value['issuer_name']}|{value['form_type']}|{value['filed_at'].date()}|edgar/data/42/{value['accession']}.txt")
    return "\n".join(lines) + "\n"


class IndexSource:
    def __init__(self, days):
        self.days = days
        self.calls = []
        self.fail = {}

    async def __call__(self, url):
        self.calls.append(url)
        if url in self.fail:
            raise self.fail[url]
        if url.endswith("/index.json"):
            path = url.removeprefix("https://www.sec.gov").removesuffix("/index.json")
            year, quarter = int(path.split("/")[-2]), int(path[-1])
            names = [f"master.{day:%Y%m%d}.idx" for day in self.days if day.year == year and (day.month-1)//3+1 == quarter]
            return json.dumps({"directory": {"name": path, "item": [{"name": name, "type": "file"} for name in names]}})
        day = datetime.strptime(url.rsplit("/", 1)[-1], "master.%Y%m%d.idx").date()
        return master(self.days[day])


def index_url(day):
    return f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{(day.month-1)//3+1}/master.{day:%Y%m%d}.idx"


@pytest.mark.asyncio
async def test_snapshot_queue_source_receipts_and_cursor_survive_checkpoint(tmp_path):
    day = date(2026, 10, 5)
    source = IndexSource({day: [filing(1)]})
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    receipt = await subject.sync(source, ("8-K",), observed_at=NOW)
    assert receipt["scope_start"] == "2026-09-29"
    assert receipt["captured_through"] == receipt["published_through"] == "2026-10-05"
    assert receipt["catchup_complete"] is True
    assert receipt["pending_filings"] == 1
    assert subject.verify_index_receipts() == 1
    backup = tmp_path / "checkpoint" / "monitor.db"
    checkpoint_database(subject.db, backup)
    restored = SECFilingCatalog(database(backup))
    assert restored.pending_entries("8-K")[0]["accession"] == filing(1)["accession"]
    assert restored.pending_entries("8-K")[0]["filed_at_precision"] == "date"
    assert restored.verify_index_receipts() == 1
    await restored.sync(source, ("8-K",), observed_at=NOW + timedelta(days=20))
    assert restored.coverage()["scope_start"] == "2026-09-29"


@pytest.mark.asyncio
async def test_more_than_120_filings_drain_durably_while_new_filings_arrive(tmp_path):
    day = date(2026, 10, 5)
    source = IndexSource({day: [filing(identity) for identity in range(1, 181)]})
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    await subject.sync(source, ("8-K",), observed_at=NOW)
    first = subject.pending_entries("8-K", 40)
    for entry in first:
        subject.note_processed(entry["accession"], observed_at=NOW)
    subject.capture([dict(filing(identity), filed_at=NOW, filed_at_precision="second") for identity in range(181, 241)], observed_at=NOW)
    received = {value["accession"] for value in first}
    while batch := subject.pending_entries("8-K", 40):
        for entry in batch:
            assert entry["accession"] not in received
            received.add(entry["accession"])
            subject.note_processed(entry["accession"], observed_at=NOW)
    assert len(received) == 240
    assert subject.coverage()["pending_filings"] == 0


@pytest.mark.asyncio
async def test_later_index_http_failure_keeps_first_index_and_does_not_advance_failed_day(tmp_path):
    first, second = date(2026, 10, 1), date(2026, 10, 2)
    source = IndexSource({first: [filing(1, day=first)], second: [filing(2, day=second)]})
    failure = TransientHTTPError("HTTP 503: Service Unavailable")
    source.fail[index_url(second)] = failure
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    with pytest.raises(TransientHTTPError):
        await subject.sync(source, ("8-K",), observed_at=NOW)
    receipt = subject.coverage()
    assert receipt["captured_through"] == "2026-10-01"
    assert receipt["catchup_complete"] is False and receipt["status"] == "error"
    assert len(subject.pending_entries("8-K")) == 1
    assert subject.verify_index_receipts() == 1
    source.fail.clear()
    await subject.sync(source, ("8-K",), observed_at=NOW)
    assert subject.coverage()["captured_through"] == "2026-10-02"
    assert len(subject.pending_entries("8-K")) == 2


@pytest.mark.asyncio
async def test_download_budget_continues_oldest_uncatalogued_days_in_next_run(tmp_path):
    days = [date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)]
    source = IndexSource({day: [filing(identity, day=day)] for identity, day in enumerate(days, 1)})
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    first = await subject.sync(source, ("8-K",), observed_at=NOW, max_indexes=2)
    assert first["indexes_captured_this_run"] == 2
    assert first["captured_through"] == "2026-10-01" and first["catchup_complete"] is False
    assert first["status"] == "pending"
    second = await subject.sync(source, ("8-K",), observed_at=NOW, max_indexes=2)
    assert second["captured_through"] == "2026-10-05" and second["catchup_complete"] is True
    assert subject.verify_index_receipts() == 4
    assert source.calls.count(index_url(days[0])) == 1


@pytest.mark.asyncio
async def test_weekend_coverage_follows_real_directory_availability_without_guessed_404s(tmp_path):
    sunday = datetime(2026, 10, 4, 15, tzinfo=UTC)
    friday = date(2026, 10, 2)
    source = IndexSource({friday: [filing(1, day=friday)]})
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    receipt = await subject.sync(source, ("8-K",), observed_at=sunday)
    assert receipt["captured_through"] == "2026-10-02" and receipt["catchup_complete"] is True
    assert not any("20261003.idx" in url or "20261004.idx" in url for url in source.calls)
    assert len([url for url in source.calls if url.endswith("index.json")]) == 2


@pytest.mark.parametrize("replacement", [
    "<html><body>Access denied</body></html>",
    "unexpected header\n42|Example|8-K|2026-10-05|edgar/data/42/0000000042-26-000001.txt",
    "CIK|Company Name|Form Type|Date Filed|Filename\n42|Example|8-K|2026-10-05|https://evil.example/filing.txt",
    "CIK|Company Name|Form Type|Date Filed|Filename\n42|Example|8-K|2026-10-05|edgar/data/43/0000000042-26-000001.txt",
    "CIK|Company Name|Form Type|Date Filed|Filename\n42|Example|8-K|2026-10-06|edgar/data/42/0000000042-26-000001.txt",
    "CIK|Company Name|Form Type|Date Filed|Filename\n42|Example|8-K|2026-10-05|edgar/data/42/../0000000042-26-000001.txt",
])
def test_invalid_master_indexes_reject_unexpected_sources_and_dates(replacement):
    with pytest.raises(ValueError):
        parse_master_index(replacement, day=date(2026, 10, 5), enabled_forms=("8-K",))


@pytest.mark.asyncio
async def test_invalid_index_never_creates_queue_or_success_cursor(tmp_path):
    source = IndexSource({date(2026, 10, 5): [filing(1)]})
    async def fetch(url):
        return "<html>access denied</html>" if url.endswith(".idx") else await source(url)
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    with pytest.raises(ValueError, match="HTML"):
        await subject.sync(fetch, ("8-K",), observed_at=NOW)
    assert subject.coverage()["captured_through"] is None
    assert subject.coverage()["pending_filings"] == 0
    assert subject.verify_index_receipts() == 0


def test_atom_exact_timestamp_is_retained_when_date_only_catalogue_reappears(tmp_path):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    approximate = filing(1)
    exact = dict(approximate, filed_at=NOW-timedelta(hours=1), filed_at_precision="second")
    assert subject.capture([approximate], observed_at=NOW) == 1
    assert subject.capture([exact], observed_at=NOW) == 0
    assert subject.capture([approximate], observed_at=NOW) == 0
    assert subject.pending_entries("8-K")[0]["filed_at"] == exact["filed_at"]
    assert subject.pending_entries("8-K")[0]["filed_at_precision"] == "second"


def test_explicit_requeue_reopens_processed_entry_for_source_archive_repair(tmp_path):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    value = filing(1)
    subject.capture([value], observed_at=NOW)
    subject.note_processed(value["accession"], observed_at=NOW)
    subject.capture([value], observed_at=NOW)
    assert subject.pending_entries("8-K") == []
    subject.capture([value], observed_at=NOW, requeue=True)
    assert len(subject.pending_entries("8-K")) == 1


@pytest.mark.asyncio
async def test_missing_directory_is_an_error_and_never_fabricates_holiday_completeness(tmp_path):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    async def fetch(_url):
        raise TransientHTTPError("HTTP 404")
    with pytest.raises(TransientHTTPError):
        await subject.sync(fetch, ("8-K",), observed_at=NOW)
    assert subject.coverage()["catchup_complete"] is False
    assert subject.coverage()["published_through"] is None
    assert subject.coverage()["status"] == "error"


@pytest.mark.asyncio
async def test_corrupt_portable_index_receipt_fails_readback(tmp_path):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    await subject.sync(IndexSource({date(2026, 10, 5): [filing(1)]}), ("8-K",), observed_at=NOW)
    with subject.db.connect() as conn:
        conn.execute("UPDATE sec_index_days SET gzip_blob=?", (gzip.compress(b"altered"),))
    with pytest.raises(ValueError, match="integrity"):
        subject.verify_index_receipts()


@pytest.mark.asyncio
async def test_catchup_advances_past_empty_closed_quarters_with_two_directory_budget(tmp_path):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    source = IndexSource({date(2026, 10, 5): [filing(1)]})
    await subject.sync(source, ("8-K",), observed_at=NOW)
    future = datetime(2027, 10, 6, 15, tzinfo=UTC)
    source.days[date(2027, 10, 5)] = [filing(2, day=date(2027, 10, 5))]
    for _ in range(3):
        start = len(source.calls)
        result = await subject.sync(source, ("8-K",), observed_at=future)
        assert len([url for url in source.calls[start:] if url.endswith("index.json")]) <= 2
    assert result["captured_through"] == "2027-10-05"
    assert result["catchup_complete"] is True
    assert subject.coverage()["catchup_complete"] is True
    assert len(subject.pending_entries("8-K")) == 2


@pytest.mark.asyncio
async def test_new_index_in_current_quarter_is_discovered_on_following_sync(tmp_path):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    source = IndexSource({date(2026, 10, 5): [filing(1)]})
    await subject.sync(source, ("8-K",), observed_at=NOW)
    source.days[date(2026, 10, 6)] = [filing(2, day=date(2026, 10, 6))]
    await subject.sync(source, ("8-K",), observed_at=NOW+timedelta(days=1))
    assert subject.coverage()["captured_through"] == "2026-10-06"
    assert len(subject.pending_entries("8-K")) == 2


@pytest.mark.asyncio
async def test_index_queue_and_cursor_transaction_roll_back_together(tmp_path, monkeypatch):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    def fail_capture(*_args, **_kwargs):
        raise RuntimeError("simulated queue write failure")
    monkeypatch.setattr(subject, "_capture", fail_capture)
    with pytest.raises(RuntimeError, match="queue write failure"):
        await subject.sync(IndexSource({date(2026, 10, 5): [filing(1)]}), ("8-K",), observed_at=NOW)
    assert subject.coverage()["captured_through"] is None
    assert subject.coverage()["pending_filings"] == 0
    assert subject.verify_index_receipts() == 0


def test_supported_direct_official_index_alias_and_future_atom_timestamp(tmp_path):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    value = filing(1)
    value["source_url"] = f"https://www.sec.gov/Archives/edgar/data/42/{value['accession']}-index.html"
    assert subject.capture([value], observed_at=NOW) == 1
    with pytest.raises(ValueError, match="future"):
        subject.capture([dict(value, filed_at=NOW+timedelta(seconds=1), filed_at_precision="second")], observed_at=NOW)


def test_unrelated_official_lowercase_forms_do_not_reject_valid_index_rows():
    raw = master([filing(1, form="17g-1"), filing(2)])
    entries = parse_master_index(raw, day=date(2026, 10, 5), enabled_forms=("8-K",))
    assert [entry["accession"] for entry in entries] == [filing(2)["accession"]]
    assert entries[0]["form_type"] == "8-K"


@pytest.mark.asyncio
async def test_explicit_empty_directory_listings_confirm_empty_scoped_catalogue(tmp_path):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    result = await subject.sync(IndexSource({}), ("8-K",), observed_at=NOW)
    assert result["captured_index_days"] == 0
    assert result["captured_through"] is None and result["published_through"] is None
    assert result["listed_through"] == "2026-10-05"
    assert result["catchup_complete"] is True and result["status"] == "ok"
    assert subject.coverage()["catchup_complete"] is True


@pytest.mark.asyncio
async def test_incomplete_index_discovery_is_pending_even_when_document_queue_is_empty(tmp_path):
    days = [date(2026, 10, 1), date(2026, 10, 2)]
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    result = await subject.sync(IndexSource({day: [] for day in days}), ("8-K",), observed_at=NOW, max_indexes=1)
    assert result["pending_filings"] == 0
    assert result["catchup_complete"] is False and result["status"] == "pending"
    assert subject.coverage()["status"] == "pending"


@pytest.mark.asyncio
async def test_enabled_form_changes_replay_saved_primary_bytes_without_downloading_index_again(tmp_path):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    day = date(2026, 10, 5)
    source = IndexSource({day: [filing(1), filing(2, form="8-K/A")]})
    await subject.sync(source, ("8-K",), observed_at=NOW)
    subject.note_processed(filing(1)["accession"], observed_at=NOW)
    assert subject.pending_entries("8-K/A") == []
    changed = await subject.sync(source, ("8-K", "8-K/A"), observed_at=NOW)
    assert changed["configured_forms"] == ["8-K", "8-K/A"]
    assert changed["pending_filings"] == 1
    assert subject.pending_entries("8-K/A")[0]["accession"] == filing(2)["accession"]
    assert source.calls.count(index_url(day)) == 1
    disabled = await subject.sync(source, ("8-K",), observed_at=NOW)
    assert disabled["pending_filings"] == 0 and disabled["pending_filings_total"] == 1
    assert disabled["status"] == "ok"
    assert len(subject.pending_entries("8-K/A")) == 1
    restored = await subject.sync(source, ("8-K", "8-K/A"), observed_at=NOW)
    assert restored["pending_filings"] == 1


@pytest.mark.asyncio
async def test_form_scope_replay_rejects_corrupt_archive_before_claiming_new_scope(tmp_path):
    subject = SECFilingCatalog(database(tmp_path / "monitor.db"))
    source = IndexSource({date(2026, 10, 5): [filing(1), filing(2, form="8-K/A")]})
    await subject.sync(source, ("8-K",), observed_at=NOW)
    with subject.db.connect() as conn:
        conn.execute("UPDATE sec_index_days SET gzip_blob=?", (b"not gzip",))
    with pytest.raises(ValueError, match="integrity"):
        await subject.sync(source, ("8-K", "8-K/A"), observed_at=NOW)
    assert subject.coverage()["configured_forms"] == ["8-K"]
    assert subject.pending_entries("8-K/A") == []
    assert subject.coverage()["status"] == "error"


def test_delayed_correspondence_does_not_reject_other_selected_filings():
    entries = parse_master_index(master([
        filing(1, day=date(2026, 8, 20), form="corresp"), filing(2),
    ]), day=date(2026, 10, 5), enabled_forms=("8-K",))
    assert [entry["accession"] for entry in entries] == [filing(2)["accession"]]


def test_selected_prior_date_filing_preserves_source_date_and_separate_index_day():
    entries = parse_master_index(master([filing(1, day=date(2026, 9, 25), form="S-1")]),
                                 day=date(2026, 10, 5), enabled_forms=("S-1",))
    assert entries[0]["filed_at"] == "2026-09-25T00:00:00+00:00"
    assert entries[0]["filed_at_precision"] == "date"
    assert entries[0]["catalogue_index_day"] == "2026-10-05"
    assert entries[0]["catalogue_source_url"] == index_url(date(2026, 10, 5))
