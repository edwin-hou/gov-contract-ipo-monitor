"""Readable, self-contained email presentation for conditional research alerts."""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .dashboard import action_label, display_number, safe_external_url


_TEXT = "font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.55;color:#223247;"
_SMALL = "font-size:12px;line-height:1.5;color:#5c6b7f;"
_HEADING = "font-size:12px;font-weight:bold;letter-spacing:1px;text-transform:uppercase;color:#526680;"


def _text(value: Any, fallback: str = "Unavailable") -> str:
    return escape(str(value if value is not None and value != "" else fallback), quote=True)


def _rows(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []


def _values(value: Any) -> list[Any]:
    return [item for item in value if item is not None] if isinstance(value, (list, tuple)) else []


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _price(value: Any, currency: Any) -> str:
    rendered = display_number(value)
    return rendered if rendered == "Unavailable" else _text(currency, "") + " " + rendered


def _stamp(value: Any) -> str:
    """Give readable Nashville time while retaining the provider's exact instant."""
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.utcoffset() is None:
            return _text(value)
        local = stamp.astimezone(ZoneInfo("America/Chicago"))
        return _text(local.strftime("%b %d, %Y at %I:%M:%S %p %Z"))
    except (ValueError, TypeError, OverflowError):
        return _text(value)


def _window(window: Any) -> str:
    window = _mapping(window)
    try:
        zone = ZoneInfo(window["local_timezone"])
        start = datetime.fromisoformat(window["local_open"])
        end = datetime.fromisoformat(window["local_close"])
        if start.utcoffset() is None or end.utcoffset() is None or end <= start:
            raise ValueError("Unverified session window")
        start, end = start.astimezone(zone), end.astimezone(zone)
        return _text(start.strftime("%a %b %d, %Y, %I:%M %p") + "–" + end.strftime("%I:%M %p %Z"))
    except (ValueError, KeyError, TypeError, OverflowError):
        return "Calendar unavailable; verify exchange hours with your broker."


def _paragraph(value: Any, *, style: str = "") -> str:
    if isinstance(value, str):
        value = value.replace("maximum_entry", "the maximum entry price")
    return '<p style="margin:0 0 12px;' + style + '">' + _text(value) + "</p>"


def _source_links(brief: dict) -> str:
    labels = {
        "primary_financial": "Financial results", "completed_price_history": "Price history",
        "sampled_commentary": "Commentary sample", "publisher_company_news": "Company news",
        "publisher_sector_news": "Sector news", "publisher_world_news": "World news",
    }
    links = []
    for index, value in enumerate(_values(brief.get("source_urls"))[:2]):
        url = safe_external_url(value)
        if url:
            host = urlsplit(url).hostname or "Source"
            label = ("Benchmark history" if brief.get("evidence_type") == "completed_price_history" and index == 1
                     else labels.get(brief.get("evidence_type"), "Source"))
            links.append('<a href="' + escape(url, quote=True) + '" style="color:#1762b8;text-decoration:underline;">'
                         + _text(label) + " · " + _text(host.removeprefix("www.")) + "</a>")
    return "<br>".join(links) if links else "Coverage only; no linked source supports this summary."


def _evidence(idea: dict) -> str:
    # The email is the short reading layer. The attached report retains every
    # brief, its exact period and its full source/coverage limitations.
    briefs = _rows(idea.get("evidence_briefs"))[:3]
    if not briefs:
        reasons = _values(idea.get("reasons"))[:3]
        detail = "".join('<li style="margin-bottom:8px;">' + _text(reason) + "</li>" for reason in reasons)
        sources = {"source_urls": _values(idea.get("evidence_urls"))[:2]}
        return ('<ul style="margin:8px 0;padding-left:20px;">' + detail + "</ul>" if detail else
                _paragraph("Required evidence is incomplete.")) + '<p style="' + _SMALL + '">' + _source_links(sources) + "</p>"
    result = []
    for brief in briefs:
        detail = '<li style="margin-bottom:16px;"><strong>' + _text(brief.get("claim")) + "</strong><br>"
        detail += _text(brief.get("meaning"), "")
        detail += '<div style="margin-top:5px;' + _SMALL + '">' + _source_links(brief) + "</div></li>"
        result.append(detail)
    return ('<ul style="margin:10px 0 0;padding-left:20px;">' + "".join(result) + "</ul>"
            + '<p style="margin:6px 0 0;' + _SMALL + '">Historical results and price trends do not establish fair valuation or future returns. Headline and commentary claims may be unverified. Full evidence cautions are in the attached report.</p>')


def _plan(idea: dict) -> str:
    action, currency = idea.get("action"), idea.get("currency")
    strategy = _mapping(idea.get("strategy"))
    if action != "conditional_buy":
        return _paragraph("No entry is suggested. Required evidence or trading conditions are incomplete."
                          if action != "reduce_if_owned" else
                          "No holdings are on file. This is an owner review reference, with no sell or short instruction for your current account.")
    trigger, stop, target = (_mapping(strategy.get(key)) for key in ("entry_trigger", "stop", "target"))
    timing, schedule = _mapping(strategy.get("time_exit")), _mapping(strategy.get("timing"))
    risk = _mapping(strategy.get("risk_budget"))
    entry = trigger.get("price", idea.get("entry"))
    prices = [
        ("Buy trigger", entry), ("Maximum entry", strategy.get("maximum_entry")),
        ("Stop / invalidation", stop.get("price", idea.get("invalidation"))),
        ("Target reference", target.get("price", idea.get("target"))),
    ]
    table = '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:14px 0;">'
    for label, price in prices:
        table += '<tr><td style="padding:10px 12px;background:#f1f5f9;border-bottom:1px solid #dde5ef;">' + label + "</td>"
        table += '<td align="right" style="padding:10px 12px;background:#f1f5f9;border-bottom:1px solid #dde5ef;font-weight:bold;">' + _price(price, currency) + "</td></tr>"
    table += "</table>"
    details = '<p style="margin:0 0 8px;"><strong>When to consider buying</strong></p>'
    details += _paragraph(trigger.get("verification") or "Verify a fresh regular-session broker quote, the breakout trigger, acceptable spread and the entry cap.")
    if schedule.get("entry_window"):
        details += '<p style="margin:0 0 8px;">Nashville check window: <strong>' + _window(schedule["entry_window"]) + "</strong>.</p>"
    details += '<p style="margin:0 0 16px;' + _SMALL + '">Skip an entry above the cap, below invalidation, or after the setup expires on ' + _text(strategy.get("setup_valid_through"), "the fifth session") + ". A scheduled window is not a predicted profitable time.</p>"
    details += '<p style="margin:0 0 8px;"><strong>When to review an exit, only after an actual buy</strong></p>'
    details += '<p style="margin:0 0 12px;">Review at the stop or target, or if the thesis fails. Review after <strong>' + _text(timing.get("review_after_sessions", 5)) + " completed sessions</strong>; close or reassess by <strong>session " + _text(timing.get("exit_after_sessions", 15)) + "</strong> from your actual verified fill.</p>"
    if schedule.get("illustrative_review") and schedule.get("illustrative_time_exit"):
        details += '<p style="margin:0 0 12px;' + _SMALL + '">Illustration only: if filled on ' + _text(schedule.get("illustrative_entry_date")) + ", review " + _window(schedule["illustrative_review"]) + "; time exit " + _window(schedule["illustrative_time_exit"]) + ". Recalculate from your real fill; none is assumed.</p>"
    details += '<p style="margin:0 0 14px;' + _SMALL + '">Planned risk per share: ' + _price(risk.get("planned_risk_per_share"), risk.get("currency", currency)) + ", plus costs. Choose size from your cash and loss budget; no quantity is assumed. Gaps can exceed the planned loss and prevent an exit at the stop.</p>"
    return table + details


def _card(idea: dict) -> str:
    action = idea.get("action")
    color = "#1b5b4b" if action == "conditional_buy" else "#67501f"
    parts = ['<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:18px;border:1px solid #dbe3ec;border-radius:12px;background:#ffffff;"><tr><td style="padding:24px;">']
    parts += ['<p style="margin:0 0 7px;font-size:12px;font-weight:bold;color:' + color + ';">' + _text(action_label(action)).upper() + " · CONDITIONAL RESEARCH</p>",
              '<h2 style="margin:0 0 5px;font-size:25px;line-height:1.2;color:#12243b;">' + _text(idea.get("symbol")) + "</h2>",
              '<p style="margin:0 0 18px;' + _SMALL + '">' + _text(idea.get("company"), "") + " · " + _text(idea.get("exchange")) + " · " + _text(idea.get("currency")) + "</p>"]
    quote = _mapping(idea.get("current_quote"))
    if quote:
        quote_label = {"session_close": "Latest regular-session close", "delayed": "Delayed price reference", "live": "Latest price reference"}.get(quote.get("quote_type"), "Unverified price reference")
        label = quote_label + " · " + str(quote.get("market_phase", "unknown")) + " market"
        parts += ['<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef4fb;border-radius:8px;"><tr><td style="padding:15px 16px;">',
                  '<p style="margin:0 0 4px;' + _HEADING + '">' + _text(label) + "</p>",
                  '<p style="margin:0 0 5px;font-size:27px;line-height:1.3;font-weight:bold;color:#12243b;">' + _price(quote.get("price"), idea.get("currency")) + "</p>",
                  '<p style="margin:0;' + _SMALL + '">Price as of ' + _stamp(quote.get("quote_at") or quote.get("session_date")) + "<br>Provider checked " + _stamp(quote.get("observed_at")) + "</p>"]
        delay = quote.get("delay_seconds")
        if isinstance(delay, (float, int)) and not isinstance(delay, bool) and delay > 0:
            parts.append('<p style="margin:5px 0 0;' + _SMALL + '">Provider-reported delay: ' + _text(delay) + " seconds.</p>")
        elif delay is None:
            parts.append('<p style="margin:5px 0 0;' + _SMALL + '">Provider delay is unverified.</p>')
        freshness = _mapping(quote.get("freshness"))
        parts.append('<p style="margin:5px 0 0;' + _SMALL + '">Quote validation: ' + _text(freshness.get("status") or quote.get("status"), "unknown") + ". Daily history through " + _text(idea.get("price_as_of")) + ".</p>")
        parts.append('<p style="margin:6px 0 0;' + _SMALL + '">Informational reference. Confirm the fresh executable price and spread with your broker.</p></td></tr></table>')
    else:
        parts.append(_paragraph("Current quote unavailable. Do not treat the completed daily price as a current execution price.", style=_SMALL))
    review = _mapping(idea.get("ai_review"))
    if review:
        parts += ['<p style="margin:18px 0 6px;' + _HEADING + '">Why the AI flagged this</p>',
                  _paragraph(review.get("rationale")),
                  '<p style="margin:0 0 14px;padding:10px 12px;border-left:3px solid #bc8c3a;background:#fff8e9;"><strong>What could go wrong:</strong> ' + _text(review.get("counterargument")) + "</p>"]
    parts += [_plan(idea), '<p style="margin:20px 0 6px;' + _HEADING + '">Evidence, briefly</p>', _evidence(idea)]
    risks = _values(idea.get("risks"))[:2]
    if risks:
        parts.append('<p style="margin:18px 0 7px;font-weight:bold;">Key risks</p><ul style="margin:0;padding-left:20px;' + _SMALL + '">' + "".join('<li style="margin-bottom:6px;">' + _text(item) + "</li>" for item in risks) + "</ul>")
    parts.append("</td></tr></table>")
    return "".join(parts)


def report_email_html(report: dict, *, notice: str = "", test: bool = False) -> str:
    """Escape source text, link only safe HTTPS URLs, and embed no remote assets."""
    ideas = _rows(report.get("trade_ideas"))
    selected = report.get("notification_scope") == "ai_approved_only"
    if selected and any(_mapping(idea.get("ai_review")).get("decision") != "notify" for idea in ideas):
        raise ValueError("AI-only mail contains an unapproved candidate")
    title = "An opportunity to review" if selected else "Your investment research"
    count = str(len(ideas))
    subtitle = count + (" conditional setup" if len(ideas) == 1 else " research setups") + " · 5–15 trading sessions"
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Investment research</title></head>',
             '<body style="margin:0;padding:0;background:#f2f5f9;' + _TEXT + '"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f5f9;"><tr><td align="center" style="padding:20px 10px;">',
             '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;"><tr><td>',
             '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#12243b;border-radius:12px;"><tr><td style="padding:26px 24px;">',
             '<p style="margin:0 0 10px;font-size:11px;font-weight:bold;letter-spacing:1.5px;color:#afc5df;">INVESTMENT MONITOR</p>',
             '<h1 style="margin:0 0 10px;font-size:27px;line-height:1.25;color:#ffffff;">' + title + "</h1>",
             '<p style="margin:0;color:#d8e5f4;font-size:14px;">' + subtitle + "</p></td></tr></table>",
             '<p style="margin:15px 8px 0;' + _SMALL + '">Report collected ' + _stamp(report.get("completed_at")) + ".<br>Holdings on file: none. No purchase, fill, position or order is assumed.</p>"]
    if test:
        parts.append('<p style="margin:14px 8px;padding:12px;background:#fff4da;color:#67501f;">Preview / pipeline test. This does not announce a new buy or sell decision.</p>')
    if notice:
        parts.append('<p style="margin:14px 8px;">' + _text(notice) + "</p>")
    parts.extend(_card(idea) for idea in ideas)
    if not ideas:
        parts.append('<p style="padding:20px;background:#ffffff;border:1px solid #dbe3ec;border-radius:8px;">No actionable setup is included in this report.</p>')
    parts += ['<p style="margin:20px 8px 8px;' + _SMALL + '">Conditional research plans; not execution instructions or a promise of profit. Valuation, spreads, suitability and actual fills still need verification. Public commentary is a biased sample, not internet-wide opinion. Stop references do not guarantee an exit price.</p>',
              '<p style="margin:0 8px;' + _SMALL + '">The attached report retains financial periods, currencies, source dates and coverage limitations. Collection and verified delivery are recorded separately.</p>',
              "</td></tr></table></td></tr></table></body></html>"]
    return "".join(parts)
