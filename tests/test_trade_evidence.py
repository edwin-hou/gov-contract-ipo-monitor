"""Relevant, sourced explanations and conditional session dates, not forecasts."""
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta

import pytest

from contract_ipo_monitor.quotes import CurrentQuote
from contract_ipo_monitor.dashboard import market_sections_html
from contract_ipo_monitor.research import report_markdown
from contract_ipo_monitor.sources.discourse import DiscourseEvidence, DiscourseBatch
from contract_ipo_monitor.sentiment import score_evidence, summarize_sentiment
from contract_ipo_monitor.trades import next_session_window, session_window
from contract_ipo_monitor.universe import default_universe, rank_universe
from contract_ipo_monitor.worldnews import WorldEvent, relevant_world_events
from test_trade_engine import NOW, assess, financials, history, instrument
from test_market_integration import database, monitor, seed


def article(title="Test issuer raises revenue guidance", text="Test issuer expects strong demand and profitable growth.", **changes):
    data = dict(evidence_id="news-1", source_kind="news", source_url="https://example.org/company/1",
                origin_key="publisher:example.org", title=title, text=text, text_kind="publisher_summary",
                company_names=("Test issuer",), retrieved_at=NOW, published_at=NOW-timedelta(hours=1))
    data.update(changes)
    return DiscourseEvidence(**data)


def event(title, text="", *, themes=("geopolitics",), publisher="BBC", identity="1", **changes):
    data = dict(event_id=identity, title=title, text=text, source_url="https://www.bbc.com/news/"+identity,
                publisher=publisher, published_at=NOW-timedelta(hours=1), observed_at=NOW,
                themes=themes, bias_flags=())
    data.update(changes)
    return WorldEvent(**data)


def quote(price=100, **changes):
    data = dict(symbol="TEST", provider_symbol="TEST", source="yahoo", source_url="https://query1.finance.yahoo.com/v8/finance/chart/TEST",
                price=price, currency="USD", exchange="NASDAQ", exchange_timezone="America/New_York",
                quote_at=NOW-timedelta(seconds=30), observed_at=NOW, quote_type="live", market_phase="regular",
                delay_seconds=0, status="fresh", session_date=NOW.date(), timestamp_precision="second")
    data.update(changes)
    return CurrentQuote(**data)


def by_id(idea):
    return {item["id"]: item for item in idea["evidence_briefs"]}


def test_brief_figures_use_actual_comparable_primary_amounts_and_price_dates():
    idea = assess(company_evidence=(article(),))
    briefs = by_id(idea)
    assert len(briefs) == 4
    assert "USD 150,000,000" in briefs["financial"]["claim"]
    assert "50.0%" in briefs["financial"]["claim"] and "20.0%" in briefs["financial"]["claim"]
    assert "2026-06-30" in briefs["financial"]["claim"]
    assert "2026-08-01" in briefs["financial"]["limitation"]
    assert "2026-10-02" in briefs["price_trend"]["claim"]
    assert "ACWI" in briefs["price_trend"]["claim"]
    assert briefs["financial"]["source_urls"] == [financials().source_url]
    assert all(set(item) == {"id", "evidence_type", "claim", "meaning", "relevance", "direction", "limitation", "source_urls"} for item in briefs.values())
    assert "internet-wide" in briefs["commentary"]["relevance"]


@pytest.mark.parametrize("margin, action, explanation", [
    (.046, "wait", "reported net margin 4.6% is below 5%"),
    (.04999, "wait", "reported net margin is below 5% before rounding"),
    (.05, "conditional_buy", "Meets the trade-entry screen"),
])
def test_profitable_growth_shortlist_and_stricter_trade_margin_boundary_are_distinct(margin, action, explanation):
    fact = replace(financials(), net_income=financials().revenue*margin)
    shortlisted = rank_universe({fact.symbol: fact}, now=NOW, instruments=(instrument(),))[0]
    idea = assess(fact=fact)
    assert shortlisted.eligible is True
    assert idea["action"] == action
    assert explanation in by_id(idea)["financial"]["meaning"]
    if action == "wait":
        assert idea["entry"] is idea["invalidation"] is idea["target"] is None
        assert any(explanation in condition for condition in idea["conditions"])
    else:
        assert "at least 10% revenue growth, positive reported net income and at least 5% net margin" in by_id(idea)["financial"]["meaning"]


