from datetime import timedelta
import pytest

import hermes_market_monitor as monitor
from test_hermes_market_monitor import setup, NOW, SHA


def config_for(tmp_path, **kwargs):
    config = setup(tmp_path)
    receipt = {"status": "degraded", "main_sha": SHA, "report_completed_at": (NOW-timedelta(hours=3)).isoformat(),
               "newest_run_status": "completed", "newest_run_conclusion": "success", **kwargs}
    monitor.write_json(config.receipt, receipt)
    return config


def test_delayed_schedule_requests_exactly_one_catchup_and_preserves_ledger(tmp_path):
    config = config_for(tmp_path)
    before = config.ledger.read_bytes()
    calls = []
    def github(op):
        calls.append(op)
        if op == "main": return {"sha": SHA}
        assert op == "dispatch"
    monitor.request_due_collection(config, now=NOW, github=github)
    monitor.request_due_collection(config, now=NOW+timedelta(hours=1), github=github)
    assert calls == ["main", "dispatch"] and config.ledger.read_bytes() == before
    assert monitor.read_json(config.receipt)["catchup_claim"]["outcome"] == "requested"


def test_uncertain_dispatch_is_claimed_before_post_and_not_repeated(tmp_path):
    config = config_for(tmp_path)
    def github(op):
        if op == "main": return {"sha": SHA}
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
