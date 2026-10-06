from copy import deepcopy
from io import BytesIO
import re

import pytest
from pypdf import PdfReader

from contract_ipo_monitor.report_pdf import report_pdf


def sample_report():
    return {
        "completed_at": "2026-10-06T02:39:44+00:00", "status": "degraded",
        "trade_ideas": [{
            "symbol": "MU", "company": "Micron Technology", "exchange": "NASDAQ", "currency": "USD",
            "listing_kind": "ordinary", "action": "conditional_buy", "entry": 1109.83,
            "invalidation": 1026.11, "target": 1277.27, "price_as_of": "2026-10-05",
            "current_quote": {"price": 1063.96, "currency": "USD", "quote_at": "2026-10-05T20:00:02+00:00",
                              "observed_at": "2026-10-06T02:39:03+00:00", "quote_type": "session_close",
                              "market_phase": "closed", "delay_seconds": 0, "status": "fresh"},
            "strategy": {"maximum_entry": 1120.29, "setup_valid_through": "2026-10-12",
                         "entry_trigger": {"verification": "Confirm breakout and acceptable spread with a fresh broker quote."},
                         "time_exit": {"review_after_sessions": 5, "exit_after_sessions": 15}},
            "fundamentals": {"period_type": "quarter", "period_end": "2026-09-03", "reported_at": "2026-09-30",
                             "reporting_currency": "USD", "accounting_standard": "US GAAP",
                             "revenue": 54229000000, "net_income": 37701000000},
            "evidence_briefs": [{"id": "financial", "claim": "Quarter revenue USD 54,229,000,000, up 379.3% YoY.",
                                 "meaning": "Historical growth meets the screen, not a valuation forecast.",
                                 "limitation": "Fourteen weeks versus thirteen in the comparison quarter.",
                                 "source_urls": ["https://investors.micron.com/results?view=full&year=2026"]}],
            "risks": ["AI memory cycle and valuation risk."]}],
        "coverage": [{"source": "reddit", "status": "error", "error": "HTTP 403",
                      "observed_at": "2026-10-06T02:39:18+00:00", "source_url": "https://www.reddit.com",
                      "limitations": ["Commentary unavailable; no bypass."]}],
        "limitations": ["English-language sample; selection bias remains."],
    }


def reader(report=None, **kwargs):
    return PdfReader(BytesIO(report_pdf(report or sample_report(), **kwargs)))


def text(pdf):
    return re.sub(r"\s+", " ", "\n".join(page.extract_text() for page in pdf.pages))


def links(pdf):
    return [str(annotation.get_object().get("/A", {}).get("/URI"))
            for page in pdf.pages for annotation in page.get("/Annots", [])
            if annotation.get_object().get("/A", {}).get("/URI")]


def test_saved_prices_periods_trade_limits_and_no_fill_are_readable():
    pdf = reader()
    rendered = text(pdf)
    for expected in ("Micron Technology", "NASDAQ", "USD 1,063.96", "USD 1,109.83", "USD 1,120.29",
                     "USD 1,026.11", "USD 1,277.27", "2026-10-05T20:00:02+00:00", "session_close",
                     "5 completed sessions", "15 sessions", "No fill is assumed", "2026-09-03", "2026-09-30",
                     "USD 54,229,000,000", "HTTP 403", "selection bias"):
        assert expected in rendered
    assert "not a live execution price" in rendered
    assert "not a valuation forecast" in rendered
    assert "reported net income: USD 37,701,000,000.00" in rendered
    assert "Page 1" in rendered
    assert len(pdf.pages) <= 4
    assert links(pdf) == ["https://investors.micron.com/results?view=full&year=2026", "https://www.reddit.com"]


def test_pdf_bytes_are_deterministic_and_report_is_unchanged():
    report = sample_report()
    before = deepcopy(report)
    assert report_pdf(report) == report_pdf(report)
    assert report == before


@pytest.mark.parametrize("action, expected", [
    ("wait", "WAIT - no entry suggested"),
    ("reduce_if_owned", "not a sell or short instruction"),
    ("unrecognized", "WAIT - unrecognized action"),
])
def test_wait_and_owned_only_never_assume_a_purchase(action, expected):
    report = sample_report()
    report["trade_ideas"][0]["action"] = action
    rendered = text(reader(report))
    assert expected in rendered
    assert "No purchase, fill, position or order is assumed" in rendered


def test_missing_quote_and_evidence_are_explicit_wait_reasons():
    report = sample_report()
    idea = report["trade_ideas"][0]
    idea.pop("current_quote")
    idea["evidence_briefs"] = []
    rendered = text(reader(report))
    assert "Latest quote unavailable" in rendered
    assert "Completed daily-price date: 2026-10-05" in rendered
    assert "Missing evidence is a reason to wait" in rendered


def test_untrusted_markup_and_unsafe_link_schemes_are_not_executed():
    report = sample_report()
    brief = report["trade_ideas"][0]["evidence_briefs"][0]
    brief["claim"] = '<img src="file:///secret"> & <b>untrusted</b>'
    brief["source_urls"] = ["javascript:alert(1)", "file:///secret", "https://user:password@example.com/x",
                            "https://example.com/valid?a=1&b=2", 'https://example.com/\nheader']
    pdf = reader(report)
    assert '<img src="file:///secret"> & <b>untrusted</b>' in text(pdf)
    assert "https://example.com/valid?a=1&b=2" in links(pdf)
    assert not any("secret" in url or "javascript" in url or "password" in url for url in links(pdf))


