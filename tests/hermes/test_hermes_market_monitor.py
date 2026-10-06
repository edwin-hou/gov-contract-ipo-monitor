from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

import hermes_market_monitor as monitor

NOW = datetime(2026, 10, 5, 23, tzinfo=UTC)
SHA = "a" * 40
FINANCIAL = "https://issuer.example.org/results"
PRICE = "https://prices.example.org/history/TEST"


def report(at=NOW - timedelta(minutes=10), *, entry=100, action="conditional_buy"):
    return {
        "completed_at": at.isoformat(), "status": "degraded", "market_restore_errors": [],
        "trade_ideas": [{"symbol": "TEST", "company": "Test Issuer", "exchange": "NASDAQ", "currency": "USD",
                         "action": action, "price_as_of": "2026-10-02", "entry": entry if action == "conditional_buy" else None,
                         "invalidation": 90 if action != "wait" else None, "target": entry + 20 if action == "conditional_buy" else None,
                         "fundamentals": {"source_url": FINANCIAL, "period_end": "2026-06-30", "reported_at": "2026-08-01",
                                          "period_type": "quarter", "accounting_standard": "US GAAP", "reporting_currency": "USD"},
                         "evidence_urls": [FINANCIAL, PRICE], "conditions": ["Confirm a fresh quote before considering an entry"],
                         "reasons": ["Historical screen passed"], "risks": ["Price gaps can increase losses"], "limitations": [],
                         "world_context": [{"title": "Publisher-reported shipping event", "publisher": "Example News",
                                            "source_url": "https://news.example.org/story", "published_at": at.isoformat(), "themes": ["supply_chain"]}],
                         "horizon": "5–15 trading sessions"},
                        {"symbol": "0700.HK", "company": "Tencent", "exchange": "HKEX", "currency": "HKD", "action": "wait",
                         "price_as_of": None, "entry": None, "target": None, "invalidation": None,
                         "conditions": ["Comparable native-currency benchmark is missing"], "risks": [], "limitations": [], "evidence_urls": []}],
        "price_coverage": [{"symbol": "TEST", "status": "ok", "source": "provider", "currency": "USD", "as_of": "2026-10-02",
                            "source_url": PRICE, "observed_at": (at - timedelta(seconds=5)).isoformat()}],
        "world_coverage_ready": True,
        "world_coverage": [{"source": "world_news:" + str(i), "status": "ok", "source_url": f"https://news{i}.example.org/rss",
                            "observed_at": (at - timedelta(seconds=5)).isoformat()} for i in range(2)],
        "health": {"collectors": {"sec": {"ok": True, "disabled": False, "last_success_at": at.isoformat()}}},
        "ipos": [], "listed_companies": [], "historic_coverage_gaps": [],
        "coverage": [{"source": "reddit", "status": "error", "error": "HTTP 403"}],
    }


class FakeGitHub:
    def __init__(self, current, *, run_id=101, status="completed", conclusion="success", sha=SHA, branch="main"):
        self.report, self.run_id, self.status, self.conclusion = current, run_id, status, conclusion
        self.sha, self.branch, self.calls = sha, branch, []

    def __call__(self, operation, *arguments):
        self.calls.append((operation, arguments))
        if operation == "main":
            return {"branch": "main", "sha": SHA}
        if operation == "workflow":
            return {"name": "Monitor", "path": monitor.WORKFLOW_PATH, "state": "active"}
        if operation == "runs":
            return [{"id": self.run_id, "name": "Monitor", "head_sha": self.sha, "head_branch": self.branch,
                     "status": self.status, "conclusion": self.conclusion}]
        if operation == "run":
            return {"id": self.run_id, "path": monitor.WORKFLOW_PATH, "head_sha": self.sha, "head_branch": self.branch,
                    "status": self.status, "conclusion": self.conclusion, "run_started_at": (NOW-timedelta(hours=1)).isoformat(),
                    "updated_at": NOW.isoformat(), "html_url": f"https://github.com/edwin-hou/gov-contract-ipo-monitor/actions/runs/{self.run_id}"}
        if operation == "artifacts":
            return [{"id": 999, "name": "ipo-monitor-report", "expired": False, "size_in_bytes": 1000}]
        if operation == "download":
            path = Path(arguments[1]) / "latest.json"
            monitor.write_json(path, self.report)
            return None
        raise AssertionError(operation)


