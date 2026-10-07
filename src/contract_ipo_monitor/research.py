"""Durable discourse evidence, collection receipts, and reviewable research reports."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .db import Database
from .sources.discourse import DiscourseBatch, DiscourseEvidence, evidence_from_dict


SCHEMA = """
CREATE TABLE IF NOT EXISTS discourse_evidence(
 id INTEGER PRIMARY KEY, evidence_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
 evidence_json TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 UNIQUE(evidence_id,payload_hash));
CREATE TABLE IF NOT EXISTS discourse_coverage(
 source TEXT NOT NULL, source_url TEXT NOT NULL, coverage_json TEXT NOT NULL,
 observed_at TEXT NOT NULL, PRIMARY KEY(source,source_url));
CREATE TABLE IF NOT EXISTS monitor_runs(
 id INTEGER PRIMARY KEY, completed_at TEXT NOT NULL, status TEXT NOT NULL, report_json TEXT NOT NULL);
"""


def serializable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item)))


class ResearchStore:
    def __init__(self, db: Database):
        self.db = db

    def initialize(self) -> None:
        with closing(self.db.connect()) as conn:
            conn.executescript(SCHEMA)

    def record_batch(self, batch: DiscourseBatch) -> int:
        inserted = 0
        with self.db.transaction() as conn:
            for evidence in batch.records:
                payload = serializable(asdict(evidence))
                # Retrieval time is an observation, never a new content version.
                content = {key: value for key, value in payload.items() if key != "retrieved_at"}
                digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
                stamp = evidence.retrieved_at.isoformat()
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO discourse_evidence(evidence_id,payload_hash,evidence_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    (evidence.evidence_id, digest, json.dumps(payload, sort_keys=True), stamp, stamp),
                )
                inserted += int(bool(cursor.rowcount))
                conn.execute("UPDATE discourse_evidence SET last_seen_at=MAX(last_seen_at,?) WHERE evidence_id=? AND payload_hash=?", (stamp, evidence.evidence_id, digest))
            conn.execute("DELETE FROM discourse_coverage")
            for coverage in batch.coverage:
                conn.execute(
                    "INSERT INTO discourse_coverage(source,source_url,coverage_json,observed_at) VALUES(?,?,?,?) ON CONFLICT(source,source_url) DO UPDATE SET coverage_json=excluded.coverage_json,observed_at=excluded.observed_at",
                    (coverage.source, coverage.source_url, json.dumps(serializable(asdict(coverage))), coverage.observed_at.isoformat()),
                )
        return inserted

    def records(self, *, limit: int = 10000) -> list[DiscourseEvidence]:
        with closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT evidence_json,last_seen_at FROM discourse_evidence ORDER BY last_seen_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        seen = set()
        for row in rows:
            data = json.loads(row["evidence_json"])
            if data["evidence_id"] in seen:
                continue
            seen.add(data["evidence_id"])
            data["retrieved_at"] = row["last_seen_at"]
            result.append(evidence_from_dict(data))
        return result

    def coverage(self) -> list[dict[str, Any]]:
        with closing(self.db.connect()) as conn:
            return [json.loads(row[0]) for row in conn.execute("SELECT coverage_json FROM discourse_coverage ORDER BY source,source_url")]

    def save_run(self, report: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO monitor_runs(completed_at,status,report_json) VALUES(?,?,?)", (report["completed_at"], report["status"], json.dumps(serializable(report))))
            conn.execute("DELETE FROM monitor_runs WHERE completed_at < ?", ((datetime.fromisoformat(report["completed_at"]) - timedelta(days=365)).isoformat(),))

    def latest_run(self) -> dict[str, Any] | None:
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT report_json FROM monitor_runs ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None


def report_markdown(report: dict[str, Any]) -> str:
    from html import escape
    from .dashboard import action_label, display_number, safe_external_url

    def clean(value: Any) -> str:
        value = "Unavailable" if value is None else str(value)
        return escape(value, quote=True).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("[", "\\[").replace("]", "\\]").replace("`", "\\`")

    def link(value: Any, label: Any = "Source") -> str:
        url = safe_external_url(value)
        return f"[{clean(label)}]({url})" if url else "Source unavailable"

    def items(value: Any) -> list[str]:
        return [str(item) for item in value if item is not None] if isinstance(value, (list, tuple)) else []

    def rows(value: Any) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []

    lines = ["# Global company, IPO and sentiment monitor", "", f"Run: {clean(report['completed_at'])} · **{clean(report['status'])}**", "",
             "IPO status comes from regulatory evidence. Sentiment describes the collected sample and never confirms an IPO or predicts returns.", "",
             "## Collection", "", "| Source | Status | Details |", "|---|---|---|"]
    for name, state in report.get("health", {}).get("collectors", {}).items():
        lines.append(f"| {clean(name)} | {'ok' if state.get('ok') else 'degraded'} | {clean(state.get('error') or state.get('last_success_at') or '')} |")
    counts = report.get("counts", {})
    if counts:
        activity_labels = {
            "contracts": "Contract records checked", "listing_signals": "Listing signals checked",
            "ipo_events": "Filing records checked for IPO evidence", "discourse_records": "Commentary versions added",
            "sam_records": "SAM records checked", "alerts_created": "Alerts created", "emails_sent": "Emails accepted by SMTP",
        }
        lines += ["", "## Run activity", "", "Checks may include previously saved records.", "",
                  "| Activity | Count |", "|---|---|"]
        for name, count in counts.items():
            lines.append(f"| {clean(activity_labels.get(name, name))} | {clean(count)} |")
    history = report.get("history")
    if history:
        lines += ["", "## Saved history", "", "| Evidence | Unique records | Saved versions |", "|---|---|---|",
                  f"| Contract records | {clean(history['contract_records'])} | {clean(history['contract_versions'])} |",
                  f"| Filings reviewed for IPO evidence | {clean(history['ipo_filings'])} | {clean(history['ipo_versions'])} |",
                  f"| Commentary items | {clean(history['commentary_items'])} | {clean(history['commentary_versions'])} |",
                  "", "Saved versions retain changes to the same record."]
    historic_gaps = report.get("historic_coverage_gaps", [])
    if historic_gaps:
        lines += ["", "## Unresolved historical coverage gaps", "",
                  "A healthy current collection does not resolve these earlier gaps.", "",
                  "| SEC form | Recorded at | Unresolved error |", "|---|---|---|"]
        for gap in historic_gaps:
            form = gap.get("name", "").removeprefix("sec_feed_gap:")
            if form == "legacy":
                form = "Unspecified legacy form"
            lines.append(f"| {clean(form)} | {clean(gap.get('updated_at', ''))} | {clean(gap.get('last_error', ''))} |")
    sec_collection = report.get("sec_collection")
    if isinstance(sec_collection, dict):
        lines += ["", "## SEC collection progress", "",
                  f"Status: **{clean(sec_collection.get("status"))}**. Pending filing documents: **{clean(sec_collection.get("pending_filings", 0))}**.",
                  f"Daily-index scope starts {clean(sec_collection.get("scope_start"))}; captured through {clean(sec_collection.get("captured_through"))}; latest published index seen {clean(sec_collection.get("published_through"))}.",
                  f"Published-index catch-up complete: {clean(sec_collection.get("catchup_complete"))}. {clean(sec_collection.get("scope"))}"]
        if sec_collection.get("current_feed_truncated_forms"):
            lines.append("Current-feed page windows reached their bound for: " + clean(", ".join(sec_collection["current_feed_truncated_forms"])) + ". Published daily indexes provide durable catch-up; pending work remains above.")
        if sec_collection.get("error"):
            lines.append("Collection error: " + clean(sec_collection["error"]))
        if sec_collection.get("issuer_review_count"):
            lines += ["", f"**{clean(sec_collection['issuer_review_count'])} SEC submissions await issuer attribution.** Affected IPO and listing assertions remain inactive."]
            for review in rows(sec_collection.get("issuer_reviews"))[:5]:
                lines.append(f"- {clean(review.get('accession'))} ({clean(review.get('form'))}): {clean(review.get('reason'))}")
    market_scope = any(key in report for key in ("listed_companies", "universe", "trade_ideas", "price_coverage", "world_news", "world_coverage"))
    if market_scope:
        metadata = report.get("universe") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        lines += ["", "## Listed-company growth and profit screen", "",
                  "A configurable shortlist of global issuers, not an exhaustive global market screen. Historical growth and profit do not establish fair valuation or expected returns. Amounts are absolute reporting-currency units; financial reporting currency can differ from trading currency.", "",
                  f"Research reviewed: {clean(metadata.get('reviewed_at'))}. {clean(metadata.get('methodology', ''))}", "",
                  *[f"- {clean(item)}" for item in items(metadata.get("limitations"))], "",
                  "| Issuer / country | Symbol / exchange / trading currency | YoY total revenue growth | Reported net margin | Net income / reporting currency | Period / reported date / basis | Screen and reasons | Primary evidence |",
                  "|---|---|---|---|---|---|---|---|"]
        companies = rows(report.get("listed_companies"))
        financial_notes = []
        for company in companies:
            financials = company.get("financials") or {}
            if not isinstance(financials, dict):
                financials = {}
            basis = " ".join(items([financials.get("period_type"), financials.get("accounting_standard")]))
            state = "Passes financial screen" if company.get("eligible") is True else "Does not pass financial screen"
            lines.append(f"| {clean(company.get('name'))} / {clean(company.get('issuer_country'))} | {clean(company.get('symbol'))} / {clean(company.get('exchange'))} / {clean(company.get('trading_currency'))} | {display_number(company.get('revenue_growth_percent'), decimals=1, suffix='%')} | {display_number(company.get('net_margin_percent'), decimals=1, suffix='%')} | {display_number(financials.get('net_income'), decimals=0)} {clean(financials.get('currency', ''))} | {clean(financials.get('period_end'))} / {clean(financials.get('reported_at'))} / {clean(basis)} | {state}: {clean('; '.join(items(company.get('reasons'))))} | {link(financials.get('source_url'), 'Financial results')} |")
            for limitation in items(financials.get("limitations")):
                financial_notes.append(f"{clean(company.get('symbol'))} financial limitation: {clean(limitation)}")
        for note in financial_notes:
            lines += ["", note]
        if not companies:
            lines += ["", "No sourced listed-company research is saved yet."]
        for exclusion in rows(metadata.get("reviewed_exclusions")):
            lines += ["", f"Reviewed exclusion — {clean(exclusion.get('name'))}: {clean(exclusion.get('reason'))}. {link(exclusion.get('source_url'))}."]
        discovery = rows(report.get("listed_discovery"))
        if "listed_discovery" in report:
            scope = report.get("listed_discovery_summary") or {}
            lines += ["", "## Newly discovered listed issuers — research/watch findings", "",
                      "Bounded current SEC 10-Q/10-K filings discover companies beyond the reviewed shortlist. A fresh comparable quarter must show at least 10% year-over-year entity-wide total revenue growth and positive reported US GAAP net income. These findings remain WAIT for security class, trading currency, price/news, valuation and trade review; no buy/sell instruction is implied.", "",
                      f"New issuer facts limit: {clean(scope.get('new_ciks_per_hour_limit', 5))} per hour. Current candidate view limit: {clean(scope.get('active_candidate_limit', 25))}. This is an incomplete US-filing sample, not an exhaustive global screen.", "",
                      "| Issuer / CIK | SEC ticker / exchange | State | YoY revenue / net margin | Profit / reporting currency | Period / reported | Review reasons | Primary financial / listing evidence |",
                      "|---|---|---|---|---|---|---|---|"]
            for candidate in discovery:
                financial = candidate.get("financials") or {}
                lines.append(f"| {clean(candidate.get('name'))} / {clean(candidate.get('cik'))} | {clean(candidate.get('symbol'))} / {clean(candidate.get('exchange'))} | {clean(candidate.get('status'))} | {display_number(candidate.get('revenue_growth_percent'), decimals=1, suffix='%')} / {display_number(candidate.get('net_margin_percent'), decimals=1, suffix='%')} | {display_number(financial.get('net_income'), decimals=0)} {clean(financial.get('currency'))} | {clean(financial.get('period_end'))} / {clean(financial.get('reported_at'))} | {clean('; '.join(items(candidate.get('reasons'))))} | {link(financial.get('source_url'), 'Financial filing')} / {link(candidate.get('listing_source_url'), 'SEC listing directory')} |")
            if not discovery:
                lines += ["", "No additional issuer currently passes or awaits the bounded discovery review. Rejected financial screens remain saved in the checkpoint."]
            lines += ["", "| Discovery source | Status | Observed | Items | Limitation / error |", "|---|---|---|---|---|"]
            for receipt in rows(report.get("listed_discovery_coverage")):
                lines.append(f"| {link(receipt.get('source_url'), receipt.get('source'))} | {clean(receipt.get('status'))} | {clean(receipt.get('observed_at'))} | {clean(receipt.get('collected_count', 0))} | {clean(receipt.get('error') or '; '.join(items(receipt.get('limitations'))))} |")
        lines += ["", "## Conditional trade ideas", "",
                  "Conditional buy means review the entry trigger and all checks before considering a purchase. Reduce if already owned is a review for an existing holding. Wait means the required evidence or setup is missing. No orders are placed.", "",
                  "Trend calculations use completed daily observations. Current quote references below are collected separately and labelled live, delayed, session-close or unavailable; none proves a broker execution price. Screening rules and AI reviews have no validated return forecast. Invalidation is a risk reference, not a guaranteed exit; gaps and costs can increase losses. Levels use the displayed trading currency.", "",
                  "| Company / listing / currency | Research state | Last completed close / date | Conditional entry | Invalidation / holding review level | Target reference | Risk / reward reference | Main check / why wait |",
                  "|---|---|---|---|---|---|---|---|"]
        ideas = rows(report.get("trade_ideas"))
        for idea in ideas:
            action = idea.get("action", "wait")
            indicator = idea.get("indicators") or {}
            if not isinstance(indicator, dict):
                indicator = {}
            levels = [display_number(idea.get(key)) if action == "conditional_buy" or key == "invalidation" and action == "reduce_if_owned" else "—"
                      for key in ("entry", "invalidation", "target", "risk_reward")]
            checks = items(idea.get("conditions")) or items(idea.get("reasons")) or ["No verified setup or required evidence is available."]
            lines.append(f"| {clean(idea.get('symbol'))} · {clean(idea.get('company'))} / {clean(idea.get('exchange'))} / {clean(idea.get('currency'))} / {clean(idea.get('listing_kind'))} | {action_label(action)} | {display_number(indicator.get('last_close'))} {clean(idea.get('currency'))} / {clean(idea.get('price_as_of'))} | {' | '.join(levels)} | {clean(checks[0])} |")
            strategy = idea.get("strategy") or {}
            if action == "conditional_buy" and isinstance(strategy, dict):
                risk = strategy.get("risk_budget") or {}
                lines += ["", f"{clean(idea.get('symbol'))} entry cap: {display_number(strategy.get('maximum_entry'))} {clean(idea.get('currency'))}; setup valid through {clean(strategy.get('setup_valid_through'))}. Review after 5 completed sessions and exit by 15 sessions from a verified actual fill; no fill or holding is assumed.",
                          f"Planned risk per share: {display_number(risk.get('planned_risk_per_share'))} {clean(idea.get('currency'))}. {clean(risk.get('budget_formula'))}. Quantity requires your portfolio value, loss allowance, actual entry, stop, costs and broker lot size: {clean(risk.get('quantity_formula'))}. No quantity is assumed.", ""]
        if not ideas:
            lines += ["", "No conditional trade ideas are saved yet."]
        for idea in ideas:
            action = idea.get("action", "wait")
            reasons, checks = items(idea.get("reasons")), items(idea.get("conditions"))
            if action not in {"conditional_buy", "reduce_if_owned"} and not checks:
                checks = reasons or ["No verified setup or required evidence is available."]
            lines += ["", f"### {clean(idea.get('symbol'))}: {action_label(action)}", "",
                      f"Horizon: {clean(idea.get('horizon'))}. Generated: {clean(idea.get('generated_at'))}. Confidence: {clean(idea.get('confidence'))}.", "",
                      "Why wait:" if action not in {"conditional_buy", "reduce_if_owned"} else "Conditions to review:", "",
                      *[f"- {clean(item)}" for item in checks], "", "Research reasons:", "",
                      *[f"- {clean(item)}" for item in reasons], "", "Risks and limitations:", "",
                      *[f"- {clean(item)}" for item in items(idea.get("risks")) + items(idea.get("limitations"))]]
            current = idea.get("current_quote") or {}
            if isinstance(current, dict) and current:
                freshness = current.get("freshness") or {}
                lines += ["", f"Latest price reference: **{display_number(current.get('price'))} {clean(idea.get('currency'))}**; as of {clean(current.get('quote_at') or current.get('session_date'))}, {clean(current.get('quote_type'))}. Checked {clean(current.get('observed_at'))}. Freshness: {clean(freshness.get('status', current.get('status')))} — {clean(freshness.get('reason'))}. {link(current.get('source_url'), 'Quote source')}."]
            schedule = (idea.get("strategy") or {}).get("timing") or {}
            window = schedule.get("entry_window") or {}
            if window:
                from .notifications import _window_text
                lines += ["", f"Conditional buy-check window in Nashville: **{clean(_window_text(window, 'local'))}**. A purchase still requires the price trigger, entry cap and fresh broker confirmation; this is not a predicted profitable time."]
                review_window, exit_window = schedule.get("illustrative_review") or {}, schedule.get("illustrative_time_exit") or {}
                if review_window and exit_window:
                    lines += [f"If actually filled on {clean(schedule.get('illustrative_entry_date'))}, illustrative review: {clean(_window_text(review_window, 'local'))}; time exit: {clean(_window_text(exit_window, 'local'))}. Recalculate from the real fill."]
            briefs = rows(idea.get("evidence_briefs"))
            if briefs:
                lines += ["", "Evidence in plain language:", ""]
                for brief in briefs[:5]:
                    sources = " · ".join(link(url, "Source") for url in items(brief.get("source_urls"))[:2])
                    lines.append(f"- {clean(brief.get('claim'))} **{clean(brief.get('meaning'))}** Limitation: {clean(brief.get('limitation'))}. {sources}")
            for event in rows(idea.get("world_context")):
                lines += ["", f"Related publisher report: {link(event.get('source_url'), event.get('title'))} — {clean(event.get('publisher'))}, {clean(event.get('published_at'))}; {clean(', '.join(items(event.get('themes'))))}. {clean(event.get('interpretation') or 'Exposure interpretation is unverified; price direction is unknown.')}"]
            urls = items(idea.get("evidence_urls"))
            if not briefs:
                lines += ["", "Evidence: " + (" · ".join(link(url) for url in urls) or "No linked evidence is available.")]
        lines += ["", "## Price coverage", "", "| Symbol | Provider | Status | Completed price date | Error / limitation |", "|---|---|---|---|---|"]
        for receipt in rows(report.get("price_coverage")):
            detail = "; ".join(items([receipt.get("error"), *items(receipt.get("limitations"))]))
            lines.append(f"| {clean(receipt.get('symbol'))} | {clean(receipt.get('source'))} | {clean(receipt.get('status'))} | {clean(receipt.get('as_of'))} | {clean(detail)} |")
        if not report.get("price_coverage"):
            lines += ["", "No price source receipt yet. Missing verified history keeps ideas in wait."]
        lines += ["", "## World-news context", "",
                  "Headlines and feed summaries reflect publisher selection. The underlying events are publisher-reported and unverified here. Themes are keyword matches; relevance to an issuer is an interpretation and does not establish market direction.", "",
                  "| Reported headline | Publisher | Published | Matched themes | Unverified interpretation |", "|---|---|---|---|---|"]
        events = rows(report.get("world_news"))
        for event in events[:50]:
            lines.append(f"| {link(event.get('source_url'), event.get('title'))} | {clean(event.get('publisher'))} | {clean(event.get('published_at'))} | {clean(', '.join(items(event.get('themes'))) or 'No matched theme')} | {clean(event.get('interpretation') or 'Theme relevance is an unverified inference.')} Price direction is unknown. |")
        if len(events) > 50:
            lines += ["", f"Showing the latest 50 of {len(events)} saved headlines in this report. The detailed audit data retains all entries."]
        if not events:
            lines += ["", "No publisher-reported world headlines saved yet."]
        lines += ["", "## World-news coverage", "", "| Source | Status | Observed | Items | Error / limitation |", "|---|---|---|---|---|"]
        for receipt in rows(report.get("world_coverage")):
            detail = "; ".join(items([receipt.get("error"), *items(receipt.get("limitations"))]))
            lines.append(f"| {link(receipt.get('source_url'), receipt.get('source'))} | {clean(receipt.get('status'))} | {clean(receipt.get('observed_at'))} | {clean(receipt.get('collected_count', 0))} | {clean(detail)} |")
        if not report.get("world_coverage"):
            lines += ["", "No world-news source receipt yet."]
    lines += ["", "## IPO evidence", "", "| Company | Stage | Evidence confidence | Source |", "|---|---|---|---|"]
    for candidate in report.get("ipos", []):
        url = next(iter(candidate.get("source_urls", [])), "")
        lines.append(f"| {clean(candidate['issuer_name'])} | {clean(candidate['status'])} | {clean(candidate['confidence'])} | {link(url)} |")
    if not report.get("ipos"):
        lines += ["", "No IPO evidence has been collected yet. Check collection coverage before interpreting an empty result."]
    lines += ["", "## Sentiment sample", "", "| Company | Sentiment | Scored items | Origins | Bias and coverage flags |", "|---|---|---|---|---|"]
    for company in report.get("sentiment", []):
        name = company.get("company_name", company.get("company", "Company"))
        flags = "; ".join(flag.replace("_", " ") for flag in company.get("bias_flags", []))
        lines.append(f"| {clean(name)} | {clean(company.get('label', 'unknown'))} | {clean(company.get('scored_count', 0))} | {clean(company.get('independent_origins', 0))} | {clean(flags)} |")
    lines += ["", "Full scores, evidence, and exclusions are saved in [the detailed audit data](latest.json).", "",
              "## Source coverage", "", "| Source | Status | Items | Limitation or error |", "|---|---|---|---|"]
    for source in report.get("coverage", []):
        detail = source.get("error") or "; ".join(source.get("limitations", []))
        lines.append(f"| {clean(source['source'])} | {clean(source['status'])} | {clean(source.get('collected_count', 0))} | {clean(detail)} |")
    lines += ["", "## Limitations", "", *[f"- {clean(item)}" for item in report.get("limitations", [])], ""]
    return "\n".join(lines)


def write_report(report: dict[str, Any], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in (("latest.json", json.dumps(serializable(report), indent=2, ensure_ascii=False)), ("latest.md", report_markdown(report))):
        target = directory / name
        temporary = directory / f".{name}.{os.getpid()}.tmp"
        temporary.write_text(content + "\n", encoding="utf-8")
        temporary.replace(target)


MAX_CHECKPOINT_BYTES = 250_000_000


def checkpoint_database(db: Database, destination: Path) -> None:
    """SQLite backup includes committed WAL data; copying the .db alone does not."""
    from .checkpoint import validate_database

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    try:
        with closing(db.connect()) as source, closing(sqlite3.connect(temporary)) as target:
            source.backup(target)
        # Match the portable artifact/restore contract before publication; an
        # unusable backup must never replace the preceding usable checkpoint.
        validate_database(temporary, max_bytes=MAX_CHECKPOINT_BYTES)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
