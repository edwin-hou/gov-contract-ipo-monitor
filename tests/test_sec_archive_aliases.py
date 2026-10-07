"""Retrieval URLs are part of SEC archive-manifest identity."""
import gzip
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from contract_ipo_monitor.db import Database
from contract_ipo_monitor.sources.sec import SECCollector, SECNormalizer
from contract_ipo_monitor.sources.sec_issuer import filing_index_filers


FIXTURES = Path(__file__).parent / "fixtures"
ACCESSION = "9999999995-26-003117"
STAMP = "2026-10-01T01:00:00+00:00"
OLD_INDEX = f"https://www.sec.gov/Archives/edgar/data/2058594/{ACCESSION.replace('-', '')}/{ACCESSION}-index.htm"
NEW_INDEX = OLD_INDEX.replace("/2058594/", "/2058261/")
OLD_KEY = ("accession", "document_sha256", "index_sha256")
NEW_KEY = (*OLD_KEY, "document_url", "index_url")


def legacy_archive(path, db_type=Database):
    db = db_type(path)
    db.initialize()
    index = (FIXTURES / "sec_joint_cubebio_effect_index_20260930.htm").read_text(encoding="utf-8")
    document = (FIXTURES / "sec_joint_cubebio_effect_primary_20260930.xml").read_text(encoding="utf-8")
    document_url = SECNormalizer.primary_document_url(OLD_INDEX, index, "EFFECT")
    with db.connect() as conn:
        conn.executescript("""
            CREATE TABLE sec_raw_documents(
                sha256 TEXT PRIMARY KEY, source_url TEXT NOT NULL, gzip_blob BLOB NOT NULL,
                original_bytes INTEGER NOT NULL, archived_at TEXT NOT NULL);
            CREATE TABLE sec_raw_filing_archives(
                accession TEXT NOT NULL, document_sha256 TEXT NOT NULL REFERENCES sec_raw_documents(sha256),
                index_sha256 TEXT NOT NULL REFERENCES sec_raw_documents(sha256),
                document_url TEXT NOT NULL, index_url TEXT NOT NULL, archived_at TEXT NOT NULL,
                PRIMARY KEY(accession,document_sha256,index_sha256));
        """)
        digests = []
        for url, text in ((document_url, document), (OLD_INDEX, index)):
            raw = text.encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            digests.append(digest)
            conn.execute("INSERT INTO sec_raw_documents VALUES(?,?,?,?,?)",
                         (digest, url, gzip.compress(raw, compresslevel=6, mtime=0), len(raw), STAMP))
        manifest = (ACCESSION, *digests, document_url, OLD_INDEX, STAMP)
        conn.execute("INSERT INTO sec_raw_filing_archives VALUES(?,?,?,?,?,?)", manifest)
    return db, index, document, manifest


