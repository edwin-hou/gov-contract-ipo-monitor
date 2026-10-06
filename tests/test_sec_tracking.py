import gzip
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from contract_ipo_monitor.archive import EvidenceArchive
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.sources.http import ResilientClient
from contract_ipo_monitor.sources.sec import SECCollector, SECNormalizer, document_text
from contract_ipo_monitor.tracking import IPOEvidence, IPOTracker


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def classify(text, form="S-1"):
    return SECNormalizer().tracking_document(
        form_type=form, accession="0000000001-26-123456", issuer_name="Example Inc", cik="42",
        filed_at=NOW, source_url="https://www.sec.gov/Archives/edgar/data/42/doc.htm", text=text,
    )


def test_early_ipo_is_tracked_without_exchange_price_or_government_award():
    result = classify("Registration No. 333-123. This is our initial public offering of common stock.")
    assert result.is_ipo
    assert result.registration_id == "333-123"
    assert result.exchange is None
    assert result.proposed_price is None


def test_resale_discussing_historical_ipo_never_confirms_ipo():
    result = classify(
        "This prospectus relates solely to the resale by selling stockholders. "
        "We completed our initial public offering of our common stock last year. "
        "Our shares are listed on Nasdaq."
    )
    assert not result.is_ipo
    assert result.offering_kind == "resale"


def test_historical_ipo_mention_in_follow_on_does_not_confirm_ipo():
    result = classify(
        "We are offering common stock in this offering. Our common stock is currently listed on Nasdaq. "
        "We completed our initial public offering in 2022."
    )
    assert not result.is_ipo
    assert result.offering_kind == "follow_on"


def test_secondary_selling_stockholders_with_primary_ipo_is_valid():
    result = classify(
        "This is our initial public offering. We are offering 20,000,000 common shares. "
        "Selling stockholders are offering 5,000,000 additional shares."
    )
    assert result.is_ipo


def test_form_s1_alone_is_uncertain_and_not_confirmed():
    result = classify("Registration statement under the Securities Act of 1933.")
    assert not result.is_ipo
    assert result.offering_kind == "unclassified"


def test_html_script_hidden_phrase_does_not_classify_registration_as_ipo():
    result = classify(
        "<html><script>This is our initial public offering</script>"
        "<body><p>This prospectus relates solely to resale.</p></body></html>"
    )
    assert not result.is_ipo
    assert "initial public offering" not in result.evidence_excerpt


def test_html_split_phrase_and_currency_are_normalized():
    result = classify(
        '<html><body><p>This is our <b>initial public offering</b>.</p>'
        '<p>We applied to list on Nasdaq under the symbol &ldquo;TEST&rdquo;.</p>'
        '<p>The public offering price is expected to be between <b>$250.00</b> and <b>$260.00</b>.</p></body></html>'
    )
    assert result.is_ipo
    assert result.proposed_price == 255
    assert result.ticker == "TEST"


def test_effect_xml_recovers_registration_number_and_is_not_ipo_itself():
    result = classify(
        '<edgarSubmission><effectiveData><fileNumber>333-123</fileNumber></effectiveData></edgarSubmission>',
        form="EFFECT",
    )
    assert result.registration_id == "333-123"
    assert result.event_type == "effective"
    assert not result.is_ipo


def test_effect_metadata_uses_underlying_issuer_and_keeps_posting_date_separate():
    result = classify(
        '<edgarSubmission><submissionType>EFFECT</submissionType><effectiveData>'
        '<finalEffectivenessDispDate>2026-10-02</finalEffectivenessDispDate><form>F-1</form>'
        '<filer><cik>91</cik><entityName>Actual Issuer Ltd</entityName>'
        '<fileNumber>333-123-01</fileNumber></filer></effectiveData></edgarSubmission>', form="EFFECT",
    )
    assert result.cik == "0000000091"
    assert result.issuer_name == "Actual Issuer Ltd"
    assert result.registration_id == "333-123-01"
    assert result.underlying_form == "F-1"
    assert result.effective_date == date(2026, 10, 2)
    assert result.filed_at == NOW


