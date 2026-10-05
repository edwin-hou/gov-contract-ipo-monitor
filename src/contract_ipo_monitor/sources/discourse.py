"""Bounded public discourse ingestion. These records never verify an IPO.

Feeds contain publisher/forum summaries, Reddit contains bounded public posts/comments,
Hacker News contains public stories/comments, and YouTube contains metadata
plus captions only when a public track is accessible.
No remote HTML or script is executed and no engagement count is a truth score.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import math
import re
import socket
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx


def _identity_term(value: str, *, canonical: bool = False) -> str | None:
    term = value.strip()
    if term.startswith("$"):
        return term if re.fullmatch(r"\$[A-Za-z][A-Za-z0-9]{0,9}(?:[.-][A-Za-z0-9]{1,3})?", term) else None
    if len(term) < 3 or term.casefold() in {"and", "the", "inc", "corp", "ltd", "llc", "plc", "ai", "company", "group", "holdings", "stock", "shares"}:
        return None
    if not canonical and re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z])?", term):
        # Bare ticker aliases such as ON or AI are ordinary words in text.
        return None
    return term


@dataclass(frozen=True)
class CompanyWatch:
    name: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Company name must be nonempty text")
        # Remove only complete trailing legal suffixes, never internal words or
        # substrings such as Bancorp. Preserve the SEC/legal name as identity.
        suffix = re.compile(
            r"(?:,\s*|\s+)(?:inc|corp|ltd|limited|corporation|l\.?\s*l\.?\s*c|p\.?\s*l\.?\s*c)\.?[,;\s]*$",
            re.I,
        )
        short = self.name.strip()
        while True:
            stripped = suffix.sub("", short).rstrip(" ,.;")
            if stripped == short:
                break
            short = stripped
        aliases = list(self.aliases)
        # Automatic one-word aliases are too ambiguous for entity matching.
        # A caller can explicitly supply such a brand name when appropriate.
        if short != self.name.strip() and len(re.findall(r"[^\W_]+", short)) >= 2 and sum(c.isalpha() for c in short) >= 6:
            if not any(alias.casefold() == short.casefold() for alias in aliases):
                aliases.append(short)
        object.__setattr__(self, "aliases", tuple(aliases))

    @property
    def query_name(self) -> str:
        """Prefer a natural brand spelling; cashtags remain matching aliases."""
        values = [_identity_term(self.name, canonical=True), *(_identity_term(value) for value in self.aliases)]
        usable = [value for value in values if value]
        natural = [value for value in usable if not value.startswith("$")]
        return min(natural or usable, default=self.name.strip(),
                   key=lambda value: (len(value), value.casefold()))


@dataclass(frozen=True)
class DiscourseConfig:
    feed_urls: tuple[str, ...] = ()
    forum_feed_urls: tuple[str, ...] = ()
    forums_enabled: bool = True
    video_urls: tuple[str, ...] = ()
    company_news_enabled: bool = True
    news_query_mode: str = "general"
    reddit_enabled: bool = True
    reddit_access_token: str = field(default="", repr=False)
    reddit_client_id: str = field(default="", repr=False)
    reddit_client_secret: str = field(default="", repr=False)
    reddit_refresh_token: str = field(default="", repr=False)
    reddit_posts_per_company: int = 5
    reddit_comments_per_post: int = 10
    reddit_max_comments_per_run: int = 60
    hacker_news_enabled: bool = True
    youtube_api_key: str = ""
    youtube_search_per_company: int = 3
    max_youtube_searches_per_run: int = 3
    max_videos_per_run: int = 15
    user_agent: str = "ipo-discourse-monitor/1.0 (personal research)"
    max_items_per_source: int = 30
    max_response_bytes: int = 2_000_000
    timeout_seconds: float = 15.0
    lookback_days: int = 14

    def __post_init__(self) -> None:
        if not 1 <= self.max_items_per_source <= 100:
            raise ValueError("max_items_per_source must be between 1 and 100")
        if not 1024 <= self.max_response_bytes <= 10_000_000:
            raise ValueError("max_response_bytes must be between 1024 and 10000000")
        if not 1 <= self.timeout_seconds <= 60 or not 1 <= self.lookback_days <= 365:
            raise ValueError("timeout_seconds or lookback_days outside safe bounds")
        if len(self.feed_urls) > 30 or len(self.video_urls) > 30:
            raise ValueError("At most 30 feeds and 30 seed videos per run")
        if len(self.forum_feed_urls) > 30:
            raise ValueError("At most 30 forum feeds per run")
        if self.news_query_mode not in {"general", "ipo"}:
            raise ValueError("news_query_mode must be general or ipo")
        if not 0 <= self.reddit_posts_per_company <= 10 or not 0 <= self.reddit_comments_per_post <= 50 or not 0 <= self.reddit_max_comments_per_run <= 500:
            raise ValueError("Reddit post/comment limits outside safe bounds")
        if self.reddit_client_secret and not self.reddit_client_id or self.reddit_refresh_token and not self.reddit_client_id:
            raise ValueError("Reddit client_id is required with client_secret/refresh_token")
        if any(len(value) > 8192 or any(character.isspace() for character in value) for value in (
            self.reddit_access_token, self.reddit_client_id, self.reddit_client_secret, self.reddit_refresh_token,
        )):
            raise ValueError("Reddit credential format is invalid")
        if not 0 <= self.youtube_search_per_company <= 5 or not 1 <= self.max_videos_per_run <= 30:
            raise ValueError("YouTube search/video limits outside safe bounds")
        if not 1 <= self.max_youtube_searches_per_run <= 20:
            raise ValueError("At most 20 YouTube search calls per run")


@dataclass(frozen=True)
class DiscourseEvidence:
    evidence_id: str
    source_kind: str
    source_url: str
    origin_key: str
    title: str
    text: str
    text_kind: str
    company_names: tuple[str, ...]
    retrieved_at: datetime
    published_at: datetime | None = None
    author: str | None = None
    language: str | None = "en"
    bias_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceCoverage:
    source: str
    source_url: str
    observed_at: datetime
    status: str
    collected_count: int = 0
    error: str | None = None
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscourseBatch:
    records: tuple[DiscourseEvidence, ...]
    coverage: tuple[SourceCoverage, ...]


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str, limit: int = 20000) -> str:
    parser = _TextParser()
    parser.feed(str(value)[:limit * 4])
    text = " ".join(parser.parts)
    return " ".join(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", html.unescape(text)).split())[:limit]


def matching_companies(text: str, companies: Sequence[CompanyWatch]) -> tuple[str, ...]:
    """Explicit boundary matches; a ticker substring is never identity proof."""
    # A company word inside a social-profile URL or navigation link is not a
    # textual company mention. Keep surrounding prose and exact cashtags.
    text = re.sub(r"(?:https?://|www\.)\S+", " ", text, flags=re.I)
    results = []
    for company in companies:
        terms = [_identity_term(company.name, canonical=True), *(_identity_term(alias) for alias in company.aliases)]
        if any(term and re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I) for term in terms):
            results.append(company.name)
    return tuple(results)


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in {"https", "http"} or not host or parts.username or parts.password:
        raise ValueError("Expected a public HTTP(S) URL without credentials")
    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith((".local", ".internal")):
        raise ValueError("Local source URLs are not accepted")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Non-public address is not accepted")
    if parts.port not in {None, 80, 443}:
        raise ValueError("Source URL must use a standard HTTP(S) port")
    return url


def _canonical_url(url: str) -> str:
    parts = urlsplit(_safe_url(url))
    # Keep semantic query parameters; strip only common tracking parameters.
    query = {k: v for k, v in parse_qs(parts.query).items()
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}}
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query, doseq=True), ""))


def _date(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return result.astimezone(UTC) if result.tzinfo else None


def evidence_from_dict(payload: dict) -> DiscourseEvidence:
    """Restore archived JSON without inventing missing dates or evidence fields."""
    fields = dict(payload)
    for key in ("retrieved_at", "published_at"):
        value = fields.get(key)
        fields[key] = _date(value) if isinstance(value, str) else value
    if not isinstance(fields.get("retrieved_at"), datetime) or fields["retrieved_at"].tzinfo is None:
        raise ValueError("Archived evidence requires an aware retrieved_at")
    fields["company_names"] = tuple(fields.get("company_names", ()))
    fields["bias_flags"] = tuple(fields.get("bias_flags", ()))
    return DiscourseEvidence(**fields)


def _xml(value: str) -> ET.Element:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", value, re.I):
        raise ValueError("XML declarations with entities are not accepted")
    return ET.fromstring(value)


def video_id(url: str) -> str:
    parts = urlsplit(_safe_url(url))
    host = (parts.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        identifier = parts.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        identifier = parse_qs(parts.query).get("v", [""])[0]
        if not identifier and parts.path.startswith(("/shorts/", "/embed/")):
            identifier = parts.path.split("/")[2]
    else:
        raise ValueError("Seed video URL must use youtube.com or youtu.be")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
        raise ValueError("Invalid YouTube video ID")
    return identifier


def _player_response(page: str) -> dict:
    # Decode one JSON object only; JavaScript is never evaluated.
    for match in re.finditer(r'(?:ytInitialPlayerResponse\s*=|"ytInitialPlayerResponse"\s*:)\s*', page):
        try:
            value, _ = json.JSONDecoder().raw_decode(page[match.end():])
            if isinstance(value, dict):
                return value
        except (ValueError, json.JSONDecodeError):
            continue
    raise ValueError("Public player metadata not available")


def _error(exc: Exception) -> str:
    # Do not persist response bodies, auth credentials, or request query strings.
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "Request timed out"
    if isinstance(exc, (ValueError, ET.ParseError)):
        return f"Invalid/unavailable source: {str(exc)[:150]}"
    return type(exc).__name__


class DiscourseCollector:
    def __init__(self, config: DiscourseConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self.client = client or httpx.AsyncClient(
            timeout=config.timeout_seconds, follow_redirects=False,
            headers={"User-Agent": config.user_agent},
        )
        self._owns_client = client is None
        self.partial_batch = DiscourseBatch((), ())
        self._active_source: tuple[str, str, datetime] | None = None
        self._completed_records: list[DiscourseEvidence] = []
        self._completed_coverage: list[SourceCoverage] = []
        self._reddit_token = ""
        self._reddit_token_expires_at: datetime | None = None

    def _checkpoint(self, records: Sequence[DiscourseEvidence],
                    coverage: Sequence[SourceCoverage], *, video: bool = False) -> None:
        if video:
            records = [*self._completed_records, *records]
            coverage = [*self._completed_coverage, *coverage]
        unique = {record.evidence_id: record for record in records}
        self.partial_batch = DiscourseBatch(tuple(unique.values()), tuple(coverage))

    async def _check_public_dns(self, url: str) -> None:
        # Mock transports deliberately do not use networking or DNS in unit tests.
        if isinstance(self.client._transport, httpx.MockTransport):
            return
        parts = urlsplit(url)
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError("Source hostname resolves to a non-public address")

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _get(self, url: str, **kwargs) -> str:
        return await self._request("GET", url, **kwargs)

    async def _request(self, method: str, url: str, *, headers: dict[str, str] | None = None, **kwargs) -> str:
        _safe_url(url)
        # Streaming limits decompressed bytes and the whole response duration.
        async def read() -> str:
            await self._check_public_dns(url)
            async with self.client.stream(method, url, follow_redirects=False,
                                          timeout=self.config.timeout_seconds,
                                          headers={"User-Agent": self.config.user_agent, **(headers or {})}, **kwargs) as response:
                response.raise_for_status()
                if not 200 <= response.status_code < 300:
                    raise ValueError("Redirect responses are not followed")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.config.max_response_bytes:
                        raise ValueError("Response exceeds size limit")
                    chunks.append(chunk)
                return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        return await asyncio.wait_for(read(), timeout=self.config.timeout_seconds)

    async def _reddit_bearer(self, now: datetime) -> str:
        if self.config.reddit_access_token:
            return self.config.reddit_access_token
        if self._reddit_token and self._reddit_token_expires_at and now < self._reddit_token_expires_at:
            return self._reddit_token
        if not self.config.reddit_client_id:
            return ""
        data = {"grant_type": "refresh_token", "refresh_token": self.config.reddit_refresh_token} if self.config.reddit_refresh_token else {
            "grant_type": "client_credentials", "scope": "read",
        }
        response = json.loads(await self._request("POST", "https://www.reddit.com/api/v1/access_token",
            data=data, auth=httpx.BasicAuth(self.config.reddit_client_id, self.config.reddit_client_secret)))
        if not isinstance(response, dict):
            raise ValueError("Reddit OAuth token response is invalid")
        token = response.get("access_token")
        scope = str(response.get("scope") or "").split()
        if not isinstance(token, str) or not token or any(character.isspace() for character in token):
            raise ValueError("Reddit OAuth did not return a valid access token")
        if str(response.get("token_type", "")).casefold() != "bearer" or scope and not {"read", "*"}.intersection(scope):
            raise ValueError("Reddit OAuth token lacks read permission")
        try:
            seconds = float(response.get("expires_in", 3600))
        except (ValueError, TypeError):
            raise ValueError("Reddit OAuth token expiry is invalid") from None
        if not math.isfinite(seconds) or not 0 < seconds <= 86400:
            raise ValueError("Reddit OAuth token expiry is invalid")
        self._reddit_token, self._reddit_token_expires_at = token, now + timedelta(seconds=max(0, seconds - 60))
        return token

    async def _reddit_get(self, path: str, token: str, *, params: dict) -> str:
        host = "https://oauth.reddit.com" if token else "https://www.reddit.com"
        headers = {"Authorization": "bearer " + token} if token else None
        return await self._get(host + path, params=params, headers=headers)

    def _evidence(self, *, kind: str, url: str, origin: str, title: str, text: str,
                  text_kind: str, companies: Sequence[CompanyWatch], now: datetime,
                  published_at: datetime | None = None, author: str | None = None,
                  language: str | None = "en", flags: tuple[str, ...] = ()) -> DiscourseEvidence | None:
        title, text = plain_text(title, 1000), plain_text(text)
        names = matching_companies(title + " " + text, companies)
        if not names:
            return None
        url = _canonical_url(url)
        return DiscourseEvidence(
            evidence_id=hashlib.sha256((kind + ":" + url + ":" + text_kind).encode()).hexdigest(),
            source_kind=kind, source_url=url, origin_key=origin, title=title, text=text,
            text_kind=text_kind, company_names=names, retrieved_at=now, published_at=published_at,
            author=author, language=language, bias_flags=flags,
        )

    async def collect(self, companies: Sequence[CompanyWatch]) -> DiscourseBatch:
        # Outer wait_for cancellation must not discard already completed sources.
        # One collector instance supports one in-flight batch at a time.
        self.partial_batch = DiscourseBatch((), ())
        self._completed_records = []
        self._completed_coverage = []
        self._active_source = None
        try:
            return await self._collect(companies)
        except asyncio.CancelledError:
            if self._active_source is not None:
                source, url, observed = self._active_source
                interruption = SourceCoverage(source, url, observed, "error", error=(
                    "Overall collection interrupted by timeout or cancellation; this source did not complete"),
                    limitations=("Completed sources remain available in partial_batch",))
                self.partial_batch = DiscourseBatch(self.partial_batch.records,
                    (*self.partial_batch.coverage, interruption))
            raise
        finally:
            self._active_source = None

    async def _collect(self, companies: Sequence[CompanyWatch]) -> DiscourseBatch:
        if len(companies) > 100:
            raise ValueError("At most 100 companies per run")
        records = self._completed_records
        coverage = self._completed_coverage
        now = datetime.now(UTC)
        query_suffix = " IPO" if self.config.news_query_mode == "ipo" else ""
        feeds = list(dict.fromkeys(self.config.feed_urls))
        if self.config.company_news_enabled:
            feeds.extend("https://news.google.com/rss/search?" + urlencode({
                "q": f'"{company.query_name}"{query_suffix} when:{self.config.lookback_days}d',
                "hl": "en-US", "gl": "US", "ceid": "US:en",
            }) for company in companies)
        for feed in dict.fromkeys(feeds):
            self._active_source = ("news_rss", feed, now)
            try:
                content = await self._get(feed)
                items = self._parse_feed(content, feed, companies, now)
                records.extend(items)
                root = _xml(content)
                available = len(root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry"))
                sample = f"Inspected at most {self.config.max_items_per_source} of {available} feed entries; company matches retained"
                coverage.append(SourceCoverage("news_rss", feed, now, "ok", len(items), limitations=(
                    "Publisher summaries only; search ranking and editorial selection bias", sample)))
            except Exception as exc:
                coverage.append(SourceCoverage("news_rss", feed, now, "error", error=_error(exc)))
            self._checkpoint(records, coverage)
            self._active_source = None
        if self.config.forums_enabled:
            if not self.config.forum_feed_urls:
                coverage.append(SourceCoverage("forum_rss", "", now, "disabled", limitations=(
                    "No forum feed URLs configured; Hacker News and Reddit have separate coverage",)))
                self._checkpoint(records, coverage)
            for feed in dict.fromkeys(self.config.forum_feed_urls):
                self._active_source = ("forum_rss", feed, now)
                try:
                    content = await self._get(feed)
                    items = self._parse_feed(content, feed, companies, now, forum=True)
                    records.extend(items)
                    root = _xml(content)
                    available = len(root.findall(".//item") or root.findall("{http://www.w3.org/2005/Atom}entry"))
                    coverage.append(SourceCoverage("forum_rss", feed, now, "ok", len(items), limitations=(
                        f"Inspected at most {self.config.max_items_per_source} of {available} configured forum feed entries; no complete thread traversal",
                        "Forum/community and author self-selection bias; quoted text can be misattributed; popularity ignored",)))
                except Exception as exc:
                    coverage.append(SourceCoverage("forum_rss", feed, now, "error", error=_error(exc)))
                self._checkpoint(records, coverage)
                self._active_source = None
        else:
            coverage.append(SourceCoverage("forum_rss", "", now, "disabled"))
            self._checkpoint(records, coverage)
        if self.config.reddit_enabled:
            await self._collect_reddit(companies, records, coverage, now)
        else:
            coverage.append(SourceCoverage("reddit", "https://www.reddit.com", now, "disabled"))
            self._checkpoint(records, coverage)
        if self.config.hacker_news_enabled:
            for company in companies:
                params = {
                    "query": company.query_name, "tags": "(story,comment)", "page": 0,
                    "hitsPerPage": self.config.max_items_per_source,
                    "numericFilters": f"created_at_i>{int((now - timedelta(days=self.config.lookback_days)).timestamp())}",
                }
                url = "https://hn.algolia.com/api/v1/search_by_date?" + urlencode(params)
                self._active_source = ("hackernews", url, now)
                try:
                    payload = json.loads(await self._get(url))
                    items = self._parse_hackernews(payload, companies, now)
                    records.extend(items)
                    available = payload.get("nbHits", "unknown")
                    coverage.append(SourceCoverage("hackernews", url, now, "ok", len(items), limitations=(
                        f"One date-sorted page of at most {self.config.max_items_per_source} hits from {available} reported matches in {self.config.lookback_days} days; no further pages fetched",
                        "Public story/comment sample from one technical community; topic and author self-selection bias",
                        "Author origins are account labels, not audited independent people; votes and popularity are ignored",)))
                except Exception as exc:
                    coverage.append(SourceCoverage("hackernews", url, now, "error", error=_error(exc)))
                self._checkpoint(records, coverage)
                self._active_source = None
        else:
            coverage.append(SourceCoverage("hackernews", "https://hn.algolia.com", now, "disabled"))
            self._checkpoint(records, coverage)
        video_seeds = list(self.config.video_urls)
        if self.config.youtube_api_key and self.config.youtube_search_per_company:
            count = min(len(companies), self.config.max_youtube_searches_per_run)
            start = now.hour * count % len(companies) if companies else 0
            search_companies = [companies[(start + offset) % len(companies)] for offset in range(count)]
            if len(companies) > count:
                coverage.append(SourceCoverage("youtube_search_limit", "https://www.youtube.com", now, "partial", limitations=(
                    "YouTube discovery is capped per run and rotates companies by UTC hour to limit API quota use",)))
                self._checkpoint(records, coverage)
            for company in search_companies:
                search_url = "https://www.youtube.com/results?" + urlencode({"search_query": company.query_name + query_suffix})
                self._active_source = ("youtube_search", search_url, now)
                try:
                    # Publish time is explicitly bounded; ranking is not representative sampling.
                    since = now.replace(microsecond=0) - timedelta(days=self.config.lookback_days)
                    result = json.loads(await self._get("https://www.googleapis.com/youtube/v3/search", params={
                        "part": "snippet", "type": "video", "q": company.query_name + query_suffix,
                        "order": "date", "publishedAfter": since.isoformat().replace("+00:00", "Z"),
                        "maxResults": self.config.youtube_search_per_company,
                        "key": self.config.youtube_api_key,
                    }))
                    discovered = ["https://www.youtube.com/watch?v=" + item["id"]["videoId"]
                                  for item in result.get("items", [])]
                    video_seeds.extend(discovered)
                    coverage.append(SourceCoverage("youtube_search", search_url, now, "ok", len(discovered), limitations=(
                        "Search results are a ranked and bounded sample, not an internet-wide poll",)))
                except Exception as exc:
                    coverage.append(SourceCoverage("youtube_search", search_url, now,
                                                   "error", error=_error(exc)))
                self._checkpoint(records, coverage)
                self._active_source = None
        elif companies:
            coverage.append(SourceCoverage("youtube_search", "https://www.youtube.com", now, "disabled", limitations=(
                "Automatic YouTube discovery requires YOUTUBE_API_KEY; configured seed videos are still collected",)))
            self._checkpoint(records, coverage)
        seen_videos: set[str] = set()
        for seed in video_seeds:
            try:
                identifier = video_id(seed)
            except ValueError as exc:
                coverage.append(SourceCoverage("youtube_metadata", seed, now, "error", error=_error(exc)))
                self._checkpoint(records, coverage)
                continue
            if identifier in seen_videos:
                continue
            if len(seen_videos) >= self.config.max_videos_per_run:
                coverage.append(SourceCoverage("youtube_video_limit", "https://www.youtube.com", now, "partial", limitations=(
                    "Global per-run video limit reached; some discovered/configured videos were not collected",)))
                self._checkpoint(records, coverage)
                break
            seen_videos.add(identifier)
            items, statuses = await self._youtube(identifier, companies, now)
            records.extend(items)
            coverage.extend(statuses)
            self._checkpoint(records, coverage)
            self._active_source = None
        # Same record appearing across company searches or mirrors counts once.
        unique = {record.evidence_id: record for record in records}
        self.partial_batch = DiscourseBatch(tuple(unique.values()), tuple(coverage))
        return self.partial_batch

    async def _collect_reddit(self, companies: Sequence[CompanyWatch], records: list[DiscourseEvidence],
                              coverage: list[SourceCoverage], now: datetime) -> None:
        self._active_source = ("reddit_oauth", "https://www.reddit.com/api/v1/access_token", now)
        try:
            token = await self._reddit_bearer(now)
        except Exception as exc:
            coverage.append(SourceCoverage("reddit_oauth", "https://www.reddit.com/api/v1/access_token", now,
                "error", error=_error(exc), limitations=("OAuth failed; anonymous fallback and permission bypass are not attempted",)))
            self._checkpoint(records, coverage)
            self._active_source = None
            return
        host = "https://oauth.reddit.com" if token else "https://www.reddit.com"
        comment_budget = self.config.reddit_max_comments_per_run
        seen_posts: set[str] = set()
        for company in companies:
            params = {"q": f'"{company.query_name}"', "sort": "new", "t": "month",
                      "limit": self.config.max_items_per_source, "raw_json": 1}
            url = host + "/search.json?" + urlencode(params)
            self._active_source = ("reddit", url, now)
            try:
                items = self._parse_reddit(json.loads(await self._reddit_get("/search.json", token, params=params)), companies, now)
                records.extend(items)
                coverage.append(SourceCoverage("reddit", url, now, "ok", len(items), limitations=(
                    "Approved OAuth read endpoint" if token else "Approved OAuth is not configured; only public JSON access is attempted and may be blocked",
                    f"One date-sorted public post page; comments sampled for at most {self.config.reddit_posts_per_company} matched posts per company",
                    "Community/author self-selection and topic selection bias; votes and popularity are ignored",)))
            except Exception as exc:
                coverage.append(SourceCoverage("reddit", url, now, "error", error=_error(exc), limitations=(
                    "Approved OAuth credentials are needed when public JSON access is denied; no bypass is attempted",)))
                self._checkpoint(records, coverage)
                self._active_source = None
                continue
            self._checkpoint(records, coverage)
            for post in items[:self.config.reddit_posts_per_company]:
                match = re.search(r"/comments/([A-Za-z0-9]+)(?:/|$)", urlsplit(post.source_url).path)
                if not match or match.group(1) in seen_posts:
                    continue
                identifier = match.group(1)
                seen_posts.add(identifier)
                limit = min(self.config.reddit_comments_per_post, comment_budget)
                if not limit:
                    continue
                # Failed reads consume the same budget, keeping total work bounded.
                comment_budget -= limit
                path = "/comments/" + identifier + ".json"
                comment_params = {"limit": limit, "depth": 2, "sort": "new", "raw_json": 1}
                comment_url = host + path + "?" + urlencode(comment_params)
                self._active_source = ("reddit_comments", comment_url, now)
                try:
                    payload = json.loads(await self._reddit_get(path, token, params=comment_params))
                    comments = self._parse_reddit_comments(payload, post, companies, now, limit=limit)
                    records.extend(comments)
                    coverage.append(SourceCoverage("reddit_comments", comment_url, now, "ok", len(comments), limitations=(
                        f"At most {limit} comments inspected, depth at most 2; morechildren and private threads are not fetched",
                        "Newest-comment, community and author selection bias; quoted opinions may be misattributed",)))
                except Exception as exc:
                    coverage.append(SourceCoverage("reddit_comments", comment_url, now, "error", error=_error(exc)))
                self._checkpoint(records, coverage)
            self._active_source = None
        coverage.append(SourceCoverage("reddit_comment_budget", "https://www.reddit.com", now, "ok", limitations=(
            f"Comment reads requested up to {self.config.reddit_max_comments_per_run - comment_budget} of {self.config.reddit_max_comments_per_run} allowed comments; unselected posts/threads are not sampled",)))
        self._checkpoint(records, coverage)

    def _parse_feed(self, content: str, feed_url: str, companies: Sequence[CompanyWatch],
                    now: datetime, *, forum: bool = False) -> list[DiscourseEvidence]:
        root = _xml(content)
        items = root.findall(".//item")
        if not items:
            items = root.findall("{http://www.w3.org/2005/Atom}entry")
        if root.tag.split("}")[-1] not in {"rss", "feed", "RDF"}:
            raise ValueError("Expected an RSS or Atom feed")
        results: list[DiscourseEvidence] = []
        for item in items[:self.config.max_items_per_source]:
            fields = {child.tag.split("}")[-1]: child for child in item}
            get = lambda key: fields[key].text or "" if key in fields else ""
            title = get("title")
            url = get("link") or (fields["link"].get("href", "") if "link" in fields else "")
            if not url:
                continue
            try:
                source = fields.get("source")
                publisher_url = source.get("url", "") if source is not None else ""
                origin = urlsplit(publisher_url or url).hostname or urlsplit(feed_url).hostname or "unknown"
                author = get("creator") or get("author")
                if "author" in fields and list(fields["author"]):
                    author = " ".join(node.text or "" for node in fields["author"] if node.tag.split("}")[-1] == "name")
                author = plain_text(author, 100) if author and "@" not in author else None
                origin_key = ("forum:" if forum else "news:") + origin.lower()
                if forum and author:
                    origin_key += ":author:" + author.casefold()
                record = self._evidence(kind="forum" if forum else "news", url=url, origin=origin_key,
                    title=title, text=get("description") or get("summary") or get("content"),
                    text_kind="forum_post" if forum else "publisher_summary", companies=companies, now=now,
                    published_at=_date(get("pubDate") or get("published") or get("updated")),
                    author=author if forum else None, language=None if forum else "en",
                    flags=("community_selection", "author_self_selection", "quoted_opinions_possible", "feed_summary_only", "unverified_claims") if forum else ("editorial_selection", "summary_only"))
                if record:
                    results.append(record)
            except ValueError:
                continue
        return results

    def _parse_reddit(self, content: dict, companies: Sequence[CompanyWatch],
                      now: datetime) -> list[DiscourseEvidence]:
        if not isinstance(content, dict) or not isinstance(content.get("data"), dict):
            raise ValueError("Expected a Reddit search listing")
        children = content["data"].get("children")
        if not isinstance(children, list):
            raise ValueError("Expected a Reddit search listing")
        results = []
        for child in children[:self.config.max_items_per_source]:
            if not isinstance(child, dict):
                continue
            if child.get("kind") not in {None, "t3"}:
                continue
            row = child.get("data", {})
            if not isinstance(row, dict) or row.get("subreddit_type") == "private":
                continue
            permalink = row.get("permalink", "")
            if not isinstance(permalink, str) or not permalink.startswith("/r/") or row.get("removed_by_category"):
                continue
            text = row.get("selftext") or ""
            if text in {"[removed]", "[deleted]"}:
                continue
            try:
                published = datetime.fromtimestamp(float(row["created_utc"]), UTC) if row.get("created_utc") else None
                author = row.get("author")
                author = author if isinstance(author, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", author) else None
                record = self._evidence(kind="reddit", url="https://www.reddit.com" + permalink,
                    origin="reddit:author:" + author.casefold() if author else "reddit:" + str(row.get("subreddit", "unknown")).casefold(),
                    title=str(row.get("title", "")), text=str(text), text_kind="public_post",
                    companies=companies, now=now, published_at=published,
                    author=author, language=None, flags=("self_selection", "community_selection", "unverified_claims"))
                if record:
                    results.append(record)
            except (TypeError, ValueError, OverflowError, OSError):
                continue
        return results

    def _parse_reddit_comments(self, content: list, post: DiscourseEvidence,
                               companies: Sequence[CompanyWatch], now: datetime,
                               *, limit: int) -> list[DiscourseEvidence]:
        if not isinstance(content, list) or len(content) < 2 or not isinstance(content[1], dict):
            raise ValueError("Expected a Reddit public comment listing")
        if isinstance(content[0], dict):
            posts = content[0].get("data", {}).get("children", [])
            if isinstance(posts, list) and any(isinstance(item, dict) and isinstance(item.get("data"), dict)
                and item["data"].get("subreddit_type") == "private" for item in posts):
                raise ValueError("Private Reddit threads are excluded")
        children = content[1].get("data", {}).get("children")
        if not isinstance(children, list):
            raise ValueError("Expected Reddit comment children")
        results: list[DiscourseEvidence] = []
        pending = [(child, 0) for child in reversed(children[:limit])]
        inspected = 0
        while pending and inspected < limit:
            child, depth = pending.pop()
            if not isinstance(child, dict) or child.get("kind") != "t1":
                continue
            inspected += 1
            row = child.get("data", {})
            if not isinstance(row, dict) or row.get("subreddit_type") == "private":
                continue
            body = row.get("body") or ""
            if not isinstance(body, str):
                continue
            identifier = str(row.get("id", ""))
            if body in {"[deleted]", "[removed]"} or row.get("removed_by_category") or not re.fullmatch(r"[A-Za-z0-9]{1,20}", identifier):
                continue
            replies = row.get("replies")
            if depth < 2 and isinstance(replies, dict):
                nested = replies.get("data", {}).get("children", [])
                if isinstance(nested, list):
                    pending.extend((reply, depth + 1) for reply in reversed(nested[:limit]))
            author = row.get("author")
            author = author if isinstance(author, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", author) else None
            try:
                published = datetime.fromtimestamp(float(row["created_utc"]), UTC) if row.get("created_utc") else None
            except (TypeError, ValueError, OverflowError, OSError):
                published = None
            # Require company mention in comment itself. Thread headlines alone
            # must not attribute unrelated replies to a company's sentiment.
            names = matching_companies(plain_text(str(body)), companies)
            if not names:
                continue
            url = post.source_url.rstrip("/") + "/" + identifier + "/"
            record = self._evidence(kind="reddit", url=url,
                origin="reddit:author:" + author.casefold() if author else post.origin_key,
                title="Reddit company discussion: " + ", ".join(names), text=str(body), text_kind="public_comment", companies=companies,
                now=now, published_at=published, author=author, language=None,
                flags=("community_selection", "author_self_selection", "quoted_opinions_possible", "unverified_claims"))
            if record:
                results.append(record)
        return results

    def _parse_hackernews(self, content: dict, companies: Sequence[CompanyWatch],
                          now: datetime) -> list[DiscourseEvidence]:
        if not isinstance(content, dict) or not isinstance(content.get("hits"), list):
            raise ValueError("Expected an Algolia Hacker News search response")
        results: list[DiscourseEvidence] = []
        for row in content["hits"][:self.config.max_items_per_source]:
            if not isinstance(row, dict):
                continue
            identifier = str(row.get("objectID", ""))
            if not re.fullmatch(r"[0-9]{1,20}", identifier) or int(identifier) <= 0:
                continue
            tags = row.get("_tags", [])
            if not isinstance(tags, list):
                continue
            tags = [tag for tag in tags if isinstance(tag, str)]
            if not {"comment", "story"}.intersection(tags):
                continue
            is_comment = "comment" in tags
            title = str((row.get("story_title") if is_comment else row.get("title")) or "")
            text = str((row.get("comment_text") if is_comment else row.get("story_text")) or "")
            if is_comment and (not plain_text(text) or plain_text(text).casefold() in {"[deleted]", "[removed]"}):
                continue
            if row.get("deleted") or row.get("dead"):
                continue
            author = row.get("author")
            if not isinstance(author, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", author):
                author = None
            published = _date(row.get("created_at"))
            if published is None and row.get("created_at_i") is not None:
                try:
                    published = datetime.fromtimestamp(float(row["created_at_i"]), UTC)
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
            flags = ("community_selection", "author_self_selection", "topic_search_selection",
                     "quoted_opinions_possible", "unverified_claims")
            if not author:
                flags += ("author_origin_unknown",)
            if not is_comment and not plain_text(text):
                flags += ("story_headline_only",)
            record = self._evidence(kind="hackernews", url="https://news.ycombinator.com/item?id=" + identifier,
                origin="hackernews:" + (author or "unknown"), title=title, text=text,
                text_kind="public_comment" if is_comment else "public_story", companies=companies,
                now=now, published_at=published, author=author, language=None, flags=flags)
            if record:
                results.append(record)
        return results

    async def _youtube(self, identifier: str, companies: Sequence[CompanyWatch],
                       now: datetime) -> tuple[list[DiscourseEvidence], list[SourceCoverage]]:
        url = "https://www.youtube.com/watch?v=" + identifier
        records: list[DiscourseEvidence] = []
        coverage: list[SourceCoverage] = []
        metadata: dict = {}
        self._active_source = ("youtube_metadata", url, now)
        try:
            if self.config.youtube_api_key:
                result = json.loads(await self._get("https://www.googleapis.com/youtube/v3/videos", params={
                    "part": "snippet", "id": identifier, "key": self.config.youtube_api_key,
                }))
                metadata = result["items"][0]["snippet"]
            else:
                result = json.loads(await self._get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}))
                metadata = {"title": result.get("title", ""), "channelTitle": result.get("author_name", "")}
            record = self._evidence(kind="youtube", url=url,
                origin="youtube:" + str(metadata.get("channelId") or metadata.get("channelTitle") or identifier),
                title=str(metadata.get("title", "")), text=str(metadata.get("description", "")),
                text_kind="video_metadata", companies=companies, now=now,
                published_at=_date(metadata.get("publishedAt")), author=metadata.get("channelTitle"),
                language=metadata.get("defaultAudioLanguage"), flags=("metadata_only", "creator_selection", "unverified_claims"))
            if record:
                records.append(record)
            coverage.append(SourceCoverage("youtube_metadata", url, now, "ok", int(record is not None), limitations=(
                "Video title/description is not a transcript and is excluded from sentiment",)))
        except Exception as exc:
            coverage.append(SourceCoverage("youtube_metadata", url, now, "error", error=_error(exc)))
        self._checkpoint(records, coverage, video=True)
        self._active_source = ("youtube_captions", url, now)
        try:
            player = _player_response(await self._get(url))
            tracks = player.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
            english = [track for track in tracks if str(track.get("languageCode", "")).startswith("en")]
            if not english:
                coverage.append(SourceCoverage("youtube_captions", url, now, "unavailable", limitations=(
                    "No accessible English caption track in public watch metadata; video speech was not analyzed",)))
                self._checkpoint(records, coverage, video=True)
                return records, coverage
            track = sorted(english, key=lambda row: row.get("kind") == "asr")[0]
            caption_url = _safe_url(str(track["baseUrl"]))
            host = (urlsplit(caption_url).hostname or "").lower()
            if host not in {"youtube.com", "www.youtube.com"} or urlsplit(caption_url).scheme != "https":
                raise ValueError("Caption URL is outside the allowed YouTube hosts")
            response = await self._get(caption_url)
            if not response.strip():
                raise ValueError("Public caption track returned no text")
            if response.lstrip().startswith("{"):
                document = json.loads(response)
                transcript = " ".join(str(segment.get("utf8", ""))
                    for event in document.get("events", []) for segment in event.get("segs", []))
            else:
                document = _xml(response)
                transcript = " ".join("".join(node.itertext()) for node in document.iter()
                                      if node.tag.split("}")[-1] in {"text", "p"})
            if not plain_text(transcript).strip():
                raise ValueError("Public caption track returned no text")
            details = player.get("videoDetails", {})
            flags = ("creator_selection", "unverified_claims")
            if track.get("kind") == "asr":
                flags += ("automatic_captions",)
            record = self._evidence(kind="youtube", url=url,
                origin="youtube:" + str(details.get("channelId") or metadata.get("channelId") or metadata.get("channelTitle") or identifier),
                title=str(details.get("title") or metadata.get("title", "")), text=transcript,
                text_kind="video_transcript", companies=companies, now=now,
                published_at=_date(metadata.get("publishedAt")), author=details.get("author"),
                language=track.get("languageCode"), flags=flags)
            if record:
                records.append(record)
            coverage.append(SourceCoverage("youtube_captions", url, now, "ok", int(record is not None), limitations=(
                "Public captions may be inaccurate; one creator is not representative of public opinion",)))
        except Exception as exc:
            coverage.append(SourceCoverage("youtube_captions", url, now, "unavailable", error=_error(exc), limitations=(
                "Video speech was not analyzed; official captions API requires owner authorization",)))
        self._checkpoint(records, coverage, video=True)
        return records, coverage
