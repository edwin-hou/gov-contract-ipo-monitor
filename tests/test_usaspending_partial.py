"""An incomplete scan must retain observed records without claiming coverage."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from contract_ipo_monitor.sources.http import PermanentHTTPError, TransientHTTPError
from contract_ipo_monitor.sources.usaspending import (
    IncompleteUSAspendingCollection,
    USAspendingCollector,
    USAspendingRecipientResolver,
)
from contract_ipo_monitor.config import Settings
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.health import HealthRegistry
from contract_ipo_monitor.research import report_markdown
from contract_ipo_monitor.service import MonitorService


NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


def award(identity):
    return {
        "Award ID": f"AWARD-{identity}",
        "generated_internal_id": f"RECORD-{identity}",
        "Recipient Name": "Example Manufacturing Inc.",
        "Recipient UEI": "ABC123456789",
        "Award Amount": 10000,
        "Start Date": "2026-10-05",
        "Awarding Agency": "Example Department",
    }


def response(rows, *, more=False):
    return {"results": rows, "page_metadata": {"hasNext": more, "next": 2 if more else None}}


class Client:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    async def request_json(self, *_args, **_kwargs):
        self.calls.append(dict(_kwargs.get("json", {})))
        value = next(self.outcomes)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_page_budget_retains_all_completed_records_and_incomplete_reason():
    collector = USAspendingCollector(Client([response([award(1), award(2)], more=True)]))
    with pytest.raises(IncompleteUSAspendingCollection, match="refusing to truncate silently") as raised:
        await collector.collect(observed_at=NOW, max_pages=1)
    assert [record.source_record_id for record in raised.value.records] == ["RECORD-1", "RECORD-2"]
    assert raised.value.page == 1
    assert raised.value.reason == str(raised.value)
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert len(collector.partial_records) == 2


@pytest.mark.asyncio
async def test_later_http_failure_retains_first_page_and_original_cause():
    failure = TransientHTTPError("HTTP 503: Service Unavailable")
    collector = USAspendingCollector(Client([response([award(1)], more=True), failure]))
    with pytest.raises(IncompleteUSAspendingCollection, match="HTTP 503") as raised:
        await collector.collect(observed_at=NOW)
    assert [record.award_id for record in raised.value.records] == ["AWARD-1"]
    assert raised.value.page == 2
    assert raised.value.__cause__ is failure


@pytest.mark.asyncio
async def test_invalid_later_row_retains_only_successfully_validated_evidence():
    invalid = award(2)
    del invalid["Start Date"]
    collector = USAspendingCollector(Client([response([award(1), invalid])]))
    with pytest.raises(IncompleteUSAspendingCollection, match="missing its source award date") as raised:
        await collector.collect(observed_at=NOW)
    assert [record.source_record_id for record in raised.value.records] == ["RECORD-1"]
    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_deadline_cancellation_retains_records_without_redefining_cancellation():
    class SlowClient:
        async def request_json(self, *_args, **kwargs):
            if kwargs["json"]["page"] == 1:
                return response([award(1)], more=True)
            await asyncio.Event().wait()

    collector = USAspendingCollector(SlowClient())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(collector.collect(observed_at=NOW), timeout=.05)
    assert [record.award_id for record in collector.partial_records] == ["AWARD-1"]


@pytest.mark.asyncio
async def test_next_attempt_resets_partial_records_without_mutating_previous_exception():
    collector = USAspendingCollector(Client([response([award(1)], more=True), response([])]))
    with pytest.raises(IncompleteUSAspendingCollection) as raised:
        await collector.collect(observed_at=NOW, max_pages=1)
    assert await collector.collect(observed_at=NOW) == []
    assert collector.partial_records == []
    assert [record.award_id for record in raised.value.records] == ["AWARD-1"]


@pytest.mark.asyncio
async def test_first_page_failure_keeps_original_error_type_and_clears_prior_partial():
    failure = TransientHTTPError("HTTP 503: Service Unavailable")
    collector = USAspendingCollector(Client([response([award(1)]), failure]))
    assert len(await collector.collect(observed_at=NOW)) == 1
    with pytest.raises(TransientHTTPError) as raised:
        await collector.collect(observed_at=NOW)
    assert raised.value is failure
    assert collector.partial_records == []


@pytest.mark.asyncio
async def test_duplicate_rows_do_not_create_duplicate_partial_observations():
    collector = USAspendingCollector(Client([response([award(1), award(1)], more=True)]))
    with pytest.raises(IncompleteUSAspendingCollection) as raised:
        await collector.collect(observed_at=NOW, max_pages=1)
    assert len(raised.value.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", (None, {}, {"hasNext": None}, {"hasNext": 0}, {"hasNext": "false"}, []))
async def test_missing_or_invalid_pagination_receipt_retains_awards_without_claiming_completion(metadata):
    payload = {"results": [award(1)]}
    if metadata is not None:
        payload["page_metadata"] = metadata
    collector = USAspendingCollector(Client([payload]))
    with pytest.raises(IncompleteUSAspendingCollection, match="pagination") as raised:
        await collector.collect(observed_at=NOW)
    assert [record.source_record_id for record in raised.value.records] == ["RECORD-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("next_page", (3, True, 2.9, "2.5", "not-a-page"))
async def test_next_page_must_be_the_immediate_integer_successor(next_page):
    payload = response([award(1)], more=True)
    payload["page_metadata"]["next"] = next_page
    collector = USAspendingCollector(Client([payload, response([])]))
    with pytest.raises(IncompleteUSAspendingCollection, match="pagination") as raised:
        await collector.collect(observed_at=NOW)
    assert raised.value.page == 1
    assert len(raised.value.records) == 1


@pytest.mark.asyncio
async def test_response_page_mismatch_cannot_be_used_as_a_completion_receipt():
    payload = response([award(1)])
    payload["page_metadata"]["page"] = 2
    collector = USAspendingCollector(Client([payload]))
    with pytest.raises(IncompleteUSAspendingCollection, match="pagination"):
        await collector.collect(observed_at=NOW)


@pytest.mark.asyncio
async def test_documented_search_after_cursor_avoids_capped_total_false_completion():
    calls = []

    class CappedEndpoint:
        async def request_json(self, *_args, **kwargs):
            payload = dict(kwargs["json"])
            calls.append(payload)
            cursor = payload.get("last_record_unique_id")
            if payload["page"] == 1:
                identity, more = 1, True
            elif cursor is None:
                # Same source defect as the 10,000-record cap: an offset page
                # says false while a sequential cursor still has more rows.
                identity, more = 2, False
            else:
                identity = cursor + 1
                more = identity < 3
            return {"results": [award(identity)], "page_metadata": {
                "page": payload["page"], "hasNext": more,
                "last_record_unique_id": identity if more else None,
                "last_record_sort_value": f"2026-10-0{6-identity}" if more else "None",
            }}

    collector = USAspendingCollector(CappedEndpoint())
    records = await collector.collect(observed_at=NOW, limit=1)
    assert [record.award_id for record in records] == ["AWARD-1", "AWARD-2", "AWARD-3"]
    assert [call.get("last_record_unique_id") for call in calls] == [None, 1, 2]
    assert [call.get("last_record_sort_value") for call in calls] == [None, "2026-10-05", "2026-10-04"]


@pytest.mark.asyncio
async def test_legacy_full_page_false_requires_a_bounded_tail_probe():
    collector = USAspendingCollector(Client([response([award(1)]), response([award(2)]), response([])]))
    records = await collector.collect(observed_at=NOW, limit=1)
    assert [record.award_id for record in records] == ["AWARD-1", "AWARD-2"]


@pytest.mark.asyncio
async def test_full_page_false_at_budget_cannot_be_claimed_as_complete():
    collector = USAspendingCollector(Client([response([award(1)])]))
    with pytest.raises(IncompleteUSAspendingCollection, match="refusing to truncate silently") as raised:
        await collector.collect(observed_at=NOW, limit=1, max_pages=1)
    assert len(raised.value.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity,value", ((True, "2026-10-05"), (-1, "2026-10-05"), (1, None), (None, "2026-10-05"), (1, [])))
async def test_malformed_cursor_never_silently_falls_back_to_offset_paging(identity, value):
    payload = response([award(1)], more=True)
    payload["page_metadata"].update(last_record_unique_id=identity, last_record_sort_value=value)
    collector = USAspendingCollector(Client([payload]))
    with pytest.raises(IncompleteUSAspendingCollection, match="cursor"):
        await collector.collect(observed_at=NOW)
    assert len(collector.partial_records) == 1


@pytest.mark.asyncio
async def test_repeated_cursor_preserves_new_rows_without_claiming_progress():
    pages = [response([award(1)], more=True), response([award(2)], more=True)]
    for page in pages:
        page["page_metadata"].update(last_record_unique_id=1, last_record_sort_value="2026-10-05")
    pages[1]["page_metadata"]["next"] = 3
    collector = USAspendingCollector(Client(pages))
    with pytest.raises(IncompleteUSAspendingCollection, match="cursor"):
        await collector.collect(observed_at=NOW, limit=1)
    assert len(collector.partial_records) == 2


@pytest.mark.asyncio
async def test_recipient_identity_must_be_unique_across_all_source_pages():
    source = Client([
        response([{"name": "Example Manufacturing Inc.", "uei": "FIRST"}], more=True),
        response([{"name": "Example Manufacturing Corporation", "uei": "SECOND"}]),
    ])
    assert await USAspendingRecipientResolver(source).resolve_uei("Example Manufacturing Inc.") is None


@pytest.mark.asyncio
async def test_incomplete_recipient_lookup_never_caches_a_positive_or_missing_identity():
    source = Client([
        response([{"name": "Example Manufacturing Inc.", "uei": "FIRST"}], more=True),
        response([{"name": "Example Manufacturing Inc.", "uei": "VERIFIED"}]),
    ])
    resolver = USAspendingRecipientResolver(source, max_pages=1)
    with pytest.raises(RuntimeError, match="page budget"):
        await resolver.resolve_uei("Example Manufacturing Inc.")
    assert await resolver.resolve_uei("Example Manufacturing Inc.") == "VERIFIED"


@pytest.mark.asyncio
async def test_transient_cursor_outage_uses_same_endpoint_offset_scan_with_tail_proof_and_warning():
    first = response([award(1)], more=True)
    first["page_metadata"].update(last_record_unique_id=1, last_record_sort_value="2026-10-05")
    source = Client([first, TransientHTTPError("HTTP 503"), response([award(2)]), response([]), response([])])
    collector = USAspendingCollector(source)
    records = await collector.collect(observed_at=NOW, limit=1)
    assert [record.award_id for record in records] == ["AWARD-1", "AWARD-2"]
    assert [call["page"] for call in source.calls] == [1, 2, 2, 3]
    assert [call.get("last_record_unique_id") for call in source.calls] == [None, 1, None, None]
    assert len(collector.pagination_warnings) == 1
    assert "HTTP 503" in collector.pagination_warnings[0] and "tail verification" in collector.pagination_warnings[0]
    assert await collector.collect(observed_at=NOW) == []
    assert collector.pagination_warnings == []


@pytest.mark.asyncio
async def test_cursor_auth_denial_never_switches_request_shape_or_claims_completion():
    first = response([award(1)], more=True)
    first["page_metadata"].update(last_record_unique_id=1, last_record_sort_value="2026-10-05")
    denial = PermanentHTTPError(403, "Forbidden")
    source = Client([first, denial])
    collector = USAspendingCollector(source)
    with pytest.raises(IncompleteUSAspendingCollection, match="HTTP 403") as failure:
        await collector.collect(observed_at=NOW, limit=1)
    assert len(source.calls) == 2 and source.calls[1]["last_record_unique_id"] == 1
    assert failure.value.__cause__ is denial
    assert len(collector.partial_records) == 1 and collector.pagination_warnings == []


def service(tmp_path, source, *, timeout=600):
    class EmptySEC:
        async def collect(self, **_kwargs):
            return []

    db = Database(tmp_path / "monitor.db")
    db.initialize()
    settings = Settings(
        database_path=tmp_path / "monitor.db",
        evidence_archive_path=tmp_path / "evidence",
        sec_user_agent="Test monitor test@example.invalid",
        markets_enabled=False,
        discourse_enabled=False,
    ).model_copy(update={"source_timeout_seconds": timeout})
    return MonitorService(
        settings=settings, db=db, health=HealthRegistry(), sec_source=EmptySEC(),
        usaspending_source=source, smtp_worker=None, now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_run_retains_partial_awards_and_preserves_previous_success_watermark(tmp_path):
    failure = TransientHTTPError("HTTP 503: Service Unavailable")
    source = USAspendingCollector(Client([response([award(1)], more=True), failure]))
    subject = service(tmp_path, source)
    previous = NOW - timedelta(days=2)
    subject.db.update_collector_state("usaspending", cursor="previous-complete-window", success_at=previous)
    counts = await subject.run_once()
    assert counts["contracts"] == 1
    assert subject.db.count("contract_evidence") == 1
    assert subject.last_report["history"]["contract_records"] == 1
    assert subject.last_report["health"]["collectors"]["usaspending"]["ok"] is False
    assert subject.last_report["health"]["ready"] is False
    with subject.db.connect() as conn:
        state = conn.execute("SELECT * FROM collector_state WHERE name='usaspending'").fetchone()
    assert state["last_success_at"] == previous.isoformat()
    assert state["cursor"] == "previous-complete-window"
    assert "HTTP 503" in state["last_error"]


@pytest.mark.asyncio
async def test_run_deadline_retains_completed_awards_but_never_marks_scan_success(tmp_path):
    class SlowClient:
        async def request_json(self, *_args, **kwargs):
            if kwargs["json"]["page"] == 1:
                return response([award(1)], more=True)
            await asyncio.Event().wait()

    subject = service(tmp_path, USAspendingCollector(SlowClient()), timeout=.05)
    counts = await subject.run_once()
    assert counts["contracts"] == 1
    assert subject.db.count("contract_evidence") == 1
    with subject.db.connect() as conn:
        state = conn.execute("SELECT * FROM collector_state WHERE name='usaspending'").fetchone()
    assert state["last_success_at"] is None
    assert state["cursor"] is None
    assert "TimeoutError" in state["last_error"]


@pytest.mark.asyncio
async def test_repeated_partial_run_versions_do_not_duplicate_retained_awards(tmp_path):
    failure = TransientHTTPError("HTTP 503: Service Unavailable")
    source = USAspendingCollector(Client([
        response([award(1)], more=True), failure,
        response([award(1)], more=True), failure,
    ]))
    subject = service(tmp_path, source)
    await subject.run_once()
    await subject.run_once()
    assert subject.db.count("contract_evidence") == 1
    assert subject.db.count("source_records") == 1
    assert subject.last_report["history"]["contract_records"] == 1


@pytest.mark.asyncio
async def test_completed_offset_recovery_saves_warning_without_losing_success_watermark_and_next_clean_run_resets_it(tmp_path):
    first = response([award(1)], more=True)
    first["page_metadata"].update(last_record_unique_id=1, last_record_sort_value="2026-10-05")
    collector = USAspendingCollector(Client([
        first, TransientHTTPError("HTTP 503"), response([award(2)]), response([]),
    ]))
    subject = service(tmp_path, collector)
    counts = await subject.run_once()
    report = subject.research.latest_run()
    assert counts["contracts"] == 2 and subject.db.count("contract_evidence") == 2
    assert report["status"] == "degraded"
    assert report["health"]["collectors"]["usaspending"]["ok"] is True
    assert report["health"]["ready"] is True
    receipt = report["usaspending_collection"]
    assert receipt["status"] == "degraded" and receipt["scan_completed"] is True
    assert receipt["error"] is None and len(receipt["warnings"]) == 1
    assert "HTTP 503" in receipt["warnings"][0]
    coverage = next(item for item in report["coverage"] if item["source"] == "usaspending")
    assert coverage["status"] == "degraded" and coverage["limitations"] == receipt["warnings"]
    with subject.db.connect() as conn:
        state = conn.execute("SELECT * FROM collector_state WHERE name='usaspending'").fetchone()
    assert state["last_success_at"] == NOW.isoformat() and state["cursor"] == NOW.date().isoformat()
    assert state["last_error"] is None
    readable = report_markdown(report)
    assert "USAspending pagination" in readable and "HTTP 503" in readable
    assert "The award scan completed" in readable
    await subject.run_once()
    clean = subject.research.latest_run()
    assert clean["status"] == "ok" and clean["usaspending_collection"]["warnings"] == []
    assert clean["usaspending_collection"]["status"] == "ok"
    assert "HTTP 503" not in report_markdown(clean)
    assert len(receipt["warnings"]) == 1


@pytest.mark.asyncio
async def test_failed_offset_recovery_saves_warning_and_error_without_advancing_success(tmp_path):
    first = response([award(1)], more=True)
    first["page_metadata"].update(last_record_unique_id=1, last_record_sort_value="2026-10-05")
    collector = USAspendingCollector(Client([
        first, TransientHTTPError("HTTP 503"), TransientHTTPError("HTTP 502"),
    ]))
    subject = service(tmp_path, collector)
    previous = NOW - timedelta(days=2)
    subject.db.update_collector_state("usaspending", cursor="previous-complete-window", success_at=previous)
    await subject.run_once()
    report = subject.research.latest_run()
    receipt = report["usaspending_collection"]
    assert receipt["status"] == "error" and receipt["scan_completed"] is False
    assert len(receipt["warnings"]) == 1 and "HTTP 502" in receipt["error"]
    assert report["health"]["collectors"]["usaspending"]["ok"] is False
    with subject.db.connect() as conn:
        state = conn.execute("SELECT * FROM collector_state WHERE name='usaspending'").fetchone()
    assert state["last_success_at"] == previous.isoformat() and state["cursor"] == "previous-complete-window"
    assert subject.db.count("contract_evidence") == 1
    readable = report_markdown(report)
    assert "HTTP 503" in readable and "HTTP 502" in readable
    assert "The award scan completed" not in readable
