"""Deterministic hosted-report checks for Hermes no_agent cron; no orders.

GitHub Actions remains the collector. Authorized mail is a durable separate outbox.
Empty stdout means no material change. The separate check receipt proves each
check without consuming the previously notified trading baseline.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote, urlsplit

DEFAULT_BASE = Path(__file__).resolve().parent.parent
REPO_SOURCE = Path(__file__).resolve().parent / "gov-contract-ipo-monitor" / "src"
TRUSTED_WORK = Path(__file__).resolve().parent
# Hermes enables Python safe-path isolation. Explicitly add only this deployed,
# task-owned directory; do not depend on the process cwd or implicit sys.path.
if str(TRUSTED_WORK) not in sys.path:
    sys.path.insert(0, str(TRUSTED_WORK))
if str(REPO_SOURCE) not in sys.path:
    sys.path.insert(0, str(REPO_SOURCE))
from deployment_settings import settings as deployment_settings
DEFAULT_BASE = Path(deployment_settings()["base"])
WORKFLOW_PATH = ".github/workflows/monitor.yml"
IDEA_KEYS = ("action", "entry", "invalidation", "target", "currency", "exchange", "price_as_of")
ACTIVE = {"conditional_buy", "reduce_if_owned"}


class CheckError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass(frozen=True)
class Config:
    base: Path = DEFAULT_BASE

    @property
    def work(self):
        return self.base / "work"

    @property
    def ledger(self):
        return self.work / "trade-notifications.json"

    @property
    def receipt(self):
        return self.work / "hermes-trade-check.json"

    @property
    def outputs(self):
        return self.base / "outputs" / "hermes-market"


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _decode(raw):
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def read_json(path: Path, *, optional: bool = False):
    if optional and not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            raw = stream.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError("Size limit")
        result = _decode(raw)
        if not isinstance(result, dict):
            raise ValueError("Expected object")
        return result
    except (OSError, ValueError, UnicodeError):
        raise CheckError("local_json_invalid", "A required local report or baseline is missing, oversized, or invalid.") from None


def read_verified_report(path: Path, *bindings: dict):
    """Reuse exactly sealed report bytes; never create authority from a new hash."""
    expected = set()
    for binding in bindings:
        source = binding.get("source_report")
        if not isinstance(source, str) or not source or Path(source).resolve() != path.resolve():
            continue
        digest = binding.get("report_sha256")
        if digest is None:
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CheckError("report_hash_mismatch", "The saved local report authority is invalid; no report is reauthorized.")
        expected.add(digest)
    if not expected:
        raise CheckError("report_hash_missing", "The local report has no saved hash binding; a verified hosted artifact is required before reuse.")
    if len(expected) != 1:
        raise CheckError("report_hash_mismatch", "The saved local report authorities conflict; no report is reauthorized.")
    digest = next(iter(expected))
    try:
        with path.open("rb") as stream:
            raw = stream.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError("Size limit")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise CheckError("report_hash_mismatch", "The previously verified local report changed unexpectedly.")
        report = _decode(raw)
        if not isinstance(report, dict):
            raise ValueError("Expected object")
    except (OSError, ValueError, UnicodeError):
        raise CheckError("local_json_invalid", "A required local report or baseline is missing, oversized, or invalid.") from None
    return report, digest


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_json(path, data):
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


@contextmanager
def check_lock(path: Path):
    """Kernel-released lock: a terminated check cannot leave a stale owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CheckError("check_busy", "Another deterministic monitor check is already running.") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class GitHubHelper:
    def __init__(self, config: Config):
        self.config = config

    def __call__(self, operation: str, *arguments):
        # Never import github_ops.py: it has a top-level command dispatcher.
        argv = [str(self.config.work / "monitor-venv" / "Scripts" / "python.exe"),
                str(self.config.work / "github_ops.py"), operation, *(str(item) for item in arguments)]
        try:
            result = subprocess.run(argv, shell=False, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=180, cwd=self.config.base,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0 or len(result.stdout) > 2_000_000:
                raise ValueError("Helper failure")
            return _decode(result.stdout) if operation not in {"download", "dispatch"} else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # stderr can contain credential-manager/HTTP traces; never expose it.
            raise CheckError("github_check_failed", "The GitHub readback or artifact download did not complete successfully.") from None


def timestamp(value, label):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError("Timezone missing")
        return result.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise CheckError("timestamp_invalid", f"{label} has no valid timezone-aware timestamp.") from None


def day(value, label):
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise CheckError("source_date_invalid", f"{label} has no valid calendar date.") from None


def external_url(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(char) < 33 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
    except ValueError:
        return None
    return quote(value, safe=":/?=&%#@+,-._~")


def rows(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def validate_report(report, now):
    completed = timestamp(report.get("completed_at"), "Report")
    if completed > now or now - completed > timedelta(hours=6):
        raise CheckError("report_stale", "The hosted report is future-dated or older than six hours; no new setup is published.")
    if report.get("status") not in {"ok", "degraded"} or report.get("market_restore_errors"):
        raise CheckError("report_invalid", "The report is incomplete or contains market restoration errors.")
    ideas = report.get("trade_ideas")
    if not isinstance(ideas, list) or not 1 <= len(ideas) <= 100 or len(rows(ideas)) != len(ideas):
        raise CheckError("report_invalid", "The hosted report has no complete bounded trade watchlist.")
    symbols = [idea.get("symbol") for idea in ideas]
    if any(not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9^.\-]{1,32}", symbol) for symbol in symbols) or len(set(symbols)) != len(symbols):
        raise CheckError("report_invalid", "The hosted report has invalid or duplicate instrument identities.")
    coverage = {item.get("symbol"): item for item in rows(report.get("price_coverage"))}
    active = [idea for idea in ideas if idea.get("action") in ACTIVE]
    for idea in ideas:
        if idea.get("action") not in ACTIVE | {"wait"} or not re.fullmatch(r"[A-Z]{3}", str(idea.get("currency"))) or not idea.get("exchange"):
            raise CheckError("report_invalid", "The report has an invalid research state, venue, or trading currency.")
    for idea in active:
        symbol = idea["symbol"]
        receipt = coverage.get(symbol, {})
        if (receipt.get("status") != "ok" or receipt.get("currency") != idea["currency"]
                or receipt.get("as_of") != idea.get("price_as_of") or not external_url(receipt.get("source_url"))):
            raise CheckError("price_evidence_invalid", f"Verified price identity, date, currency, or coverage is missing for {symbol}.")
        observed = timestamp(receipt.get("observed_at"), "Price source")
        priced = day(idea.get("price_as_of"), "Completed price")
        # Keep the same venue/session policy as the trade engine, including
        # holidays, early closes and an incomplete current regular session.
        from contract_ipo_monitor.trades import completed_sessions_since, completed_session_date
        try:
            session_age = completed_sessions_since(priced, completed, idea["exchange"])
            regular_day = completed_session_date(idea["exchange"], datetime.combine(priced, datetime.max.time(), UTC)) == priced
        except ValueError:
            raise CheckError("price_evidence_stale", f"Reviewed regular-session validation is unavailable for {symbol}.") from None
        if (observed > completed or now - observed > timedelta(hours=6) or not regular_day
                or priced > completed.date() or not 0 <= session_age <= 1):
            raise CheckError("price_evidence_stale", f"Completed price evidence for {symbol} is future-dated or stale.")
        fact = idea.get("fundamentals") or {}
        if not isinstance(fact, dict) or not external_url(fact.get("source_url")):
            raise CheckError("financial_evidence_invalid", f"Primary financial evidence is missing for {symbol}.")
        reported, period = day(fact.get("reported_at"), "Financial report"), day(fact.get("period_end"), "Financial period")
        max_period_age = 180 if fact.get("period_type") == "quarter" else 450 if fact.get("period_type") == "annual" else 0
        if (not max_period_age or not period <= reported <= completed.date() or (now.date() - reported).days > 120
                or (now.date() - period).days > max_period_age):
            raise CheckError("financial_evidence_stale", f"Financial evidence for {symbol} is future-dated or expired.")
        evidence = idea.get("evidence_urls", [])
        if fact["source_url"] not in evidence or receipt["source_url"] not in evidence:
            raise CheckError("source_evidence_missing", f"The financial and price source links are not included in {symbol}'s evidence.")
        if idea["action"] == "conditional_buy":
            stop, entry, target = (idea.get(key) for key in ("invalidation", "entry", "target"))
            if not all(finite(value) for value in (stop, entry, target)) or not 0 < stop < entry < target:
                raise CheckError("setup_levels_invalid", f"The conditional levels for {symbol} are missing or inconsistent.")
        elif not finite(idea.get("invalidation")) or idea["invalidation"] <= 0:
            raise CheckError("setup_levels_invalid", f"The holding review level for {symbol} is missing or inconsistent.")
    if active:
        world_sources = {item.get("source") for item in rows(report.get("world_coverage"))
                         if item.get("status") in {"ok", "partial"} and external_url(item.get("source_url"))
                         and timedelta(0) <= completed - timestamp(item.get("observed_at"), "World source") <= timedelta(hours=6)}
        if report.get("world_coverage_ready") is not True or len(world_sources) < 2:
            raise CheckError("world_evidence_missing", "Fresh world-news source coverage from at least two publishers is missing.")
    return completed


def idea_snapshot(report):
    return {idea["symbol"]: {key: idea.get(key) for key in IDEA_KEYS} for idea in report["trade_ideas"]}


def material_changes(previous, current):
    changes = []
    for symbol, idea in current.items():
        old = previous.get(symbol)
        reasons = []
        if old is None:
            if idea["action"] in ACTIVE:
                reasons.append("New actionable research state")
        elif old.get("action") != idea["action"]:
            reasons.append(f"Research state changed from {old.get('action', 'unknown')} to {idea['action']}")
        elif any(old.get(key) != idea.get(key) for key in ("exchange", "currency")):
            reasons.append("Listing venue or trading currency changed")
        elif idea["action"] in ACTIVE:
            for key in ("entry", "invalidation", "target"):
                before, after = old.get(key), idea.get(key)
                if before == after:
                    continue
                if not finite(before) or not finite(after) or before <= 0 or abs(after - before) / before >= .01 - 1e-12:
                    reasons.append(f"{key.capitalize()} changed by at least 1% or became unavailable")
        if reasons:
            changes.append({"symbol": symbol, "reasons": reasons})
    for symbol, old in previous.items():
        if symbol not in current and old.get("action") in ACTIVE:
            changes.append({"symbol": symbol, "reasons": ["Previously actionable instrument is absent from the current configured watchlist"]})
    return changes


def ipo_snapshot(report):
    keys = ("status", "active", "ipo_confirmed", "confidence", "ticker", "exchange", "effective_date")
    return {str(item.get("issuer_key")) + ":" + str(item.get("registration_id") or "unknown"):
            {**{key: item.get(key) for key in keys}, "issuer_name": item.get("issuer_name")}
            for item in rows(report.get("ipos")) if item.get("issuer_key")}


def ipo_changes(previous, report):
    current = ipo_snapshot(report)
    changes = []
    for identity, item in current.items():
        old = previous.get(identity)
        if item.get("ipo_confirmed") is True and item != old or old and old.get("ipo_confirmed") is True and item != old:
            changes.append({"identity": identity, "issuer_name": item.get("issuer_name"), "status": item.get("status")})
    if changes:
        sec = report.get("health", {}).get("collectors", {}).get("sec", {})
        if not sec.get("ok") or sec.get("disabled"):
            raise CheckError("ipo_evidence_missing", "Fresh SEC collection is unavailable; new IPO-stage notices are withheld.")
        for item in rows(report.get("ipos")):
            if any(change["identity"] == str(item.get("issuer_key")) + ":" + str(item.get("registration_id") or "unknown") for change in changes):
                if not any(external_url(url) and urlsplit(url).hostname in {"sec.gov", "www.sec.gov", "data.sec.gov"} for url in item.get("source_urls", [])):
                    raise CheckError("ipo_evidence_missing", "A changed IPO stage has no valid SEC evidence link.")
    return changes


def maintenance_needed(report, now, already):
    reminders = []
    for company in rows(report.get("listed_companies")):
        fact = company.get("financials") or {}
        if not isinstance(fact, dict) or fact.get("source_kind") != "issuer":
            continue
        reported, period = day(fact.get("reported_at"), "Snapshot report"), day(fact.get("period_end"), "Snapshot period")
        expiry = min(reported + timedelta(days=120), period + timedelta(days=180 if fact.get("period_type") == "quarter" else 450))
        identity = hashlib.sha256(json.dumps([company.get("symbol"), fact.get("source_url"), str(reported), str(period)]).encode()).hexdigest()
        if 0 <= (expiry - now.date()).days <= 30 and identity not in already:
            reminders.append({"id": identity, "symbol": company.get("symbol"), "expiry": expiry.isoformat(), "source_url": fact.get("source_url")})
    return reminders


def clean(value):
    return escape(str(value), quote=True).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("[", "\\[").replace("]", "\\]").replace("`", "\\`")


def link(url, label="Evidence"):
    destination = external_url(url)
    return f"[{clean(label)}]({destination})" if destination else "Source unavailable"


def digest_text(report, run, changes, ipos, maintenance):
    labels = {"conditional_buy": "Conditional buy", "reduce_if_owned": "Reduce if already owned", "wait": "Wait"}
    number = lambda value: f"{value:,.2f}" if finite(value) else "—"
    lines = ["# IPO and trade monitoring update", "", f"Verified hosted report: {clean(report['completed_at'])}. {link(run.get('html_url'), 'GitHub run')}", "",
             "Saved to local files and Hermes job output. Authorized email delivery has a separate provider receipt. No brokerage order is sent. Confirm a fresh quote before acting; these daily-price screening rules have no validated return forecast.", ""]
    for change in changes:
        lines.append(f"- {clean(change['symbol'])}: {clean('; '.join(change['reasons']))}.")
    for change in ipos:
        lines.append(f"- IPO evidence changed for {clean(change['issuer_name'])}: {clean(change['status'])}. Filing stages do not prove trading began.")
        for candidate in rows(report.get("ipos")):
            identity = str(candidate.get("issuer_key")) + ":" + str(candidate.get("registration_id") or "unknown")
            if identity == change["identity"]:
                lines.append("  Primary filing evidence: " + " · ".join(link(url, "SEC filing") for url in candidate.get("source_urls", [])[:3]))
    for reminder in maintenance:
        lines.append(f"- Maintenance: {clean(reminder['symbol'])}'s reviewed issuer snapshot reaches its evidence expiry on {clean(reminder['expiry'])}; review a newer primary result. {link(reminder.get('source_url'))}. No automatic code change or model is requested.")
    lines += ["", "## Current research states (including waits)", "",
              "| Ticker | Venue | Currency | State | Completed price date | Entry | Invalidation / holding review | Target |",
              "|---|---|---|---|---|---|---|---|"]
    for idea in report["trade_ideas"]:
        active = idea["action"] == "conditional_buy"
        levels = [number(idea.get(key)) if active or key == "invalidation" and idea['action'] == "reduce_if_owned" else "—" for key in ("entry", "invalidation", "target")]
        lines.append(f"| {clean(idea['symbol'])} | {clean(idea['exchange'])} | {clean(idea['currency'])} | {labels[idea['action']]} | {clean(idea.get('price_as_of') or 'Unavailable')} | {' | '.join(levels)} |")
    for idea in report["trade_ideas"]:
        lines += ["", f"## {clean(idea['symbol'])}: {labels[idea['action']]}", "", f"Horizon: {clean(idea.get('horizon') or '5–15 trading sessions')}.", "",
                  *[f"- {clean(value)}" for value in idea.get("conditions", [])], "",
                  "Research reasons:", *[f"- {clean(value)}" for value in idea.get("reasons", [])]]
        fact = idea.get("fundamentals")
        if isinstance(fact, dict):
            lines += ["", f"Company financial evidence: period {clean(fact.get('period_end'))}, reported {clean(fact.get('reported_at'))}; {clean(fact.get('accounting_standard'))}, {clean(fact.get('reporting_currency'))}. {link(fact.get('source_url'), 'Primary financial results')}."]
        for event in rows(idea.get("world_context"))[:3]:
            lines += ["", f"World context: {link(event.get('source_url'), event.get('title'))} — {clean(event.get('publisher'))}, {clean(event.get('published_at'))}; themes {clean(', '.join(event.get('themes', [])))}. Relevance and price direction are unverified."]
        lines += ["", "Risks and limitations:", *[f"- {clean(value)}" for value in idea.get("risks", []) + idea.get("limitations", [])], "",
                  "Sources: " + " · ".join(link(url) for url in idea.get("evidence_urls", [])[:8])]
    lines += ["", "## Collection limits", "", f"Report status: {clean(report['status'])}. Existing optional-source and historical gaps remain visible in the full report; they are not repeated as new alerts.", "",
              "The configured global-issuer shortlist is concentrated in technology and U.S. listings. Native-currency/benchmark coverage and portfolio suitability can keep instruments in wait. Invalidation is a risk reference, not a guaranteed exit; gaps and costs can increase losses.", ""]
    return "\n".join(lines)


def publish(path, text):
    content = text.encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    if path.exists() and path.read_bytes() == content:
        return digest, False
    atomic_write(path, text)
    if path.read_bytes() != content:
        raise CheckError("local_delivery_failed", "The locally published digest did not pass readback validation.")
    return digest, True


def source_gaps(report):
    gaps = {str(item.get("source")) + ": " + str(item.get("status")) for item in rows(report.get("coverage")) + rows(report.get("world_coverage"))
            if item.get("status") not in {"ok", "disabled"}}
    gaps.update("price:" + str(item.get("symbol")) + ": " + str(item.get("status")) for item in rows(report.get("price_coverage"))
                if item.get("status") != "ok")
    gaps.update("collector:" + str(name) for name, state in report.get("health", {}).get("collectors", {}).items()
                if not state.get("ok") and not state.get("disabled"))
    gaps.update(str(item.get("name")) for item in rows(report.get("historic_coverage_gaps")))
    return sorted(gaps)


def finish_quiet(config, receipt, report, now):
    """Maintenance may become due without a new artifact; trading stays untouched."""
    maintenance_state = dict(receipt.get("maintenance_notified", {}))
    maintenance = maintenance_needed(report, now, maintenance_state)
    notice = ""
    if maintenance:
        identity = hashlib.sha256(json.dumps([item["id"] for item in maintenance]).encode()).hexdigest()[:16]
        digest_path = config.outputs / ("maintenance-" + identity + ".md")
        run_id = receipt.get("verified_run") or ""
        run = {"html_url": "https://github.com/edwin-hou/gov-contract-ipo-monitor/actions/runs/" + str(run_id)}
        digest_sha, created = publish(digest_path, digest_text(report, run, [], [], maintenance))
        maintenance_state.update({item["id"]: now.isoformat() for item in maintenance})
        receipt.update(outcome="published_change" if created else "recovered_local_delivery", digest_path=str(digest_path),
                       digest_sha256=digest_sha, maintenance_notified=maintenance_state)
        if created:
            notice = f"Reviewed issuer evidence approaches expiry; maintenance notice saved locally: {digest_path}\nNo external message, code push, or model call was made.\n"
    write_json(config.receipt, receipt)
    return notice


def poll(config=Config(), *, now=None, github=None):
    now = (now or datetime.now(UTC)).astimezone(UTC)
    github = github or GitHubHelper(config)
    previous_receipt = read_json(config.receipt, optional=True)
    receipt = {**previous_receipt, "completed_at": now.isoformat(), "outcome": "failure", "status": "error"}
    # A receipt describes this invocation. Keep the prior failure only in the
    # deduplication state; recovered polls must not retain old failure details.
    for key in ("failure_code", "internal_error_type", "missing_module", "message", "pending_reason"):
        receipt.pop(key, None)
    try:
        ledger = read_json(config.ledger)
        if not isinstance(ledger.get("ideas"), dict) or not str(ledger.get("last_notified_run", "")).isdigit():
            raise CheckError("baseline_invalid", "The existing notification baseline is invalid; it will not be replaced.")
        main, workflow = github("main"), github("workflow")
        main_sha = main.get("sha")
        if main.get("branch") != "main" or not re.fullmatch(r"[0-9a-f]{40}", str(main_sha)) or workflow.get("path") != WORKFLOW_PATH or workflow.get("state") != "active":
            raise CheckError("workflow_identity_invalid", "The live main branch or scheduled monitor workflow identity is invalid or inactive.")
        receipt["main_sha"] = main_sha
        candidates = [item for item in github("runs", main_sha) if item.get("name") == workflow.get("name") and item.get("head_branch") == "main" and item.get("head_sha") == main_sha]
        candidates.sort(key=lambda item: int(item["id"]), reverse=True)
        newest = candidates[0] if candidates else None
        receipt["newest_run"] = str(newest["id"]) if newest else None
        receipt["newest_run_status"] = newest.get("status") if newest else None
        receipt["newest_run_conclusion"] = newest.get("conclusion") if newest else None
        # Collection status and report authority are separate. A failed or
        # queued collection must not hide a preceding successful artifact that
        # this installation has not consumed. Keep the newest attempt visible,
        # then verify the newest successful current-main report normally.
        candidate = next((item for item in candidates if item.get("status") == "completed"
                          and item.get("conclusion") == "success"), None)
        if newest is not None and candidate is not None and newest["id"] != candidate["id"]:
            receipt["pending_reason"] = "The latest current-main collection is unsuccessful or unfinished; the preceding successful current-main artifact still requires normal report verification."
        source_path = Path(previous_receipt.get("source_report") or ledger.get("source_report", ""))
        if candidate is None:
            report, sealed_hash = read_verified_report(source_path, previous_receipt, ledger)
            validate_report(report, now)
            receipt.update(status="degraded" if source_gaps(report) else "ok", outcome="pending_report",
                           source_report=str(source_path), report_completed_at=report["completed_at"], source_gaps=source_gaps(report),
                           report_sha256=sealed_hash,
                           last_failure_code=None, pending_reason="No newer successful current-main monitor artifact is complete.")
            return finish_quiet(config, receipt, report, now)
        run_id = str(candidate["id"])
        if run_id == str(ledger["last_notified_run"]) and not previous_receipt.get("verified_run"):
            run = github("run", run_id)
            if (run.get("path") != WORKFLOW_PATH or run.get("head_branch") != "main" or run.get("head_sha") != main_sha
                    or run.get("status") != "completed" or run.get("conclusion") != "success"):
                raise CheckError("run_identity_invalid", "The baseline hosted run does not match the current main monitor workflow.")
            source_path = Path(ledger.get("source_report", ""))
            report, sealed_hash = read_verified_report(source_path, ledger)
            completed = validate_report(report, now)
            if completed != timestamp(ledger.get("last_report_at"), "Baseline report"):
                raise CheckError("baseline_invalid", "The local baseline report does not match the preserved notification ledger.")
            receipt.update(status="degraded" if source_gaps(report) else "ok", outcome="no_newer_report", source_report=str(source_path),
                           verified_run=run_id, head_sha=main_sha, report_sha256=sealed_hash,
                           report_completed_at=completed.isoformat(), verification="existing_notified_baseline_readback; current_main_run_identity",
                           artifact_downloaded=False, source_gaps=source_gaps(report), last_failure_code=None)
            return finish_quiet(config, receipt, report, now)
        if previous_receipt.get("verified_run") == run_id and previous_receipt.get("head_sha") == main_sha:
            report, sealed_hash = read_verified_report(source_path, previous_receipt, ledger)
            validate_report(report, now)
            receipt.update(status="degraded" if source_gaps(report) else "ok", outcome="no_newer_report", last_failure_code=None,
                           report_sha256=sealed_hash, source_gaps=source_gaps(report))
            return finish_quiet(config, receipt, report, now)
        run = github("run", run_id)
        if (str(run.get("id")) != run_id or run.get("path") != WORKFLOW_PATH or run.get("head_branch") != "main"
                or run.get("head_sha") != main_sha or run.get("status") != "completed" or run.get("conclusion") != "success"):
            raise CheckError("run_identity_invalid", "The completed hosted run does not match the current main monitor workflow.")
        artifacts = [item for item in github("artifacts", run_id) if item.get("name") == "ipo-monitor-report" and not item.get("expired")
                     and isinstance(item.get("size_in_bytes"), int) and 0 < item["size_in_bytes"] <= 30_000_000]
        if len(artifacts) != 1 or not str(artifacts[0].get("id", "")).isdigit():
            raise CheckError("report_artifact_missing", "The completed hosted run has no unique usable research-report artifact.")
        directory = config.outputs / ("run-" + run_id)
        github("download", artifacts[0]["id"], directory)
        source_path = directory / "latest.json"
        report = read_json(source_path)
        completed = validate_report(report, now)
        if not timestamp(run.get("run_started_at") or run.get("created_at"), "Run start") <= completed <= timestamp(run.get("updated_at"), "Run completion"):
            raise CheckError("report_run_mismatch", "The actual downloaded report timestamp does not belong to its hosted run.")
        if github("main").get("sha") != main_sha:
            raise CheckError("main_changed", "Main changed during verification; publication is withheld until its new report completes.")
        baseline_time = timestamp(ledger.get("last_report_at"), "Baseline report")
        if int(run_id) < int(ledger["last_notified_run"]) or completed < baseline_time:
            raise CheckError("report_regression", "The hosted report is older than the preserved notification baseline.")
        snapshot = idea_snapshot(report)
        changes = material_changes(ledger["ideas"], snapshot) if completed > baseline_time else []
        old_ipos = ledger.get("ipos")
        if old_ipos is None:
            baseline_report = read_json(Path(ledger.get("source_report", "")))
            if timestamp(baseline_report.get("completed_at"), "Baseline IPO report") != baseline_time:
                raise CheckError("baseline_invalid", "The baseline IPO report timestamp does not match the preserved notification ledger.")
            old_ipos = ipo_snapshot(baseline_report)
        changed_ipos = ipo_changes(old_ipos, report) if completed > baseline_time else []
        maintenance = maintenance_needed(report, now, previous_receipt.get("maintenance_notified", {}))
        receipt.update(status="degraded" if source_gaps(report) else "ok", outcome="no_change", source_report=str(source_path),
                       verified_run=run_id, head_sha=main_sha, artifact_id=str(artifacts[0]["id"]),
                       report_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(), report_completed_at=completed.isoformat(),
                       verification="downloaded_actual_report_artifact; current_main_run_identity", artifact_downloaded=True,
                       source_gaps=source_gaps(report), last_failure_code=None)
        notice = ""
        if changes or changed_ipos or maintenance:
            fingerprint = hashlib.sha256(json.dumps([run_id, changes, changed_ipos, maintenance], sort_keys=True).encode()).hexdigest()[:16]
            digest_path = directory / ("update-" + fingerprint + ".md")
            digest_sha, created = publish(digest_path, digest_text(report, run, changes, changed_ipos, maintenance))
            # Commit only AFTER actual local delivery and readback. Quiet checks
            # preserve the old baseline so sub-threshold changes accumulate.
            if changes or changed_ipos:
                write_json(config.ledger, {**ledger, "last_report_at": report["completed_at"], "source_report": str(source_path),
                           "report_sha256": receipt["report_sha256"],
                           "last_notified_run": run_id, "ideas": snapshot, "ipos": ipo_snapshot(report),
                           "local_delivery": {"path": str(digest_path), "sha256": digest_sha, "target": "local_artifact"}})
            maintenance_state = dict(previous_receipt.get("maintenance_notified", {}))
            maintenance_state.update({item["id"]: now.isoformat() for item in maintenance})
            receipt.update(outcome="published_change" if created else "recovered_local_delivery", digest_path=str(digest_path),
                           digest_sha256=digest_sha, maintenance_notified=maintenance_state)
            if created:
                notice = f"Material IPO/trade research update saved locally: {digest_path}\nVerified report: {report['completed_at']}. No order was sent; email delivery is recorded separately.\n"
        write_json(config.receipt, receipt)
        return notice
    except CheckError as error:
        receipt.update(status="error", outcome="failure", failure_code=error.code, message=error.message)
        notice = ""
        if previous_receipt.get("last_failure_code") != error.code:
            digest_path = config.outputs / ("failure-" + hashlib.sha256(error.code.encode()).hexdigest()[:16] + ".md")
            text = f"# Deterministic monitor needs attention\n\nChecked: {now.isoformat()}\n\n{clean(error.message)}\n\nThe prior notification baseline is preserved. No unverified trade update or external message was sent.\n"
            digest_sha, _ = publish(digest_path, text)
            receipt.update(digest_path=str(digest_path), digest_sha256=digest_sha)
            notice = f"Monitor check needs attention: {error.message}\nLocal notice: {digest_path}\n"
        receipt["last_failure_code"] = error.code
        write_json(config.receipt, receipt)
        return notice


def request_due_collection(config, *, now=None, github=None):
    """One catch-up per report when the hosted hourly schedule misses two hours.

    Claim before POST: an ambiguous dispatch never permits another dispatch of
    the same checkpoint/report. Never add work behind a queued or running job.
    """
    now, github = now or datetime.now(UTC), github or GitHubHelper(config)
    receipt = read_json(config.receipt)
    if (receipt.get("status") not in {"ok", "degraded"}
            or receipt.get("newest_run_status") not in {None, "completed"}
            or receipt.get("newest_run_conclusion") not in {None, "success"}
            or now - timestamp(receipt.get("report_completed_at"), "Source report") < timedelta(hours=2)):
        return
    key = str(receipt.get("main_sha")) + ":" + str(receipt["report_completed_at"])
    if receipt.get("catchup_claim", {}).get("key") == key:
        return
    receipt["catchup_claim"] = {"key": key, "claimed_at": now.isoformat(), "outcome": "dispatch_unknown"}
    write_json(config.receipt, receipt)
    try:
        # Recheck immediately before the authorized mutation.
        if github("main").get("sha") != receipt.get("main_sha"):
            receipt["catchup_claim"]["outcome"] = "main_changed_no_dispatch"
        else:
            github("dispatch")
            receipt["catchup_claim"]["outcome"] = "requested"
    except CheckError:
        pass  # Existing pre-POST claim fences an uncertain accepted request.
    write_json(config.receipt, receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    args = parser.parse_args()
    config = Config(args.base.resolve())
    try:
        with check_lock(config.work / ".hermes-market-monitor.lock"):
            output = poll(config)
            request_due_collection(config)
            from market_notifications import process_notifications
            email_output = process_notifications(config)
            # Keep deterministic change digests as audit files. User alerts
            # under this policy require a verified AI-reviewed delivery.
            from market_notifications import delivery_config
            policy = delivery_config(config.work / "market-delivery.json", config.work)
            if policy.get("notification_policy") == "ai_approved_only" and not output.startswith("Monitor check needs attention:"):
                output = email_output
            else:
                output += email_output
        if output:
            print(output, end="")
        return 0  # receipt errors are deduped; Hermes wrapper can mark native failure.
    except CheckError as error:
        if error.code == "check_busy":
            return 0
        print("Deterministic monitor could not complete its local check; the previous baseline is preserved.")
        return 1
    except Exception as error:
        # Even unexpected failures must not leak Git/HTTP/environment traces.
        try:
            receipt = read_json(config.receipt, optional=True)
            receipt.update(status="error", outcome="failure", failure_code="local_pipeline_failed",
                           internal_error_type=type(error).__name__, completed_at=datetime.now(UTC).isoformat())
            if isinstance(error, ModuleNotFoundError) and re.fullmatch(r"[A-Za-z0-9_.]{1,100}", error.name or ""):
                receipt["missing_module"] = error.name
            write_json(config.receipt, receipt)
        except Exception:
            pass
        print("Deterministic monitor could not publish its local receipt; the previous baseline is preserved.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