def setup(tmp_path, *, baseline=None):
    config = monitor.Config(tmp_path)
    baseline = baseline or report(NOW - timedelta(hours=1))
    source = config.outputs / "baseline" / "latest.json"
    monitor.write_json(source, baseline)
    ledger = {"last_report_at": baseline["completed_at"], "last_notified_run": "100", "source_report": str(source),
              "report_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "ideas": monitor.idea_snapshot(baseline)}
    monitor.write_json(config.ledger, ledger)
    return config


def test_threshold_is_cumulative_against_preserved_baseline_and_date_alone_is_quiet():
    old = monitor.idea_snapshot(report())
    new = deepcopy(old)
    new["TEST"].update(entry=100.99, price_as_of="2026-10-05")
    assert monitor.material_changes(old, new) == []
    new["TEST"]["entry"] = 101
    assert monitor.material_changes(old, new)[0]["symbol"] == "TEST"
    new["TEST"].update(entry=99)
    assert monitor.material_changes(old, new)


def test_fresh_report_retains_collection_price_policy_across_utc_midnight():
    data = report(datetime(2026, 10, 5, 22, 14, tzinfo=UTC))
    assert monitor.validate_report(data, datetime(2026, 10, 6, 0, 30, tzinfo=UTC))
    with pytest.raises(monitor.CheckError):
        monitor.validate_report(data, datetime(2026, 10, 6, 5, tzinfo=UTC))


def test_holiday_and_incomplete_current_session_use_venue_calendar():
    at = datetime(2026, 11, 27, 15, tzinfo=UTC)  # Friday before the early close; Thursday was closed.
    data = report(at)
    data["trade_ideas"][0]["price_as_of"] = "2026-11-25"
    data["price_coverage"][0]["as_of"] = "2026-11-25"
    assert monitor.validate_report(data, at)
    for incomplete in ("2026-11-26", "2026-11-27"):
        data["trade_ideas"][0]["price_as_of"] = incomplete
        data["price_coverage"][0]["as_of"] = incomplete
        with pytest.raises(monitor.CheckError, match="future-dated or stale"):
            monitor.validate_report(data, at)


def test_new_actionable_state_action_loss_and_currency_identity_are_material():
    old = monitor.idea_snapshot(report())
    new = deepcopy(old)
    new["NEW"] = {**new["TEST"], "action": "wait"}
    assert monitor.material_changes(old, new) == []
    new["NEW"]["action"] = "conditional_buy"
    assert monitor.material_changes(old, new)[0]["symbol"] == "NEW"
    new = deepcopy(old)
    new["TEST"]["action"] = "wait"
    assert "changed from conditional_buy to wait" in monitor.material_changes(old, new)[0]["reasons"][0]
    new = deepcopy(old)
    new["TEST"]["currency"] = "HKD"
    assert "currency changed" in monitor.material_changes(old, new)[0]["reasons"][0]


def test_initial_notified_report_is_quiet_preserves_exact_ledger_and_avoids_download(tmp_path):
    config = setup(tmp_path)
    before = config.ledger.read_bytes()
    github = FakeGitHub(report(NOW - timedelta(hours=1)), run_id=100)
    assert monitor.poll(config, now=NOW, github=github) == ""
    assert config.ledger.read_bytes() == before
    assert not any(operation == "download" for operation, arguments in github.calls)
    receipt = monitor.read_json(config.receipt)
    assert receipt["outcome"] == "no_newer_report" and receipt["status"] == "degraded"
    assert receipt["completed_at"] == NOW.isoformat() and receipt["artifact_downloaded"] is False


def test_new_actual_artifact_no_material_diff_and_duplicate_checks_preserve_ledger(tmp_path):
    config = setup(tmp_path)
    before = config.ledger.read_bytes()
    github = FakeGitHub(report())
    assert monitor.poll(config, now=NOW, github=github) == ""
    assert config.ledger.read_bytes() == before
    receipt = monitor.read_json(config.receipt)
    assert receipt["verified_run"] == "101" and receipt["artifact_downloaded"] is True and receipt["report_sha256"]
    count = len([item for item in github.calls if item[0] == "download"])
    assert monitor.poll(config, now=NOW + timedelta(minutes=1), github=github) == ""
    assert len([item for item in github.calls if item[0] == "download"]) == count
    assert config.ledger.read_bytes() == before


def test_successful_quiet_poll_clears_prior_runtime_failure_details(tmp_path):
    config = setup(tmp_path)
    monitor.write_json(config.receipt, {"failure_code": "local_pipeline_failed", "internal_error_type": "ModuleNotFoundError",
                                       "missing_module": "pydantic_core._pydantic_core", "message": "Prior failure",
                                       "pending_reason": "Prior queue", "last_failure_code": "local_pipeline_failed"})
    assert monitor.poll(config, now=NOW, github=FakeGitHub(report(NOW-timedelta(hours=1)), run_id=100)) == ""
    receipt = monitor.read_json(config.receipt)
    assert receipt["status"] == "degraded" and receipt["last_failure_code"] is None
    assert not {"failure_code", "internal_error_type", "missing_module", "message", "pending_reason"}.intersection(receipt)


def test_material_update_delivers_digest_with_readback_then_commits_ledger_and_keeps_waits_visible(tmp_path):
    config = setup(tmp_path)
    notice = monitor.poll(config, now=NOW, github=FakeGitHub(report(entry=102)))
    receipt, ledger = monitor.read_json(config.receipt), monitor.read_json(config.ledger)
    digest = Path(receipt["digest_path"]).read_text(encoding="utf-8")
    assert "saved locally" in notice and ledger["last_notified_run"] == "101"
    assert ledger["ideas"]["TEST"]["entry"] == 102 and ledger["local_delivery"]["sha256"] == receipt["digest_sha256"]
    for value in ("TEST", "NASDAQ", "USD", "2026-10-02", "102.00", "90.00", "122.00", "5–15 trading sessions",
                  "0700.HK", "HKEX", "HKD", "Wait", "Comparable native-currency benchmark is missing", "Primary financial results",
                  "Example News", "supply_chain", "Price gaps can increase losses", "Authorized email delivery"):
        assert value in digest


def test_local_digest_failure_never_consumes_baseline(tmp_path, monkeypatch):
    config = setup(tmp_path)
    before = config.ledger.read_bytes()
    def fail(path, text):
        raise monitor.CheckError("local_delivery_failed", "Local delivery failed.")
    monkeypatch.setattr(monitor, "publish", fail)
    with pytest.raises(monitor.CheckError):
        monitor.poll(config, now=NOW, github=FakeGitHub(report(entry=102)))
    assert config.ledger.read_bytes() == before


@pytest.mark.parametrize("mutate,code", [
    (lambda p: p.update(completed_at=(NOW-timedelta(hours=7)).isoformat()), "report_stale"),
    (lambda p: p.update(completed_at=(NOW+timedelta(seconds=1)).isoformat()), "report_stale"),
    (lambda p: p.update(price_coverage=[]), "price_evidence_invalid"),
    (lambda p: p["price_coverage"][0].update(currency="HKD"), "price_evidence_invalid"),
    (lambda p: p["price_coverage"][0].update(observed_at=(NOW-timedelta(hours=7)).isoformat()), "price_evidence_stale"),
    (lambda p: p["trade_ideas"][0].update(fundamentals=None), "financial_evidence_invalid"),
    (lambda p: p["trade_ideas"][0]["fundamentals"].update(reported_at="2026-10-06"), "financial_evidence_stale"),
    (lambda p: p["trade_ideas"][0]["fundamentals"].update(period_end="2025-06-30", reported_at="2025-08-01"), "financial_evidence_stale"),
    (lambda p: p["trade_ideas"][0].update(entry=89), "setup_levels_invalid"),
    (lambda p: p["trade_ideas"][0].update(evidence_urls=[]), "source_evidence_missing"),
    (lambda p: p.update(world_coverage_ready=False), "world_evidence_missing"),
])
def test_stale_missing_or_inconsistent_source_evidence_fails_closed(mutate, code):
    data = report()
    mutate(data)
    with pytest.raises(monitor.CheckError) as error:
        monitor.validate_report(data, NOW)
    assert error.value.code == code


def test_failure_notice_is_deduped_receipts_remain_fresh_and_baseline_is_preserved(tmp_path):
    config = setup(tmp_path)
    before = config.ledger.read_bytes()
    broken = report()
    broken["trade_ideas"][0]["fundamentals"] = None
    github = FakeGitHub(broken)
    assert "needs attention" in monitor.poll(config, now=NOW, github=github)
    assert monitor.poll(config, now=NOW + timedelta(minutes=1), github=github) == ""
    receipt = monitor.read_json(config.receipt)
    assert receipt["status"] == "error" and receipt["completed_at"] == (NOW+timedelta(minutes=1)).isoformat()
    assert config.ledger.read_bytes() == before


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("completed", "failure")])
def test_newer_unfinished_or_failed_run_is_pending_and_never_relabels_baseline_as_new(tmp_path, status, conclusion):
    config = setup(tmp_path)
    before = config.ledger.read_bytes()
    github = FakeGitHub(report(entry=102), status=status, conclusion=conclusion)
    assert monitor.poll(config, now=NOW, github=github) == ""
    receipt = monitor.read_json(config.receipt)
    assert receipt["outcome"] == "pending_report" and receipt["newest_run"] == "101"
    assert not any(operation == "download" for operation, arguments in github.calls)
    assert config.ledger.read_bytes() == before


