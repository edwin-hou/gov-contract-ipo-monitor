"""Bind verified hosted research to durable, explicitly authorized Hermes mail."""
from __future__ import annotations

import hashlib
import asyncio
import copy
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

WORK = Path(__file__).resolve().parent
SOURCE = WORK / "gov-contract-ipo-monitor" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from contract_ipo_monitor.notifications import EmailOutbox, report_message
from contract_ipo_monitor.reference_plans import advance_reference_plans

from deployment_settings import settings as deployment_settings
TARGET = deployment_settings()["confirmed_recipient"]


def analyst_inference(request: dict, config: dict, work: Path) -> dict:
    """One bounded Hermes worker; no global model or provider changes."""
    executable = Path(config["hermes_home"]) / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    command = [str(executable), "-I", "-X", "utf8", str(work / "ipo_analyst_worker.py")]
    from ipo_trade_monitor import child_environment
    environment = child_environment()
    environment["HERMES_HOME"] = config["hermes_home"]
    try:
        response = subprocess.run(command, input=json.dumps(request, allow_nan=False).encode(),
                                  capture_output=True, shell=False, timeout=110, cwd=work,
                                  env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if response.returncode != 0 or len(response.stdout) > 64_000:
            raise ValueError("Worker failed")
        value = json.loads(response.stdout)
        from hermes_market_monitor import write_json
        write_json(work / "last-analyst-worker.json", {key: value.get(key) for key in
                   ("status", "provider", "model", "usage", "error_code", "diagnostics")})
        return value
    except Exception:
        return {"status": "unavailable", "provider": "openai-codex", "model": request["model"],
                "text": "", "usage": {}, "error_code": "analyst_worker_unavailable"}


def refresh_analysis_quotes(report: dict, now: datetime) -> dict:
    from contract_ipo_monitor.quotes import CurrentQuoteCollector, quote_to_dict, quote_freshness
    from contract_ipo_monitor.universe import default_universe
    report = copy.deepcopy(report)
    instruments = {item.symbol: item for item in default_universe()}

    async def collect():
        collector = CurrentQuoteCollector(clock=lambda: datetime.now(UTC))
        try:
            for idea in report.get("trade_ideas", []):
                if idea.get("action") != "conditional_buy":
                    continue
                # A failed refresh explicitly replaces the older artifact quote.
                idea["current_quote"] = {}
                instrument = instruments.get(idea.get("symbol"))
                if instrument is None:
                    continue
                try:
                    quote = await collector.collect(instrument, observed_at=now)
                except Exception:
                    continue  # Keep failed refresh empty; never reuse the old quote.
                idea["current_quote"] = {**quote_to_dict(quote), "freshness": quote_freshness(quote, datetime.now(UTC))}
        finally:
            close = getattr(collector, "aclose", None)
            if close:
                result = close()
                if hasattr(result, "__await__"):
                    await result
    asyncio.run(collect())
    return report


def stage_ai_notifications(config, outbox, receipt, report, now, *, work: Path,
                           infer=None, refresh=refresh_analysis_quotes, clock=None):
    from contract_ipo_monitor.analyst import AnalystStore, DEFAULT_MODEL, evaluate, fingerprint, prepare_candidates
    # Preserve old delivery history; pending non-AI reports lose authorization.
    with outbox.connect() as connection:
        connection.execute("UPDATE investment_mail SET status='cancelled',error_code='superseded_by_ai_only_policy' WHERE status='pending' AND event_key NOT LIKE 'ai-reviewed:%'")
        # An uncertain POST may already have arrived. Reconcile before any more
        # opportunities rather than create a different identity to resend it.
        if connection.execute("SELECT 1 FROM investment_mail WHERE status IN ('unknown','sending') LIMIT 1").fetchone():
            return [], {"status": "delivery_reconciliation_pending", "model_called": False}
    policy = config.get("analyst") or {}
    if policy.get("enabled") is not True or policy.get("model") != DEFAULT_MODEL or policy.get("reasoning_effort") != "medium":
        return [], {"status": "analyst_not_configured", "model_called": False}
    report = refresh(report, now)
    store = AnalystStore(work / "market-analyst.sqlite3")
    # Read the durable delivered receipts, not only this invocation's returned
    # ids. This closes a crash after provider verification / before notice save.
    with outbox.connect() as connection:
        delivered = [row[0] for row in connection.execute("SELECT id FROM investment_mail WHERE status='sent' AND event_key LIKE 'ai-reviewed:%' ORDER BY id")]
    record_ai_deliveries(outbox, work, delivered)
    refreshed_at = (clock or (lambda: datetime.now(UTC)))()
    result = evaluate(report, store, infer or (lambda request: analyst_inference(request, config, work)),
                      now=refreshed_at, model=policy["model"], daily_limit=policy.get("daily_limit", 4))
    approved = result.pop("approved")
    authorized = {idea["symbol"] for idea in approved}
    # A pending immutable message may have lost authority since it was queued.
    # Reconcile uncertain POSTs read-only, but never send invalidated candidates.
    with outbox.connect() as connection:
        pending = connection.execute("SELECT id,raw_message FROM investment_mail WHERE status='pending' AND event_key LIKE 'ai-reviewed:%'").fetchall()
        from email import policy as mail_policy
        from email.parser import BytesParser
        for row in pending:
            try:
                message = BytesParser(policy=mail_policy.default).parsebytes(bytes(row["raw_message"]))
                original = next(json.loads(part.get_payload(decode=True)) for part in message.iter_attachments() if part.get_filename() == "research-report.json")
                original_ideas = original["trade_ideas"]
                valid = all(idea["symbol"] in authorized and idea["ai_review"]["review_identity"] == result.get("review_identity") for idea in original_ideas)
            except Exception:
                valid = False
            if not valid:
                connection.execute("UPDATE investment_mail SET status='cancelled',error_code='ai_authority_changed' WHERE id=? AND status='pending'", (row["id"],))
    if not approved:
        return [], {**result, "send_allowed": False}
    completed_analysis = (clock or (lambda: datetime.now(UTC)))()
    selected = {**report, "trade_ideas": approved, "notification_scope": "ai_approved_only",
                "ai_analysis": {**result, "reasoning_effort": "medium", "evaluated_at": completed_analysis.isoformat()},
                "holdings": []}
    # Model latency cannot convert an expired quote into a sendable alert.
    still_valid, _ = prepare_candidates(selected, now=completed_analysis)
    if {x["symbol"] for x in still_valid} != {x["symbol"] for x in approved}:
        with outbox.connect() as connection:
            connection.execute("UPDATE investment_mail SET status='cancelled',error_code='quote_expired_during_review' WHERE status='pending' AND event_key LIKE 'ai-reviewed:%'")
        return [], {**result, "status": "quotes_expired_during_analysis", "send_allowed": False}
    expiry = min(datetime.fromisoformat(x["current_quote"]["observed_at"].replace("Z", "+00:00")) + timedelta(minutes=5) for x in approved)
    if expiry <= completed_analysis:
        with outbox.connect() as connection:
            connection.execute("UPDATE investment_mail SET status='cancelled',error_code='quote_expired_during_review' WHERE status='pending' AND event_key LIKE 'ai-reviewed:%'")
        return [], {**result, "status": "quotes_expired_during_analysis", "send_allowed": False}
    key = "ai-reviewed:" + result["review_identity"] + ":" + fingerprint([selected["completed_at"], [x["symbol"] for x in approved]])[:16]
    # Never reuse stale immutable bytes. Definitely unsent cancelled/expired
    # publications may recover with a new generation after fresh authorization.
    prefix = "ai-reviewed:" + result["review_identity"] + ":%"
    with outbox.connect() as connection:
        connection.execute("UPDATE investment_mail SET status='expired',error_code='report_expired' WHERE status='pending' AND event_key LIKE ? AND expires_at<=?",
                           (prefix, completed_analysis.isoformat()))
        if connection.execute("SELECT 1 FROM investment_mail WHERE event_key LIKE ? AND status='pending' LIMIT 1", (prefix,)).fetchone():
            return [], {**result, "notification": "pending", "send_allowed": True}
        existing = connection.execute("SELECT * FROM investment_mail WHERE event_key=?", (key,)).fetchone()
        if existing and existing["status"] not in {"cancelled", "expired"}:
            return [], {**result, "notification": existing["status"], "send_allowed": False}
        if existing:
            generation = connection.execute("SELECT COUNT(*) FROM investment_mail WHERE event_key LIKE ?", (prefix,)).fetchone()[0]
            key += ":renew:" + str(generation)
    notice = "AI-reviewed conditional opportunity. The model considers the supplied evidence worth reviewing; it does not establish expected profits. Only approved candidates are included. Holdings remain empty."
    raw = report_message(selected, sender=config["sender"], recipient=config["recipient"], event_key=key,
                         created_at=completed_analysis, notice=notice)
    identifier = outbox.enqueue(key, raw, config["recipient"], now=completed_analysis, expires_at=expiry)
    return [identifier], {**result, "approved_symbols": [x["symbol"] for x in approved], "notification": "queued", "send_allowed": True}


def record_ai_deliveries(outbox, work: Path, identifiers):
    from email import policy
    from email.parser import BytesParser
    from contract_ipo_monitor.analyst import AnalystStore
    store = AnalystStore(work / "market-analyst.sqlite3")
    for identifier in identifiers:
        row = outbox.get(identifier)
        if row["status"] != "sent" or not row["event_key"].startswith("ai-reviewed:"):
            continue
        message = BytesParser(policy=policy.default).parsebytes(bytes(row["raw_message"]))
        for part in message.iter_attachments():
            if part.get_filename() == "research-report.json":
                report = json.loads(part.get_payload(decode=True))
                for idea in report["trade_ideas"]:
                    if idea.get("ai_review", {}).get("decision") == "notify":
                        store.save_notice(idea)


def delivery_config(path: Path, work: Path) -> dict:
    if not path.exists():
        return {"enabled": False}
    if path.stat().st_size > 64_000:
        raise ValueError("delivery_config_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
        raise ValueError("delivery_config_invalid")
    if not value["enabled"]:
        return {"enabled": False}
    if value.get("notification_policy") != "ai_approved_only":
        raise ValueError("delivery_policy_not_authorized")
    if value.get("sender") != TARGET or value.get("recipient") != TARGET or value.get("holdings") != []:
        raise ValueError("confirmed_delivery_scope_mismatch")
    for name in ("hermes_home", "outbox_path"):
        if not isinstance(value.get(name), str) or not Path(value[name]).is_absolute():
            raise ValueError("delivery_config_invalid")
    outbox = Path(value["outbox_path"]).resolve()
    if not outbox.is_relative_to(work.resolve()) or outbox == work.resolve():
        raise ValueError("delivery_outbox_outside_workspace")
    return value


def enqueue(outbox, config, report, key, notice, now):
    # An event owns immutable bytes, including its Date header, across retries.
    if outbox.event(key):
        return None
    completed = datetime.fromisoformat(report["completed_at"].replace("Z", "+00:00"))
    return outbox.enqueue(key, report_message(report, sender=config["sender"], recipient=config["recipient"],
                                             event_key=key, created_at=now, notice=notice),
                          config["recipient"], now=now, expires_at=completed + timedelta(hours=6))


def candidate_snapshot(report):
    result = {}
    for item in report.get("listed_discovery", []):
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol or len(symbol) > 30:
            continue
        stable = {key: item.get(key) for key in ("cik", "symbol", "exchange", "status", "financials", "reasons")}
        result[symbol] = stable
    return result


def stage_notifications(config, outbox, receipt, report, ledger, now, *, outputs_root):
    queued = []
    initial_key = config.get("initial_event_key")
    # Wait for the strategy/discovery release before delivering the initial report.
    if (config.get("initial_report_requested") is True and isinstance(initial_key, str) and initial_key
            and "listed_discovery" in report and all("strategy" in x for x in report.get("trade_ideas", []))):
        identifier = enqueue(outbox, config, report, initial_key,
                             "Initial verified report after the pipeline audit. Holdings are empty. Entry and exit references are research conditions, with no order or fill assumed.", now)
        if identifier:
            queued.append(identifier)
    # Re-read the delivered local notice on every check, including crash recovery.
    # A receipt's digest is usable only when it binds this exact source report.
    local = ledger.get("local_delivery") or {}
    path = receipt.get("digest_path") or local.get("path")
    digest = receipt.get("digest_sha256") or local.get("sha256")
    if path and digest and receipt.get("outcome") != "failure":
        notice_path = Path(path).resolve()
        if not notice_path.is_relative_to(outputs_root.resolve()):
            raise ValueError("notice_outside_outputs")
        raw = notice_path.read_bytes()
        if len(raw) > 1_000_000 or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("local_notice_readback_mismatch")
        # A saved baseline notice may describe an older report. Its attachment
        # must never be silently replaced with a different current report.
        if report["completed_at"] in raw.decode("utf-8"):
            identifier = enqueue(outbox, config, report, "local-notice:" + digest, raw.decode("utf-8"), now)
            if identifier:
                queued.append(identifier)
        elif (local.get("sha256") == digest and local.get("path") == str(notice_path)
              and ledger.get("last_report_at") in raw.decode("utf-8") and ledger.get("report_sha256")
              and not outbox.event("local-notice:" + digest)
              and timedelta(0) <= now - datetime.fromisoformat(ledger["last_report_at"].replace("Z", "+00:00")) <= timedelta(hours=6)):
            # A crash can occur after local publication, before mail staging,
            # and before the next hourly artifact arrives. Recover that notice
            # without presenting its older levels as current instructions.
            from hermes_market_monitor import read_json, validate_report, idea_snapshot
            original_path = Path(ledger["source_report"]).resolve()
            if not original_path.is_relative_to(outputs_root.resolve()):
                raise ValueError("earlier_report_outside_outputs")
            original = read_json(original_path)
            validate_report(original, now)
            if (hashlib.sha256(original_path.read_bytes()).hexdigest() != ledger["report_sha256"]
                    or original["completed_at"] != ledger["last_report_at"] or idea_snapshot(original) != ledger["ideas"]):
                raise ValueError("earlier_notice_source_mismatch")
            notice = ("Recovered an earlier locally saved update. The following earlier text is historical. Current research actions, levels and strategy are listed in this email's main report and latest attachments; no position or fill is inferred.\n\n"
                      + raw.decode("utf-8"))
            identifier = enqueue(outbox, config, report, "local-notice:" + digest, notice, now)
            if identifier:
                queued.append(identifier)
    plans, changes = advance_reference_plans(outbox.plans(), report)
    if changes:
        key = "reference:" + hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest()
        notice = ""
        for event in changes:
            notice += f"{event['symbol']}: {event['kind']}\n{event['message']}\n"
            if event.get("price_date"):
                notice += f"Completed close: {event.get('close')} {event.get('currency')}, {event['price_date']}.\n"
        identifier = enqueue(outbox, config, report, key, notice, now)
        if identifier:
            queued.append(identifier)
    # Publication precedes plan/state advancement. Replaying after a crash is
    # safe because the event key and queued bytes already exist.
    outbox.save_plans(plans)
    current = candidate_snapshot(report)
    previous = outbox.state("listed_discovery")
    candidate_changes = [item for symbol, item in current.items()
                         if (item.get("status") == "qualified_review" or previous.get(symbol, {}).get("status") == "qualified_review")
                         and item != previous.get(symbol)]
    candidate_changes += [{**item, "status": "no_longer_in_current_discovery_view",
                           "reasons": ["The bounded current discovery view no longer covers this finding. Recheck the primary filing before using it; disappearance does not imply poor results."]}
                          for symbol, item in previous.items() if symbol not in current and item.get("status") == "qualified_review"]
    if candidate_changes:
        key = "discovery:" + hashlib.sha256(json.dumps(candidate_changes, sort_keys=True).encode()).hexdigest()
        lines = ["New or changed issuer discovery findings. These require price/news/liquidity review before any trade; none is an automatic buy."]
        lines += [f"{x['symbol']} ({x.get('exchange')}): {x.get('status')}; " + "; ".join(x.get("reasons") or []) for x in candidate_changes]
        identifier = enqueue(outbox, config, report, key, "\n".join(lines), now)
        if identifier:
            queued.append(identifier)
    outbox.save_state("listed_discovery", current)
    return queued


def run_mail_worker(config, work: Path, *, reconcile_only=False):
    executable = Path(config["hermes_home"]) / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    command = [str(executable), str(work / "hermes_email_worker.py"), "--config", str(work / "market-delivery.json"),
               "--receipt", str(work / "hermes-email-check.json")]
    if reconcile_only:
        command.append("--reconcile-only")
    result = subprocess.run(command, shell=False, capture_output=True, timeout=300,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=work)
    if result.returncode not in (0, 1):
        raise ValueError("mail_worker_failed")
    return result.returncode


def process_notifications(monitor_config, *, now=None, worker=run_mail_worker):
    from hermes_market_monitor import read_json, validate_report, timestamp, write_json
    now = now or datetime.now(UTC)
    work = monitor_config.work
    receipt = read_json(monitor_config.receipt)
    mail_path = work / "hermes-email-check.json"
    try:
        config = delivery_config(work / "market-delivery.json", work)
        if not config["enabled"]:
            receipt.update(email_status="ok", email_outcome="disabled", email_sent=[], mail_receipt_path=str(mail_path))
            write_json(monitor_config.receipt, receipt)
            return ""
        verified = receipt.get("status") in {"ok", "degraded"} and receipt.get("outcome") != "failure"
        queued = []
        if verified:
            report = read_json(Path(receipt["source_report"]))
            validate_report(report, now)
            if hashlib.sha256(Path(receipt["source_report"]).read_bytes()).hexdigest() != receipt.get("report_sha256"):
                raise ValueError("verified_report_changed")
            ledger = read_json(monitor_config.ledger)
            outbox = EmailOutbox(Path(config["outbox_path"]))
            if config.get("notification_policy") == "ai_approved_only":
                queued, analysis = stage_ai_notifications(config, outbox, receipt, report, now, work=work)
                receipt["ai_analysis"] = analysis
            else:
                queued = stage_notifications(config, outbox, receipt, report, ledger, now,
                                             outputs_root=monitor_config.base / "outputs")
        started = datetime.now(UTC)
        allow_send = verified and (config.get("notification_policy") != "ai_approved_only" or receipt.get("ai_analysis", {}).get("send_allowed") is True)
        worker(config, work, reconcile_only=not allow_send)
        mail = read_json(mail_path)
        if (timestamp(mail.get("completed_at"), "Mail worker") < started
                or mail.get("status") not in {"ok", "degraded", "error"}):
            raise ValueError("mail_receipt_invalid")
        sent = mail.get("newly_sent_ids", []) + mail.get("reconciled_ids", [])
        if config.get("notification_policy") == "ai_approved_only" and sent:
            record_ai_deliveries(EmailOutbox(Path(config["outbox_path"])), work, sent)
        receipt.update(email_status=mail["status"], email_outcome=mail.get("outcome"), email_sent=sent,
                       email_queued=queued, mail_receipt_path=str(mail_path))
        receipt.pop("email_error_code", None)
        write_json(monitor_config.receipt, receipt)
        return f"Verified research report email delivery to {TARGET}.\n" if sent else ""
    except Exception:
        # Credential/provider text is never included in monitoring receipts.
        previously_failed = receipt.get("email_status") == "error"
        receipt.update(email_status="error", email_outcome="failure", email_sent=[], mail_receipt_path=str(mail_path),
                       email_error_code="mail_dispatch_failed")
        write_json(monitor_config.receipt, receipt)
        return "" if previously_failed else "Research email delivery needs review; see the local monitoring receipt.\n"
