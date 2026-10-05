from copy import deepcopy
from html.parser import HTMLParser

import pytest

from contract_ipo_monitor.dashboard import market_sections_html, safe_external_url
from contract_ipo_monitor.db import Database
from contract_ipo_monitor.observability import dashboard_html
from contract_ipo_monitor.research import ResearchStore, report_markdown


def market_report():
    """Mock research observations, not recommendations for actual securities."""
    return {
        "completed_at": "2026-10-05T18:00:00+00:00", "status": "degraded", "health": {},
        "universe": {
            "reviewed_at": "2026-10-05", "methodology": "Rank historical total revenue growth, then net margin.",
            "limitations": ["Selection is concentrated in technology and AI infrastructure and U.S. listings.",
                            "Tencent's Hong Kong HKD listing remains watch-only while native price coverage is unverified."],
            "reviewed_exclusions": [{"name": "Excluded Issuer", "reason": "Growth below the filter",
                                      "source_url": "https://example.org/exclusion"}],
        },
        "listed_companies": [{
            "symbol": "EXA", "name": "Example Issuer", "exchange": "NASDAQ", "trading_currency": "USD",
            "issuer_country": "Japan", "eligible": True, "reasons": ["Positive reported profit and historical growth"],
            "revenue_growth_percent": 24.1, "net_margin_percent": 14.2,
            "financials": {"net_income": 1234000000, "currency": "JPY", "period_end": "2026-06-30",
                           "reported_at": "2026-08-01", "period_type": "quarter", "accounting_standard": "IFRS",
                           "source_url": "https://example.org/results?part=1&basis=reported",
                           "limitations": ["Financial reporting and trading currencies differ."]},
        }],
        "trade_ideas": [
            {"symbol": "EXA", "company": "Example Issuer", "exchange": "NASDAQ", "currency": "USD",
             "listing_kind": "ADR", "action": "conditional_buy", "horizon": "5–15 trading sessions",
             "generated_at": "2026-10-05T18:00:00+00:00", "price_as_of": "2026-10-02",
             "indicators": {"last_close": 108.5}, "entry": 110, "invalidation": 90, "target": 150, "risk_reward": 2,
             "conditions": ["A fresh quote must confirm the trigger; review the current spread"],
             "reasons": ["Positive historical trend"], "risks": ["Execution gaps can exceed the planned risk"],
             "confidence": "sourced_conditional_setup; predictive_accuracy_unmeasured",
             "limitations": ["No brokerage orders are placed."], "evidence_urls": ["https://example.org/prices"],
             "world_context": [{"title": "Reported shipping disruption", "publisher": "Example News",
                                "published_at": "2026-10-05T10:00:00+00:00", "themes": ["supply_chain"],
                                "source_url": "https://example.org/news", "interpretation": "Exposure relevance is unverified."}]},
            {"symbol": "EXB", "company": "Other Example", "exchange": "NYSE", "currency": "USD", "listing_kind": "ordinary",
             "action": "reduce_if_owned", "price_as_of": "2026-10-02", "indicators": {"last_close": 84},
             "invalidation": 80, "conditions": ["Only applies if already owned; confirm a break below the recent low"],
             "reasons": ["Price trend weakened"], "evidence_urls": []},
            {"symbol": "0700.HK", "company": "Tencent", "exchange": "HKEX", "currency": "HKD", "listing_kind": "ordinary",
             "action": "wait", "price_as_of": None, "indicators": None,
             "conditions": ["Usable verified native HKD price history is unavailable"], "reasons": ["Sourced financial results remain visible"]},
        ],
        "price_coverage": [{"symbol": "EXA", "source": "price_provider", "status": "ok", "as_of": "2026-10-02", "error": None},
                           {"symbol": "0700.HK", "source": "price_provider", "status": "unavailable", "as_of": None,
                            "error": "Native HKD price history unavailable"}],
        "world_news": [{"title": "Reported shipping disruption", "source_url": "https://example.org/news", "publisher": "Example News",
                        "published_at": "2026-10-05T10:00:00+00:00", "themes": ["supply_chain"],
                        "interpretation": "Keyword matching suggests exposure relevance, which is unverified."}],
        "world_coverage": [{"source": "world_news:Example", "source_url": "https://example.org/rss", "status": "partial",
                            "observed_at": "2026-10-05T18:00:00+00:00", "collected_count": 1,
                            "error": "One invalid item rejected", "limitations": ["One bounded publisher feed page"]}],
        "ipos": [], "sentiment": [], "coverage": [], "limitations": [],
    }