@pytest.mark.parametrize("prior_verified", [False, True])
def test_failed_collection_cannot_rebind_changed_cached_report_bytes(tmp_path, prior_verified):
    config = setup(tmp_path)
    if prior_verified:
        assert monitor.poll(config, now=NOW, github=FakeGitHub(report())) == ""
        binding = monitor.read_json(config.receipt)
    else:
        binding = monitor.read_json(config.ledger)
    baseline = config.ledger.read_bytes()
    path = Path(binding["source_report"])
    altered = monitor.read_json(path)
    altered["trade_ideas"][0]["entry"] = 103
    monitor.write_json(path, altered)
    assert "needs attention" in monitor.poll(config, now=NOW, github=FakeGitHub(report(), run_id=102, conclusion="failure"))
    receipt = monitor.read_json(config.receipt)
    assert receipt["status"] == "error" and receipt["failure_code"] == "report_hash_mismatch"
    assert config.ledger.read_bytes() == baseline
    if prior_verified:
        assert receipt["report_sha256"] == binding["report_sha256"]
    else:
        assert "report_sha256" not in receipt


@pytest.mark.parametrize("prior_verified", [False, True])
def test_missing_cached_report_hash_requires_verified_hosted_artifact(tmp_path, prior_verified):
    config = setup(tmp_path)
    if prior_verified:
        assert monitor.poll(config, now=NOW, github=FakeGitHub(report())) == ""
        binding_path = config.receipt
    else:
        binding_path = config.ledger
    binding = monitor.read_json(binding_path)
    binding.pop("report_sha256")
    monitor.write_json(binding_path, binding)
    before = config.ledger.read_bytes()
    assert "needs attention" in monitor.poll(config, now=NOW, github=FakeGitHub(report(), run_id=102, conclusion="failure"))
    receipt = monitor.read_json(config.receipt)
    assert receipt["failure_code"] == "report_hash_missing" and receipt["status"] == "error"
    assert "report_sha256" not in receipt
    assert config.ledger.read_bytes() == before


