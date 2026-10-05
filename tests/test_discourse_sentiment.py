from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import asyncio
import json

import httpx
import pytest

from contract_ipo_monitor.sentiment import score_evidence, summarize_sentiment
from contract_ipo_monitor.sources.discourse import (
    CompanyWatch, DiscourseCollector, DiscourseConfig, DiscourseEvidence,
    evidence_from_dict, matching_companies, plain_text, video_id,
)

NOW = datetime(2026, 10, 5, 20, tzinfo=UTC)
COMPANIES = [CompanyWatch("Anduril", ("Anduril Industries",)), CompanyWatch("Stripe")]


def evidence(index=1, *, text="Anduril has promising technology and strong commercial prospects for the future.",
             kind="news", origin=None, **kwargs):
    return DiscourseEvidence(
        evidence_id=str(index), source_kind=kind, source_url=f"https://example.com/{index}",
        origin_key=origin or f"{kind}:publisher{index}", title="Anduril company analysis",
        text=text, text_kind="publisher_summary", company_names=("Anduril",),
        retrieved_at=NOW, published_at=NOW, **kwargs,
    )


def collector(handler, **kwargs):
    config = DiscourseConfig(company_news_enabled=False, reddit_enabled=False, **kwargs)
    return DiscourseCollector(config, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_identity_boundaries_and_aliases_do_not_match_substrings():
    assert matching_companies("Anduril Industries might file an IPO", COMPANIES) == ("Anduril",)
    assert matching_companies("Stripey and Andurilish", COMPANIES) == ()
    assert matching_companies("Anduril and STRIPE", COMPANIES) == ("Anduril", "Stripe")


def test_text_ingestion_strips_markup_scripts_and_controls():
    assert plain_text('<b>Anduril</b><script>evil()</script> &amp; <style>hidden</style>Stripe\x00') == "Anduril & Stripe"


def test_video_urls_require_exact_host_and_valid_id():
    assert video_id("https://youtu.be/0BE2AAOlYWI?t=3") == "0BE2AAOlYWI"
    assert video_id("https://www.youtube.com/shorts/0BE2AAOlYWI") == "0BE2AAOlYWI"
    for url in ["https://youtube.com.evil.test/watch?v=0BE2AAOlYWI", "https://youtu.be/invalid", "http://127.0.0.1/test"]:
        with pytest.raises(ValueError):
            video_id(url)


@pytest.mark.asyncio
async def test_private_dns_source_host_is_blocked_before_network(monkeypatch):
    source = DiscourseCollector(DiscourseConfig(company_news_enabled=False, reddit_enabled=False))
    async def private_address(*args, **kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", private_address)
    try:
        with pytest.raises(ValueError, match="non-public"):
            await source._get("https://malicious-public-name.example/rss")
    finally:
        await source.aclose()


@pytest.mark.asyncio
async def test_rss_deduplicates_across_feeds_tracks_publisher_and_publication():
    rss = '<rss><channel><item><title>Anduril IPO prospects</title><link>https://article.example/a?utm_source=feed</link><description><![CDATA[Anduril <b>promising</b> prospects]]></description><pubDate>Mon, 05 Oct 2026 15:00:00 GMT</pubDate><source url="https://publisher.example">Publisher</source></item></channel></rss>'
    source = collector(lambda request: httpx.Response(200, text=rss), feed_urls=("https://feed.example/1", "https://feed.example/2"))
    try:
        batch = await source.collect(COMPANIES)
        assert len(batch.records) == 1
        assert batch.records[0].origin_key == "news:publisher.example"
        assert batch.records[0].text == "Anduril promising prospects"
        assert batch.records[0].published_at == datetime(2026, 10, 5, 15, tzinfo=UTC)
        assert batch.records[0].source_url == "https://article.example/a"
        assert [item.status for item in batch.coverage if item.source == "news_rss"] == ["ok", "ok"]
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_atom_and_missing_dates_do_not_invent_publication():
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Anduril prepares an IPO</title><link href="https://example.com/a"/><summary>Anduril offering expected</summary></entry></feed>'
    source = collector(lambda request: httpx.Response(200, text=atom), feed_urls=("https://feed.example/rss",))
    try:
        batch = await source.collect(COMPANIES)
        assert len(batch.records) == 1
        assert batch.records[0].published_at is None
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_feed_sampling_receipt_discloses_inspected_and_available_count():
    rss = '<rss><channel>' + ''.join(f'<item><title>Anduril IPO {index}</title><link>https://example.com/{index}</link></item>' for index in range(5)) + '</channel></rss>'
    source = collector(lambda request: httpx.Response(200, text=rss),
                       feed_urls=("https://feed.example/rss",), max_items_per_source=2)
    try:
        batch = await source.collect(COMPANIES)
        receipt = next(item for item in batch.coverage if item.source == "news_rss")
        assert receipt.status == "ok" and receipt.collected_count == 2
        assert "Inspected at most 2 of 5 feed entries" in receipt.limitations[1]
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_overall_timeout_preserves_finished_news_and_marks_interrupted_reddit():
    entered = asyncio.Event()
    rss = '<rss><channel><item><title>Anduril IPO</title><link>https://example.com/a</link></item></channel></rss>'
    async def handler(request):
        if request.url.host == "feed.example":
            return httpx.Response(200, text=rss)
        entered.set()
        await asyncio.Event().wait()
    source = DiscourseCollector(DiscourseConfig(company_news_enabled=False,
        feed_urls=("https://feed.example/rss",)), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    task = asyncio.create_task(source.collect([COMPANIES[0]]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, timeout=0.01)
        assert len(source.partial_batch.records) == 1
        assert source.partial_batch.coverage[0].source == "news_rss"
        interruption = source.partial_batch.coverage[-1]
        assert interruption.source == "reddit" and interruption.status == "error"
        assert "Overall collection interrupted" in interruption.error
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await source.client.aclose()


@pytest.mark.asyncio
async def test_overall_caption_timeout_preserves_video_metadata():
    entered = asyncio.Event()
    async def handler(request):
        if request.url.path == "/oembed":
            return httpx.Response(200, json={"title": "Anduril IPO analysis", "author_name": "Creator"})
        entered.set()
        await asyncio.Event().wait()
    source = collector(handler, video_urls=("https://youtu.be/0BE2AAOlYWI",))
    task = asyncio.create_task(source.collect([COMPANIES[0]]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, timeout=0.01)
        assert source.partial_batch.records[0].text_kind == "video_metadata"
        assert source.partial_batch.coverage[-2].source == "youtube_metadata"
        assert source.partial_batch.coverage[-1].source == "youtube_captions"
        assert source.partial_batch.coverage[-1].status == "error"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await source.client.aclose()


@pytest.mark.asyncio
async def test_source_http_timeout_is_distinct_from_overall_interruption():
    def handler(request):
        raise httpx.ReadTimeout("secret response details")
    source = collector(handler, feed_urls=("https://feed.example/rss",))
    try:
        batch = await source.collect(COMPANIES)
        assert batch.coverage[0].error == "Request timed out"
        assert source.partial_batch == batch
        assert "interrupted" not in batch.coverage[0].error
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_failures_size_limit_redirects_and_xml_entities_report_coverage():
    def handler(request):
        if request.url.path == "/big":
            return httpx.Response(200, text="x" * 2000)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
        return httpx.Response(200, text='<!DOCTYPE rss [<!ENTITY bad "unsafe">]><rss/>')
    source = collector(handler, feed_urls=("https://feed.example/big", "https://feed.example/redirect", "https://feed.example/entities"), max_response_bytes=1024)
    try:
        batch = await source.collect(COMPANIES)
        assert batch.records == ()
        failed = [item for item in batch.coverage if item.source == "news_rss"]
        assert len(failed) == 3 and all(item.status == "error" for item in failed)
        assert "size limit" in failed[0].error
        assert failed[1].error == "HTTP 302"
        assert "entities" in failed[2].error
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_reddit_blocks_are_explicit_without_bypass_or_error_body():
    source = DiscourseCollector(DiscourseConfig(company_news_enabled=False), httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(403, text="secret error body"))))
    try:
        batch = await source.collect([COMPANIES[0]])
        assert not batch.records
        assert batch.coverage[0].status == "error"
        assert batch.coverage[0].error == "HTTP 403"
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_reddit_public_posts_ignore_deleted_content_and_engagement():
    payload = {"data": {"children": [
        {"data": {"permalink": "/r/stocks/comments/123/anduril", "subreddit": "stocks", "title": "Anduril IPO", "selftext": "Anduril is a promising company with strong potential but its valuation is risky.", "score": 90000, "created_utc": NOW.timestamp()}},
        {"data": {"permalink": "/r/stocks/comments/124/removed", "title": "Anduril", "selftext": "[deleted]"}},
    ]}}
    source = DiscourseCollector(DiscourseConfig(company_news_enabled=False), httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))))
    try:
        batch = await source.collect([COMPANIES[0]])
        assert len(batch.records) == 1
        assert batch.records[0].origin_key == "reddit:stocks"
        assert batch.records[0].author is None
        assert "score" not in asdict(batch.records[0])
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_youtube_metadata_never_becomes_a_transcript_or_scored_opinion():
    def handler(request):
        if request.url.path == "/oembed":
            return httpx.Response(200, json={"title": "Anduril: exciting promising IPO opportunity", "author_name": "Video creator"})
        return httpx.Response(200, text='var ytInitialPlayerResponse = {"videoDetails":{"title":"Anduril"}};')
    source = collector(handler, video_urls=("https://youtu.be/0BE2AAOlYWI",))
    try:
        batch = await source.collect([COMPANIES[0]])
        assert len(batch.records) == 1 and batch.records[0].text_kind == "video_metadata"
        assert batch.records[0].published_at is None
        assert score_evidence(batch.records[0], "Anduril").score is None
        captions = next(item for item in batch.coverage if item.source == "youtube_captions")
        assert captions.status == "unavailable"
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_public_youtube_captions_are_separate_and_automatic_captions_flagged():
    player = {"videoDetails": {"title": "Anduril IPO analysis", "channelId": "channel123", "author": "Creator"},
              "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [
                  {"languageCode": "en", "kind": "asr", "baseUrl": "https://www.youtube.com/api/timedtext?v=0BE2AAOlYWI"}]}}}
    def handler(request):
        if request.url.path == "/oembed":
            return httpx.Response(200, json={"title": "Anduril IPO", "author_name": "Creator"})
        if request.url.path == "/watch":
            return httpx.Response(200, text="var ytInitialPlayerResponse = " + json.dumps(player) + ";")
        return httpx.Response(200, text='<transcript><text>Anduril is innovative and promising with strong potential for growth.</text></transcript>')
    source = collector(handler, video_urls=("https://youtu.be/0BE2AAOlYWI",))
    try:
        batch = await source.collect([COMPANIES[0]])
        transcript = next(item for item in batch.records if item.text_kind == "video_transcript")
        assert transcript.origin_key == "youtube:channel123"
        assert "automatic_captions" in transcript.bias_flags
        assert score_evidence(transcript, "Anduril").label == "positive"
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_untrusted_caption_host_is_never_requested():
    requests = []
    player = {"captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{"languageCode": "en", "baseUrl": "https://evil.example/captions"}]}}}
    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/oembed":
            return httpx.Response(200, json={"title": "Anduril", "author_name": "Creator"})
        return httpx.Response(200, text="ytInitialPlayerResponse = " + json.dumps(player))
    source = collector(handler, video_urls=("https://youtu.be/0BE2AAOlYWI",))
    try:
        batch = await source.collect([COMPANIES[0]])
        assert not any("evil.example" in url for url in requests)
        assert next(item for item in batch.coverage if item.source == "youtube_captions").status == "unavailable"
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_company_news_and_api_discovery_are_bounded_and_keys_not_archived():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == "news.google.com":
            return httpx.Response(200, text='<rss><channel/></rss>')
        return httpx.Response(403, text="private-api-key")
    source = DiscourseCollector(DiscourseConfig(reddit_enabled=False, youtube_api_key="private-api-key"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        batch = await source.collect([COMPANIES[0]])
        assert requests[0].url.params["q"].startswith('"Anduril" IPO')
        assert requests[1].url.params["maxResults"] == "3"
        assert "private-api-key" not in json.dumps(asdict(batch), default=str)
    finally:
        await source.client.aclose()


@pytest.mark.asyncio
async def test_youtube_search_budget_caps_company_queries():
    searches = []
    def handler(request):
        searches.append(request.url.params["q"])
        return httpx.Response(200, json={"items": []})
    source = collector(handler, youtube_api_key="key")
    try:
        batch = await source.collect([CompanyWatch(f"Company {index}") for index in range(12)])
        assert len(searches) == 3
        assert next(item for item in batch.coverage if item.source == "youtube_search_limit").status == "partial"
    finally:
        await source.client.aclose()


def test_archive_evidence_restore_preserves_dates_and_tuples():
    raw = json.loads(json.dumps(asdict(evidence()), default=str))
    restored = evidence_from_dict(raw)
    assert restored == evidence()
    raw["retrieved_at"] = "2026-10-05T20:00:00"
    with pytest.raises(ValueError):
        evidence_from_dict(raw)


def test_negation_and_sentence_boundaries():
    record = evidence(text="Anduril is not promising and its technology is not excellent. Its business is strong.")
    tone = score_evidence(record, "Anduril")
    assert tone.negative_hits == ("not excellent", "not promising")
    assert tone.positive_hits == ("strong",)
    assert tone.label == "negative"


def test_sparse_and_fact_only_evidence_are_unknown_not_neutral():
    assert summarize_sentiment("Anduril", [], now=NOW).label == "unknown"
    assert summarize_sentiment("Anduril", [evidence()], now=NOW).score is None
    tone = score_evidence(evidence(text="Anduril reported a company update on Monday and has not announced an IPO date."), "Anduril")
    assert tone.label == "unknown" and tone.score is None


def test_popularity_repetition_and_origins_do_not_dominate():
    records = [evidence(index, origin="news:one", text=f"Anduril has strong promising innovative impressive technology with commercial opportunity number {index}.") for index in range(1, 51)]
    records += [evidence(51, text="Anduril is overvalued and risky with weak potential for growth.", kind="reddit", origin="reddit:stocks"),
                evidence(52, text="Anduril is overvalued and risky with weak prospects after this company update.", kind="reddit", origin="reddit:investing")]
    summary = summarize_sentiment("Anduril", records, now=NOW)
    assert summary.independent_origins == 3
    assert abs(summary.score) < 0.15
    assert "origin_weight_capped" in summary.bias_flags
    assert "conflicting_views" in summary.bias_flags


def test_duplicates_stale_and_future_records_are_excluded():
    first = evidence()
    records = [first, replace(first, evidence_id="copy", source_url="https://mirror.example/a"),
               replace(evidence(3), published_at=NOW - timedelta(days=31)),
               replace(evidence(4), published_at=NOW + timedelta(days=2))]
    summary = summarize_sentiment("Anduril", records, now=NOW)
    assert summary.scored_count == 1
    assert summary.excluded_count == 3
    assert summary.label == "unknown"
    assert "duplicates_removed" in summary.bias_flags


def test_multiple_companies_unsupported_language_and_bias_are_explicit():
    mixed = replace(evidence(), company_names=("Anduril", "Stripe"))
    assert score_evidence(mixed, "Anduril").score is None
    foreign = replace(evidence(), language="es")
    assert score_evidence(foreign, "Anduril").score is None
    promoted = evidence(text="Sponsored: Anduril is promising and this affiliate says guaranteed 10x returns are coming; reportedly an IPO is planned.")
    flags = score_evidence(promoted, "Anduril").bias_flags
    assert "possible_sponsorship_or_financial_interest" in flags
    assert "promotional_or_absolute_language" in flags
    assert "speculation_or_unverified_claim" in flags


def test_balanced_document_can_measure_neutral_tone():
    tone = score_evidence(evidence(text="Anduril is promising but risky, and the offering deserves careful evaluation by readers."), "Anduril")
    assert tone.label == "neutral" and tone.score == 0