def test_aw_is_amendment_withdrawal_and_not_legacy_withdrawal_signal():
    result = classify("We request withdrawal of Amendment No. 3 to Registration No. 333-123", form="AW")
    assert result.event_type == "amendment_withdrawn"
    assert SECNormalizer().classify_document(
        form_type="AW", accession="aw", issuer_name="Example Inc", cik="42", filed_at=NOW,
        source_url="https://www.sec.gov/Archives/aw", text="Requests withdrawal of Amendment No. 3",
    ) is None


def test_ambiguous_multiple_file_numbers_fail_closed():
    result = classify("Registration No. 333-123 and Registration No. 333-456. This is our initial public offering.")
    assert result.registration_id is None


def test_primary_doc_resolver_uses_exact_filing_type_over_exhibit_and_navigation():
    html = '''<a href="/Archives/edgar/data/42/navigation.htm">Other link</a>
    <table><tr><td>2</td><td>Exhibit</td><td><a href="exhibit.htm">x</a></td><td>EX-99.1</td></tr>
    <tr><td>1</td><td>Prospectus</td><td><a href="offering.htm">x</a></td><td>S-1/A</td></tr></table>'''
    url = SECNormalizer().primary_document_url(
        "https://www.sec.gov/Archives/edgar/data/42/000-index.htm", html, "S-1/A",
    )
    assert url.endswith("/offering.htm")


def test_effect_resolver_removes_xsl_wrapper_and_accepts_xml():
    html = '''<table><tr><td>1</td><td>EFFECT</td>
    <td><a href="xslEFFECTX01/primary_doc.xml">primary</a></td><td>EFFECT</td></tr></table>'''
    url = SECNormalizer().primary_document_url(
        "https://www.sec.gov/Archives/edgar/data/42/000-index.htm", html, "EFFECT",
    )
    assert url == "https://www.sec.gov/Archives/edgar/data/42/primary_doc.xml"


def test_primary_doc_resolver_rejects_foreign_link():
    index = "https://www.sec.gov/Archives/edgar/data/42/000-index.htm"
    assert SECNormalizer().primary_document_url(index, '<a href="https://malicious.example/doc.htm">link</a>') == index


def atom(accessions, *, form="S-1", malformed=False):
    entries = []
    for accession in accessions:
        updated = "" if malformed else "<updated>2026-10-05T12:00:00Z</updated>"
        entries.append(
            f'<entry><title>{form} - Example (Holdings) Inc. (0000000042) (Filer)</title>'
            f'{updated}<category term="{form}"/>'
            '<link rel="self" href="https://www.sec.gov/wrong"/>'
            f'<link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/42/{accession}-index.htm"/></entry>'
        )
    return '<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>"


def test_atom_issuer_cik_accession_and_alternate_link_are_exact():
    row = SECNormalizer().parse_atom(atom(["0000000001-26-123456"]))[0]
    assert row["issuer_name"] == "Example (Holdings) Inc."
    assert row["cik"] == "0000000042"
    assert row["accession"] == "0000000001-26-123456"
    assert row["source_url"].endswith("-index.htm")


def test_atom_missing_date_does_not_fabricate_freshness():
    with pytest.raises(ValueError, match="timestamp"):
        SECNormalizer().parse_atom(atom(["0000000001-26-123456"], malformed=True))


@pytest.mark.asyncio
async def test_feed_paginates_deduplicates_and_reports_truncation():
    calls = []
    pages = [atom(["0000000001-26-000001", "0000000001-26-000002"]), atom(["0000000001-26-000002", "0000000001-26-000003"])]

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=pages.pop(0))

    client = ResilientClient(transport=httpx.MockTransport(handler))
    collector = SECCollector(client, max_pages=2, request_interval=0)
    entries = await collector.current_entries("S-1", count=2)
    assert len(entries) == 3
    assert calls[1].url.params["start"] == "2"
    assert collector.last_feed_truncated
    await client.aclose()