def snapshot(db):
    with db.connect() as conn:
        manifests = [tuple(row) for row in conn.execute("SELECT * FROM sec_raw_filing_archives ORDER BY accession,document_url,index_url")]
        documents = [tuple(row) for row in conn.execute("SELECT * FROM sec_raw_documents ORDER BY sha256")]
        columns = conn.execute("PRAGMA table_info(sec_raw_filing_archives)").fetchall()
        key = tuple(row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"])
    return manifests, documents, key


@pytest.mark.asyncio
async def test_legacy_receipts_migrate_losslessly_and_identical_real_source_bytes_retain_both_co_filer_aliases(tmp_path):
    db, index, document, original = legacy_archive(tmp_path / "monitor.db")
    before = snapshot(db)
    assert before[2] == OLD_KEY
    collector = SECCollector(None, db=db, request_interval=0)
    migrated = snapshot(db)
    assert migrated[:2] == before[:2] and migrated[2] == NEW_KEY
    SECCollector(None, db=db)
    assert snapshot(db) == migrated  # Repeated startup is an exact no-op.
    entry = {"accession": ACCESSION, "form_type": "EFFECT", "cik": "2058594", "issuer_name": "Cubebio Co., Ltd",
             "filed_at": datetime(2026, 9, 30, 12, tzinfo=UTC), "source_url": OLD_INDEX,
             "catalogue_filers": filing_index_filers(index, ACCESSION)}
    observed = datetime.now(UTC)
    collector.catalog.capture([entry], observed_at=observed)
    collector.catalog.record_issuer_review(entry, {"status": "unresolved", "reason": "Shared EFFECT retains both paired filers.",
        "processed_document_at": STAMP, "raw_archive_path": f"sqlite:sec_raw_documents/{original[1]}",
        "raw_payload_hash": original[1], "source_url": original[3], "index_sha256": original[2], "index_url": OLD_INDEX})
    collector.mark_processed(ACCESSION, observed_at=observed)
    assert collector.is_processed(ACCESSION)
    collector.catalog.capture([entry], observed_at=observed, requeue=True)
    calls = []
    async def fetch(url, **kwargs):
        calls.append(url)
        return index if url.endswith("-index.htm") else document
    collector._text = fetch
    event, signal = await collector.collect_entry(collector.catalog.pending_entries("EFFECT")[0])
    assert event is None and signal is None and calls[0] == NEW_INDEX
    collector.mark_processed(ACCESSION, observed_at=observed)
    manifests, documents, key = snapshot(db)
    assert len(manifests) == 2 and original in manifests
    assert {row[4] for row in manifests} == {OLD_INDEX, NEW_INDEX}
    assert {row[:3] for row in manifests} == {original[:3]}
    assert documents == before[1] and key == NEW_KEY
    assert collector.is_processed(ACCESSION)
    assert collector.requeue_invalid_catalog_receipts() == 0
    restarted = SECCollector(None, db=db)
    assert restarted.is_processed(ACCESSION) and restarted.catalog.pending_entries("EFFECT") == []
    with db.connect() as conn:
        review = json.loads(conn.execute("SELECT entry_json FROM sec_pending_filings").fetchone()[0])["issuer_review"]
        assert review["index_url"] == NEW_INDEX
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_manifest_key_preserves_each_url_dimension_and_deduplicates_exact_retrieval(tmp_path):
    db, index, document, original = legacy_archive(tmp_path / "monitor.db")
    collector = SECCollector(None, db=db)
    alternate_document = original[3].replace("/2058261/", "/2058594/")
    for document_url, index_url in ((alternate_document, OLD_INDEX), (original[3], NEW_INDEX), (original[3], NEW_INDEX)):
        collector._archive_documents(accession=ACCESSION, index_url=index_url, index_html=index,
                                     document_url=document_url, document=document)
    manifests, _documents, key = snapshot(db)
    assert len(manifests) == 3 and key == NEW_KEY
    assert {(row[3], row[4]) for row in manifests} == {(original[3], OLD_INDEX), (alternate_document, OLD_INDEX), (original[3], NEW_INDEX)}
    with db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO sec_raw_filing_archives VALUES(?,?,?,?,?,?)",
                         (ACCESSION, "f" * 64, original[2], original[3], NEW_INDEX, STAMP))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


class RenameFailureDatabase(Database):
    @contextmanager
    def transaction(self):
        with super().transaction() as conn:
            class Connection:
                def execute(self, sql, *args):
                    if sql.startswith("ALTER TABLE sec_raw_filing_archives_url_migration"):
                        raise sqlite3.OperationalError("Injected final migration rename failure")
                    return conn.execute(sql, *args)
            yield Connection()


def test_migration_failure_after_table_drop_rolls_back_original_schema_receipts_and_bytes(tmp_path):
    db, _index, _document, _manifest = legacy_archive(tmp_path / "monitor.db", RenameFailureDatabase)
    before = snapshot(db)
    with pytest.raises(sqlite3.OperationalError, match="Injected final migration rename failure"):
        SECCollector(None, db=db)
    assert snapshot(db) == before
    with db.connect() as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='sec_raw_filing_archives_url_migration'").fetchone() is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