def test_reused_baseline_cannot_be_reauthorized_by_run_identity_without_saved_hash(tmp_path):
    config = setup(tmp_path)
    ledger = monitor.read_json(config.ledger)
    ledger.pop("report_sha256")
    monitor.write_json(config.ledger, ledger)
    before = config.ledger.read_bytes()
    github = FakeGitHub(report(), run_id=100)
    assert "needs attention" in monitor.poll(config, now=NOW, github=github)
    assert monitor.read_json(config.receipt)["failure_code"] == "report_hash_missing"
    assert not any(operation == "download" for operation, _ in github.calls)
    assert config.ledger.read_bytes() == before


def test_conflicting_receipt_and_ledger_hashes_never_choose_a_fresh_authority(tmp_path):
    config = setup(tmp_path)
    assert "Material IPO/trade" in monitor.poll(config, now=NOW, github=FakeGitHub(report(entry=102)))
    receipt = monitor.read_json(config.receipt)
    ledger = monitor.read_json(config.ledger)
    assert ledger["source_report"] == receipt["source_report"]
    ledger["report_sha256"] = "b" * 64
    monitor.write_json(config.ledger, ledger)
    before = config.ledger.read_bytes()
    assert "needs attention" in monitor.poll(config, now=NOW, github=FakeGitHub(report(), run_id=102, conclusion="failure"))
    failed = monitor.read_json(config.receipt)
    assert failed["failure_code"] == "report_hash_mismatch" and failed["report_sha256"] == receipt["report_sha256"]
    assert config.ledger.read_bytes() == before


