"""SAM pagination failures retain evidence without asserting source completeness."""
import asyncio
from datetime import UTC, datetime

import pytest

from contract_ipo_monitor.sources.http import TransientHTTPError
from contract_ipo_monitor.sources.sam import SAMCollector


NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def award(identity):
    return {
        "contractId": {"piid": f"AWARD-{identity}"},
        "awardDetails": {"dateSigned": "10/17/2026", "awardeeData": {
            "awardeeHeader": {"awardeeName": "Example Manufacturing Inc."},
        }},
    }


class Client:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    async def request_json(self, *_args, **_kwargs):
        value = next(self.outcomes)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_later_http_failure_retains_completed_sam_page():
    failure = TransientHTTPError("HTTP 503")
    collector = SAMCollector(Client([{"awardSummary": [award(1)], "totalRecords": 2}, failure]), "key")
    with pytest.raises(RuntimeError, match="HTTP 503"):
        await collector.collect(observed_at=NOW, last_modified_start=NOW.date(), limit=1)
    assert [record.award_id for record in collector.partial_records] == ["AWARD-1"]


@pytest.mark.asyncio
async def test_later_invalid_sam_row_retains_only_completed_observations():
    invalid = award(2)
    invalid["awardDetails"].pop("dateSigned")
    collector = SAMCollector(Client([{"awardSummary": [award(1), invalid], "totalRecords": 2}]), "key")
    with pytest.raises(RuntimeError, match="source date signed"):
        await collector.collect(observed_at=NOW, last_modified_start=NOW.date())
    assert [record.award_id for record in collector.partial_records] == ["AWARD-1"]


@pytest.mark.asyncio
async def test_sam_deadline_keeps_completed_pages_and_cancellation_type():
    class SlowClient:
        async def request_json(self, *_args, **kwargs):
            if kwargs["params"]["offset"] == 0:
                return {"awardSummary": [award(1)], "totalRecords": 2}
            await asyncio.Event().wait()

    collector = SAMCollector(SlowClient(), "key")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(collector.collect(observed_at=NOW, last_modified_start=NOW.date(), limit=1), .03)
    assert len(collector.partial_records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("total", (True, -1, 1.9, "1.9", "not-a-count"))
async def test_sam_total_is_an_exact_nonnegative_count(total):
    collector = SAMCollector(Client([{"awardSummary": [award(1)], "totalRecords": total}]), "key")
    with pytest.raises(RuntimeError, match="totalRecords"):
        await collector.collect(observed_at=NOW, last_modified_start=NOW.date())
    assert len(collector.partial_records) == 1


@pytest.mark.asyncio
async def test_sam_total_cannot_claim_fewer_rows_than_the_source_returned():
    collector = SAMCollector(Client([{"awardSummary": [award(1), award(2)], "totalRecords": 1}]), "key")
    with pytest.raises(RuntimeError, match="totalRecords"):
        await collector.collect(observed_at=NOW, last_modified_start=NOW.date())
    assert len(collector.partial_records) == 2


@pytest.mark.asyncio
async def test_repeated_sam_page_is_not_mistaken_for_reaching_total():
    collector = SAMCollector(Client([
        {"awardSummary": [award(1)], "totalRecords": 2},
        {"awardSummary": [award(1)], "totalRecords": 2},
    ]), "key")
    with pytest.raises(RuntimeError, match="pagination"):
        await collector.collect(observed_at=NOW, last_modified_start=NOW.date(), limit=1)
    assert len(collector.partial_records) == 1


@pytest.mark.asyncio
async def test_sam_total_change_preserves_rows_but_withholds_completion():
    collector = SAMCollector(Client([
        {"awardSummary": [award(1)], "totalRecords": 2},
        {"awardSummary": [award(2)], "totalRecords": 3},
    ]), "key")
    with pytest.raises(RuntimeError, match="totalRecords"):
        await collector.collect(observed_at=NOW, last_modified_start=NOW.date(), limit=1)
    assert [record.award_id for record in collector.partial_records] == ["AWARD-1", "AWARD-2"]


@pytest.mark.asyncio
async def test_next_sam_call_clears_partial_without_mutating_saved_failure():
    collector = SAMCollector(Client([
        {"awardSummary": [award(1)], "totalRecords": 2},
        {"awardSummary": [], "totalRecords": 0},
    ]), "key")
    with pytest.raises(RuntimeError) as failure:
        await collector.collect(observed_at=NOW, last_modified_start=NOW.date(), limit=1, max_pages=1)
    saved = failure.value.records
    assert await collector.collect(observed_at=NOW, last_modified_start=NOW.date()) == []
    assert collector.partial_records == [] and len(saved) == 1