def test_shortlist_and_trade_failure_labels_remain_clear_in_markdown_and_dashboard():
    fact = replace(financials(), net_income=6_900_000)
    company = rank_universe({fact.symbol: fact}, now=NOW, instruments=(instrument(),))[0].to_dict()
    idea = assess(fact=fact)
    report = {"completed_at": NOW.isoformat(), "status": "degraded", "health": {},
              "listed_companies": [company], "trade_ideas": [idea]}
    for rendered in (report_markdown(report), market_sections_html(report)):
        assert "Passes research shortlist" in rendered
        assert "Does not meet the trade-entry screen" in rendered
        assert "reported net margin 4.6% is below 5%" in rendered
        assert "Passes financial screen" not in rendered


@pytest.mark.parametrize("changes, explanation", [
    ({"revenue": 109_000_000}, "reported revenue growth 9.0% is below 10%"),
    ({"revenue": 109_990_000}, "reported revenue growth is below 10% before rounding"),
    ({"net_income": 0}, "reported net income USD 0 is not positive"),
    ({"net_income": -1_000_000}, "reported net income USD -1,000,000 is not positive"),
])
def test_financial_brief_identifies_the_actual_failed_trade_requirement(changes, explanation):
    idea = assess(fact=replace(financials(), **changes))
    assert idea["action"] == "wait"
    assert explanation in by_id(idea)["financial"]["meaning"]
    assert any(explanation in condition for condition in idea["conditions"])


def test_stale_or_wrong_issuer_facts_cannot_become_supporting_evidence():
    assert "financial" not in by_id(assess(fact=replace(financials(), reported_at=date(2026, 6, 30)), fundamental_max_age_days=30))
    assert "financial" not in by_id(assess(fact=replace(financials(), symbol="OTHER")))
    stale = assess(history=history(end=date(2026, 9, 29)))
    assert stale["action"] == "wait" and stale["entry"] is None
    assert "price_trend" not in by_id(stale)


def test_primary_comparison_without_prior_date_does_not_crash_explanation():
    idea = assess(fact=replace(financials(), prior_period_end=None, prior_period_start=None))
    assert idea["fundamentals"]["prior_period_end"] is None


@pytest.mark.parametrize("record", [
    article("Passenger crash investigated", "A sponsor banner links to Micron. Air crash investigators report no survivors."),
    article("Test issuer discussion", "The revenue figures of another company were discussed. Follow Test issuer on social media."),
    article(company_names=("Test issuer", "Other company")),
    article(published_at=NOW-timedelta(days=8)),
    article(published_at=None),
    article(published_at=NOW.replace(tzinfo=None)),
    article(retrieved_at=NOW.replace(tzinfo=None)),
    article(published_at=NOW+timedelta(seconds=1)),
    article(source_url="https://localhost/private"),
    article(source_kind="youtube", text_kind="video_metadata"),
])
def test_incidental_ambiguous_stale_unsafe_or_metadata_items_are_not_company_briefs(record):
    assert not any(item["evidence_type"] == "publisher_company_news" for item in assess(company_evidence=(record,))["evidence_briefs"])


def test_news_brief_is_small_deduplicated_and_marks_claims_unverified():
    original = article()
    idea = assess(company_evidence=(original, replace(original, evidence_id="duplicate"), article(evidence_id="news-2", source_url="https://example.org/company/2")))
    stories = [item for item in idea["evidence_briefs"] if item["evidence_type"] == "publisher_company_news"]
    assert len(stories) == 1 and len(idea["evidence_briefs"]) <= 5
    assert stories[0]["direction"] == "unknown"
    assert "unverified" in stories[0]["limitation"]
    assert "not a confirmed earnings fact" in stories[0]["meaning"]


