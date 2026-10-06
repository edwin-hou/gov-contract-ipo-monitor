"""Local-only integration tests: no model, quote, mail or gateway network calls."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import sys

WORK = Path(__file__).resolve().parent
SOURCE = WORK / "gov-contract-ipo-monitor" / "src"
for path in (WORK, SOURCE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pytest

import hermes_market_monitor as monitor
import market_notifications as notifications
from contract_ipo_monitor.analyst import AnalystStore, DEFAULT_MODEL
from contract_ipo_monitor.gmail_delivery import UnknownDelivery, message_content_sha256
from contract_ipo_monitor.notifications import EmailOutbox, report_message
from contract_ipo_monitor.quotes import CurrentQuote, quote_from_dict, quote_to_dict


NOW = datetime(2026, 10, 5, 22, tzinfo=UTC)
FINANCIAL_URL = "https://investors.micron.com/quarterly-results"
HISTORY_URL = "https://api.nasdaq.com/api/quote/MU/historical?assetclass=stocks"


def idea(symbol="MU", *, action="conditional_buy"):
    quote = CurrentQuote(symbol, symbol, "yahoo", f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        100., "USD", "NMS", "America/New_York", NOW.replace(hour=20, second=2), NOW,
        quote_type="session_close", market_phase="closed", delay_seconds=0, status="fresh",
        session_date=NOW.date(), timestamp_precision="second", provider_timestamp="1791230402",
        timestamp_basis="last_trade", provider_market_status="Closed",
        regular_session_start=NOW.replace(hour=13, minute=30), regular_session_end=NOW.replace(hour=20))
    return {"symbol": symbol, "company": "Simulated issuer " + symbol, "exchange": "NASDAQ", "currency": "USD",
        "action": action, "entry": 101., "invalidation": 95., "target": 113., "risk_reward": 2.,
        "price_as_of": "2026-10-05", "current_quote": quote_to_dict(quote),
        "strategy": {"scope": "paper_reference_only", "assumed_position": False, "holdings_status": "not_provided",
            "maximum_entry": 102., "setup_valid_through": "2026-10-12",
            "entry_trigger": {"price": 101., "verification": "Verify the trigger and a fresh broker quote."},
            "stop": {"price": 95.}, "target": {"price": 113.},
            "time_exit": {"review_after_sessions": 5, "exit_after_sessions": 15},
            "risk_budget": {"currency": "USD", "quantity": None, "planned_risk_per_share": 6.}},
        "fundamentals": {"source_url": FINANCIAL_URL, "period_end": "2026-06-30", "reported_at": "2026-08-01",
            "period_type": "quarter", "accounting_standard": "US GAAP", "currency": "USD"},
        "evidence_briefs": [
            {"id": "financial", "evidence_type": "primary_financial", "claim": "Reported revenue increased and net income was positive.",
             "meaning": "Supports the business thesis; it does not establish fair valuation.", "source_urls": [FINANCIAL_URL]},
            {"id": "trend", "evidence_type": "completed_price_history", "claim": "The completed-bar trend passed the configured screen.",
             "meaning": "A conditional breakout requires price and cost confirmation.", "source_urls": [HISTORY_URL]}],
        "evidence_urls": [FINANCIAL_URL, HISTORY_URL], "conditions": ["Confirm the fresh broker quote and entry cap."],
        "risks": ["Growth may already be priced in."], "limitations": ["No valuation or fill is established."],
        "world_context": [], "sentiment": {"label": "unknown", "score": None}, "horizon": "5–15 sessions"}


def report(*ideas):
    ideas = list(ideas) or [idea()]
    return {"completed_at": NOW.isoformat(), "status": "degraded", "market_restore_errors": [], "trade_ideas": ideas,
        "holdings": [], "listed_discovery": [], "ipos": [], "listed_companies": [], "historic_coverage_gaps": [],
        "price_coverage": [{"symbol": x["symbol"], "status": "ok", "currency": "USD", "as_of": "2026-10-05",
            "source_url": HISTORY_URL, "observed_at": NOW.isoformat()} for x in ideas],
        "world_coverage_ready": True,
        "world_coverage": [{"source": "world_news:"+str(n), "status": "ok", "source_url": f"https://news{n}.example.org/rss",
                            "observed_at": NOW.isoformat()} for n in range(2)],
        "health": {"collectors": {"sec": {"ok": True, "disabled": False, "last_success_at": NOW.isoformat()}}}}


def configured(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    box = EmailOutbox(work / "mail.sqlite3")
    config = {"enabled": True, "sender": notifications.TARGET, "recipient": notifications.TARGET, "holdings": [],
        "outbox_path": str(box.path), "hermes_home": str(tmp_path / "unused-hermes"),
        "notification_policy": "ai_approved_only", "initial_report_requested": True, "initial_event_key": "initial-report",
        "analyst": {"enabled": True, "model": DEFAULT_MODEL, "reasoning_effort": "medium", "daily_limit": 4}}
    return config, box, work


def refreshed(data, now):
    data = deepcopy(data)
    for item in data["trade_ideas"]:
        if item.get("current_quote"):
            item["current_quote"]["observed_at"] = now.isoformat()
    return data


class Model:
    def __init__(self, decisions=None):
        self.decisions, self.calls = decisions or {}, []

    def __call__(self, request):
        self.calls.append(deepcopy(request))
        assert request["model"] == DEFAULT_MODEL and request["reasoning_effort"] == "medium"
        assert request["payload"]["account_context"]["holdings"] == []
        verdicts = [{"symbol": x["symbol"], "decision": self.decisions.get(x["symbol"], "notify"),
                     "evidence_ids": ["financial", "trend"],
                     "rationale": "A conditional opportunity if the trigger and broker cost checks pass.",
                     "counterargument": "Growth may already be reflected in the price."}
                    for x in request["payload"]["candidates"]]
        return {"status": "ok", "provider": "openai-codex", "model": DEFAULT_MODEL,
                "text": json.dumps({"decisions": verdicts}), "usage": {}}


def stage(config, box, work, data, *, model=None, now=NOW, refresh=refreshed, finished=None):
    times = iter((now, finished or now))
    return notifications.stage_ai_notifications(config, box, {}, data, now, work=work,
        infer=model or Model(), refresh=refresh, clock=lambda: next(times))


def attached(row):
    message = BytesParser(policy=policy.default).parsebytes(bytes(row["raw_message"]))
    data = next(json.loads(part.get_payload(decode=True)) for part in message.iter_attachments()
                if part.get_filename() == "research-report.json")
    return message, data


class SimulatedTransport:
    """Provide matching local receipts, with no real POST or lookup."""
    def __init__(self, *, ambiguous=False):
        self.deliveries, self.lookups, self.ambiguous = [], [], ambiguous

    def receipt(self, raw, recipient, rfc822_id):
        return {"gmail_message_id": "simulated-message", "thread_id": "simulated-thread", "delivered_label": "INBOX",
            "recipient": recipient, "sender": notifications.TARGET, "rfc822_id": rfc822_id,
            "provider_accepted_at": NOW.isoformat(), "verified_at": NOW.isoformat(),
            "content_sha256": message_content_sha256(raw), "raw_content_sha256": hashlib.sha256(raw).hexdigest(),
            "readback_raw_sha256": hashlib.sha256(raw).hexdigest()}

    def deliver(self, raw, recipient, rfc822_id):
        self.deliveries.append((raw, recipient, rfc822_id))
        if self.ambiguous:
            raise UnknownDelivery("Simulated ambiguous POST")
        return self.receipt(raw, recipient, rfc822_id)

    def reconcile(self, rfc822_id, recipient, raw, **kwargs):
        self.lookups.append((rfc822_id, recipient))
        return self.receipt(raw, recipient, rfc822_id)


def test_only_ai_approved_candidates_queue_with_empty_holdings_and_brief_evidence(tmp_path):
    config, box, work = configured(tmp_path)
    model = Model({"MU": "notify", "NVDA": "wait"})
    queued, result = stage(config, box, work, report(idea(), idea("NVDA"), idea("AVGO", action="reduce_if_owned")), model=model)
    assert len(queued) == 1 and result["send_allowed"] is True
    message, data = attached(box.get(queued[0]))
    assert [x["symbol"] for x in data["trade_ideas"]] == ["MU"] and data["holdings"] == []
    assert data["notification_scope"] == "ai_approved_only" and data["trade_ideas"][0]["ai_review"]["decision"] == "notify"
    assert data["trade_ideas"][0]["current_quote"]["freshness"]["executable"] is False
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "Holdings on file: none" in body and "No purchase, fill, position or order is assumed" in body
    assert "Evidence in plain language" in body and "Counterargument:" in body
    assert FINANCIAL_URL in body and HISTORY_URL in body
    assert "NVDA |" not in body and "AVGO |" not in body and len(body) < 5000
    assert len(model.calls) == 1 and box.plans() == {}


def test_model_wait_and_no_candidates_never_queue(tmp_path):
    config, box, work = configured(tmp_path)
    model = Model({"MU": "wait"})
    queued, result = stage(config, box, work, report(), model=model)
    assert queued == [] and result["send_allowed"] is False and result["status"] == "complete"
    data = report(idea(action="wait"))
    queued, result = stage(config, box, work, data, model=lambda request: pytest.fail("Model called without candidates"))
    assert queued == [] and result["status"] == "no_candidates" and not result["model_called"]
    assert box.states() == {}


@pytest.mark.parametrize("event", ["initial-report", "discovery:new", "reference:target", "local-notice:change", "maintenance:failure"])
def test_initial_discovery_reference_and_maintenance_pending_mail_loses_ai_policy_authorization(tmp_path, event):
    config, box, work = configured(tmp_path)
    data = report(idea(action="wait"))
    raw = report_message(data, sender=config["sender"], recipient=config["recipient"], event_key=event, created_at=NOW)
    identifier = box.enqueue(event, raw, config["recipient"], now=NOW, expires_at=NOW+timedelta(hours=1))
    queued, result = stage(config, box, work, data, model=lambda request: pytest.fail("Nonopportunity model call"))
    assert queued == [] and result["send_allowed"] is False
    row = box.get(identifier)
    assert row["status"] == "cancelled" and row["error_code"] == "superseded_by_ai_only_policy"
    assert row["raw_message"] == raw and box.plans() == {}


@pytest.mark.parametrize("elapsed_seconds", [300, 301])
def test_quote_expiring_during_model_latency_never_queues(tmp_path, elapsed_seconds):
    config, box, work = configured(tmp_path)
    model = Model()
    queued, result = stage(config, box, work, report(), model=model, finished=NOW+timedelta(seconds=elapsed_seconds))
    assert len(model.calls) == 1 and queued == [] and box.states() == {}
    assert result["status"] == "quotes_expired_during_analysis" and not result["send_allowed"]


@pytest.mark.parametrize("reason", ["unavailable", "above_cap", "below_stop"])
def test_old_pending_ai_mail_loses_authorization_on_fresh_failure_or_invalid_price(tmp_path, reason):
    config, box, work = configured(tmp_path)
    model = Model()
    first, _ = stage(config, box, work, report(), model=model)
    immutable = box.get(first[0])["raw_message"]
    def fail_or_change(data, now):
        data = refreshed(data, now)
        if reason == "unavailable": data["trade_ideas"][0]["current_quote"] = {}
        else: data["trade_ideas"][0]["current_quote"]["price"] = 103. if reason == "above_cap" else 94.
        return data
    queued, result = stage(config, box, work, report(), model=model, now=NOW+timedelta(minutes=1), refresh=fail_or_change)
    assert queued == [] and not result["send_allowed"] and len(model.calls) == 1
    row = box.get(first[0])
    assert row["status"] == "cancelled" and row["error_code"] == "ai_authority_changed"
    assert row["raw_message"] == immutable


def test_crash_after_verified_outbox_send_backfills_notice_before_duplicate_authorization(tmp_path):
    config, box, work = configured(tmp_path)
    model = Model()
    first, _ = stage(config, box, work, report(), model=model)
    transport = SimulatedTransport()
    assert box.deliver_once(transport, now=NOW)["status"] == "sent"
    store = AnalystStore(work / "market-analyst.sqlite3")
    assert store.last_notice("MU") is None  # crash before the separate ledger write
    queued, result = stage(config, box, work, report(), model=model, now=NOW+timedelta(minutes=1))
    assert queued == [] and not result["send_allowed"] and store.last_notice("MU") is not None
    assert box.states() == {"sent": 1} and len(model.calls) == 1 and len(transport.deliveries) == 1
    assert box.deliver_once(transport, now=NOW+timedelta(minutes=1)) is None
    previous = store.last_notice("MU")
    notifications.record_ai_deliveries(box, work, first)
    notifications.record_ai_deliveries(box, work, first)
    assert store.last_notice("MU") == previous


@pytest.mark.parametrize("unsent_state", ["cancelled", "expired"])
def test_fresh_recovery_regenerates_only_definitely_unsent_mail_on_same_hosted_report_without_model_repeat(tmp_path, unsent_state):
    config, box, work = configured(tmp_path)
    data, model = report(), Model()
    original_ids, original_result = stage(config, box, work, data, model=model)
    original_row = box.get(original_ids[0])
    if unsent_state == "cancelled":
        def failed_refresh(report, now):
            value = refreshed(report, now)
            value["trade_ideas"][0]["current_quote"] = {}
            return value
        invalidated, result = stage(config, box, work, data, model=model, now=NOW+timedelta(minutes=1), refresh=failed_refresh)
        assert invalidated == [] and not result["send_allowed"]
        recovery_at = NOW+timedelta(minutes=2)
    else:
        recovery_at = NOW+timedelta(minutes=6)
        # Lease expiration before any provider POST proves this event was unsent.
        assert box.lease(recovery_at) is None
    assert box.get(original_ids[0])["status"] == unsent_state
    assert box.get(original_ids[0])["attempts"] == 0
    recovered, result = stage(config, box, work, data, model=model, now=recovery_at)
    assert len(recovered) == 1 and recovered[0] != original_ids[0]
    assert result["send_allowed"] is True and result["notification"] == "queued"
    assert result["review_identity"] == original_result["review_identity"] and len(model.calls) == 1
    original_after, fresh_row = box.get(original_ids[0]), box.get(recovered[0])
    assert original_after["status"] == unsent_state and original_after["raw_message"] == original_row["raw_message"]
    assert fresh_row["status"] == "pending" and fresh_row["rfc822_id"] != original_row["rfc822_id"]
    assert fresh_row["raw_message"] != original_row["raw_message"]
    _, attachment = attached(fresh_row)
    assert attachment["completed_at"] == data["completed_at"]
    assert attachment["trade_ideas"][0]["current_quote"]["observed_at"] == recovery_at.isoformat()
    assert datetime.fromisoformat(fresh_row["expires_at"]) == recovery_at+timedelta(minutes=5)
    repeated, result = stage(config, box, work, data, model=model, now=recovery_at+timedelta(minutes=1))
    assert repeated == [] and result["notification"] == "pending" and result["send_allowed"] is True
    assert len(model.calls) == 1 and box.states() == {unsent_state: 1, "pending": 1}
    assert box.get(recovered[0])["raw_message"] == fresh_row["raw_message"]


def test_pending_or_unknown_provider_receipts_never_advance_delivered_notice(tmp_path):
    config, box, work = configured(tmp_path)
    queued, _ = stage(config, box, work, report())
    notifications.record_ai_deliveries(box, work, queued)
    store = AnalystStore(work / "market-analyst.sqlite3")
    assert store.last_notice("MU") is None
    transport = SimulatedTransport(ambiguous=True)
    assert box.deliver_once(transport, now=NOW)["status"] == "unknown"
    notifications.record_ai_deliveries(box, work, queued)
    assert store.last_notice("MU") is None
    def never_refresh(*args): pytest.fail("Quote collection must wait for ambiguous delivery reconciliation")
    staged, result = stage(config, box, work, report(idea("NVDA")), refresh=never_refresh,
                           model=lambda request: pytest.fail("New model call with ambiguous POST"))
    assert staged == [] and result["status"] == "delivery_reconciliation_pending"
    assert not result["model_called"] and box.states() == {"unknown": 1}
    assert box.reconcile_unknown(transport, now=NOW+timedelta(minutes=1))[0]["status"] == "sent"
    assert len(transport.deliveries) == 1 and len(transport.lookups) == 1
    notifications.record_ai_deliveries(box, work, queued)
    assert store.last_notice("MU") is not None


@pytest.mark.parametrize("field,value", [
    ("world_context", [{"title": "Simulated supply-chain disruption", "publisher": "Source", "source_url": "https://news.example.org/context", "themes": ["supply_chain"]}]),
    ("sentiment", {"label": "negative", "score": -.5}),
    ("risks", ["A material additional cost risk is now reported."]),
])
def test_context_change_reopens_semantic_review_and_wait_cancels_pending_old_approval(tmp_path, field, value):
    config, box, work = configured(tmp_path)
    model = Model()
    first, initial = stage(config, box, work, report(), model=model)
    unchanged, repeated = stage(config, box, work, report(), model=model, now=NOW+timedelta(minutes=1))
    assert unchanged == [] and repeated["notification"] == "pending" and len(model.calls) == 1
    changed = report()
    changed["trade_ideas"][0][field] = value
    model.decisions["MU"] = "wait"
    queued, result = stage(config, box, work, changed, model=model, now=NOW+timedelta(minutes=2))
    assert len(model.calls) == 2 and result["review_identity"] != initial["review_identity"]
    assert queued == [] and not result["send_allowed"] and box.get(first[0])["status"] == "cancelled"
    assert model.calls[-1]["payload"]["candidates"][0][field] == value


def test_no_holdings_reduce_reference_cannot_become_sell_or_short_notice(tmp_path):
    config, box, work = configured(tmp_path)
    queued, result = stage(config, box, work, report(idea("AVGO", action="reduce_if_owned")),
        model=lambda request: pytest.fail("No owned-position model call allowed"))
    assert queued == [] and result["withheld"]["AVGO"] == "no_long_setup_or_no_owned_position"
    assert not result["send_allowed"] and box.states() == {} and box.plans() == {}


def test_quote_refresh_replaces_old_values_with_actual_quote_objects_and_closes_collector(monkeypatch):
    import contract_ipo_monitor.quotes as quotes
    instances = []
    class Collector:
        def __init__(self, **kwargs): self.calls, self.closed = [], False; instances.append(self)
        async def collect(self, instrument, *, observed_at):
            self.calls.append((instrument.symbol, observed_at))
            value = idea(instrument.symbol)["current_quote"]
            value["price"] = 101.25
            value["observed_at"] = observed_at.isoformat()
            return quote_from_dict(value)
        async def aclose(self): self.closed = True
    monkeypatch.setattr(quotes, "CurrentQuoteCollector", Collector)
    original = report(idea(), idea("AVGO", action="reduce_if_owned"))
    updated = notifications.refresh_analysis_quotes(original, NOW)
    assert updated["trade_ideas"][0]["current_quote"]["price"] == 101.25
    assert updated["trade_ideas"][0]["current_quote"]["quote_at"] == NOW.replace(hour=20,second=2).isoformat()
    assert original["trade_ideas"][0]["current_quote"]["price"] == 100.  # preserve source artifact
    assert instances[0].calls == [("MU", NOW)] and instances[0].closed


def test_quote_refresh_failure_clears_old_quote_and_continues_without_leaking_errors(monkeypatch):
    import contract_ipo_monitor.quotes as quotes
    instances = []
    class Collector:
        def __init__(self, **kwargs): self.calls, self.closed = [], False; instances.append(self)
        async def collect(self, instrument, *, observed_at):
            self.calls.append(instrument.symbol)
            if instrument.symbol == "MU":
                raise RuntimeError("Simulated provider failure with a private value that must not be copied")
            value = idea(instrument.symbol)["current_quote"]
            value["observed_at"] = observed_at.isoformat()
            return quote_from_dict(value)
        async def aclose(self): self.closed = True
    monkeypatch.setattr(quotes, "CurrentQuoteCollector", Collector)
    original = report(idea(), idea("NVDA"))
    updated = notifications.refresh_analysis_quotes(original, NOW)
    assert updated["trade_ideas"][0]["current_quote"] == {}
    assert updated["trade_ideas"][1]["current_quote"]["symbol"] == "NVDA"
    assert instances[0].calls == ["MU", "NVDA"] and instances[0].closed
    assert original["trade_ideas"][0]["current_quote"]["price"] == 100.
    assert "private value" not in json.dumps(updated)


def test_sequential_open_quotes_newer_than_refresh_start_are_evaluated_after_refresh(tmp_path, monkeypatch):
    import contract_ipo_monitor.quotes as quotes
    config, box, work = configured(tmp_path)
    start = NOW.replace(hour=16)
    instances = []
    class Collector:
        def __init__(self, **kwargs): self.calls, self.closed = [], False; instances.append(self)
        async def collect(self, instrument, *, observed_at):
            self.calls.append((instrument.symbol, observed_at))
            elapsed = len(self.calls)*30
            data = idea(instrument.symbol)["current_quote"]
            data.update(quote_at=(start+timedelta(seconds=elapsed-1)).isoformat(),
                        observed_at=(start+timedelta(seconds=elapsed)).isoformat(), quote_type="live", market_phase="regular",
                        provider_market_status="Open")
            return quote_from_dict(data)
        async def aclose(self): self.closed = True
    monkeypatch.setattr(quotes, "CurrentQuoteCollector", Collector)
    data = report(idea(), idea("NVDA"))
    data["completed_at"] = start.isoformat()
    model = Model()
    checkpoints = iter((start+timedelta(seconds=61), start+timedelta(seconds=62)))
    queued, result = notifications.stage_ai_notifications(config,box,{},data,start,work=work,infer=model,
        refresh=notifications.refresh_analysis_quotes,clock=lambda:next(checkpoints))
    assert len(queued) == 1 and result["send_allowed"] is True and len(model.calls) == 1
    _, selected = attached(box.get(queued[0]))
    assert [x["symbol"] for x in selected["trade_ideas"]] == ["MU","NVDA"]
    assert [x["current_quote"]["observed_at"] for x in selected["trade_ideas"]] == [
        (start+timedelta(seconds=30)).isoformat(),(start+timedelta(seconds=60)).isoformat()]
    assert all(x["current_quote"]["freshness"]["status"] == "fresh" for x in selected["trade_ideas"])
    assert instances[0].calls == [("MU",start),("NVDA",start)] and instances[0].closed


def test_process_ai_policy_only_reconciles_unknown_post_and_blocks_legacy_route(tmp_path, monkeypatch):
    config, box, work = configured(tmp_path)
    queued, _ = stage(config, box, work, report())
    assert box.deliver_once(SimulatedTransport(ambiguous=True), now=NOW)["status"] == "unknown"
    runtime = monitor.Config(tmp_path)
    source = runtime.outputs / "verified" / "latest.json"
    data = report()
    monitor.write_json(source, data)
    monitor.write_json(runtime.receipt, {"status":"degraded", "outcome":"no_change", "source_report":str(source),
        "report_sha256":hashlib.sha256(source.read_bytes()).hexdigest()})
    monitor.write_json(runtime.ledger, {})
    monitor.write_json(work / "market-delivery.json", config)
    original = notifications.stage_ai_notifications
    def local_stage(*args, **kwargs):
        return original(*args, **kwargs, infer=lambda request:pytest.fail("Model call with unknown delivery"),
            refresh=lambda *args:pytest.fail("Refresh with unknown delivery"), clock=lambda:NOW)
    monkeypatch.setattr(notifications, "stage_ai_notifications", local_stage)
    monkeypatch.setattr(notifications, "stage_notifications", lambda *args, **kwargs:pytest.fail("Legacy notification path used"))
    calls = []
    def worker(configuration, directory, *, reconcile_only=False):
        calls.append(reconcile_only)
        monitor.write_json(directory / "hermes-email-check.json", {"status":"degraded", "outcome":"delivery_unresolved",
            "completed_at":datetime.now(UTC).isoformat(), "newly_sent_ids":[], "reconciled_ids":[]})
    assert notifications.process_notifications(runtime, now=NOW, worker=worker) == ""
    assert calls == [True] and box.states() == {"unknown": 1}
    assert monitor.read_json(runtime.receipt)["ai_analysis"]["status"] == "delivery_reconciliation_pending"
