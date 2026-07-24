from __future__ import annotations

import asyncio
import logging
import random
import signal
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import uvicorn

from .archive import EvidenceArchive
from .config import Settings
from .db import Database
from .health import HealthRegistry, create_health_app
from .processor import EvidenceProcessor
from .smtp_worker import SMTPTransport, SMTPWorker
from .sources.http import PermanentHTTPError, ResilientClient
from .sources.market import TwelveDataCollector
from .sources.sam import SAMCollector
from .sources.sec import SECCollector
from .sources.usaspending import USAspendingCollector

logger = logging.getLogger(__name__)


class ContractSource(Protocol):
    async def collect(self, *, observed_at: datetime) -> list[Any]: ...


class ListingSource(Protocol):
    async def collect(self, *, observed_at: datetime) -> list[Any]: ...


class SECSource:
    def __init__(self, collector: SECCollector, forms: tuple[str, ...]):
        self.collector = collector
        self.forms = forms

    async def collect(self, *, observed_at: datetime) -> list[Any]:
        signals: list[Any] = []
        for form in self.forms:
            entries = await self.collector.current_entries(form)
            for entry in entries:
                try:
                    signal = await self.collector.classify_entry(entry)
                except Exception as exc:
                    logger.warning("SEC filing parse failed", extra={"entry": entry.get("source_url"), "error": str(exc)})
                    continue
                if signal is not None:
                    signals.append(signal)
        return signals


class SAMSource:
    def __init__(self, collector: SAMCollector):
        self.collector = collector
        self._deleted_next = False

    async def collect(self, *, observed_at: datetime) -> list[Any]:
        deleted = self._deleted_next
        self._deleted_next = not self._deleted_next
        return await self.collector.collect(
            observed_at=observed_at,
            last_modified_start=observed_at.date() - timedelta(days=2),
            deleted=deleted,
        )