@pytest.mark.asyncio
async def test_processed_feed_page_stops_without_false_truncation(tmp_path):
    calls = []
    accessions = ["0000000001-26-000001", "0000000001-26-000002"]

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=atom(accessions))

    db = Database(tmp_path / "monitor.db")
    db.initialize()
    client = ResilientClient(transport=httpx.MockTransport(handler))
    collector = SECCollector(client, db=db, max_pages=1, request_interval=0)
    for accession in accessions:
        collector.mark_processed(accession, observed_at=NOW)
    assert len(await collector.current_entries("S-1", count=2)) == 2
    assert not collector.last_feed_truncated
    assert len(calls) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_sec_collector_rejects_untrusted_feed_document_url_before_fetch():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="This is our initial public offering")

    client = ResilientClient(transport=httpx.MockTransport(handler))
    collector = SECCollector(client, request_interval=0)
    entry = SECNormalizer().parse_atom(atom(["0000000001-26-000001"]))[0]
    entry["source_url"] = "http://127.0.0.1/private"
    with pytest.raises(ValueError, match="official EDGAR"):
        await collector.collect_entry(entry)
    assert calls == []
    await client.aclose()


@pytest.mark.asyncio
async def test_collect_entry_fetches_primary_once_for_tracker_and_legacy(tmp_path):
    calls = []
    index = '''<table><tr><td>1</td><td>Initial Registration</td>
    <td><a href="offering.htm">offering</a></td><td>S-1</td></tr></table>'''

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("-index.htm"):
            return httpx.Response(200, text=index)
        if request.url.path.endswith("offering.htm"):
            return httpx.Response(200, text="Registration No. 333-123. This is our initial public offering. We applied to list on Nasdaq.")
        return httpx.Response(200, json={"addresses": {"business": {"city": "Nashville"}}})

    client = ResilientClient(transport=httpx.MockTransport(handler))
    collector = SECCollector(client, request_interval=0, archive=EvidenceArchive(tmp_path / "evidence"))
    entry = SECNormalizer().parse_atom(atom(["0000000001-26-123456"]))[0]
    ipo, listing = await collector.collect_entry(entry)
    assert ipo.is_ipo
    assert listing.is_initial_listing
    assert listing.registration_id == "333-123"
    assert ipo.raw_archive_path is not None
    archived = json.loads(Path(ipo.raw_archive_path).read_text())
    assert "initial public offering" in archived["payload"]["document"]
    assert sum(path.endswith("offering.htm") for path in calls) == 1
    await client.aclose()


