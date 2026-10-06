"""Local presentation/security checks; no quote, model or email network calls."""
from copy import deepcopy
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

import pytest

from contract_ipo_monitor.email_html import report_email_html
from contract_ipo_monitor.gmail_delivery import message_content_sha256
from contract_ipo_monitor.notifications import report_message


NOW = datetime(2026, 10, 5, 22, tzinfo=UTC)


def report():
    return {
        "completed_at": NOW.isoformat(), "status": "degraded", "health": {}, "counts": {},
        "notification_scope": "ai_approved_only", "holdings": [],
        "trade_ideas": [{
            "symbol": "EXAMPLE", "company": "Example issuer", "exchange": "NASDAQ", "currency": "USD",
            "action": "conditional_buy", "entry": 101., "invalidation": 95., "target": 113.,
            "price_as_of": "2026-10-05", "current_quote": {
                "price": 100., "quote_type": "session_close", "market_phase": "closed", "status": "fresh",
                "quote_at": "2026-10-05T20:00:02+00:00", "observed_at": NOW.isoformat(), "delay_seconds": 0,
                "freshness": {"status": "fresh", "executable": False}},
            "ai_review": {"decision": "notify", "model": "gpt-5.6-sol", "reasoning_effort": "medium",
                          "rationale": "A conditional opportunity if the price trigger and broker checks pass.",
                          "counterargument": "Growth may already be priced in."},
            "strategy": {"maximum_entry": 102., "setup_valid_through": "2026-10-12",
                "entry_trigger": {"price": 101., "verification": "Confirm the breakout with a fresh broker quote."},
                "stop": {"price": 95.}, "target": {"price": 113.},
                "time_exit": {"review_after_sessions": 5, "exit_after_sessions": 15},
                "risk_budget": {"planned_risk_per_share": 6., "currency": "USD"},
                "timing": {"entry_window": {
                    "local_timezone": "America/Chicago", "local_open": "2026-10-06T08:30:00-05:00",
                    "local_close": "2026-10-06T15:00:00-05:00"}}},
            "evidence_briefs": [
                {"evidence_type": "primary_financial", "claim": "Reported revenue increased.",
                 "meaning": "Supports growth, but not a valuation forecast.", "limitation": "Historical reported results.",
                 "source_urls": ["https://investors.example.com/results?period=quarter&currency=USD"]},
                {"evidence_type": "completed_price_history", "claim": "The completed trend passed the screen.",
                 "meaning": "A breakout still needs price confirmation.", "source_urls": ["https://www.example.com/history"]}],
            "risks": ["Losses can exceed the planned stop."]}]}


def message(data=None):
    return report_message(data or report(), sender="sender@example.com", recipient="recipient@example.com",
                          event_key="example-event", created_at=NOW)


class Structure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.links, self.attributes = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)
        self.links.extend(value for name, value in attrs if name == "href")


def test_rich_email_is_action_first_with_references_and_brief_evidence():
    parsed = BytesParser(policy=policy.default).parsebytes(message())
    html, plain = (parsed.get_body(preferencelist=(kind,)).get_content() for kind in ("html", "plain"))
    assert "An opportunity to review" in html and "EXAMPLE" in html
    assert html.index("Buy trigger") < html.index("Evidence, briefly")
    for text in ("USD 101.00", "USD 102.00", "USD 95.00", "USD 113.00", "Latest regular-session close",
                 "03:00:02 PM CDT", "Provider checked", "Quote validation: fresh", "closed market",
                 "fresh executable price", "08:30 AM", "03:00 PM CDT", "only after an actual buy",
                 "5 completed sessions", "session 15", "No purchase, fill, position or order is assumed",
                 "Growth may already be priced in.", "Financial results", "Full evidence cautions are in the attached report."):
        assert text in html
    assert "not execution instructions" in plain and "Holdings on file: none" in plain
    assert "https://investors.example.com/results?period=quarter&currency=USD" in plain
    assert [part.get_filename() for part in parsed.iter_attachments()] == ["research-report.pdf", "research-report.json"]
    assert parsed.get_content_type() == "multipart/mixed"
    assert next(parsed.iter_parts()).get_content_type() == "multipart/alternative"


def test_mime_identity_and_raw_body_are_repeatable():
    first, second = message(), message()
    assert first == second
    assert message_content_sha256(first) == message_content_sha256(second)
    changed = report()
    changed["trade_ideas"][0]["company"] = "Changed displayed issuer"
    assert message_content_sha256(first) != message_content_sha256(message(changed))