@pytest.mark.parametrize("status,conclusion", [("queued", None), ("in_progress", None), ("completed", "failure")])
def test_newer_failed_or_pending_collection_does_not_hide_unconsumed_successful_report(tmp_path, status, conclusion):
    config = setup(tmp_path, baseline=report(NOW - timedelta(hours=7)))

    class History(FakeGitHub):
        def __call__(self, operation, *arguments):
            if operation == "runs":
                success = super().__call__(operation, *arguments)[0]
                return [{**success, "id": 102, "status": status, "conclusion": conclusion}, success]
            return super().__call__(operation, *arguments)

    current = report(entry=102)
    github = History(current)
    assert "Material IPO/trade" in monitor.poll(config, now=NOW, github=github)
    receipt = monitor.read_json(config.receipt)
    assert receipt["newest_run"] == "102" and receipt["newest_run_status"] == status
    assert receipt["newest_run_conclusion"] == conclusion and receipt["verified_run"] == "101"
    assert receipt["report_completed_at"] == current["completed_at"]
    assert receipt["artifact_downloaded"] is True and receipt["status"] == "degraded"
    assert "failure_code" not in receipt and "unsuccessful or unfinished" in receipt["pending_reason"]
    assert [args for operation, args in github.calls if operation == "run"] == [("101",)]
    assert [args for operation, args in github.calls if operation == "download"] == [(999, config.outputs / "run-101")]
    assert monitor.read_json(config.ledger)["last_notified_run"] == "101"
    assert monitor.poll(config, now=NOW + timedelta(minutes=1), github=github) == ""
    assert sum(operation == "download" for operation, _ in github.calls) == 1
    repeated = monitor.read_json(config.receipt)
    assert repeated["newest_run"] == "102" and repeated["verified_run"] == "101"


def test_failed_current_main_cannot_fall_back_to_success_on_another_commit(tmp_path):
    config = setup(tmp_path, baseline=report(NOW - timedelta(hours=7)))
    before = config.ledger.read_bytes()

    class WrongCommit(FakeGitHub):
        def __call__(self, operation, *arguments):
            if operation == "runs":
                success = super().__call__(operation, *arguments)[0]
                return [{**success, "id": 102, "conclusion": "failure"}, {**success, "head_sha": "b" * 40}]
            return super().__call__(operation, *arguments)

    github = WrongCommit(report(entry=102))
    assert "needs attention" in monitor.poll(config, now=NOW, github=github)
    receipt = monitor.read_json(config.receipt)
    assert receipt["newest_run"] == "102" and receipt["failure_code"] == "report_stale"
    assert not any(operation in {"run", "artifacts", "download"} for operation, _ in github.calls)
    assert config.ledger.read_bytes() == before


