"""A failed SAM phase preserves completed contracts without advancing success."""
import asyncio
from datetime import timedelta

import pytest

from contract_ipo_monitor.service import SAMSource
from test_processor_service import contract
from test_usaspending_partial import NOW, service


class EmptyUSA:
    async def collect(self, **kwargs):
        return []


class SAMPhases:
    fail = True
    slow = False

    async def collect(self, *, deleted, **kwargs):
        self.partial_records = []
        if not deleted:
            self.partial_records = [contract().model_copy(update={"source": "sam", "retrieved_at": NOW,
                                                                  "published_at": NOW, "award_date": NOW.date()})]
            return list(self.partial_records)
        if not self.fail:
            return []
        self.partial_records = [contract().model_copy(update={"source": "sam", "source_record_id": "deleted-record",
            "status": "deleted", "deleted": True, "retrieved_at": NOW, "published_at": NOW,
            "award_date": NOW.date()})]
        if self.slow:
            await asyncio.Event().wait()
        raise RuntimeError("SAM deletion page unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", [False, True])
async def test_sam_adapter_preserves_both_phases_on_later_failure(slow):
    collector = SAMPhases()
    collector.slow = slow
    source = SAMSource(collector)
    with pytest.raises((RuntimeError, TimeoutError)):
        await asyncio.wait_for(source.collect(observed_at=NOW), timeout=.03)
    assert [item.source_record_id for item in source.partial_records] == ["a", "deleted-record"]


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", [False, True])
async def test_run_and_periodic_sam_keep_partial_evidence_and_old_watermark(tmp_path, slow):
    collector = SAMPhases()
    collector.slow = slow
    subject = service(tmp_path, EmptyUSA(), timeout=.03 if slow else 600)
    subject.sam_source = SAMSource(collector)
    previous = NOW - timedelta(days=1)
    subject.db.update_collector_state("sam", cursor="preceding-complete-scan", success_at=previous)
    summary = await subject.run_once()
    assert summary["sam_records"] == 2
    assert subject.db.count("contract_evidence") == 2
    assert subject.last_report["health"]["collectors"]["sam"]["ok"] is False
    with subject.db.connect() as conn:
        row = conn.execute("SELECT * FROM collector_state WHERE name='sam'").fetchone()
    assert row["last_success_at"] == previous.isoformat()
    assert row["cursor"] == "preceding-complete-scan"
    assert row["last_error"]
    # The daemon uses the same path, so it retains the same partial observations.
    with pytest.raises((RuntimeError, TimeoutError)):
        await subject._poll_sam()
    assert subject.db.count("contract_evidence") == 2
    collector.fail = False
    await subject._poll_sam()
    assert subject.last_sam_count == 1
    with subject.db.connect() as conn:
        recovered = conn.execute("SELECT * FROM collector_state WHERE name='sam'").fetchone()
    assert recovered["last_success_at"] == NOW.isoformat()
    assert recovered["last_error"] is None
