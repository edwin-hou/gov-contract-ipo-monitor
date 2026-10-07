"""Presentation helpers for sourced market research; no orders or live quotes."""
from __future__ import annotations

import ipaddress
import math
from html import escape
from typing import Any
from urllib.parse import quote, urlsplit


def safe_external_url(value: Any) -> str | None:
    """Keep restored/untrusted evidence from introducing active or local links."""
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
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return None
    except ValueError:
        return None
    # Parentheses and brackets cannot escape Markdown link destinations. Existing
    # percent escapes and ordinary HTTPS query separators remain usable.
    return quote(value, safe=":/?=&%#@+,-._~")


def display_number(value: Any, *, decimals: int = 2, suffix: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "Unavailable"
    return f"{value:,.{decimals}f}{suffix}"


def action_label(value: Any) -> str:
    return {"conditional_buy": "Conditional buy", "reduce_if_owned": "Reduce if already owned", "wait": "Wait"}.get(str(value), "Wait")


def _text(value: Any, fallback: str = "Unavailable") -> str:
    return escape(str(value if value is not None and value != "" else fallback), quote=True)


def _rows(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []


def _texts(value: Any) -> list[str]:
    return [str(item) for item in value if item is not None] if isinstance(value, (list, tuple)) else []


def _link(url: Any, label: Any = "Source") -> str:
    destination = safe_external_url(url)
    return f'<a href="{escape(destination, quote=True)}" target="_blank" rel="noopener noreferrer">{_text(label)}</a>' if destination else "Source unavailable"


def _list(values: Any, *, empty: str = "") -> str:
    items = _texts(values)
    return "<ul>" + "".join(f"<li>{_text(item)}</li>" for item in items) + "</ul>" if items else _text(empty, "")


def _coverage_detail(item: dict[str, Any]) -> str:
    return "; ".join(_texts([item.get("error"), *_texts(item.get("limitations"))]))


def market_sections_html(report: dict[str, Any], *, limit: int = 50) -> str:
    """Additive dashboard panels. Every external value is escaped before display."""
    limit = max(1, min(int(limit), 500))
    if not any(key in report for key in ("listed_companies", "universe", "trade_ideas", "price_coverage", "world_news", "world_coverage")):
        return ""
    sections = []
    metadata = report.get("universe") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    company_rows = []
    for company in _rows(report.get("listed_companies"))[:limit]:
        financials = company.get("financials") or {}
        if not isinstance(financials, dict):
            financials = {}
        basis = " ".join(_texts([financials.get("period_type"), financials.get("accounting_standard")]))
        company_rows.append(
            "<tr>"
            f"<td>{_text(company.get('name'))}<br><small>{_text(company.get('issuer_country'))}</small></td>"
            f"<td>{_text(company.get('symbol'))} · {_text(company.get('exchange'))}<br>{_text(company.get('trading_currency'))}</td>"
            f"<td>{display_number(company.get('revenue_growth_percent'), decimals=1, suffix='%')}</td>"
            f"<td>{display_number(company.get('net_margin_percent'), decimals=1, suffix='%')}</td>"
            f"<td>{display_number(financials.get('net_income'), decimals=0)} {_text(financials.get('currency'), '')}</td>"
            f"<td>Period: {_text(financials.get('period_end'))}<br>Reported: {_text(financials.get('reported_at'))}<br>{_text(basis, '')}</td>"
            f"<td>{'Passes research shortlist' if company.get('eligible') is True else 'Does not pass research shortlist'}{_list(company.get('reasons'))}</td>"
            f"<td>{_link(financials.get('source_url'), 'Financial results')}{_list(financials.get('limitations'))}</td></tr>"
        )
    company_body = "".join(company_rows) or '<tr><td colspan="8">No sourced listed-company research is saved yet.</td></tr>'
    company_count = len(_rows(report.get("listed_companies")))
    sample_note = f"<p>Showing the first {limit} of {company_count} configured companies.</p>" if company_count > limit else ""
    exclusions = "".join(
        f"<li>{_text(row.get('name'))}: {_text(row.get('reason'))} — {_link(row.get('source_url'))}</li>"
        for row in _rows(metadata.get("reviewed_exclusions"))
    )
    sections.append(
        '<div class="card"><h2>Listed-company growth and profit screen</h2>'
        '<p>A configurable research shortlist of global issuers, not an exhaustive global market screen. '
        'Historical growth and profit do not establish fair valuation or expected returns. Financial reporting currency can differ from trading currency; amounts are absolute currency units.</p>'
        f"<p>Research reviewed: {_text(metadata.get('reviewed_at'))}. {_text(metadata.get('methodology'), '')}</p>"
        f"{_list(metadata.get('limitations'))}"
        f"{'<p>Reviewed exclusions:</p><ul>' + exclusions + '</ul>' if exclusions else ''}{sample_note}"
        '<table><thead><tr><th>Issuer</th><th>Listing / trading currency</th><th>YoY total revenue growth</th><th>Reported net margin</th><th>Reported net income</th><th>Financial period / date</th><th>Screen and reasons</th><th>Primary evidence</th></tr></thead>'
        f"<tbody>{company_body}</tbody></table></div>"
    )
    ideas = _rows(report.get("trade_ideas"))[:limit]
    idea_rows, idea_details = [], []
    for idea in ideas:
        action = idea.get("action", "wait")
        indicators = idea.get("indicators") or {}
        if not isinstance(indicators, dict):
            indicators = {}
        currency = _text(idea.get("currency"))
        # A wait state must not accidentally expose stale saved order levels.
        active_setup = action == "conditional_buy"
        levels = [display_number(idea.get(key)) if active_setup or key == "invalidation" and action == "reduce_if_owned" else "—"
                  for key in ("entry", "invalidation", "target", "risk_reward")]
        checks = _texts(idea.get("conditions"))
        reasons = _texts(idea.get("reasons"))
        if action not in {"conditional_buy", "reduce_if_owned"} and not checks:
            checks = reasons or ["No verified setup or required evidence is available."]
        idea_rows.append(
            "<tr>"
            f"<td>{_text(idea.get('symbol'))} · {_text(idea.get('company'))}<br><small>{_text(idea.get('exchange'))} / {currency} / {_text(idea.get('listing_kind'))}</small></td>"
            f"<td>{action_label(action)}<br><small>{_text(checks[0], '') if checks else ''}</small></td><td>{display_number(indicators.get('last_close'))} {currency}<br>{_text(idea.get('price_as_of'))}</td>"
            + "".join(f"<td>{level}</td>" for level in levels) + "</tr>"
        )
        sources = " · ".join(_link(url, "Evidence") for url in _texts(idea.get("evidence_urls")))
        briefs = _rows(idea.get("evidence_briefs"))
        if briefs:
            sources = "<p>Evidence in plain language:</p><ul>" + "".join(
                f"<li>{_text(brief.get('claim'))} <strong>{_text(brief.get('meaning'))}</strong> "
                f"Limitation: {_text(brief.get('limitation'))}. " + " · ".join(_link(url, "Source") for url in _texts(brief.get("source_urls"))[:2]) + "</li>"
                for brief in briefs[:5]) + "</ul>"
        current = idea.get("current_quote") or {}
        quote_note = ""
        if isinstance(current, dict) and current:
            freshness = current.get("freshness") or {}
            quote_note = (f"<p>Latest price reference: <strong>{display_number(current.get('price'))} {currency}</strong>; "
                          f"as of {_text(current.get('quote_at') or current.get('session_date'))}, {_text(current.get('quote_type'))}. "
                          f"Checked {_text(current.get('observed_at'))}; freshness {_text(freshness.get('status', current.get('status')))}. "
                          f"{_link(current.get('source_url'), 'Quote source')}. Not an executable broker quote.</p>")
        schedule = (idea.get("strategy") or {}).get("timing") or {}
        window = schedule.get("entry_window") or {}
        if window:
            from .notifications import _window_text
            quote_note += f"<p>Conditional buy-check window in Nashville: {_text(_window_text(window, 'local'))}. Enter only if the price trigger, entry cap and fresh broker checks pass.</p>"
        context = "".join(
            f"<li>{_link(event.get('source_url'), event.get('title'))} — {_text(event.get('publisher'))}, {_text(event.get('published_at'))}; "
            f"{_text(', '.join(_texts(event.get('themes'))))}. {_text(event.get('interpretation'), 'Exposure interpretation is unverified; price direction is unknown.')}</li>"
            for event in _rows(idea.get("world_context"))
        )
        idea_details.append(
            f"<details><summary>{_text(idea.get('symbol'))}: {action_label(action)} — checks and evidence</summary>"
            f"<p>Horizon: {_text(idea.get('horizon'))}. Generated: {_text(idea.get('generated_at'))}. Confidence: {_text(idea.get('confidence'))}.</p>"
            f"{quote_note}"
            f"<p>{'Why wait' if action not in {'conditional_buy', 'reduce_if_owned'} else 'Conditions to review'}:</p>{_list(checks)}"
            f"<p>Research reasons:</p>{_list(reasons, empty='No supporting reason recorded.')}"
            f"<p>Risks and limitations:</p>{_list(_texts(idea.get('risks')) + _texts(idea.get('limitations')))}"
            f"{'<p>Related publisher reports (unverified exposure interpretation):</p><ul>' + context + '</ul>' if context else ''}"
            f"<p>{sources or 'No linked evidence is available.'}</p></details>"
        )
    idea_body = "".join(idea_rows) or '<tr><td colspan="7">No conditional trade ideas are saved yet.</td></tr>'
    sections.append(
        '<div class="card"><h2>Conditional trade ideas</h2>'
        '<p>Conditional buy means review the entry trigger and all checks before considering a purchase. Reduce if already owned is a review for an existing holding. Wait means the required evidence or setup is missing. No orders are placed.</p>'
        '<p>Trend calculations use completed daily observations, not executable live quotes. Separately collected quote references show their timestamp, type and freshness; none guarantees a broker execution price. Screening rules and AI reviews have no validated return forecast. Invalidation is a risk reference, not a guaranteed exit; gaps and costs can increase losses. Levels use the displayed trading currency.</p>'
        '<table><thead><tr><th>Company / listing</th><th>Research state</th><th>Last completed close / date</th><th>Conditional entry</th><th>Invalidation / holding review level</th><th>Target reference</th><th>Risk / reward reference</th></tr></thead>'
        f"<tbody>{idea_body}</tbody></table>{''.join(idea_details)}</div>"
    )
    price_rows = "".join(
        f"<tr><td>{_text(item.get('symbol'))}</td><td>{_text(item.get('source'))}</td><td>{_text(item.get('status'))}</td><td>{_text(item.get('as_of'))}</td><td>{_text(_coverage_detail(item), '')}</td></tr>"
        for item in _rows(report.get("price_coverage"))[:limit]
    ) or '<tr><td colspan="5">No price source receipt yet. Missing verified history keeps ideas in wait.</td></tr>'
    sections.append('<div class="card"><h2>Price coverage</h2><table><thead><tr><th>Symbol</th><th>Provider</th><th>Status</th><th>Completed price date</th><th>Error / limitation</th></tr></thead>' + f"<tbody>{price_rows}</tbody></table></div>")
    events = _rows(report.get("world_news"))
    world_rows = "".join(
        f"<tr><td>{_link(event.get('source_url'), event.get('title'))}</td><td>{_text(event.get('publisher'))}</td><td>{_text(event.get('published_at'))}</td><td>{_text(', '.join(_texts(event.get('themes'))), 'No matched theme')}</td>"
        f"<td>{_text(event.get('interpretation'), 'Theme relevance is an unverified inference.')} Price direction is unknown.</td></tr>"
        for event in events[:limit]
    ) or '<tr><td colspan="5">No publisher-reported world headlines saved yet.</td></tr>'
    sample_note = f"<p>Showing the latest {min(limit, len(events))} of {len(events)} saved headlines in this report.</p>" if len(events) > limit else ""
    sections.append(
        '<div class="card"><h2>World-news context</h2><p>Headlines and feed summaries reflect publisher selection. The underlying events are publisher-reported and unverified here. Themes are keyword matches; relevance to an issuer is an interpretation and does not establish market direction.</p>'
        + sample_note + '<table><thead><tr><th>Reported headline</th><th>Publisher</th><th>Published</th><th>Matched themes</th><th>Unverified interpretation</th></tr></thead>'
        + f"<tbody>{world_rows}</tbody></table></div>"
    )
    coverage_rows = "".join(
        f"<tr><td>{_link(item.get('source_url'), item.get('source'))}</td><td>{_text(item.get('status'))}</td><td>{_text(item.get('observed_at'))}</td><td>{_text(item.get('collected_count', 0))}</td><td>{_text(_coverage_detail(item), '')}</td></tr>"
        for item in _rows(report.get("world_coverage"))[:limit]
    ) or '<tr><td colspan="5">No world-news source receipt yet.</td></tr>'
    sections.append('<div class="card"><h2>World-news coverage</h2><table><thead><tr><th>Source</th><th>Status</th><th>Observed</th><th>Items</th><th>Error / limitation</th></tr></thead>' + f"<tbody>{coverage_rows}</tbody></table></div>")
    return "\n".join(sections)