def test_processed_receipt_persists_only_when_explicitly_marked(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    client = ResilientClient()
    collector = SECCollector(client, db=db)
    assert not collector.is_processed("example")
    collector.mark_processed("example", observed_at=NOW)
    reloaded = SECCollector(client, db=db)
    assert reloaded.is_processed("example")


@pytest.mark.asyncio
async def test_default_database_archive_preserves_primary_and_index_through_backup(tmp_path):
    index = '''<table><tr><td>1</td><td>Registration</td>
    <td><a href="offering.htm">offering</a></td><td>S-1</td></tr></table>'''
    document = "Registration No. 333-123. This is our initial public offering of common stock."

    def handler(request):
        return httpx.Response(200, text=index if request.url.path.endswith("-index.htm") else document)

    db = Database(tmp_path / "monitor.db")
    db.initialize()
    client = ResilientClient(transport=httpx.MockTransport(handler))
    collector = SECCollector(client, db=db, request_interval=0)
    entry = SECNormalizer().parse_atom(atom(["0000000001-26-123456"]))[0]
    evidence, signal = await collector.collect_entry(entry)
    digest = hashlib.sha256(document.encode()).hexdigest()
    assert signal is None  # Missing exchange has no effect on broad IPO tracking.
    assert evidence.raw_archive_path == f"sqlite:sec_raw_documents/{digest}"
    tracker = IPOTracker(db)
    tracker.initialize()
    tracker.record(evidence, observed_at=NOW)
    with closing(db.connect()) as conn:
        primary = conn.execute("SELECT * FROM sec_raw_documents WHERE sha256=?", (digest,)).fetchone()
        manifest = conn.execute("SELECT * FROM sec_raw_filing_archives").fetchone()
        archived_index = conn.execute("SELECT gzip_blob FROM sec_raw_documents WHERE sha256=?", (manifest["index_sha256"],)).fetchone()
        assert gzip.decompress(primary["gzip_blob"]).decode() == document
        assert primary["original_bytes"] == len(document.encode())
        assert primary["source_url"].endswith("offering.htm")
        assert gzip.decompress(archived_index["gzip_blob"]).decode() == index
        with closing(sqlite3.connect(tmp_path / "backup.db")) as backup:
            conn.backup(backup)

    restored_db = Database(tmp_path / "backup.db")
    restored_tracker = IPOTracker(restored_db)
    restored_event = restored_tracker.evidence()[0]
    assert restored_event.raw_archive_path == evidence.raw_archive_path
    with closing(restored_db.connect()) as restored:
        row = restored.execute("SELECT gzip_blob FROM sec_raw_documents WHERE sha256=?", (digest,)).fetchone()
        assert gzip.decompress(row["gzip_blob"]).decode() == document
        assert restored.execute("SELECT COUNT(*) FROM sec_raw_documents").fetchone()[0] == 2
    # Re-observation preserves the same immutable reference and avoids duplicate blobs.
    again, _ = await collector.collect_entry(entry)
    assert again.raw_archive_path == evidence.raw_archive_path
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sec_raw_documents").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM sec_raw_filing_archives").fetchone()[0] == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_irrelevant_8k_has_no_raw_archive(tmp_path):
    index = '''<table><tr><td>1</td><td>Current Report</td>
    <td><a href="report.htm">report</a></td><td>8-K</td></tr></table>'''

    def handler(request):
        return httpx.Response(200, text=index if request.url.path.endswith("-index.htm") else "Routine change of auditor.")

    db = Database(tmp_path / "monitor.db")
    db.initialize()
    client = ResilientClient(transport=httpx.MockTransport(handler))
    collector = SECCollector(client, db=db, request_interval=0)
    entry = SECNormalizer().parse_atom(atom(["0000000001-26-123456"], form="8-K"))[0]
    assert await collector.collect_entry(entry) == (None, None)
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sec_raw_documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sec_raw_filing_archives").fetchone()[0] == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_sec_byte_limit_precedes_classification_and_archiving(tmp_path):
    index = '''<table><tr><td>1</td><td>Registration</td>
    <td><a href="offering.htm">offering</a></td><td>S-1</td></tr></table>'''

    def handler(request):
        return httpx.Response(200, text=index if request.url.path.endswith("-index.htm") else "This is our initial public offering. " + "x" * 1000)

    db = Database(tmp_path / "monitor.db")
    db.initialize()
    client = ResilientClient(transport=httpx.MockTransport(handler))
    collector = SECCollector(client, db=db, request_interval=0, max_document_bytes=500)
    entry = SECNormalizer().parse_atom(atom(["0000000001-26-123456"]))[0]
    with pytest.raises(ValueError, match="byte limit"):
        await collector.collect_entry(entry)
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sec_raw_documents").fetchone()[0] == 0
    assert not collector.is_processed(entry["accession"])
    await client.aclose()


@pytest.mark.asyncio
async def test_legacy_receipt_replays_missing_archive_once_preserving_versions(tmp_path):
    index = '''<table><tr><td>1</td><td>Registration</td>
    <td><a href="offering.htm">offering</a></td><td>S-1</td></tr></table>'''
    document = "Registration No. 333-123. This is our initial public offering of common stock."

    def handler(request):
        return httpx.Response(200, text=index if request.url.path.endswith("-index.htm") else document)

    db = Database(tmp_path / "monitor.db")
    db.initialize()
    tracker = IPOTracker(db)
    tracker.initialize()
    client = ResilientClient(transport=httpx.MockTransport(handler))
    collector = SECCollector(client, db=db, request_interval=0)
    entry = SECNormalizer().parse_atom(atom(["0000000001-26-123456"]))[0]
    legacy = collector.normalizer.tracking_document(
        **{key: entry[key] for key in ("form_type", "accession", "issuer_name", "cik", "filed_at")},
        source_url="https://www.sec.gov/Archives/edgar/data/42/offering.htm", text=document,
    )
    assert legacy.raw_archive_path is None
    tracker.record(legacy, observed_at=NOW)
    collector.mark_processed(entry["accession"], observed_at=NOW)
    assert not collector.is_processed(entry["accession"])

    recovered, _ = await collector.collect_entry(entry)
    assert tracker.record(recovered, observed_at=NOW)
    assert recovered.raw_archive_path.startswith("sqlite:sec_raw_documents/")
    assert collector.is_processed(entry["accession"])
    # History stays intact; the newest archived version makes later skips safe.
    assert tracker.summary()["evidence"] == 2
    assert not tracker.record(recovered, observed_at=NOW)
    assert collector.is_processed(entry["accession"])
    with closing(db.connect()) as conn:
        conn.execute("UPDATE sec_raw_documents SET gzip_blob=? WHERE sha256=?", (b"damaged gzip", recovered.raw_payload_hash))
    assert not collector.is_processed(entry["accession"])
    # A new verified source fetch repairs corrupted storage under the same digest.
    again, _ = await collector.collect_entry(entry)
    assert not tracker.record(again, observed_at=NOW)
    assert collector.is_processed(entry["accession"])
    await client.aclose()