class MonitorService:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        health: HealthRegistry,
        sec_source: ListingSource,
        usaspending_source: ContractSource,
        smtp_worker: SMTPWorker,
        sam_source: ContractSource | None = None,
        market_lookup: Callable[[str], Any] | None = None,
        now: Callable[[], datetime] | None = None,
        archive: EvidenceArchive | None = None,
        clients: tuple[ResilientClient, ...] = (),
    ):
        self.settings = settings
        self.db = db
        self.health = health
        self.sec_source = sec_source
        self.usaspending_source = usaspending_source
        self.sam_source = sam_source
        self.smtp_worker = smtp_worker
        self.now = now or (lambda: datetime.now(UTC))
        self.processor = EvidenceProcessor(db, now=self.now, market_lookup=market_lookup, archive=archive)
        self.clients = clients
        self.stop_event = asyncio.Event()

    @classmethod
    def build_default(cls, settings: Settings) -> "MonitorService":
        settings.prepare_paths()
        db = Database(settings.database_path)
        db.initialize()
        health = HealthRegistry()
        health.set_database_ready(True)
        sec_client = ResilientClient(headers={"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}, max_attempts=3)
        usa_client = ResilientClient(headers={"User-Agent": settings.sec_user_agent or "gov-contract-ipo-monitor"}, max_attempts=3)
        clients: list[ResilientClient] = [sec_client, usa_client]
        sec_source = SECSource(SECCollector(sec_client), settings.enabled_sec_forms)
        usa_source = USAspendingCollector(usa_client)
        sam_source = None
        if settings.sam_api_key:
            sam_client = ResilientClient(headers={"User-Agent": settings.sec_user_agent or "gov-contract-ipo-monitor"}, max_attempts=3)
            clients.append(sam_client)
            sam_source = SAMSource(SAMCollector(sam_client, settings.sam_api_key))
        market_lookup = None
        if settings.twelve_data_api_key:
            market_client = ResilientClient(headers={"User-Agent": settings.sec_user_agent or "gov-contract-ipo-monitor"}, max_attempts=3)
            clients.append(market_client)
            market = TwelveDataCollector(market_client, settings.twelve_data_api_key)
            market_lookup = lambda symbol: market.snapshot(symbol, observed_at=datetime.now(UTC))
        transport = SMTPTransport(
            host=settings.smtp_host, port=settings.smtp_port,
            username=settings.smtp_username or None, password=settings.smtp_password or None,
            sender=settings.smtp_sender, recipients=settings.smtp_recipients,
            security=settings.smtp_security,
        )
        worker = SMTPWorker(db, transport=transport)
        return cls(
            settings=settings, db=db, health=health, sec_source=sec_source,
            usaspending_source=usa_source, sam_source=sam_source, smtp_worker=worker,
            market_lookup=market_lookup, archive=EvidenceArchive(settings.evidence_archive_path),
            clients=tuple(clients),
        )

    async def run_once(self) -> dict[str, int]:
        summary = {"contracts": 0, "listing_signals": 0, "sam_records": 0, "alerts_created": 0, "emails_sent": 0}
        self.health.set_database_ready(True)
        try:
            contracts = await self.usaspending_source.collect(observed_at=self.now())
            summary["contracts"] = len(contracts)
            for evidence in contracts:
                results = await self.processor.ingest_contract_async(evidence)
                summary["alerts_created"] += sum(int(result.alert_created) for result in results)
            self.health.mark_success("usaspending")
        except Exception as exc:
            self.health.mark_error("usaspending", f"{type(exc).__name__}: {exc}", disabled=isinstance(exc, PermanentHTTPError))
            logger.exception("USAspending collection failed")

        try:
            signals = await self.sec_source.collect(observed_at=self.now())
            summary["listing_signals"] = len(signals)
            for listing in signals:
                results = await self.processor.ingest_listing_async(listing)
                summary["alerts_created"] += sum(int(result.alert_created) for result in results)
            self.health.mark_success("sec")
        except Exception as exc:
            self.health.mark_error("sec", f"{type(exc).__name__}: {exc}", disabled=isinstance(exc, PermanentHTTPError))
            logger.exception("SEC collection failed")

        if self.sam_source is not None:
            try:
                records = await self.sam_source.collect(observed_at=self.now())
                summary["sam_records"] = len(records)
                for evidence in records:
                    results = await self.processor.ingest_contract_async(evidence)
                    summary["alerts_created"] += sum(int(result.alert_created) for result in results)
                self.health.mark_success("sam")
            except Exception as exc:
                self.health.mark_error("sam", f"{type(exc).__name__}: {exc}", disabled=isinstance(exc, PermanentHTTPError))
                logger.exception("SAM collection failed")

        while await asyncio.to_thread(self.smtp_worker.run_once):
            summary["emails_sent"] += 1
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
                return
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
            asyncio.create_task(self._outbox_loop()),
        ]
        if self.sam_source is not None:
            tasks.append(asyncio.create_task(self._collector_loop("sam", self.settings.sam_interval_seconds, self._poll_sam)))
        if serve_health:
            config = uvicorn.Config(create_health_app(self.health), host=self.settings.health_host, port=self.settings.health_port, log_level="info")
            server = uvicorn.Server(config)
            tasks.append(asyncio.create_task(server.serve()))
        await self.stop_event.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for client in self.clients:
            await client.aclose()

    async def _poll_usaspending(self) -> None:
        records = await self.usaspending_source.collect(observed_at=self.now())
        for record in records:
            await self.processor.ingest_contract_async(record)
        self.health.mark_success("usaspending")

    async def _poll_sec(self) -> None:
        signals = await self.sec_source.collect(observed_at=self.now())
        for item in signals:
            await self.processor.ingest_listing_async(item)
        self.health.mark_success("sec")

    async def _poll_sam(self) -> None:
        if self.sam_source is None:
            return
        records = await self.sam_source.collect(observed_at=self.now())
        for record in records:
            await self.processor.ingest_contract_async(record)
        self.health.mark_success("sam")