def test_commentary_links_support_scored_items_and_conflicts_are_explicit():
    unscored = article(source_kind="hackernews", text_kind="public_comment", evidence_id="not-scored")
    measured = article(source_kind="hackernews", text_kind="public_comment", evidence_id="scored", source_url="https://example.org/opinion",
                       text="Test issuer earnings are weak and growth is risky; wait for demand data.")
    summary = {"label": "positive", "score": .2, "scored_count": 4, "independent_origins": 4,
               "bias_flags": ["conflicting_views"], "by_source": [{"source_kind": "news", "scored_count": 3}, {"source_kind": "hackernews", "scored_count": 1}],
               "evidence_tones": [{"evidence_id": "not-scored", "score": None}, {"evidence_id": "scored", "score": score_evidence(measured, "Test issuer").score, "excluded_reason": None}]}
    brief = by_id(assess(company_evidence=(unscored, measured), sentiment=summary))["commentary"]
    assert brief["source_urls"] == [measured.source_url]
    assert "3 news, 1 hackernews" in brief["claim"]
    assert "conflicting views" in brief["meaning"]


def test_aggregate_links_use_accepted_thirty_day_tone_not_seven_day_business_catalysts():
    product = article("Test issuer GPU review", "Test issuer makes excellent innovative devices; this detailed product experience impressed the author.",
                      source_kind="hackernews", text_kind="public_comment", published_at=NOW-timedelta(days=8))
    summary = asdict(summarize_sentiment("Test issuer", (product,), now=NOW))
    briefs = by_id(assess(company_evidence=(product,), sentiment=summary))
    assert not any(item["evidence_type"] == "publisher_company_news" for item in briefs.values())
    assert briefs["commentary"]["source_urls"] == [product.source_url]
    assert briefs["commentary"]["evidence_type"] == "sampled_commentary"
    assert "product, user or discussion topic rather than issuer prospects" in briefs["commentary"]["limitation"]


def test_aggregate_commentary_links_diversify_platform_then_origin():
    records = tuple(article("Test issuer product discussion " + str(index),
                    "Test issuer has excellent impressive products, according to this detailed experience number " + str(index),
                    evidence_id=str(index), source_url="https://example.org/"+str(index),
                    source_kind=kind, text_kind="public_comment", origin_key=origin,
                    published_at=NOW-timedelta(hours=index+1))
                    for index, (kind, origin) in enumerate((("hackernews", "author:a"), ("hackernews", "author:a"), ("news", "publisher:b"))))
    summary = asdict(summarize_sentiment("Test issuer", records, now=NOW))
    brief = by_id(assess(company_evidence=records, sentiment=summary))["commentary"]
    assert brief["source_urls"] == [records[0].source_url, records[2].source_url]


@pytest.mark.parametrize("label, expected", [("positive", "tone is positive"), ("negative", "tone is negative"),
    ("neutral", "tone is neutral"), ("unknown", "Insufficient measured tone")])
def test_nonconflicting_aggregate_meaning_matches_measured_label(label, expected):
    brief = by_id(assess(sentiment={"label": label, "scored_count": 3, "independent_origins": 3}))["commentary"]
    assert expected in brief["meaning"]
    assert brief["source_urls"] == []
    assert brief["evidence_type"] == "sampled_commentary_coverage"
    assert "coverage context only" in brief["limitation"]


@pytest.mark.parametrize("changes", [
    {"text_kind": "video_metadata"}, {"company_names": ("Test issuer", "Other")},
    {"published_at": NOW-timedelta(days=31)}, {"published_at": NOW+timedelta(seconds=1)},
    {"retrieved_at": NOW-timedelta(days=31)}, {"language": "fr"}, {"source_url": "https://localhost/private"},
])
def test_aggregate_links_never_override_tone_exclusions_or_unsafe_dates(changes):
    record = article(**changes)
    summary = {"label": "positive", "scored_count": 3, "independent_origins": 3,
               "evidence_tones": [{"evidence_id": record.evidence_id, "score": .5, "excluded_reason": None}]}
    brief = by_id(assess(company_evidence=(record,), sentiment=summary))["commentary"]
    assert brief["source_urls"] == [] and brief["evidence_type"] == "sampled_commentary_coverage"


