from __future__ import annotations

import asyncio
import logging
import random
import signal
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import uvicorn

from .archive import EvidenceArchive
from .config import Settings
from .db import Database
from .health import HealthRegistry, create_health_app
from .processor import EvidenceProcessor
from .research import ResearchStore, serializable
from .market import DailyPriceCollector
from .market_monitor import MarketMonitor
from .fundamentals import SECCompanyFactsCollector
from .worldnews import WorldNewsCollector
from .tracking import IPOTracker
from .sentiment import summarize_sentiment
from .sources.discourse import CompanyWatch, DiscourseCollector, DiscourseConfig
from .smtp_worker import SMTPTransport, SMTPWorker
from .sources.http import PermanentHTTPError, ResilientClient
from .sources.market import TwelveDataCollector
from .sources.sam import SAMCollector
from .sources.sec import SECCollector
from .sources.usaspending import USAspendingCollector, USAspendingRecipientResolver

logger = logging.getLogger(__name__)


class ContractSource(Protocol):
    async def collect(self, *, observed_at: datetime) -> list[Any]: ...


class ListingSource(Protocol):
    async def collect(self, *, observed_at: datetime) -> list[Any]: ...


class SECSource:
    def __init__(
        self,
        collector: SECCollector,
        forms: tuple[str, ...],
        *,
        recipient_resolver: USAspendingRecipientResolver | None = None,
    ):
        self.collector = collector
        self.forms = forms
        self.recipient_resolver = recipient_resolver
        self.ipo_events: list[Any] = []
        self.processed_entries: list[dict[str, Any]] = []
        self.parse_errors: list[str] = []
        self.partial_signals: list[Any] = []

    async def collect(self, *, observed_at: datetime) -> list[Any]:
        self.ipo_events = []
        self.processed_entries = []
        self.parse_errors = []
        self.partial_signals = []
        signals = self.partial_signals
        seen: set[str] = set()
        for form in self.forms:
            try:
                entries = await self.collector.current_entries(form, count=40)
            except Exception as exc:
                self.parse_errors.append(f"{form}: {type(exc).__name__}: {exc}")
                continue
            if getattr(self.collector, "last_feed_truncated", False):
                self.parse_errors.append(f"{form}: feed page limit reached; older filings may be missing")
                if getattr(self.collector, "db", None) is not None:
                    self.collector.db.update_collector_state(f"sec_feed_gap:{form}", error="Bootstrap/page-limit coverage gap: older public filings were not fully scanned.")
            for entry in entries:
                key = str(entry.get("accession") or entry.get("source_url"))
                if key in seen or self.collector.is_processed(key):
                    continue
                seen.add(key)
                try:
                    event, signal = await self.collector.collect_entry(entry)
                except Exception as exc:
                    logger.warning("SEC filing parse failed", extra={"entry": entry.get("source_url"), "error": str(exc)})
                    self.parse_errors.append(f"{key}: {type(exc).__name__}: {exc}")
                    continue
                if event is not None:
                    self.ipo_events.append(event)
                self.processed_entries.append(entry)
                if signal is None:
                    continue
                if self.recipient_resolver is not None and not signal.linked_ueis:
                    try:
                        uei = await self.recipient_resolver.resolve_uei(signal.issuer_name)
                    except Exception as exc:
                        logger.info("Optional SEC-to-UEI enrichment failed", extra={"issuer": signal.issuer_name, "error": str(exc)})
                    else:
                        if uei:
                            signal = signal.model_copy(update={"linked_ueis": (uei,)})
                signals.append(signal)
        return signals


class SAMSource:
    def __init__(self, collector: SAMCollector):
        self.collector = collector

    async def collect(self, *, observed_at: datetime) -> list[Any]:
        records = []
        for deleted in (False, True):
            records.extend(await self.collector.collect(
                observed_at=observed_at,
                last_modified_start=observed_at.date() - timedelta(days=2),
                deleted=deleted,
            ))
        return records