def test_untrusted_text_and_link_values_cannot_create_active_content():
    data = report()
    attack = '<img src="https://tracker.example.org/open" onerror="alert(1)"><script>alert(2)</script>'
    idea = data["trade_ideas"][0]
    for key in ("symbol", "company", "exchange", "currency"):
        idea[key] = attack
    idea["ai_review"]["rationale"] = attack
    idea["ai_review"]["counterargument"] = attack
    idea["risks"] = [attack]
    idea["evidence_briefs"][0].update(claim=attack, meaning=attack, limitation=attack,
        source_urls=["javascript:alert(1)", "https://user:secret@example.com/result"])
    idea["evidence_briefs"][1]["source_urls"] = ['https://example.com/result?q=" onmouseover="bad']
    html = report_email_html(data, notice=attack)
    tree = Structure()
    tree.feed(html)
    assert "&lt;img" in html and "&lt;script&gt;" in html
    assert not {"img", "script", "iframe", "object", "embed", "link", "style", "form"}.intersection(tree.tags)
    assert not any(name.lower().startswith("on") for name, _ in tree.attributes)
    assert all(url.startswith("https://") for url in tree.links)
    assert "javascript:" not in tree.links and all("secret" not in url for url in tree.links)
    assert "Coverage only; no linked source supports this summary." in html


@pytest.mark.parametrize("url", ["http://example.com", "file:///C:/private/report", "data:text/html,bad",
    "https://localhost/report", "https://127.0.0.1/report", "https://192.168.1.1/report",
    "https://example.internal/report", "https://example.com:8443/report", "https://example.com/\nreport"])
def test_unsafe_destinations_are_plain_coverage_notes(url):
    data = report()
    data["trade_ideas"][0]["evidence_briefs"] = [{"claim": "Sample coverage", "meaning": "Unlinked.", "source_urls": [url]}]
    tree = Structure()
    tree.feed(report_email_html(data))
    assert tree.links == []


def test_ai_only_approval_gate_also_covers_html_generation():
    data = report()
    data["trade_ideas"][0]["ai_review"]["decision"] = "wait"
    with pytest.raises(ValueError, match="unapproved"):
        report_email_html(data)
    with pytest.raises(ValueError, match="unapproved"):
        message(data)


def test_wait_and_owner_only_cards_do_not_invent_entry_or_holdings():
    data = report()
    data.pop("notification_scope")
    one = data["trade_ideas"][0]
    one["action"] = "wait"
    two = deepcopy(one)
    two["action"] = "reduce_if_owned"
    data["trade_ideas"].append(two)
    html = report_email_html(data)
    assert "No entry is suggested" in html and "No holdings are on file" in html
    assert "no sell or short instruction" in html and "Buy trigger" not in html


def test_delayed_unknown_and_missing_quotes_remain_explicit():
    data = report()
    quote = data["trade_ideas"][0]["current_quote"]
    quote.update(quote_type="delayed", market_phase="regular", delay_seconds=900)
    html = report_email_html(data)
    assert "Delayed price reference" in html and "900 seconds" in html
    quote.update(quote_type="unknown", delay_seconds=None, status="unknown", freshness={"status": "unknown"})
    html = report_email_html(data)
    assert "Unverified price reference" in html and "delay is unverified" in html and "validation: unknown" in html
    data["trade_ideas"][0].pop("current_quote")
    assert "Current quote unavailable" in report_email_html(data)


def test_empty_preview_and_calendar_fallbacks_are_readable():
    data = report()
    data["trade_ideas"][0]["strategy"]["timing"]["entry_window"] = {"local_timezone": "America/Chicago"}
    assert "Calendar unavailable" in report_email_html(data)
    data["trade_ideas"] = []
    html = report_email_html(data, test=True)
    assert "No actionable setup" in html and "Preview / pipeline test" in html


def test_benchmark_sources_are_distinguished_from_the_stock_history():
    data = report()
    data["trade_ideas"][0]["evidence_briefs"][1]["source_urls"].append("https://www.example.com/benchmark")
    html = report_email_html(data)
    assert "Price history" in html and "Benchmark history" in html


@pytest.mark.parametrize("open_at, close_at", [
    ("2026-10-06T08:30:00", "2026-10-06T15:00:00-05:00"),
    ("2026-10-06T08:30:00-05:00", "2026-10-06T15:00:00"),
    ("2026-10-06T15:00:00-05:00", "2026-10-06T08:30:00-05:00"),
])
def test_ambiguous_or_reversed_windows_do_not_invent_local_times(open_at, close_at):
    data = report()
    data["trade_ideas"][0]["strategy"]["timing"]["entry_window"].update(local_open=open_at, local_close=close_at)
    assert "Calendar unavailable" in report_email_html(data)