def test_changed_source_version_and_excluded_duplicate_cannot_support_old_tone():
    record = article()
    score = score_evidence(record, "Test issuer").score
    for tone in ({"evidence_id": record.evidence_id, "score": score, "excluded_reason": "Duplicate URL"},
                 {"evidence_id": record.evidence_id, "score": -.5, "excluded_reason": None}):
        brief = by_id(assess(company_evidence=(record,), sentiment={"label": "negative", "scored_count": 3,
                      "independent_origins": 3, "evidence_tones": [tone]}))["commentary"]
        assert brief["source_urls"] == []


def test_micron_world_evidence_excludes_crashes_and_unconnected_geopolitics_but_keeps_sector_policy():
    micron = next(item for item in default_universe() if item.symbol == "MU")
    raw = (event("Airplane crash after military drills"),
           event("Ceasefire fails as war continues", identity="2"),
           event("New export restrictions on semiconductor memory chips", "Policy affects chipmaker trade", themes=("export_controls",), identity="3"))
    selected = relevant_world_events(micron, raw, now=NOW)
    assert [item.event_id for item, _ in selected] == ["3"]
    assert selected[0][1]["connection"] == "sector_mention"
    assert "semiconductor" in selected[0][1]["relevance"]
    assert selected[0][1]["direction"] == "unknown"
    # Raw inputs remain available to the global archive/report.
    assert len(raw) == 3 and raw[0].direction == "unknown"


def test_world_same_syndicated_text_or_url_is_not_multiple_corroborating_publishers():
    micron = next(item for item in default_universe() if item.symbol == "MU")
    original = event("Export restrictions for semiconductor firms", themes=("export_controls",))
    duplicate = replace(original, event_id="2", publisher="Other", source_url="https://example.org/2")
    same_url = replace(original, event_id="3", title="Different semiconductor export restrictions report")
    assert len(relevant_world_events(micron, (original, duplicate, same_url), now=NOW)) == 1


def test_world_rates_need_market_wide_connection_not_unrelated_local_central_bank():
    cases = (event("Local central bank cuts interest rates", themes=("rates",)),
             event("Federal Reserve considers rate hike", themes=("rates",), identity="2"))
    idea = assess(events=cases)
    assert len(idea["world_context"]) == 1
    assert idea["world_context"][0]["connection"] == "market_wide_monetary_policy"
    assert cases[0].source_url not in idea["evidence_urls"]
    assert any(item["evidence_type"] == "publisher_world_context" for item in idea["evidence_briefs"])


def test_session_windows_show_real_nashville_and_exchange_clocks_and_conditional_dates():
    idea = assess()
    timing = idea["strategy"]["timing"]
    window = timing["entry_window"]
    assert window["session_date"] == "2026-10-05"
    assert window["local_open"] == "2026-10-05T08:30:00-05:00"
    assert window["local_close"] == "2026-10-05T15:00:00-05:00"
    assert window["exchange_open"] == "2026-10-05T09:30:00-04:00"
    assert timing["setup_expiry"]["session_date"] == "2026-10-09"
    assert timing["illustrative_review"]["session_date"] == "2026-10-12"
    assert timing["illustrative_time_exit"]["session_date"] == "2026-10-26"
    assert "actual verified fill" in timing["anchor"]
    assert "not predicted" in timing["limitation"]
    assert not idea["strategy"]["assumed_position"]


def test_after_close_weekend_holiday_early_close_and_dst_windows():
    assert next_session_window("NASDAQ", datetime(2026, 10, 5, 20, tzinfo=UTC))["session_date"] == "2026-10-06"
    assert next_session_window("NYSE", datetime(2026, 10, 3, 14, tzinfo=UTC))["session_date"] == "2026-10-05"
    short = next_session_window("NASDAQ", datetime(2026, 11, 26, 15, tzinfo=UTC))
    assert short["session_date"] == "2026-11-27" and short["early_close"]
    assert short["local_close"] == "2026-11-27T12:00:00-06:00"
    assert next_session_window("NASDAQ", datetime(2026, 11, 27, 18, tzinfo=UTC))["session_date"] == "2026-11-30"
    assert session_window("NYSE", date(2026, 3, 9))["local_open"].endswith("-05:00")
    assert session_window("NYSE", date(2026, 11, 2))["local_open"].endswith("-06:00")
    with pytest.raises(ValueError):
        next_session_window("HKEX", NOW)
    unsupported = assess(instrument=replace(instrument(), exchange="HKEX"))
    assert unsupported["strategy"]["timing"]["status"] == "unavailable"
    assert unsupported["strategy"]["timing"].get("illustrative_review") is None


