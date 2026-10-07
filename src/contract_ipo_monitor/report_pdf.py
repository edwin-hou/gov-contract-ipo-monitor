"""Readable, deterministic investment research PDFs, without network or orders."""
from __future__ import annotations

import math
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    CondPageBreak, KeepTogether, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)

_NAVY = colors.HexColor("#142C44")
_BLUE = colors.HexColor("#23649B")
_TEXT = colors.HexColor("#253746")
_MUTED = colors.HexColor("#556775")
_PALE = colors.HexColor("#EDF3F8")
_LINE = colors.HexColor("#D4E0E8")
_FONT = "IPOReportVera"
_BOLD = "IPOReportVeraBold"
_MAX_IDEAS = 12
_TRANSLATION = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2212": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u00a0": " ", "\u2009": " ", "\u202f": " ",
    "\u2264": "<=", "\u2265": ">=", "\u2192": "->", "\u2022": "-",
})


def _fonts() -> None:
    """Use fonts shipped with ReportLab, never a private machine font."""
    font_dir = Path(reportlab.__file__).parent / "fonts"
    for name, filename in ((_FONT, "Vera.ttf"), (_BOLD, "VeraBd.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))
    pdfmetrics.registerFontFamily(_FONT, normal=_FONT, bold=_BOLD, italic=_FONT, boldItalic=_BOLD)


def _text(value, limit: int = 800, *, fallback: str = "Unavailable") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, datetime):
        value = value.isoformat()
    text = re.sub(r"\s+", " ", str(value).translate(_TRANSLATION)).strip()
    if not text:
        return fallback
    # Unknown script runs receive an explicit readable fallback. Exact originals
    # remain in the JSON attachment, instead of producing missing-glyph boxes.
    supported = pdfmetrics.getFont(_FONT).face.charToGlyph
    text = re.sub(r"[^\x20-\x7e]+", lambda match: "".join(
        char if ord(char) in supported else "\ufffd" for char in match.group()), text)
    text = re.sub("\ufffd+", "[original text in JSON audit]", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "... (full text in JSON audit)"
    return text


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, (list, tuple)) else []


def _price(value, currency) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return "Unavailable"
    return f"{_text(currency, 12)} {value:,.2f}"


def _amount(value, currency) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "Unavailable"
    return f"{_text(currency, 12)} {value:,.2f}"


def _safe_url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 33 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        parsed.port
    except ValueError:
        return None
    return value


def _links(values, limit: int = 2, labels=None) -> str:
    result, seen = [], set()
    for index, value in enumerate(_list(values)):
        url = _safe_url(value)
        if not url or url in seen:
            continue
        seen.add(url)
        label = _text(labels[index] if labels and index < len(labels) else urlsplit(url).hostname.removeprefix("www."), 65)
        result.append(f'<link href="{escape(url, {chr(34): "&quot;"})}" color="#23649B">{escape(label)}</link>')
        if len(result) == limit:
            break
    return " | ".join(result)


def _instant(value, *, seconds: bool = False) -> str:
    """Show aware instants on Nashville's clock; never infer a missing zone."""
    try:
        from zoneinfo import ZoneInfo
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ValueError("Timestamp has no zone")
        return stamp.astimezone(ZoneInfo("America/Chicago")).strftime(
            "%a %b %d, %Y, %I:%M:%S %p %Z" if seconds else "%a %b %d, %Y, %I:%M %p %Z")
    except (TypeError, ValueError):
        return "Timestamp unavailable; inspect the original audit"


def _coverage_family(source: str) -> str:
    lowered = source.lower()
    for prefix, label in (("hackernews", "Hacker News"), ("youtube", "YouTube"), ("reddit", "Reddit"),
                          ("world_news", "World news"), ("news_rss", "Company news"), ("sec", "SEC"),
                          ("forum_rss", "Forums"), ("listed_discovery", "Listed-company discovery")):
        if lowered.startswith(prefix):
            return label
    return source


def _window(window, *, compact: bool = False) -> str:
    data = _dict(window)
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(data["local_timezone"])
        start = datetime.fromisoformat(data["local_open"])
        end = datetime.fromisoformat(data["local_close"])
        if start.utcoffset() is None or end.utcoffset() is None or end <= start:
            raise ValueError("Invalid session window")
        start, end = start.astimezone(zone), end.astimezone(zone)
        if compact:
            return end.strftime("%a %b %d, %Y by %I:%M %p %Z")
        return start.strftime("%a %b %d, %Y, %I:%M %p") + " - " + end.strftime("%I:%M %p %Z")
    except (KeyError, ValueError, TypeError):
        return "Calendar unavailable; verify exchange hours with your broker"


def _styles() -> dict:
    base = dict(fontName=_FONT, textColor=_TEXT, fontSize=9, leading=12.5,
                spaceAfter=3, splitLongWords=True)
    return {
        "body": ParagraphStyle("ResearchBody", **base),
        "small": ParagraphStyle("ResearchSmall", **{**base, "fontSize": 7.8, "leading": 10.5, "textColor": _MUTED}),
        "audit": ParagraphStyle("ResearchAudit", **{**base, "fontSize": 6.8, "leading": 8.8, "textColor": _MUTED, "spaceAfter": 3}),
        "table": ParagraphStyle("ResearchTable", **{**base, "fontSize": 7.8, "leading": 10.5, "textColor": _MUTED, "spaceAfter": 0}),
        # Keep headings attached to their first content flowable, including an
        # evidence brief already grouped with its caveat and source links.
        "appendix_title": ParagraphStyle("ResearchAppendixTitle", **{**base, "fontName": _BOLD, "fontSize": 16, "leading": 21, "textColor": _NAVY, "spaceBefore": 10, "spaceAfter": 6, "keepWithNext": True}),
        "heading": ParagraphStyle("ResearchHeading", **{**base, "fontName": _BOLD, "fontSize": 11.5, "leading": 15, "textColor": _NAVY, "spaceBefore": 6, "keepWithNext": True}),
        "title": ParagraphStyle("ResearchTitle", **{**base, "fontName": _BOLD, "fontSize": 24, "leading": 29, "textColor": _NAVY, "spaceAfter": 8, "keepWithNext": True}),
        "company": ParagraphStyle("ResearchCompany", **{**base, "fontName": _BOLD, "fontSize": 16, "leading": 21, "textColor": _NAVY, "spaceAfter": 4, "keepWithNext": True}),
        "metric_label": ParagraphStyle("ResearchMetricLabel", **{**base, "fontSize": 7.5, "leading": 10, "spaceAfter": 4, "textColor": _MUTED}),
        "metric": ParagraphStyle("ResearchMetric", **{**base, "fontName": _BOLD, "fontSize": 11, "leading": 14, "spaceAfter": 2}),
        "badge": ParagraphStyle("ResearchBadge", **{**base, "fontName": _BOLD, "fontSize": 9, "textColor": _BLUE, "spaceAfter": 5}),
    }


def report_pdf(report: dict, *, notice: str = "", test: bool = False) -> bytes:
    """Build an immutable PDF from the exact saved report, without fresh claims.

    The PDF is a bounded reading view. Its accompanying JSON is the complete
    audit, including original unsupported scripts and all source receipts.
    """
    if not isinstance(report, dict):
        raise ValueError("A saved report object is required")
    _fonts()
    ideas = _list(report.get("trade_ideas"))
    selected = report.get("notification_scope") == "ai_approved_only"
    if selected and any(not isinstance(idea, dict) or _dict(idea.get("ai_review")).get("decision") != "notify"
                        or idea.get("action") != "conditional_buy" for idea in ideas):
        raise ValueError("AI-only PDF contains an unapproved candidate")
    styles, story = _styles(), []

    def p(value, style="body", limit=800):
        return Paragraph(escape(_text(value, limit)), styles[style])

    def labelled(label, value, style="body", limit=800):
        return Paragraph(f"<b>{escape(label)}</b> {escape(_text(value, limit))}", styles[style])

    story += [p("Investment research", "title"),
              p("LAYOUT PREVIEW / NO ALERT" if test else ("AI-reviewed conditional opportunities" if selected else "Company and IPO research snapshot"), "badge"),
              labelled("Collected (Nashville):", _instant(report.get("completed_at")), "small", 100),
              labelled("Source timestamp:", report.get("completed_at"), "audit", 100),
              p("Conditions first. No purchase, fill, position or order is assumed. Prices are informational references; verify an executable broker quote before any decision.", "small", 400)]
    if notice:
        story.append(p(notice, limit=500))
    if not ideas:
        story += [p("No trade plan in this report", "heading"),
                  p("Wait. Required evidence or price conditions are incomplete; no entry is suggested.")]

    for number, raw_idea in enumerate(ideas[:_MAX_IDEAS]):
        idea = _dict(raw_idea)
        strategy = _dict(idea.get("strategy"))
        quote = _dict(idea.get("current_quote"))
        review = _dict(idea.get("ai_review"))
        currency = idea.get("currency")
        action = idea.get("action", "wait")
        label = {"conditional_buy": "CONDITIONAL BUY - trigger required", "reduce_if_owned": "REVIEW ONLY IF ALREADY OWNED", "wait": "WAIT - no entry suggested"}.get(action, "WAIT - unrecognized action")
        story += [CondPageBreak(150), Spacer(1, 9)]
        header = [p(f"{_text(idea.get('symbol'), 30)}  |  {_text(idea.get('company'), 90)}", "company"),
                  p(f"{_text(idea.get('exchange'), 40)}  /  {_text(currency, 12)}  /  {_text(idea.get('listing_kind'), 35, fallback='Listing type unavailable')}", "small"),
                  p(label, "badge")]
        quote_type = _text(quote.get("quote_type"), 30, fallback="unknown")
        quote_status = _text(_dict(quote.get("freshness")).get("status") or quote.get("status"), 30, fallback="unverified")
        if quote:
            header += [labelled("Latest price reference:", _price(quote.get("price"), quote.get("currency") or currency)),
                       p(f"Quote time (Nashville): {_instant(quote.get('quote_at'), seconds=True)} | {quote_type} | {quote_status}", "small"),
                       p(f"Provider checked: {_instant(quote.get('observed_at'))}. Delay: {_text(quote.get('delay_seconds'), 20, fallback='unknown')} seconds; {_text(quote.get('market_phase'), 30, fallback='Market phase unknown')} market. A session close is a historical reference, not a live execution price.", "small", 420),
                       p(f"Audit timestamps: quote {_text(quote.get('quote_at'), 100, fallback='unavailable')}; checked {_text(quote.get('observed_at'), 100)}.", "audit", 250)]
        else:
            header += [p("Latest quote unavailable. Refresh the quote before considering a new entry.", "body"),
                       labelled("Completed daily-price date:", idea.get("price_as_of"), "small", 100)]
        if quote_status not in {"fresh", "ok"} and quote:
            header.append(p("Quote freshness is not confirmed; do not use this reference for a new entry.", "body"))
        levels = [
            ("ENTRY TRIGGER", idea.get("entry")),
            ("DO NOT CHASE ABOVE", strategy.get("maximum_entry")),
            ("INVALIDATION / STOP", _dict(strategy.get("stop")).get("price", idea.get("invalidation"))),
            ("TARGET REFERENCE", _dict(strategy.get("target")).get("price", idea.get("target"))),
        ]
        cells = [[p(name, "metric_label"), p(_price(value, currency), "metric")] for name, value in levels]
        table = Table([cells], colWidths=[128] * 4, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _PALE), ("BOX", (0, 0), (-1, -1), .6, _LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        header += [table, Spacer(1, 7)]
        story.append(KeepTogether(header))

        if action == "conditional_buy":
            trigger = _dict(strategy.get("entry_trigger"))
            verification = trigger.get("verification") or "Only if a fresh regular-session broker quote confirms the entry trigger, acceptable spread and the entry cap."
            if isinstance(verification, str):
                verification = verification.replace("maximum_entry", "the maximum entry price")
            story += [labelled("When to buy:", verification, limit=500),
                      labelled("Skip or cancel:", f"Do not chase above {_price(strategy.get('maximum_entry'), currency)}. Setup valid through {_text(strategy.get('setup_valid_through'), 30, fallback='expiry unavailable')}; recheck evidence, corporate actions and costs.", limit=450),
                      labelled("When to sell after a real buy:", "Review an owned position at the stop, target or thesis invalidation, whichever occurs first. A stop or target does not prove a fill; gaps can worsen the exit.", limit=400)]
        elif action == "reduce_if_owned":
            story.append(p("No holding is assumed. These are review references for an existing owner, not a sell or short instruction for an account without this position.", limit=400))
        else:
            story.append(p("Wait. Required evidence or trading conditions are incomplete. The levels above are watch references, not an entry recommendation.", limit=350))
            for reason in _list(idea.get("reasons"))[:3]:
                story.append(p("- " + _text(reason, 350)))

        timing = _dict(strategy.get("timing"))
        time_exit = _dict(strategy.get("time_exit"))
        story += [labelled("Time plan:", f"Review after {_text(time_exit.get('review_after_sessions', 5), 10)} completed sessions; close or reassess by {_text(time_exit.get('exit_after_sessions', 15), 10)} sessions from an actual verified fill. No fill is assumed.", limit=400)]
        if timing.get("entry_window"):
            story.append(labelled("Approximate check window (Nashville/local):", _window(timing["entry_window"]), "small", 200))
        if timing.get("illustrative_review") or timing.get("illustrative_time_exit"):
            story.append(p(f"Illustration only: if first filled on {_text(timing.get('illustrative_entry_date'), 30)}, review {_window(timing.get('illustrative_review'), compact=True)}; time exit {_window(timing.get('illustrative_time_exit'), compact=True)}. Recalculate from your real fill.", "small", 600))
        elif timing.get("limitation"):
            story.append(p(timing["limitation"], "small", 400))
        if review:
            story += [labelled("AI view:", review.get("rationale"), limit=350),
                      labelled("Counterargument:", review.get("counterargument"), limit=350),
                      p(f"Review model: {_text(review.get('model'), 50)} / {_text(review.get('reasoning_effort'), 20)}. A model view is not a measured probability of profit.", "small", 200)]

        briefs = [_dict(x) for x in _list(idea.get("evidence_briefs"))[:5]]
        evidence_heading = p("Why this is on the watchlist", "heading")
        if not briefs:
            story += [evidence_heading, p("No concise linked evidence summary was saved. Missing evidence is a reason to wait; inspect the complete JSON audit.", limit=400)]
        for index, brief in enumerate(briefs):
            sources = _links(brief.get("source_urls"), labels=["Price history", "Benchmark history"] if brief.get("evidence_type") == "completed_price_history" else None)
            # ReportLab's keepWithNext cannot join a heading to a following
            # KeepTogether container. Put the first brief and its heading into
            # the same flat group so they move across the page boundary together.
            content = [evidence_heading] if index == 0 else []
            content.append(Paragraph("<b>- " + escape(_text(brief.get("claim"), 500)) + "</b> " + escape(_text(brief.get("meaning"), 350)), styles["body"]))
            if brief.get("limitation"):
                content.append(labelled("Caveat:", brief["limitation"], "small", 450))
            content.append(Paragraph("Sources: " + sources, styles["small"]) if sources else p("Coverage only: no linked source supports this summary.", "small"))
            story.append(KeepTogether(content))
        risks = _list(idea.get("risks"))[:4]
        if risks:
            story.append(p("Main risks", "heading"))
            for risk in risks:
                story.append(p("- " + _text(risk, 450), "small"))
        fundamentals = _dict(idea.get("fundamentals"))
        if fundamentals:
            reporting_currency = fundamentals.get("reporting_currency") or fundamentals.get("currency")
            story.append(p("Reported total revenue: " + _amount(fundamentals.get("revenue"), reporting_currency)
                           + "; reported net income: " + _amount(fundamentals.get("net_income"), reporting_currency) + ".", "small", 350))
            story.append(p("Financial period: " + _text(fundamentals.get("period_type"), 20) + " ended " + _text(fundamentals.get("period_end"), 25)
                           + "; reported " + _text(fundamentals.get("reported_at"), 25) + "; " + _text(fundamentals.get("accounting_standard"), 30)
                           + "; amounts in " + _text(reporting_currency, 12) + ". Trading and reporting currencies may differ.", "small", 350))
        story.append(Spacer(1, 8))

    if len(ideas) > _MAX_IDEAS:
        story.append(p(f"{len(ideas) - _MAX_IDEAS} additional plans are retained in the JSON audit; this PDF is bounded to {_MAX_IDEAS} readable plans.", "small"))
    story += [CondPageBreak(140), p("Source coverage and context", "appendix_title"),
              p("Source-run summary. The JSON audit preserves exact dates, full risks, original text, all receipts and waiting states. Collection and email delivery are separate records.", "small", 400)]
    sec_collection = _dict(report.get("sec_collection"))
    if sec_collection:
        complete = sec_collection.get("catchup_complete")
        catchup = "complete within this published-index scope" if complete is True else "incomplete" if complete is False else "unconfirmed"
        story.append(p("SEC filing review: " + _text(sec_collection.get("pending_filings"), 30, fallback="Unavailable")
                       + " pending documents. Index scope starts " + _text(sec_collection.get("scope_start"), 25)
                       + "; captured through " + _text(sec_collection.get("captured_through"), 25)
                       + "; latest published index seen " + _text(sec_collection.get("published_through"), 25)
                       + ". Index catch-up is " + catchup + ".", "small", 450))
        story.append(p("Daily-index filing dates have no verified intraday filing time. Pending documents have not yet been classified; index catch-up does not establish complete earlier, confidential, international or real-time filing coverage.", "small", 350))
        if sec_collection.get("error"):
            story.append(labelled("SEC collection error:", sec_collection["error"], "small", 300))
        if sec_collection.get("issuer_review_count"):
            story.append(p(_text(sec_collection["issuer_review_count"], 30)
                + " SEC submissions await issuer attribution. Affected IPO and listing assertions remain inactive.", "small", 300))
    coverage = []
    for key in ("coverage", "world_coverage", "price_coverage", "quote_coverage", "listed_discovery_coverage"):
        coverage.extend(_list(report.get(key)))
    groups = {}
    for raw in coverage[:1000]:
        row = _dict(raw)
        key = (_text(row.get("source"), 70), _text(row.get("status"), 25), _text(row.get("error"), 150, fallback=""))
        group = groups.setdefault(key, {"count": 0, "observed_at": "", "limitations": [], "source_urls": []})
        group["count"] += 1
        group["observed_at"] = max(group["observed_at"], _text(row.get("observed_at"), 100, fallback=""))
        for limitation in _list(row.get("limitations")):
            clean = _text(limitation, 260)
            if clean not in group["limitations"]:
                group["limitations"].append(clean)
        if row.get("source_url") not in group["source_urls"]:
            group["source_urls"].append(row.get("source_url"))
    if not groups:
        story.append(p("Source coverage receipts unavailable. Do not infer comprehensive internet or global-market coverage.", limit=300))
    families, gaps = {}, []
    for (source, status, error), group in sorted(groups.items()):
        name = _coverage_family(source)
        family = families.setdefault(name, {"statuses": {}, "count": 0, "source_urls": []})
        family["count"] += group["count"]
        family["statuses"][status] = family["statuses"].get(status, 0) + group["count"]
        family["source_urls"].extend(group["source_urls"])
        if error or status.lower() in {"error", "unavailable", "disabled", "stale", "unknown"}:
            detail = error or (group["limitations"][0] if group["limitations"] else "Required source evidence is unavailable.")
            gap = f"{source}: {status}. {detail}"
            if gap not in gaps:
                gaps.append(gap)
    if families:
        latest_observed = max((group["observed_at"] for group in groups.values()), default="")
        story.append(labelled("Latest source check (Nashville):", _instant(latest_observed), "small", 100))
        rows = [[p("SOURCE", "metric_label"), p("RECEIPT STATES", "metric_label"), p("COUNT", "metric_label")]]
        for name, family in list(families.items())[:12]:
            source_link = _links(family["source_urls"], limit=1, labels=[name] * len(family["source_urls"]))
            rows.append([Paragraph(source_link, styles["table"]) if source_link else p(name, "table"),
                         p(", ".join(f"{status}: {count}" for status, count in sorted(family["statuses"].items())), "table", 180),
                         p(f"{family['count']} receipt(s)", "table", 50)])
        table = Table(rows, colWidths=[155, 257, 100], hAlign="LEFT", repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), _PALE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LINEBELOW", (0, 0), (-1, 0), .5, _LINE), ("LEFTPADDING", (0, 0), (-1, -1), 4),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        story.append(table)
    if gaps:
        story.append(p("Source gaps", "heading"))
        for gap in gaps[:6]:
            story.append(p("- " + gap, "small", 350))
        if len(gaps) > 6:
            story.append(p("Additional gaps are retained in the JSON audit.", "small"))
    if len(families) > 12 or len(coverage) > 1000:
        story.append(p("Additional source receipts and gaps are retained in the full JSON audit.", "small"))
    contexts, seen_context = [], set()
    for raw in ideas[:_MAX_IDEAS]:
        idea = _dict(raw)
        for raw_context in _list(idea.get("world_context"))[:30]:
            context = _dict(raw_context)
            identity = str(context.get("source_url") or context.get("event_id") or context.get("title"))
            if identity not in seen_context:
                seen_context.add(identity)
                contexts.append((idea.get("symbol"), context))
    story.append(p("Relevant world-news context", "heading"))
    if not contexts:
        story.append(p("No company- or sector-linked world-news context was retained for these plans. This does not establish that macro risk is absent.", "small", 400))
    for symbol, context in contexts[:6]:
        story += [labelled(_text(symbol, 30) + ":", context.get("title") or context.get("text"), limit=350),
                  labelled("Relevance:", context.get("relevance") or context.get("interpretation") or "Exposure relevance is an inference; market direction is unknown.", "small", 300),
                  p(f"Published (Nashville): {_instant(context.get('published_at'))}. Direction: {_text(context.get('direction'), 30, fallback='unknown')}. Publisher-reported; independent verification is incomplete.", "small", 280)]
        links = _links([context.get("source_url")], limit=1)
        if links:
            story.append(Paragraph("Source: " + links, styles["small"]))
    if len(contexts) > 6:
        story.append(p("Additional relevant context is retained in the JSON audit.", "small"))
    story.append(p("Limits to interpretation", "heading"))
    limits = _list(report.get("limitations")) + _list(_dict(report.get("universe")).get("limitations"))
    for limitation in list(dict.fromkeys(_text(x, 400) for x in limits))[:3]:
        story.append(p("- " + limitation, "small"))
    story.append(p("This is a bounded research screen, not internet-wide sentiment or an exhaustive global opportunity list. Historical growth, headlines and AI review do not establish future returns. Stops do not guarantee execution; gaps, costs and currency moves can exceed planned losses.", "small", 500))
    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=(612, 792), leftMargin=50, rightMargin=50,
                                 topMargin=42, bottomMargin=48, title="Investment research report",
                                 author="Company and IPO monitor", pageCompression=1)

    def footer(pdf_canvas, doc):
        pdf_canvas.saveState()
        pdf_canvas.setStrokeColor(_LINE)
        pdf_canvas.line(50, 34, 562, 34)
        pdf_canvas.setFont(_FONT, 7)
        pdf_canvas.setFillColor(_MUTED)
        pdf_canvas.drawString(50, 22, "Conditional research | No order or position assumed")
        pdf_canvas.drawRightString(562, 22, f"Page {doc.page}")
        pdf_canvas.restoreState()

    def deterministic_canvas(*args, **kwargs):
        kwargs["invariant"] = 1
        return canvas.Canvas(*args, **kwargs)

    document.build(story, onFirstPage=footer, onLaterPages=footer, canvasmaker=deterministic_canvas)
    result = output.getvalue()
    if len(result) > 2_000_000:
        raise ValueError("PDF exceeds the supported attachment bound")
    return result