def test_legacy_filesystem_receipt_requires_explicit_backend_and_actual_document(tmp_path):
    document = "Registration No. 333-123. This is our initial public offering of common stock."
    url = "https://www.sec.gov/Archives/edgar/data/42/offering.htm"
    accession = "0000000001-26-123456"
    archive = EvidenceArchive(tmp_path / "raw")
    archived = archive.write(
        "sec-raw", accession,
        {"document_url": url, "index_url": url + "-index.htm", "document": document, "index_html": "Index body"},
        observed_at=NOW,
    )
    legacy = SECNormalizer().tracking_document(
        form_type="S-1", accession=accession, issuer_name="Example Inc", cik="42", filed_at=NOW,
        source_url=url, text=document,
    ).model_copy(update={"raw_archive_path": str(archived)})
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    tracker = IPOTracker(db)
    tracker.initialize()
    tracker.record(legacy, observed_at=NOW)
    client = ResilientClient()
    local = SECCollector(client, db=db, archive=archive)
    local.mark_processed(accession, observed_at=NOW)
    assert local.is_processed(accession)
    # A transient filesystem archive cannot satisfy a hosted SQLite-only receipt.
    hosted = SECCollector(client, db=db)
    assert not hosted.is_processed(accession)
    archived.unlink()
    assert not local.is_processed(accession)


@pytest.mark.asyncio
async def test_recovered_receipt_checks_companion_index_archive(tmp_path):
    index = '''<table><tr><td>1</td><td>Registration</td>
    <td><a href="offering.htm">offering</a></td><td>S-1</td></tr></table>'''
    document = "This is our initial public offering of common stock."

    def handler(request):
        return httpx.Response(200, text=index if request.url.path.endswith("-index.htm") else document)

    db = Database(tmp_path / "monitor.db")
    db.initialize()
    tracker = IPOTracker(db)
    tracker.initialize()
    client = ResilientClient(transport=httpx.MockTransport(handler))
    collector = SECCollector(client, db=db, request_interval=0)
    entry = SECNormalizer().parse_atom(atom(["0000000001-26-123456"]))[0]
    evidence, _ = await collector.collect_entry(entry)
    tracker.record(evidence, observed_at=NOW)
    collector.mark_processed(entry["accession"], observed_at=NOW)
    assert collector.is_processed(entry["accession"])
    with closing(db.connect()) as conn:
        manifest = conn.execute("SELECT index_sha256 FROM sec_raw_filing_archives").fetchone()
        conn.execute("UPDATE sec_raw_documents SET gzip_blob=? WHERE sha256=?", (b"damaged index", manifest["index_sha256"]))
    assert not collector.is_processed(entry["accession"])
    await client.aclose()


def chronology_tracker(tmp_path):
    db = Database(tmp_path / "chronology.db")
    db.initialize()
    tracker = IPOTracker(db)
    tracker.initialize()
    return tracker


def chronology_event(identity, kind, *, at=NOW, precision="second", confirmed=True,
                     accepted_at=None, source_filing_date=None):
    return IPOEvidence(
        event_id=identity, issuer_name="Chronology Example Inc.", cik="42", source="sec",
        source_kind="regulatory", source_url=f"https://www.sec.gov/Archives/{identity}.htm",
        registration_id="333-999", filed_at=at, filed_at_precision=precision,
        accepted_at=accepted_at, source_filing_date=source_filing_date,
        event_type=kind, is_ipo=confirmed and kind in {"registration", "amendment", "prospectus"},
        offering_kind="ipo" if confirmed else "unclassified",
    )