class MonitorService:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        health: HealthRegistry,
        sec_source: ListingSource,
        usaspending_source: ContractSource,
        smtp_worker: SMTPWorker | None,
        sam_source: ContractSource | None = None,
        market_lookup: Callable[[str], Any] | None = None,
        now: Callable[[], datetime] | None = None,
        archive: EvidenceArchive | None = None,
        clients: tuple[ResilientClient, ...] = (),
        discourse_source: DiscourseCollector | None = None,
        price_source: Any = None,
        world_source: Any = None,
        fundamentals_source: Any = None,
        instruments: Any = None,
        benchmarks: Any = None,
    ):
        self.settings = settings
        self.db = db
        self.health = health
        self.sec_source = sec_source
        self.usaspending_source = usaspending_source
        self.sam_source = sam_source
        self.smtp_worker = smtp_worker
        self.discourse_source = discourse_source
        self.now = now or (lambda: datetime.now(UTC))
        self.processor = EvidenceProcessor(
            db,
            now=self.now,
            market_lookup=market_lookup,
            archive=archive,
            max_price=settings.max_price,
            max_market_cap=settings.max_market_cap,
            quote_max_age_hours=settings.quote_max_age_hours,
        )
        self.clients = clients
        self.tracker = IPOTracker(db)
        self.tracker.initialize()
        self.research = ResearchStore(db)
        self.research.initialize()
        self.markets = MarketMonitor(db, settings, now=self.now, price_source=price_source,
                                     world_source=world_source, fundamentals_source=fundamentals_source,
                                     instruments=instruments, benchmarks=benchmarks)
        self.stop_event = asyncio.Event()

    @classmethod
    def build_default(cls, settings: Settings) -> "MonitorService":
        settings.prepare_paths()
        db = Database(settings.database_path)
        db.initialize()
        health = HealthRegistry(collector_max_ages={
            "sec": 2 * settings.sec_interval_seconds + settings.source_timeout_seconds,
            "usaspending": 2 * settings.usaspending_interval_seconds + settings.source_timeout_seconds,
            "sam": 2 * settings.sam_interval_seconds + settings.source_timeout_seconds,
            "discourse": 2 * settings.discourse_interval_seconds + settings.source_timeout_seconds,
            "markets": 2 * settings.market_interval_seconds + settings.source_timeout_seconds,
            "world_news": 2 * settings.market_interval_seconds + settings.source_timeout_seconds,
        })
        health.set_database_ready(True)
        sec_client = ResilientClient(
            headers={"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"},
            max_attempts=3,
            max_response_bytes=settings.sec_max_document_bytes,
        )
        usa_client = ResilientClient(
            headers={"User-Agent": settings.sec_user_agent or "gov-contract-ipo-monitor"},
            max_attempts=3,
        )
        clients: list[ResilientClient] = [sec_client, usa_client]
        recipient_resolver = USAspendingRecipientResolver(usa_client)
        sec_source = SECSource(
            SECCollector(sec_client, max_pages=settings.sec_max_pages, db=db, max_document_bytes=settings.sec_max_document_bytes),
            settings.enabled_sec_forms,
            recipient_resolver=recipient_resolver,
        )
        usa_source = USAspendingCollector(usa_client, recipient_resolver=recipient_resolver)
        sam_source = None
        if settings.sam_api_key:
            sam_client = ResilientClient(
                headers={"User-Agent": settings.sec_user_agent or "gov-contract-ipo-monitor"},
                max_attempts=3,
            )
            clients.append(sam_client)
            sam_source = SAMSource(SAMCollector(sam_client, settings.sam_api_key))
        market_lookup = None
        if settings.twelve_data_api_key:
            market_client = ResilientClient(
                headers={"User-Agent": settings.sec_user_agent or "gov-contract-ipo-monitor"},
                max_attempts=3,
            )
            clients.append(market_client)
            market = TwelveDataCollector(market_client, settings.twelve_data_api_key)
            market_lookup = lambda symbol: market.snapshot(symbol, observed_at=datetime.now(UTC))
        transport = SMTPTransport(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username or None,
            password=settings.smtp_password or None,
            sender=settings.smtp_sender,
            recipients=settings.smtp_recipients,
            security=settings.smtp_security,
        )
        worker = SMTPWorker(db, transport=transport) if settings.smtp_enabled else None
        discourse = DiscourseCollector(DiscourseConfig(
            feed_urls=settings.news_feed_urls, video_urls=settings.youtube_video_urls,
            forum_feed_urls=settings.forum_feed_urls, forums_enabled=settings.forums_enabled,
            reddit_enabled=settings.reddit_enabled, youtube_api_key=settings.youtube_api_key,
            reddit_access_token=settings.reddit_access_token, reddit_client_id=settings.reddit_client_id,
            reddit_client_secret=settings.reddit_client_secret, reddit_refresh_token=settings.reddit_refresh_token,
            reddit_posts_per_company=settings.reddit_posts_per_company,
            reddit_comments_per_post=settings.reddit_comments_per_post,
            reddit_max_comments_per_run=settings.reddit_max_comments_per_run,
            hacker_news_enabled=settings.hacker_news_enabled,
            user_agent=settings.sec_user_agent,
        )) if settings.discourse_enabled else None
        prices = world = fundamentals = None
        if settings.markets_enabled:
            prices = DailyPriceCollector(api_key=settings.twelve_data_api_key)
            world = WorldNewsCollector()
            facts_client = ResilientClient(headers={"User-Agent": settings.sec_user_agent}, max_attempts=2,
                                           allowed_hosts=("data.sec.gov",), max_response_bytes=8_000_000)
            clients.append(facts_client)
            fundamentals = SECCompanyFactsCollector(facts_client)
        return cls(
            settings=settings,
            db=db,
            health=health,
            sec_source=sec_source,
            usaspending_source=usa_source,
            sam_source=sam_source,
            smtp_worker=worker,
            market_lookup=market_lookup,
            archive=EvidenceArchive(settings.evidence_archive_path),
            clients=tuple(clients),
            discourse_source=discourse,
            price_source=prices, world_source=world, fundamentals_source=fundamentals,
        )

    def _collector_last_success(self, name: str) -> datetime | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT last_success_at FROM collector_state WHERE name=?", (name,)).fetchone()
        if row is None or not row["last_success_at"]:
            return None
        try:
            value = datetime.fromisoformat(row["last_success_at"])
        except ValueError:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def _usaspending_start_date(self, observed_at: datetime) -> date:
        previous = self._collector_last_success("usaspending")
        if previous is None:
            return observed_at.date() - timedelta(days=self.settings.usaspending_initial_lookback_days)
        return (previous - timedelta(days=self.settings.usaspending_overlap_days)).date()

    async def _collect_usaspending_records(self, observed_at: datetime) -> list[Any]:
        if isinstance(self.usaspending_source, USAspendingCollector):
            return await self.usaspending_source.collect(
                observed_at=observed_at,
                start_date=self._usaspending_start_date(observed_at),
                end_date=observed_at.date(),
                max_pages=self.settings.usaspending_max_pages,
            )
        return await self.usaspending_source.collect(observed_at=observed_at)

    async def run_once(self) -> dict[str, Any]:
        summary = {"contracts": 0, "listing_signals": 0, "ipo_events": 0, "discourse_records": 0, "sam_records": 0, "alerts_created": 0, "emails_sent": 0,
                   "price_histories": 0, "financial_updates": 0, "world_events": 0, "trade_ideas": 0}
        self.health.set_database_ready(True)
        self._alerts_before = self.db.count("alerts")
        observed = self.now()
        try:
            contracts = await asyncio.wait_for(self._collect_usaspending_records(observed), timeout=self.settings.source_timeout_seconds)
            summary["contracts"] = len(contracts)
            for evidence in contracts:
                results = await self.processor.ingest_contract_async(evidence)
                summary["alerts_created"] += sum(int(result.alert_created) for result in results)
            self.health.mark_success("usaspending")
            self.db.update_collector_state("usaspending", cursor=observed.date().isoformat(), success_at=observed, error=None)
        except Exception as exc:
            self.health.mark_error("usaspending", f"{type(exc).__name__}: {exc}", disabled=isinstance(exc, PermanentHTTPError))
            self.db.update_collector_state("usaspending", error=f"{type(exc).__name__}: {exc}", disabled=isinstance(exc, PermanentHTTPError))
            logger.exception("USAspending collection failed")

        try:
            signals = await self._poll_sec()
            summary["listing_signals"] = len(signals)
            summary["ipo_events"] = len(getattr(self.sec_source, "ipo_events", []))
        except Exception as exc:
            self.health.mark_error("sec", f"{type(exc).__name__}: {exc}", disabled=isinstance(exc, PermanentHTTPError))
            self.db.update_collector_state("sec", error=f"{type(exc).__name__}: {exc}")
            summary["listing_signals"] = len(getattr(self.sec_source, "partial_signals", []))
            summary["ipo_events"] = len(getattr(self.sec_source, "ipo_events", []))
            logger.exception("SEC collection failed")

        if self.sam_source is not None:
            try:
                records = await asyncio.wait_for(self.sam_source.collect(observed_at=self.now()), timeout=self.settings.source_timeout_seconds)
                summary["sam_records"] = len(records)
                for evidence in records:
                    results = await self.processor.ingest_contract_async(evidence)
                    summary["alerts_created"] += sum(int(result.alert_created) for result in results)
                self.health.mark_success("sam")
                self.db.update_collector_state("sam", success_at=self.now())
            except Exception as exc:
                self.health.mark_error("sam", f"{type(exc).__name__}: {exc}", disabled=isinstance(exc, PermanentHTTPError))
                self.db.update_collector_state("sam", error=f"{type(exc).__name__}: {exc}")
                logger.exception("SAM collection failed")

        for name, source, poll in (("markets", self.markets.price_source, self._poll_markets),
                                   ("world_news", self.markets.world_source, self._poll_world)):
            if source is None:
                continue
            try:
                await poll()
            except Exception as exc:
                self.health.mark_error(name, f"{type(exc).__name__}: {exc}")
                self.db.update_collector_state(name, error=f"{type(exc).__name__}: {exc}")
                logger.exception("Market research collection incomplete", extra={"collector": name})
        summary.update(self.markets.last_counts)
        if self.discourse_source is not None:
            try:
                summary["discourse_records"] = await self._poll_discourse()
            except Exception as exc:
                summary["discourse_records"] = getattr(self, "last_discourse_inserted", 0)
                self.health.mark_error("discourse", f"{type(exc).__name__}: {exc}")
                self.db.update_collector_state("discourse", error=f"{type(exc).__name__}: {exc}")
                logger.exception("Discourse collection failed")
        if self.smtp_worker is not None:
            while await asyncio.to_thread(self.smtp_worker.run_once):
                summary["emails_sent"] += 1
        summary["alerts_created"] = self.db.count("alerts") - self._alerts_before
        self.last_report = self.create_report(summary)
        summary["trade_ideas"] = sum(item["action"] != "wait" for item in self.last_report["trade_ideas"])
        self.last_report["counts"]["trade_ideas"] = summary["trade_ideas"]
        self.markets.store.save_ideas(self.last_report["trade_ideas"], observed_at=self.now())
        self.research.save_run(self.last_report)
        return summary

    async def _collector_loop(self, name: str, interval: int, collect: Callable[[], Any]) -> None:
        while not self.stop_event.is_set():
            try:
                await collect()
                self.db.update_collector_state(name, success_at=self.now(), error=None)
            except asyncio.CancelledError:
                raise
            except PermanentHTTPError as exc:
                self.health.mark_error(name, str(exc), disabled=True)
                self.db.update_collector_state(name, error=str(exc), disabled=True)
                logger.exception("collector disabled after permanent HTTP error", extra={"collector": name})
                # Permission/rate-policy failures remain visible and retry on a later poll.
            except Exception as exc:
                self.health.mark_error(name, f"{type(exc).__name__}: {exc}")
                self.db.update_collector_state(name, error=f"{type(exc).__name__}: {exc}")
                logger.exception("collector loop failed", extra={"collector": name})
            delay = max(1.0, interval + random.uniform(-min(5, interval * 0.1), min(5, interval * 0.1)))
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _outbox_loop(self) -> None:
        if self.smtp_worker is None:
            return
        while not self.stop_event.is_set():
            sent = await asyncio.to_thread(self.smtp_worker.run_once)
            if sent:
                continue
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.settings.smtp_poll_seconds)
            except TimeoutError:
                pass

    async def run_forever(self, *, serve_health: bool = True) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass
        tasks = [
            asyncio.create_task(self._collector_loop("usaspending", self.settings.usaspending_interval_seconds, self._poll_usaspending)),
            asyncio.create_task(self._collector_loop("sec", self.settings.sec_interval_seconds, self._poll_sec)),
            asyncio.create_task(self._report_loop()),
        ]
        if self.smtp_worker is not None:
            tasks.append(asyncio.create_task(self._outbox_loop()))
        if self.sam_source is not None:
            tasks.append(asyncio.create_task(self._collector_loop("sam", self.settings.sam_interval_seconds, self._poll_sam)))
        if self.discourse_source is not None:
            tasks.append(asyncio.create_task(self._collector_loop("discourse", self.settings.discourse_interval_seconds, self._poll_discourse)))
        if self.markets.price_source is not None:
            tasks.append(asyncio.create_task(self._collector_loop("markets", self.settings.market_interval_seconds, self._poll_markets)))
        if self.markets.world_source is not None:
            tasks.append(asyncio.create_task(self._collector_loop("world_news", self.settings.market_interval_seconds, self._poll_world)))
        if serve_health:
            config = uvicorn.Config(
                create_health_app(self.health, self.db),
                host=self.settings.health_host,
                port=self.settings.health_port,
                log_level="info",
            )
            server = uvicorn.Server(config)
            tasks.append(asyncio.create_task(server.serve()))
        stop_task = asyncio.create_task(self.stop_event.wait())
        # A crashed SMTP or health task must stop the supervisor, not hang forever.
        try:
            done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            await self.aclose()
        for task in done:
            if task is not stop_task and not task.cancelled() and task.exception() is not None:
                raise task.exception()

    async def _poll_usaspending(self) -> None:
        observed = self.now()
        records = await asyncio.wait_for(self._collect_usaspending_records(observed), timeout=self.settings.source_timeout_seconds)
        for record in records:
            await self.processor.ingest_contract_async(record)
        self.db.update_collector_state("usaspending", cursor=observed.date().isoformat(), success_at=observed, error=None)
        self.health.mark_success("usaspending")

    async def _poll_sec(self) -> list[Any]:
        error: Exception | None = None
        try:
            signals = await asyncio.wait_for(self.sec_source.collect(observed_at=self.now()), timeout=self.settings.source_timeout_seconds)
        except Exception as exc:
            error = exc
            signals = getattr(self.sec_source, "partial_signals", [])
        for event in getattr(self.sec_source, "ipo_events", []):
            self.tracker.record(event, observed_at=self.now())
        for item in signals:
            await self.processor.ingest_listing_async(item)
        for entry in getattr(self.sec_source, "processed_entries", []):
            self.sec_source.collector.mark_processed(str(entry.get("accession") or entry.get("source_url")), observed_at=self.now())
        failures = getattr(self.sec_source, "parse_errors", [])
        if error is not None:
            raise error
        if failures:
            raise RuntimeError(f"Partial SEC collection ({len(failures)} failures): " + "; ".join(failures[:3]))
        self.health.mark_success("sec")
        self.db.update_collector_state("sec", success_at=self.now())
        return signals

    def companies(self) -> tuple[CompanyWatch, ...]:
        public = [CompanyWatch(item.name, aliases=(*item.aliases, "$" + item.symbol)) for item in self.markets.instruments] if self.settings.markets_enabled else []
        names = list(self.settings.watch_companies)
        names += [candidate["issuer_name"] for candidate in self.tracker.candidates(limit=50) if candidate["ipo_confirmed"] and candidate["active"]]
        watches = public + [CompanyWatch(name, aliases=("Anduril Industries",) if name == "Anduril" else ()) for name in dict.fromkeys(names)]
        seen = set()
        unique = []
        for company in watches:
            if company.name.casefold() not in seen:
                seen.add(company.name.casefold())
                unique.append(company)
        return tuple(unique[:self.settings.discourse_max_companies])

    async def _poll_markets(self) -> None:
        await asyncio.wait_for(self.markets.collect_prices(), timeout=self.settings.source_timeout_seconds)
        failures = [item for item in self.markets.store.coverage("markets") if item["status"] in {"error", "unavailable", "insufficient", "not_attempted"}]
        if failures:
            raise RuntimeError(f"{len(failures)} market source gaps: " + "; ".join(f"{item['symbol']}: {item.get('error')}" for item in failures[:3]))
        self.health.mark_success("markets")
        self.db.update_collector_state("markets", success_at=self.now(), error=None)

    async def _poll_world(self) -> None:
        await asyncio.wait_for(self.markets.collect_world(), timeout=self.settings.source_timeout_seconds)
        coverage = self.markets.store.coverage("world")
        failures = [item for item in coverage if item["status"] != "ok"]
        if failures or not coverage:
            raise RuntimeError(f"World-news coverage incomplete: {len(failures)} source gaps")
        self.health.mark_success("world_news")
        self.db.update_collector_state("world_news", success_at=self.now(), error=None)

    async def _poll_discourse(self) -> int:
        if self.discourse_source is None:
            return 0
        self.last_discourse_inserted = 0
        try:
            batch = await asyncio.wait_for(self.discourse_source.collect(self.companies()), timeout=self.settings.source_timeout_seconds)
        except TimeoutError:
            partial = getattr(self.discourse_source, "partial_batch", None)
            if partial is not None:
                self.last_discourse_inserted = self.research.record_batch(partial)
            raise
        inserted = self.research.record_batch(batch)
        self.last_discourse_inserted = inserted
        failures = [item for item in batch.coverage if item.status in {"error", "unavailable", "blocked", "truncated", "partial"}]
        if failures:
            error = f"{len(failures)} source gaps: " + "; ".join(f"{item.source}: {item.error or item.status}" for item in failures[:3])
            self.health.mark_error("discourse", error)
            self.db.update_collector_state("discourse", error=error)
            raise RuntimeError(error)
        else:
            self.health.mark_success("discourse")
            self.db.update_collector_state("discourse", success_at=self.now())
        return inserted

    def create_report(self, counts: dict[str, Any] | None = None) -> dict[str, Any]:
        records = self.research.records()
        sentiment = []
        for company in self.companies():
            summary = asdict(summarize_sentiment(company.name, records, now=self.now()))
            summary["company_name"] = company.name
            sentiment.append(summary)
        health = self.health.snapshot()
        coverage = self.research.coverage()
        with self.db.connect() as conn:
            historic_gaps = [dict(row) for row in conn.execute("SELECT name,last_error,updated_at FROM collector_state WHERE name LIKE 'sec_feed_gap:%' ORDER BY name")]
            # Count persisted identities independently of content versions and
            # current-batch rechecks. One statement gives a consistent snapshot.
            history = dict(conn.execute("""
                SELECT
                  (SELECT COUNT(*) FROM (
                    SELECT 1 FROM contract_evidence
                    GROUP BY json_extract(version_json, '$.source'), json_extract(version_json, '$.source_record_id')
                  )) AS contract_records,
                  (SELECT COUNT(*) FROM contract_evidence) AS contract_versions,
                  (SELECT COUNT(*) FROM (SELECT 1 FROM ipo_evidence GROUP BY source,event_id)) AS ipo_filings,
                  (SELECT COUNT(*) FROM ipo_evidence) AS ipo_versions,
                  (SELECT COUNT(DISTINCT evidence_id) FROM discourse_evidence) AS commentary_items,
                  (SELECT COUNT(*) FROM discourse_evidence) AS commentary_versions
            """).fetchone())
        market_report = self.markets.report(sentiment)
        return serializable({
            **market_report,
            "completed_at": self.now(), "status": "ok" if health["ready"] and not historic_gaps and all(item.get("ok") for item in health["collectors"].values()) and (not self.settings.markets_enabled or market_report.get("world_coverage_ready") and market_report.get("price_coverage") and not market_report.get("market_restore_errors")) else "degraded",
            "counts": counts or {}, "history": history, "health": health, "ipo_summary": self.tracker.summary(),
            "ipos": self.tracker.candidates(limit=100), "sentiment": sentiment, "coverage": coverage,
            "watchlist": [company.name for company in self.companies()],
            "historic_coverage_gaps": historic_gaps,
            "evidence": [{"source_url": item.source_url, "source_kind": item.source_kind, "text_kind": item.text_kind, "title": item.title, "excerpt": item.text[:300], "company_names": item.company_names, "published_at": item.published_at, "bias_flags": item.bias_flags} for item in records[:100]],
            "limitations": [
                "SEC coverage is U.S. public filings in bounded current-feed pages; confidential and international filings are not covered.",
                "Watchlist companies are research targets; inclusion does not establish an announced or planned IPO.",
                "Government contracts are supplementary evidence and are not required to track an IPO.",
                "Sentiment is an English lexicon estimate of an accessible sample, balanced by publisher, Hacker News account, Reddit community, YouTube channel, and platform, with duplication controls; it is not internet-wide opinion.",
                "Unavailable captions mean the video's spoken content has not been analyzed; metadata is excluded from sentiment.",
                "SAM.gov, Reddit OAuth, video discovery and some native-exchange price providers require optional approved access. State/local contract coverage remains incomplete.",
                "The reviewed shortlist spans global issuers but is concentrated in technology and U.S. listings; it is not an exhaustive ranking of world markets.",
                "Trade ideas are conditional days-to-weeks research screens using past daily prices; valuation, portfolio suitability and predictive accuracy remain unverified.",
                "World-news associations use explicit exposure themes and publisher headlines; they tighten risk checks without establishing a verified event or price direction.",
                "SMTP is disabled by default; reports and GitHub Actions receipts remain available without email credentials.",
            ],
        })

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()
        if self.discourse_source is not None:
            await self.discourse_source.aclose()
        await self.markets.aclose()

    async def _report_loop(self) -> None:
        from .research import write_report
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=60)
            except TimeoutError:
                report = self.create_report()
                self.markets.store.save_ideas(report["trade_ideas"], observed_at=self.now())
                self.research.save_run(report)
                write_report(report, self.settings.database_path.parent / "reports")

    async def _poll_sam(self) -> None:
        if self.sam_source is None:
            return
        records = await asyncio.wait_for(self.sam_source.collect(observed_at=self.now()), timeout=self.settings.source_timeout_seconds)
        for record in records:
            await self.processor.ingest_contract_async(record)
        self.health.mark_success("sam")
