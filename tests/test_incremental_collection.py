from datetime import timedelta
from pathlib import Path

from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.health import HealthRegistry
from contract_ipo_monitor.processor import EvidenceProcessor
from contract_ipo_monitor.service import MonitorService
from contract_ipo_monitor.smtp_worker import SMTPWorker
from test_processor_service import NOW, contract, listing


class FakeUSA:
    async def collect(self, *, observed_at):
        return []


class FakeSEC:
    async def collect(self, *, observed_at):
        return []


def _service(tmp_path: Path, db: Database) -> MonitorService:
    settings = Settings(
        database_path=tmp_path / "monitor.db",
        evidence_archive_path=tmp_path / "evidence",
        sec_user_agent="Monitor test@example.com",
        smtp_host="smtp.example.com",
        smtp_sender="alerts@example.com",
        smtp_recipients=("me@example.com",),
        usaspending_initial_lookback_days=7,
        usaspending_overlap_days=2,
    )
    worker = SMTPWorker(db, transport=lambda _message: "id-1", now=lambda: NOW)
    return MonitorService(
        settings=settings,
        db=db,
        health=HealthRegistry(),
        sec_source=FakeSEC(),
        usaspending_source=FakeUSA(),
        smtp_worker=worker,
        now=lambda: NOW,
    )


def test_usaspending_window_uses_initial_lookback_then_durable_overlap(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    service = _service(tmp_path, db)
    assert service._usaspending_start_date(NOW) == (NOW - timedelta(days=7)).date()

    db.update_collector_state("usaspending", success_at=NOW - timedelta(days=1), error=None)
    assert service._usaspending_start_date(NOW) == (NOW - timedelta(days=3)).date()


def test_overlap_retrieval_timestamp_does_not_create_duplicate_contract_version(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    first = contract()
    later_observation = first.model_copy(
        update={
            "retrieved_at": NOW + timedelta(minutes=5),
            "published_at": NOW + timedelta(minutes=5),
        }
    )

    assert processor.ingest_contract(first) == []
    assert processor.ingest_contract(later_observation) == []
    assert db.count("source_records") == 1
    assert db.count("contract_evidence") == 1


def test_changed_contract_payload_with_same_award_is_versioned(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    processor = EvidenceProcessor(db, now=lambda: NOW)
    first = contract()
    changed = first.model_copy(
        update={
            "retrieved_at": NOW + timedelta(minutes=5),
            "obligated_amount": 12_000_000,
            "raw_payload_hash": "contract-updated",
        }
    )

    processor.ingest_contract(first)
    processor.ingest_contract(changed)
    assert db.count("source_records") == 2
    assert db.count("contract_evidence") == 2