def test_same_day_date_only_withdrawal_cannot_sort_away_before_precise_registration(tmp_path):
    tracker = chronology_tracker(tmp_path)
    tracker.record(chronology_event("s1", "registration"))
    tracker.record(chronology_event("rw", "withdrawn", at=NOW.replace(hour=0), precision="date"))
    candidate = tracker.candidates()[0]
    assert candidate["status"] == "chronology_unresolved" and candidate["active"] is False
    assert candidate["ipo_confirmed"] is True
    assert candidate["first_filed_at_precision"] == "date"
    assert candidate["last_filed_at_precision"] == "second"
    assert any("midnight placeholder" in value for value in candidate["limitations"])
    assert tracker.summary()["withdrawn_ipos"] == 0
    assert tracker.summary()["chronology_unresolved_candidates"] == 1


def test_two_date_only_filings_cannot_establish_same_day_withdrawal_order(tmp_path):
    tracker = chronology_tracker(tmp_path)
    day = NOW.replace(hour=0)
    tracker.record(chronology_event("s1", "registration", at=day, precision="date"))
    tracker.record(chronology_event("rw", "withdrawn", at=day, precision="date"))
    assert tracker.candidates()[0]["status"] == "chronology_unresolved"
    assert tracker.candidates()[0]["active"] is False


@pytest.mark.parametrize("after", [True, False])
def test_exact_accepted_replay_resolves_date_only_withdrawal_without_losing_audit_versions(tmp_path, after):
    tracker = chronology_tracker(tmp_path)
    tracker.record(chronology_event("s1", "registration"))
    approximate = chronology_event("rw", "withdrawn", at=NOW.replace(hour=0), precision="date")
    tracker.record(approximate)
    assert tracker.candidates()[0]["status"] == "chronology_unresolved"
    exact = chronology_event("rw", "withdrawn", at=NOW+timedelta(hours=1 if after else -1))
    tracker.record(exact, observed_at=NOW+timedelta(days=1))
    # Rechecking the old already-saved version must not supersede exact evidence.
    assert tracker.record(approximate, observed_at=NOW+timedelta(days=2)) is False
    candidate = tracker.candidates()[0]
    assert candidate["status"] == ("withdrawn" if after else "filed")
    assert candidate["active"] is not after
    assert candidate["evidence_count"] == 2
    assert len(tracker.evidence()) == 3
    assert candidate["first_filed_at_precision"] == candidate["last_filed_at_precision"] == "second"


def test_date_only_registration_and_amendment_do_not_create_lifecycle_ambiguity(tmp_path):
    tracker = chronology_tracker(tmp_path)
    day = NOW.replace(hour=0)
    tracker.record(chronology_event("s1", "registration", at=day, precision="date"))
    tracker.record(chronology_event("amended", "amendment", at=day, precision="date"))
    assert tracker.candidates()[0]["status"] == "amended"
    assert tracker.candidates()[0]["active"] is True


def test_distinct_official_filing_dates_establish_withdrawal_order(tmp_path):
    tracker = chronology_tracker(tmp_path)
    tracker.record(chronology_event("s1", "registration", source_filing_date=NOW.date()))
    next_day = NOW+timedelta(days=1)
    tracker.record(chronology_event("rw", "withdrawn", at=next_day.replace(hour=0), precision="date",
                                    source_filing_date=next_day.date()))
    candidate = tracker.candidates()[0]
    assert candidate["status"] == "withdrawn" and candidate["active"] is False
    assert candidate["last_filed_at_precision"] == "date"
    assert tracker.summary()["withdrawn_ipos"] == 1