class ParsedHTML(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.tags = []
        self.attributes = []
        self.feed(value)

    def handle_starttag(self, tag, attributes):
        self.tags.append(tag)
        self.attributes.extend(attributes)


def test_saved_market_report_reaches_dashboard_without_losing_contract_panels(tmp_path):
    db = Database(tmp_path / "dashboard.db")
    db.initialize()
    store = ResearchStore(db)
    store.initialize()
    report = market_report()
    store.save_run(report)
    html = dashboard_html(db)
    for text in ("Listed-company growth and profit screen", "Conditional buy", "Reduce if already owned", "Why wait",
                 "0700.HK", "HKEX", "HKD", "NASDAQ", "USD", "2026-10-02", "110.00", "90.00", "150.00", "80.00",
                 "24.1%", "14.2%", "1,234,000,000 JPY", "quarter IFRS", "2026-06-30", "2026-08-01",
                 "technology and AI infrastructure", "watch-only", "Example News", "supply_chain", "Price direction is unknown",
                 "One invalid item rejected; One bounded publisher feed page",
                 "Recent candidate evaluations", "Recent confirmed alerts", "Online sentiment sample", "Company IPO evidence"):
        assert text in html
    parsed = ParsedHTML(html)
    assert ("href", "https://example.org/results?part=1&basis=reported") in parsed.attributes
    assert ("href", "https://example.org/news") in parsed.attributes
    assert "No orders are placed" in html and "not executable live quotes" in html


def test_report_has_sourced_financial_basis_venue_price_dates_wait_reasons_and_world_receipts():
    text = report_markdown(market_report())
    for value in ("Conditional buy", "Reduce if already owned", "### 0700.HK: Wait", "Why wait:",
                  "Usable verified native HKD price history is unavailable", "NASDAQ / USD / ADR", "HKEX / HKD",
                  "108.50 USD / 2026-10-02", "110.00 | 90.00 | 150.00 | 2.00", "— | 80.00 | — | —",
                  "24.1%", "14.2%", "1,234,000,000 JPY", "quarter IFRS", "2026-06-30 / 2026-08-01",
                  "[Financial results](https://example.org/results?part=1&basis=reported)", "watch-only",
                  "[Reported shipping disruption](https://example.org/news)", "publisher-reported and unverified",
                  "One invalid item rejected; One bounded publisher feed page", "Completed price date"):
        assert value in text
    assert text.index("Financial reporting and trading currencies differ.") > text.index("[Financial results]")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,hi", "http://example.org/x", "https://user:secret@example.org/x",
                                 "https://localhost/x", "https://127.0.0.1/x", "https://[::1]/x", "https://example.org:8443/x",
                                 "https://example.org\\@evil.org/x", "https://example.org/\nfoo", "https://example.org:bad/x"])
def test_market_evidence_links_reject_active_credential_and_nonpublic_destinations(url):
    assert safe_external_url(url) is None


def test_dashboard_escapes_all_report_fields_and_does_not_expose_active_links_or_numeric_markup(tmp_path):
    report = market_report()
    payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    report["listed_companies"][0]["name"] = payload
    report["listed_companies"][0]["financials"]["source_url"] = "javascript:alert(3)"
    report["listed_companies"][0]["revenue_growth_percent"] = payload
    report["trade_ideas"][0]["conditions"] = [payload]
    report["trade_ideas"][0]["currency"] = payload
    report["trade_ideas"][0]["indicators"]["last_close"] = payload
    report["trade_ideas"][0]["evidence_urls"] = ["https://user:secret@example.org/", "data:text/html,hello"]
    report["world_news"][0]["title"] = payload
    report["world_coverage"][0]["collected_count"] = payload
    report["world_coverage"][0]["error"] = payload
    report["sentiment"] = [{"company_name": payload, "label": "unknown", "scored_count": payload,
                            "independent_origins": payload, "bias_flags": [payload]}]
    report["coverage"] = [{"source": payload, "status": "error", "collected_count": payload, "error": payload}]
    db = Database(tmp_path / "dashboard.db")
    db.initialize()
    store = ResearchStore(db)
    store.initialize()
    store.save_run(report)
    html = dashboard_html(db)
    parsed = ParsedHTML(html)
    assert "script" not in parsed.tags and "img" not in parsed.tags
    assert not any(name.startswith("on") for name, value in parsed.attributes)
    assert all(value.startswith("https://") for name, value in parsed.attributes if name == "href")
    assert "secret" not in html and "javascript:" not in html and "data:text/html" not in html
    assert "&lt;script&gt;" in html


def test_markdown_neutralizes_untrusted_link_text_and_destination_escapes():
    report = market_report()
    report["trade_ideas"][2]["conditions"] = ["[click](javascript:alert(1)) <script>x</script> | new\nrow"]
    report["world_news"][0]["source_url"] = "https://example.org/a)[x](javascript:alert(1))"
    report["listed_companies"][0]["financials"]["source_url"] = "https://user:secret@example.org/x"
    text = report_markdown(report)
    assert "\\[click\\](javascript:alert(1)) &lt;script&gt;x&lt;/script&gt; \\| new row" in text
    assert "https://example.org/a%29%5Bx%5D%28javascript:alert%281%29%29" in text
    assert "[click](javascript:" not in text and "secret" not in text and "<script>" not in text


def test_wait_state_never_displays_restored_setup_levels_and_partial_evidence_stays_readable():
    report = market_report()
    wait = report["trade_ideas"][2]
    wait.update(entry=777777, invalidation=666666, target=999999, risk_reward=888888)
    report["listed_companies"].append({"symbol": "MISSING", "name": "No Results", "financials": None, "eligible": False})
    html, text = market_sections_html(report), report_markdown(report)
    for result in (html, text):
        assert "777,777" not in result and "666,666" not in result and "999,999" not in result
        assert "Unavailable" in result and "Does not pass financial screen" in result
        assert "Usable verified native HKD price history is unavailable" in result


def test_old_report_stays_supported_and_world_headline_sample_limit_is_disclosed():
    report = {"completed_at": "2026-10-05T18:00:00+00:00", "status": "ok"}
    assert market_sections_html(report) == ""
    assert "IPO evidence" in report_markdown(report)
    report = market_report()
    report["world_news"] = [deepcopy(report["world_news"][0]) for _ in range(51)]
    assert "latest 50 of 51" in report_markdown(report)
    assert "latest 50 of 51" in market_sections_html(report)
