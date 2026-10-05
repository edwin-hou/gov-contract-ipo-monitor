"""Publisher-reported world headlines and transparent macro theme matches.

A headline is evidence of a publisher's report, not independent verification
of the underlying event. Theme/exposure associations are interpretations and
never establish whether a security should rise or fall.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .sources.discourse import SourceCoverage, plain_text
from .sources.http import ResilientClient


@dataclass(frozen=True)
class WorldFeed:
    publisher: str
    url: str
    article_hosts: tuple[str, ...]


DEFAULT_FEEDS = (
    WorldFeed("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml", ("bbc.com", "bbc.co.uk")),
    WorldFeed("The Guardian", "https://www.theguardian.com/world/rss", ("theguardian.com",)),
    WorldFeed("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", ("aljazeera.com",)),
    WorldFeed("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", ("nytimes.com",)),
)

# Complete word/phrase boundaries prevent e.g. "oil" matching "spoiled".
# These exact terms and their matched themes are preserved with each event.
THEME_TERMS: dict[str, tuple[str, ...]] = {
    "rates": ("interest rate", "interest rates", "rate cut", "rate cuts", "rate hike", "rate hikes",
              "federal reserve", "central bank", "central banks", "monetary policy", "bond yields"),
    "inflation": ("inflation", "consumer price index", "cost of living", "deflation"),
    "geopolitics": ("war", "invasion", "military", "missile", "missiles", "ceasefire", "sanctions",
                    "armed conflict", "nuclear", "geopolitical"),
    "energy": ("crude oil", "oil prices", "oil supply", "natural gas", "gas prices", "energy",
               "opec", "lng", "power grid", "electricity prices"),
    "export_controls": ("export control", "export controls", "export ban", "export bans",
                        "export restrictions", "chip restrictions", "technology restrictions"),
    "supply_chain": ("supply chain", "supply chains", "shipping", "supply disruption", "supply disruptions",
                     "chip shortage", "semiconductor shortage", "port strike", "hormuz", "suez"),
    "regulation": ("regulation", "regulations", "regulator", "regulators", "antitrust", "tariff", "tariffs",
                   "competition law", "data privacy", "ai act", "trade restrictions"),
}


@dataclass(frozen=True)
class WorldEvent:
    event_id: str
    title: str
    text: str
    source_url: str
    publisher: str
    published_at: datetime | None
    observed_at: datetime
    themes: tuple[str, ...]
    bias_flags: tuple[str, ...]
    direction: str = "unknown"
    fact_status: str = "publisher_reported_unverified"
    matched_terms: tuple[str, ...] = ()
    interpretation: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.observed_at.utcoffset() is None or self.published_at is not None and self.published_at.utcoffset() is None:
            raise ValueError("World news timestamps must include timezones")
        if self.published_at is not None and self.published_at > self.observed_at:
            raise ValueError("World news cannot have a future publication timestamp")
        if self.direction != "unknown" or self.fact_status != "publisher_reported_unverified":
            raise ValueError("A publisher headline cannot independently verify an event or price direction")
        if not self.event_id or not self.publisher or not self.title:
            raise ValueError("World news requires identity, publisher, and headline")
        if any(theme not in THEME_TERMS for theme in self.themes):
            raise ValueError("World news contains an unknown macro theme")
        _public_url(self.source_url)


MacroEvent = WorldEvent


@dataclass(frozen=True)
class WorldNewsBatch:
    events: tuple[WorldEvent, ...]
    coverage: tuple[SourceCoverage, ...]


def _public_url(value: str, hosts: tuple[str, ...] | None = None) -> str:
    parts = urlsplit(value)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443) or not host:
        raise ValueError("World news links must be public HTTPS URLs without credentials")
    if hosts is not None and not any(host == allowed or host.endswith("." + allowed) for allowed in hosts):
        raise ValueError("World news article URL is outside its publisher domains")
    # Restored links are display evidence only and are never fetched, but still
    # cannot introduce private-address or local-host links into a report.
    import ipaddress
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global or host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("World news article URL is not public")
    query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunsplit(("https", parts.netloc.lower(), parts.path, urlencode(query), ""))


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value.strip())
        except (ValueError, TypeError, OverflowError):
            return None
    return stamp.astimezone(UTC) if stamp.utcoffset() is not None else None


def match_macro_themes(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    themes, matches = [], []
    for theme, terms in THEME_TERMS.items():
        found = [term for term in terms if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I)]
        if found:
            themes.append(theme)
            matches.extend(f"{theme}:{term}" for term in found)
    return tuple(themes), tuple(matches)


def world_event_from_dict(value: dict[str, Any]) -> WorldEvent:
    observed = value["observed_at"]
    published = value.get("published_at")
    if isinstance(observed, str):
        observed = _date(observed)
    if isinstance(published, str):
        parsed = _date(published)
        if parsed is None:
            raise ValueError("Archived world news has an invalid publication date")
        published = parsed
    if not isinstance(observed, datetime):
        raise ValueError("Archived world news requires a valid observation timestamp")
    return WorldEvent(event_id=value["event_id"], title=value["title"], text=value["text"],
                      source_url=value["source_url"], publisher=value["publisher"],
                      published_at=published, observed_at=observed, themes=tuple(value.get("themes", ())),
                      bias_flags=tuple(value.get("bias_flags", ())), direction=value.get("direction", "unknown"),
                      fact_status=value.get("fact_status", "publisher_reported_unverified"),
                      matched_terms=tuple(value.get("matched_terms", ())), interpretation=value.get("interpretation", ""),
                      content_hash=value.get("content_hash", ""))


def associate_events(exposure_keys: Iterable[str], events: Iterable[WorldEvent], *, now: datetime,
                     max_age: timedelta = timedelta(days=2)) -> tuple[WorldEvent, ...]:
    """Exact configured theme intersections; undated/stale news cannot be a catalyst."""
    if now.utcoffset() is None or max_age <= timedelta(0):
        raise ValueError("Association requires an aware timestamp and positive freshness window")
    exposures = set(exposure_keys)
    matched = []
    seen = set()
    for event in events:
        if event.event_id in seen or event.published_at is None or not now - max_age <= event.published_at <= now:
            continue
        if exposures.intersection(event.themes):
            matched.append(event)
            seen.add(event.event_id)
    return tuple(sorted(matched, key=lambda item: item.published_at, reverse=True))


class WorldNewsCollector:
    def __init__(self, client: ResilientClient | None = None, *, feeds: tuple[WorldFeed, ...] = DEFAULT_FEEDS,
                 max_items_per_feed: int = 30):
        if not 1 <= max_items_per_feed <= 30 or not 1 <= len(feeds) <= 8:
            raise ValueError("World feed or item count is outside safe bounds")
        self.feeds = feeds
        self.max_items_per_feed = max_items_per_feed
        for feed in feeds:
            _public_url(feed.url)
            if not feed.publisher or not feed.article_hosts:
                raise ValueError("World feed requires a publisher and article domain allowlist")
        self.client = client or ResilientClient(timeout=20, max_attempts=2, max_response_bytes=2_000_000,
                                              headers={"User-Agent": "global-market-research-monitor/0.4 (public-data research)"})
        self._owns_client = client is None
        self.partial_batch = WorldNewsBatch((), ())

    async def collect(self, *, observed_at: datetime) -> WorldNewsBatch:
        if observed_at.utcoffset() is None:
            raise ValueError("World news observation timestamp must include a timezone")
        observed_at = observed_at.astimezone(UTC)
        events: list[WorldEvent] = []
        coverage: list[SourceCoverage] = []
        seen_urls, seen_content = set(), set()
        self.partial_batch = WorldNewsBatch((), ())
        for index, feed in enumerate(self.feeds):
            limitations = ("One bounded publisher feed page; editorial selection and English-language coverage are incomplete.",
                           "Headlines and feed summaries are publisher reports, not independently verified events or price direction.")
            try:
                text = await self.client.request_text("GET", feed.url)
                if len(text.encode("utf-8")) > 2_000_000 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
                    raise ValueError("World feed exceeds its byte bound or contains XML entity declarations")
                root = ET.fromstring(text)
                if root.tag.rsplit("}", 1)[-1] not in {"rss", "feed", "RDF"}:
                    raise ValueError("World endpoint did not return an RSS or Atom feed")
                items = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
                collected, rejected, duplicates = 0, 0, 0
                for item in items[:self.max_items_per_feed]:
                    values = {child.tag.rsplit("}", 1)[-1]: child for child in item}
                    def content(name: str) -> str:
                        element = values.get(name)
                        return "".join(element.itertext()).strip() if element is not None else ""
                    try:
                        title = plain_text(content("title"), limit=500)
                        link_node = values.get("link")
                        raw_link = content("link") or (link_node.get("href", "") if link_node is not None else "")
                        url = _public_url(raw_link, feed.article_hosts)
                        if not title:
                            raise ValueError("World feed headline is empty")
                        summary = plain_text(content("description") or content("summary"), limit=1200)
                        published = _date(content("pubDate") or content("published") or content("updated") or content("date"))
                        if published is not None and published > observed_at:
                            raise ValueError("World feed publication timestamp is in the future")
                        digest = hashlib.sha256((title + "\n" + summary).casefold().encode()).hexdigest()
                        if url in seen_urls or digest in seen_content:
                            duplicates += 1
                            continue
                        flags = ["publisher_selection_bias", "headline_and_summary_only", "automated_theme_matching", "market_direction_unknown"]
                        if published is None:
                            flags.append("publication_date_missing_or_invalid")
                        themes, matches = match_macro_themes(title + ". " + summary)
                        event_id = hashlib.sha256(url.encode()).hexdigest()
                        event = WorldEvent(event_id, title, summary, url, feed.publisher, published, observed_at,
                                           themes, tuple(flags), matched_terms=matches, content_hash=digest,
                                           interpretation="Macro themes are transparent keyword matches. Instrument exposure relevance is an inference; market price direction is unknown.")
                        events.append(event)
                        seen_urls.add(url)
                        seen_content.add(digest)
                        collected += 1
                    except (ValueError, TypeError, OverflowError):
                        rejected += 1
                if rejected:
                    limitations += (f"Rejected {rejected} invalid, future-dated, or off-publisher items in the bounded page.",)
                if duplicates:
                    limitations += (f"Skipped {duplicates} duplicate article URLs or headline/summary texts across the collected feeds.",)
                if len(items) > self.max_items_per_feed:
                    limitations += (f"Only the first {self.max_items_per_feed} of {len(items)} feed items were examined; no pagination was attempted.",)
                coverage.append(SourceCoverage("world_news:" + feed.publisher, feed.url, observed_at,
                                               "partial" if rejected else "ok", collected, limitations=limitations))
            except asyncio.CancelledError:
                coverage.append(SourceCoverage("world_news:" + feed.publisher, feed.url, observed_at, "error",
                                               error="Collection interrupted before this feed completed", limitations=limitations))
                for remaining in self.feeds[index + 1:]:
                    coverage.append(SourceCoverage("world_news:" + remaining.publisher, remaining.url, observed_at, "not_attempted",
                                                   error="Collection stopped before this feed was attempted", limitations=limitations))
                raise
            except Exception as exc:
                coverage.append(SourceCoverage("world_news:" + feed.publisher, feed.url, observed_at, "error", error=f"{type(exc).__name__}: {str(exc)[:240]}", limitations=limitations))
            finally:
                self.partial_batch = WorldNewsBatch(tuple(events), tuple(coverage))
        return self.partial_batch

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