def test_preceding_successful_run_still_requires_valid_actual_artifact(tmp_path):
    config = setup(tmp_path, baseline=report(NOW - timedelta(hours=7)))
    before = config.ledger.read_bytes()

    class InvalidArtifact(FakeGitHub):
        def __call__(self, operation, *arguments):
            if operation == "runs":
                success = super().__call__(operation, *arguments)[0]
                return [{**success, "id": 102, "conclusion": "failure"}, success]
            if operation == "artifacts":
                return []
            return super().__call__(operation, *arguments)

    assert "needs attention" in monitor.poll(config, now=NOW, github=InvalidArtifact(report(entry=102)))
    receipt = monitor.read_json(config.receipt)
    assert receipt["newest_run"] == "102" and receipt["failure_code"] == "report_artifact_missing"
    assert config.ledger.read_bytes() == before


def test_wrong_main_identity_and_missing_artifact_cannot_publish(tmp_path):
    config = setup(tmp_path)
    github = FakeGitHub(report(entry=102), branch="feature")
    assert monitor.poll(config, now=NOW, github=github) == ""
    assert monitor.read_json(config.receipt)["outcome"] == "pending_report"
    class Missing(FakeGitHub):
        def __call__(self, operation, *arguments):
            return [] if operation == "artifacts" else super().__call__(operation, *arguments)
    assert "needs attention" in monitor.poll(config, now=NOW, github=Missing(report(entry=102)))
    assert monitor.read_json(config.ledger)["last_notified_run"] == "100"


def test_snapshot_maintenance_is_separately_deduped_without_consuming_trade_baseline(tmp_path):
    config = setup(tmp_path)
    before = config.ledger.read_bytes()
    data = report()
    data["listed_companies"] = [{"symbol": "REVIEW", "financials": {"source_kind": "issuer", "source_url": FINANCIAL,
                                                                   "reported_at": "2026-07-01", "period_end": "2026-06-30", "period_type": "quarter"}}]
    assert "saved locally" in monitor.poll(config, now=NOW, github=FakeGitHub(data))
    assert config.ledger.read_bytes() == before
    assert monitor.read_json(config.receipt)["maintenance_notified"]
    assert monitor.poll(config, now=NOW+timedelta(minutes=1), github=FakeGitHub(data)) == ""


def test_maintenance_can_be_due_on_reused_baseline_without_new_artifact(tmp_path):
    data = report(NOW-timedelta(hours=1))
    data["listed_companies"] = [{"symbol": "REVIEW", "financials": {"source_kind": "issuer", "source_url": FINANCIAL,
                                                                   "reported_at": "2026-07-01", "period_end": "2026-06-30", "period_type": "quarter"}}]
    config = setup(tmp_path, baseline=data)
    before = config.ledger.read_bytes()
    github = FakeGitHub(data, run_id=100)
    assert "approaches expiry" in monitor.poll(config, now=NOW, github=github)
    assert not any(operation == "download" for operation, arguments in github.calls)
    assert config.ledger.read_bytes() == before
    assert monitor.poll(config, now=NOW+timedelta(minutes=1), github=github) == ""


def test_helper_never_exposes_credential_traces_and_uses_hidden_no_shell_python(tmp_path, monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        class Result:
            returncode = 1
            stdout = ""
            stderr = "Authorization: Bearer SECRET; traceback"
        return Result()
    monkeypatch.setattr(monitor.subprocess, "run", run)
    with pytest.raises(monitor.CheckError) as error:
        monitor.GitHubHelper(monitor.Config(tmp_path))("main")
    assert "SECRET" not in str(error.value)
    assert calls[0][1]["shell"] is False
    assert Path(calls[0][0][0]) == tmp_path / "work" / "monitor-venv" / "Scripts" / "python.exe"


def test_untrusted_digest_text_and_evidence_links_are_escaped():
    data = report(entry=102)
    data["trade_ideas"][0]["conditions"] = ["[click](javascript:alert(1)) <script>x</script> | row"]
    data["trade_ideas"][0]["evidence_urls"] = ["https://user:SECRET@example.org", "javascript:alert(1)"]
    text = monitor.digest_text(data, {"html_url": "https://github.com/example/run"}, [], [], [])
    assert "SECRET" not in text and "<script>" not in text and "[click](javascript:" not in text
    assert "\\[click\\]" in text and "&lt;script&gt;" in text
