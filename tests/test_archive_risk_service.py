from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from contract_ipo_monitor.archive import EvidenceArchive
from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.health import HealthRegistry
from contract_ipo_monitor.risk import RiskAnalyzer
from contract_ipo_monitor.service import MonitorService
from contract_ipo_monitor.smtp_worker import SMTPWorker
from test_processor_service import NOW, contract, listing


def test_evidence_archive_is_content_addressed_and_immutable(tmp_path: Path):
    archive = EvidenceArchive(tmp_path)
    first = archive.write("sec", "0001", {"form": "S-1"}, observed_at=NOW)
    second = archive.write("sec", "0001", {"form": "S-1"}, observed_at=NOW + timedelta(minutes=1))
    assert first == second
    assert first.read_text().count('"form"') == 1


def test_risk_analyzer_extracts_material_filing_risks():
    findings = RiskAnalyzer().from_filing_text(
        "There is substantial doubt about our ability to continue as a going concern. "
        "Outstanding convertible notes and warrants may cause dilution. We effected a 1-for-20 reverse stock split."
    )
    categories = {item.category for item in findings}
    assert {"going_concern", "dilution", "reverse_split"}.issubset(categories)


class FakeUSA:
    async def collect(self, *, observed_at):
        return [contract()]


class FakeSEC:
    async def collect(self, *, observed_at):
        return [listing()]


@pytest.mark.asyncio
async def test_service_run_once_processes_sources_and_drains_outbox(tmp_path: Path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    sent = []
    worker = SMTPWorker(db, transport=lambda message: sent.append(message) or "id-1", now=lambda: NOW)
    settings = Settings(
        database_path=tmp_path / "monitor.db", evidence_archive_path=tmp_path / "evidence",
        sec_user_agent="Monitor test@example.com", smtp_host="smtp.example.com",
        smtp_sender="alerts@example.com", smtp_recipients=("me@example.com",),
    )
    health = HealthRegistry()
    service = MonitorService(
        settings=settings, db=db, health=health, sec_source=FakeSEC(),
        usaspending_source=FakeUSA(), smtp_worker=worker, now=lambda: NOW,
    )
    summary = await service.run_once()
    assert summary["contracts"] == 1
    assert summary["listing_signals"] == 1
    assert summary["emails_sent"] == 1
    assert len(sent) == 1
    assert health.snapshot()["ready"] is True
