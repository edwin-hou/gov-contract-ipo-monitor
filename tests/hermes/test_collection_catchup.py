from datetime import timedelta
import pytest

import hermes_market_monitor as monitor
from test_hermes_market_monitor import setup, report, FakeGitHub, NOW, SHA


def config_for(tmp_path, **kwargs):
    config = setup(tmp_path, baseline=report(NOW-timedelta(hours=3)))
    receipt = {"status": "degraded", "main_sha": SHA, "report_completed_at": (NOW-timedelta(hours=3)).isoformat(),
               "newest_run_status": "completed", "newest_run_conclusion": "success", **kwargs}
    monitor.write_json(config.receipt, receipt)
    return config


class CatchupGitHub(FakeGitHub):
    def __init__(self, current=None, **kwargs):
        super().__init__(current or report(), **kwargs)
        self.dispatches = 0

    def __call__(self, operation, *arguments):
        if operation == "dispatch":
            self.calls.append((operation, arguments))
            self.dispatches += 1
            return None
        return super().__call__(operation, *arguments)


def test_return_after_long_offline_gap_can_request_one_verified_collection(tmp_path):
    config = setup(tmp_path, baseline=report(NOW-timedelta(hours=8)))
    baseline = config.ledger.read_bytes()
    github = CatchupGitHub(run_id=100)
    assert "needs attention" in monitor.poll(config, now=NOW, github=github)
    assert monitor.read_json(config.receipt)["failure_code"] == "report_stale"
    monitor.request_due_collection(config, now=NOW, github=github)
    monitor.request_due_collection(config, now=NOW, github=github)
    assert github.dispatches == 1
    assert config.ledger.read_bytes() == baseline
    receipt = monitor.read_json(config.receipt)
    assert receipt["status"] == "error" and receipt["failure_code"] == "report_stale"
    assert receipt["catchup_claim"]["outcome"] == "requested"


@pytest.mark.parametrize("status,conclusion", [("queued", None), ("in_progress", None), ("completed", "failure")])
def test_catchup_rechecks_live_collection_instead_of_dispatching_behind_a_new_job(tmp_path, status, conclusion):
    config = config_for(tmp_path)
    github = CatchupGitHub(status=status, conclusion=conclusion)
    monitor.request_due_collection(config, now=NOW, github=github)
    assert github.dispatches == 0


def test_catchup_requires_exact_sealed_report_bytes_even_if_receipt_claims_old_data(tmp_path):
    config = config_for(tmp_path)
    ledger = monitor.read_json(config.ledger)
    from pathlib import Path
    Path(ledger["source_report"]).write_text("{}", encoding="utf-8")
    github = CatchupGitHub()
    monitor.request_due_collection(config, now=NOW, github=github)
    assert github.dispatches == 0


def test_delayed_schedule_requests_exactly_one_catchup_and_preserves_ledger(tmp_path):
    config = config_for(tmp_path)
    before = config.ledger.read_bytes()
    github = CatchupGitHub()
    monitor.request_due_collection(config, now=NOW, github=github)
    monitor.request_due_collection(config, now=NOW+timedelta(hours=1), github=github)
    assert [operation for operation, _ in github.calls] == ["main", "workflow", "runs", "dispatch"]
    assert config.ledger.read_bytes() == before
    assert monitor.read_json(config.receipt)["catchup_claim"]["outcome"] == "requested"


def test_uncertain_dispatch_is_claimed_before_post_and_not_repeated(tmp_path):
    config = config_for(tmp_path)
    preflight = CatchupGitHub()
    def github(op, *arguments):
        if op != "dispatch": return preflight(op, *arguments)
        assert monitor.read_json(config.receipt)["catchup_claim"]["outcome"] == "dispatch_unknown"
        raise monitor.CheckError("github_check_failed", "ambiguous")
    monitor.request_due_collection(config, now=NOW, github=github)
    monitor.request_due_collection(config, now=NOW, github=lambda op: pytest.fail("repeated"))


@pytest.mark.parametrize("fields", [{"newest_run_status": "queued"}, {"newest_run_status": "in_progress"},
                                      {"newest_run_conclusion": "failure"}, {"status": "error"},
                                      {"report_completed_at": (NOW-timedelta(minutes=119)).isoformat()}])
def test_active_failed_or_fresh_sources_do_not_start_more_work(tmp_path, fields):
    config = config_for(tmp_path, **fields)
    monitor.request_due_collection(config, now=NOW, github=lambda op: pytest.fail("dispatch/read not needed"))


def test_main_changed_withholds_dispatch(tmp_path):
    config = config_for(tmp_path)
    monitor.request_due_collection(config, now=NOW, github=lambda op: {"sha": "b"*40})
    assert monitor.read_json(config.receipt)["catchup_claim"]["outcome"] == "main_changed_no_dispatch"


def test_stale_report_error_does_not_hide_a_different_nonrecoverable_error(tmp_path):
    config = config_for(tmp_path, status="error", failure_code="report_hash_mismatch")
    monitor.request_due_collection(config, now=NOW, github=lambda op: pytest.fail("not recoverable"))


def test_catchup_uses_sealed_completion_time_and_never_renews_it_from_receipt(tmp_path):
    config = setup(tmp_path)  # Exact report is only one hour old.
    monitor.write_json(config.receipt, {"status": "degraded", "main_sha": SHA,
                                       "report_completed_at": (NOW-timedelta(hours=3)).isoformat()})
    monitor.request_due_collection(config, now=NOW, github=lambda op: pytest.fail("sealed report remains fresh"))


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested", "pending"])
def test_any_unfinished_current_main_monitor_blocks_catchup_even_if_newest_is_complete(tmp_path, status):
    config = config_for(tmp_path)

    class Queue(CatchupGitHub):
        def __call__(self, operation, *arguments):
            value = super().__call__(operation, *arguments)
            if operation == "runs":
                return value + [{**value[0], "id": 99, "status": status, "conclusion": None}]
            return value

    github = Queue()
    monitor.request_due_collection(config, now=NOW, github=github)
    assert github.dispatches == 0


def test_catchup_does_not_claim_or_dispatch_when_live_run_read_fails(tmp_path):
    config = config_for(tmp_path)
    preflight = CatchupGitHub()
    def github(op, *arguments):
        if op == "runs": raise monitor.CheckError("github_check_failed", "read failed")
        return preflight(op, *arguments)
    monitor.request_due_collection(config, now=NOW, github=github)
    assert "catchup_claim" not in monitor.read_json(config.receipt)
    assert preflight.dispatches == 0