def test_quote_does_not_turn_a_stale_daily_setup_into_buy_and_band_is_conditional():
    stale = assess(history=history(end=date(2026, 9, 29)), current_quote=quote())
    assert stale["action"] == "wait" and stale["entry"] is None
    original = assess()
    low = assess(current_quote=quote(original["entry"]-.01))
    high = assess(current_quote=quote(original["strategy"]["maximum_entry"]+.01))
    inside = assess(current_quote=quote(original["entry"]))
    assert low["strategy"]["entry_quote_check"]["status"] == "below_trigger"
    assert high["strategy"]["entry_quote_check"]["status"] == "above_entry_cap"
    assert inside["strategy"]["entry_quote_check"]["status"] == "informational_trigger_present"
    assert not inside["strategy"]["entry_quote_check"]["executable"]
    assert assess(current_quote=quote(observed_at=NOW-timedelta(minutes=6)))["strategy"]["entry_quote_check"]["status"] == "refresh_required"


@pytest.mark.asyncio
async def test_portable_quotes_preserve_observation_and_news_briefs_restore(tmp_path):
    class Prices:
        async def collect(self, item, *, observed_at):
            return history(symbol=item.symbol, step=.1 if item.symbol == "ACWI" else .4)
        async def collect_quote(self, item, *, observed_at):
            return quote()
    subject = monitor(database(tmp_path), price_source=Prices())
    seed(subject)
    subject.research.record_batch(DiscourseBatch((article(),), ()))
    assert (await subject.collect_prices())["price_histories"] == 2
    assert len(subject.store.coverage("quotes")) == 1  # no benchmark quote
    report = subject.report([])
    assert report["trade_ideas"][0]["current_quote"]["freshness"]["status"] == "fresh"
    assert any(item["evidence_type"] == "publisher_company_news" for item in report["trade_ideas"][0]["evidence_briefs"])
    later = NOW+timedelta(minutes=6)
    subject.store.record("current_quote", "TEST", quote(), observed_at=later)
    subject.now = lambda: later
    assert subject.store.latest("current_quote")["TEST"]["observed_at"] == NOW.isoformat()
    restored = subject.report([])["trade_ideas"][0]["current_quote"]
    assert restored["status"] == "stale" and restored["freshness"]["observation_age_seconds"] == 360


@pytest.mark.asyncio
async def test_optional_quote_failure_keeps_history_but_never_exposes_credentials(tmp_path):
    class Prices:
        async def collect(self, item, *, observed_at):
            return history(symbol=item.symbol)
        async def collect_quote(self, item, *, observed_at):
            raise ValueError("SECRET_TOKEN https://example.org?apikey=credential")
    subject = monitor(database(tmp_path), price_source=Prices())
    seed(subject)
    await subject.collect_prices()
    report = subject.report([])
    assert "SECRET_TOKEN" not in str(report) and "apikey=credential" not in str(report)
    assert report["quote_coverage"][0]["status"] == "error"
    assert report["trade_ideas"][0]["current_quote"] is None
    assert report["trade_ideas"][0]["strategy"]["entry_quote_check"]["status"] == "refresh_required"


def test_quote_archive_cannot_invent_original_observation_from_retrieval(tmp_path):
    subject = monitor(database(tmp_path))
    for missing in ({"symbol": "TEST"}, {"symbol": "TEST", "observed_at": "2026-10-05T15:00:00"}):
        with pytest.raises(ValueError, match="original aware"):
            subject.store.record("current_quote", "TEST", missing, observed_at=NOW)