def test_typographic_punctuation_and_unavailable_script_have_readable_fallback():
    report = sample_report()
    report["trade_ideas"][0]["evidence_briefs"][0]["claim"] = "5–15 sessions; café; 東京; £123.45; €12.00; loss ≤ 10%."
    rendered = text(reader(report))
    assert "5-15 sessions" in rendered and "café" in rendered
    assert "original text in JSON audit" in rendered
    assert "£123.45" in rendered and "€12.00" in rendered and "<= 10%" in rendered


def test_ai_only_report_rejects_unapproved_or_non_buy_candidates():
    report = sample_report()
    report["notification_scope"] = "ai_approved_only"
    with pytest.raises(ValueError, match="unapproved"):
        report_pdf(report)
    report["trade_ideas"][0]["ai_review"] = {"decision": "notify", "model": "gpt-5.6-sol",
                                          "reasoning_effort": "medium", "rationale": "Evidence supports a conditional watch.",
                                          "counterargument": "Valuation can prevent a favorable return."}
    assert "AI-reviewed conditional opportunities" in text(reader(report))
    report["trade_ideas"][0]["action"] = "wait"
    with pytest.raises(ValueError, match="unapproved"):
        report_pdf(report)


def test_preview_is_not_an_alert_and_invalid_numbers_are_not_prices():
    report = sample_report()
    report["trade_ideas"][0]["current_quote"]["price"] = float("nan")
    report["trade_ideas"][0]["current_quote"]["status"] = "stale"
    rendered = text(reader(report, test=True))
    assert "LAYOUT PREVIEW / NO ALERT" in rendered
    assert "Latest price reference: Unavailable" in rendered
    assert "Quote freshness is not confirmed" in rendered
    assert "USD nan" not in rendered


def test_world_context_and_duplicate_source_failures_are_bounded_and_retained():
    report = sample_report()
    report["coverage"] *= 50
    report["trade_ideas"][0]["world_context"] = [{"title": "Chip export restrictions under review", "direction": "unknown",
                                               "published_at": "2026-10-05T18:00:00+00:00",
                                               "relevance": "Sector exposure; price impact remains unknown.",
                                               "source_url": "https://official.example.com/news"}]
    pdf = reader(report)
    rendered = text(pdf)
    assert "50 receipt(s)" in rendered and rendered.count("HTTP 403") == 1
    assert "Chip export restrictions under review" in rendered
    assert "price impact remains unknown" in rendered
    assert "https://official.example.com/news" in links(pdf)


def test_extreme_untrusted_text_cannot_create_unbounded_pages():
    report = sample_report()
    report["trade_ideas"][0]["company"] = "Long company " * 10000
    report["trade_ideas"][0]["evidence_briefs"] *= 1000
    for brief in report["trade_ideas"][0]["evidence_briefs"]:
        brief["claim"] = "Long evidence " * 10000
    pdf = reader(report)
    assert len(pdf.pages) <= 5
    assert "full text in JSON audit" in text(pdf)


def test_no_trade_plan_and_unknown_coverage_are_visible():
    rendered = text(reader({"completed_at": "2026-10-06T02:39:44+00:00", "trade_ideas": []}))
    assert "No trade plan in this report" in rendered
    assert "Source coverage receipts unavailable" in rendered


def test_session_windows_have_local_dates_and_reject_naive_timestamps():
    report = sample_report()
    timing = {"entry_window": {"local_timezone": "America/Chicago", "local_open": "2026-10-06T08:30:00-05:00",
                               "local_close": "2026-10-06T15:00:00-05:00"}}
    report["trade_ideas"][0]["strategy"]["timing"] = timing
    assert "Tue Oct 06, 2026, 08:30 AM - 03:00 PM CDT" in text(reader(report))
    timing["entry_window"]["local_open"] = "2026-10-06T08:30:00"
    assert "Calendar unavailable; verify exchange hours" in text(reader(report))


def test_instants_use_nashville_evening_date_and_keep_exact_quote_provenance():
    report = sample_report()
    report["completed_at"] = "2026-10-06T03:01:00+00:00"
    rendered = text(reader(report))
    assert "Collected (Nashville): Mon Oct 05, 2026, 10:01 PM CDT" in rendered
    assert "Quote time (Nashville): Mon Oct 05, 2026, 03:00:02 PM CDT" in rendered
    assert "Provider checked: Mon Oct 05, 2026, 09:39 PM CDT" in rendered
    assert "quote 2026-10-05T20:00:02+00:00; checked 2026-10-06T02:39:03+00:00" in rendered
    assert "Financial period: quarter ended 2026-09-03; reported 2026-09-30" in rendered


def test_naive_instants_remain_unavailable_and_same_host_history_links_are_distinct():
    report = sample_report()
    idea = report["trade_ideas"][0]
    idea["current_quote"]["quote_at"] = "2026-10-05T20:00:02"
    idea["evidence_briefs"].append({"evidence_type": "completed_price_history", "claim": "Trend reference",
                                    "meaning": "Historical prices require a fresh quote.",
                                    "source_urls": ["https://api.nasdaq.com/quote/MU", "https://api.nasdaq.com/quote/ACWI"]})
    pdf = reader(report)
    rendered = text(pdf)
    assert "Quote time (Nashville): Timestamp unavailable" in rendered
    assert "Price history | Benchmark history" in rendered
    assert "https://api.nasdaq.com/quote/MU" in links(pdf)
    assert "https://api.nasdaq.com/quote/ACWI" in links(pdf)