@pytest.mark.parametrize("after", [True, False])
def test_actual_accepted_replay_resolves_order_without_changing_date_precision(tmp_path, after):
    tracker = chronology_tracker(tmp_path)
    tracker.record(chronology_event("s1", "registration", accepted_at=NOW, source_filing_date=NOW.date()))
    approximate = chronology_event("rw", "withdrawn", at=NOW.replace(hour=0), precision="date",
                                   source_filing_date=NOW.date())
    tracker.record(approximate)
    assert tracker.candidates()[0]["status"] == "chronology_unresolved"
    accepted = NOW+timedelta(hours=1 if after else -1)
    exact = approximate.model_copy(update={"accepted_at": accepted})
    tracker.record(exact, observed_at=NOW+timedelta(days=1))
    candidate = tracker.candidates()[0]
    assert candidate["status"] == ("withdrawn" if after else "filed")
    assert candidate["active"] is not after
    assert candidate["first_filed_at_precision"] == "date"
    assert candidate["first_accepted_at"] == accepted.isoformat()
    assert candidate["first_source_filing_date"] == NOW.date().isoformat()
    assert len(tracker.evidence()) == 3


def test_after_hours_atom_and_next_day_catalogue_do_not_imply_withdrawal_order(tmp_path):
    tracker = chronology_tracker(tmp_path)
    tracker.record(chronology_event("s1", "registration", at=datetime(2026, 10, 5, 23, tzinfo=UTC)))
    tracker.record(chronology_event("rw", "withdrawn", at=datetime(2026, 10, 6, tzinfo=UTC),
                                    precision="date", source_filing_date=date(2026, 10, 6)))
    candidate = tracker.candidates()[0]
    assert candidate["status"] == "chronology_unresolved" and candidate["active"] is False


def test_same_official_filing_date_requires_both_actual_accepted_times(tmp_path):
    tracker = chronology_tracker(tmp_path)
    tracker.record(chronology_event("s1", "registration", source_filing_date=NOW.date(), accepted_at=NOW))
    tracker.record(chronology_event("rw", "withdrawn", at=NOW+timedelta(hours=1), source_filing_date=NOW.date()))
    assert tracker.candidates()[0]["status"] == "chronology_unresolved"


def test_accepted_timestamp_requires_timezone_and_normalizes_utc():
    with pytest.raises(ValueError, match="Accepted timestamp must include a timezone"):
        chronology_event("s1", "registration", accepted_at=NOW.replace(tzinfo=None))
    from datetime import timezone
    event = chronology_event("s1", "registration", accepted_at=NOW.astimezone(timezone(timedelta(hours=-4))))
    assert event.accepted_at == NOW and event.accepted_at.tzinfo is UTC


def test_date_only_same_eastern_day_is_ambiguous_across_utc_midnight(tmp_path):
    tracker = chronology_tracker(tmp_path)
    tracker.record(chronology_event("s1", "registration", at=datetime(2026, 10, 6, 1, tzinfo=UTC)))
    tracker.record(chronology_event("rw", "withdrawn", at=NOW.replace(hour=0), precision="date"))
    assert tracker.candidates()[0]["status"] == "chronology_unresolved"


def test_unclassified_same_day_withdrawal_keeps_registration_inactive_and_unconfirmed(tmp_path):
    tracker = chronology_tracker(tmp_path)
    tracker.record(chronology_event("s1", "registration", confirmed=False))
    tracker.record(chronology_event("rw", "withdrawn", at=NOW.replace(hour=0), precision="date", confirmed=False))
    candidate = tracker.candidates()[0]
    assert candidate["status"] == "chronology_unresolved" and candidate["active"] is False
    assert candidate["ipo_confirmed"] is False


def test_single_date_only_prospectus_has_no_ambiguity_against_itself(tmp_path):
    tracker = chronology_tracker(tmp_path)
    tracker.record(chronology_event("prospectus", "prospectus", at=NOW.replace(hour=0), precision="date"))
    assert tracker.candidates()[0]["status"] == "prospectus_filed"
    assert tracker.candidates()[0]["active"] is True


def test_definite_withdrawal_remains_terminal_despite_other_ambiguous_stage(tmp_path):
    tracker = chronology_tracker(tmp_path)
    tracker.record(chronology_event("s1", "registration"))
    tracker.record(chronology_event("effect", "effective", at=NOW.replace(hour=0), precision="date"))
    tracker.record(chronology_event("rw", "withdrawn", at=NOW+timedelta(days=1)))
    assert tracker.candidates()[0]["status"] == "withdrawn"
    assert tracker.candidates()[0]["active"] is False
    assert tracker.summary()["withdrawn_ipos"] == 1
