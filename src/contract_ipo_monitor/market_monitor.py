"""Collection and durable evidence for reviewed public companies and trade research."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .db import Database
from .fundamentals import financial_fact_from_dict
from .market import price_history_from_dict
from .market_research import MarketResearchStore, payload
from .trades import assess_trade, rank_ideas
from .universe import Instrument, benchmark_instruments, default_fundamentals, default_universe, rank_universe, universe_metadata
from .worldnews import world_event_from_dict


class MarketMonitor:
    def __init__(self, db: Database, settings: Settings, *, now, price_source=None,
                 world_source=None, fundamentals_source=None, instruments=None, benchmarks=None):
        self.db, self.settings, self.now = db, settings, now
        self.price_source, self.world_source, self.fundamentals_source = price_source, world_source, fundamentals_source
        universe = default_universe() if instruments is None else instruments
        self.instruments = tuple(item for item in universe if not settings.watch_symbols or item.symbol in settings.watch_symbols)
        self.benchmarks = benchmark_instruments() if benchmarks is None else tuple(benchmarks)
        self.store = MarketResearchStore(db)
        self.store.initialize()
        if settings.markets_enabled:
            existing = self.store.latest("financials")
            # A new reviewed bundle may update an old checkpoint, while a
            # restart of the same bundle never renews or replaces newer data.
            for symbol, fact in default_fundamentals().items():
                old = financial_fact_from_dict(existing[symbol]) if symbol in existing else None
                key = lambda item: (item.period_end, item.period_type == "quarter", item.reported_at)
                if (old is None or key(fact) > key(old)) and any(item.symbol == symbol for item in self.instruments):
                    self.store.record("financials", symbol, fact, observed_at=now())
        self.last_counts: dict[str, int] = {}

    def _refresh_due(self, symbol: str, observed: datetime) -> bool:
        with self.db.connect() as conn:
            row = conn.execute("SELECT last_success_at FROM collector_state WHERE name=?", ("fundamentals:" + symbol,)).fetchone()
        if not row or not row[0]:
            return True
        stamp = datetime.fromisoformat(row[0])
        if stamp.utcoffset() is None:
            stamp = stamp.replace(tzinfo=UTC)
        return not timedelta(0) <= observed - stamp < timedelta(days=self.settings.fundamental_refresh_days)

    async def collect_prices(self) -> dict[str, int]:
        counts = {"price_histories": 0, "financial_updates": 0}
        self.last_counts = counts
        coverage = []
        observed = self.now()
        instruments = (*self.instruments, *self.benchmarks)
        previous_facts = self.store.latest("financials")
        active_index = -1
        try:
            for active_index, instrument in enumerate(instruments):
                if instrument.cik and self.fundamentals_source and self._refresh_due(instrument.symbol, observed):
                    try:
                        await asyncio.sleep(.5)  # The SEC also serves the independent IPO collector.
                        fact = await self.fundamentals_source.latest(instrument.cik, instrument.symbol, now=observed)
                        old = financial_fact_from_dict(previous_facts[instrument.symbol]) if instrument.symbol in previous_facts else None
                        usable = fact and fact.symbol == instrument.symbol and (not instrument.reporting_currency or fact.currency == instrument.reporting_currency)
                        # A quarter and a year ending on the same day are different
                        # measures. Keep the reviewed quarter in that tie.
                        key = lambda item: (item.period_end, item.period_type == "quarter", item.reported_at)
                        retained = usable and old is not None and key(fact) < key(old)
                        if usable and not retained:
                            counts["financial_updates"] += int(self.store.record("financials", instrument.symbol, fact, observed_at=observed))
                        self.db.update_collector_state("fundamentals:" + instrument.symbol, success_at=observed, error=None)
                        coverage.append({"source": "sec_companyfacts", "symbol": instrument.symbol,
                                         "status": "ok" if usable and not retained else "snapshot_retained", "observed_at": observed.isoformat(),
                                         "fetched_period_end": fact.period_end.isoformat() if fact else None,
                                         "fetched_period_type": fact.period_type if fact else None,
                                         "error": "A newer reviewed company result was retained instead of an older or annual SEC comparison." if retained else None if usable else "A complete, fresh comparable SEC result is unavailable; reviewed issuer evidence retains its own expiry."})
                    except Exception as exc:
                        detail = f"{type(exc).__name__}: {exc}"
                        self.db.update_collector_state("fundamentals:" + instrument.symbol, error=detail)
                        coverage.append({"source": "sec_companyfacts", "symbol": instrument.symbol, "status": "error",
                                         "observed_at": observed.isoformat(), "error": detail})
                if self.price_source is None:
                    continue
                try:
                    history = await self.price_source.collect(instrument, observed_at=observed)
                    self.store.record("prices", instrument.symbol, history, observed_at=observed)
                    counts["price_histories"] += int(history.status == "ok")
                    coverage.append({"source": history.source, "symbol": instrument.symbol, "status": history.status,
                                     "as_of": history.as_of.isoformat() if history.as_of else None,
                                     "source_url": history.source_url, "currency": history.currency,
                                     "observed_at": observed.isoformat(), "error": history.error})
                except Exception as exc:
                    coverage.append({"source": "daily_prices", "symbol": instrument.symbol, "status": "error",
                                     "observed_at": observed.isoformat(), "error": f"{type(exc).__name__}: {exc}"})
        except asyncio.CancelledError:
            completed = {item["symbol"] for item in coverage if item["source"] != "sec_companyfacts"}
            for instrument in instruments:
                if instrument.symbol not in completed:
                    coverage.append({"source": "daily_prices", "symbol": instrument.symbol, "status": "not_attempted",
                                     "observed_at": observed.isoformat(), "error": "Collection deadline interrupted this instrument or ended before it was attempted"})
            raise
        finally:
            self.store.record_coverage("markets", coverage, observed_at=observed)
        return counts

    def save_world_batch(self, batch) -> int:
        inserted = 0
        for event in batch.events:
            inserted += int(self.store.record("world", event.event_id, event, observed_at=event.observed_at))
        self.store.record_coverage("world", list(batch.coverage), observed_at=self.now())
        return inserted

    async def collect_world(self) -> int:
        if self.world_source is None:
            return 0
        try:
            batch = await self.world_source.collect(observed_at=self.now())
        except asyncio.CancelledError:
            self.last_counts["world_events"] = self.save_world_batch(self.world_source.partial_batch)
            raise
        except TimeoutError:
            self.last_counts["world_events"] = self.save_world_batch(self.world_source.partial_batch)
            raise
        inserted = self.save_world_batch(batch)
        self.last_counts["world_events"] = inserted
        return inserted

    def report(self, sentiment: list[dict]) -> dict[str, Any]:
        now = self.now()
        if not self.settings.markets_enabled:
            return {"listed_companies": [], "trade_ideas": [], "world_news": [], "world_coverage": [], "price_coverage": [], "universe": {"limitations": ["Public-market research is disabled."]}}
        facts, histories, events, restore_errors = {}, {}, [], []
        for symbol, data in self.store.latest("financials").items():
            try:
                facts[symbol] = financial_fact_from_dict(data)
            except (TypeError, ValueError, KeyError) as exc:
                restore_errors.append(f"{symbol} financial evidence could not be validated: {exc}")
        for symbol, data in self.store.latest("prices").items():
            try:
                histories[symbol] = price_history_from_dict(data)
            except (TypeError, ValueError, KeyError) as exc:
                restore_errors.append(f"{symbol} prices could not be validated: {exc}")
        for data in self.store.latest("world").values():
            try:
                event = world_event_from_dict(data)
                if event.published_at and timedelta(0) <= now - event.published_at <= timedelta(days=2):
                    events.append(event)
            except (TypeError, ValueError, KeyError) as exc:
                restore_errors.append(f"World evidence could not be validated: {exc}")
        events.sort(key=lambda item: item.published_at, reverse=True)
        world_coverage = self.store.coverage("world")
        fresh_publishers = set()
        for item in world_coverage:
            try:
                stamp = datetime.fromisoformat(item["observed_at"])
                if stamp.utcoffset() is not None and timedelta(0) <= now-stamp <= timedelta(hours=36) and item["status"] in {"ok", "partial"} and item.get("collected_count", 0):
                    fresh_publishers.add(item["source"].removeprefix("world_news:"))
            except (KeyError, TypeError, ValueError):
                continue
        fresh_publishers.intersection_update(event.publisher for event in events)
        sentiment_map = {item["company_name"]: item for item in sentiment}
        ideas = [assess_trade(instrument, facts.get(instrument.symbol), histories.get(instrument.symbol),
                             histories.get(instrument.benchmark_symbol), sentiment_map.get(instrument.name), events,
                             now=now, world_coverage_ok=len(fresh_publishers) >= 2,
                             max_price_age_business_days=self.settings.price_max_age_business_days,
                             fundamental_max_age_days=self.settings.fundamental_max_age_days)
                 for instrument in self.instruments]
        return {"listed_companies": [item.to_dict() for item in rank_universe(facts, now=now, instruments=self.instruments, max_age_days=self.settings.fundamental_max_age_days)],
                "trade_ideas": rank_ideas(ideas), "world_news": [payload(item) for item in events[:120]],
                "world_coverage": world_coverage, "world_coverage_ready": len(fresh_publishers) >= 2,
                "price_coverage": self.store.coverage("markets"), "universe": universe_metadata(),
                "market_restore_errors": restore_errors}

    async def aclose(self) -> None:
        for source in (self.price_source, self.world_source):
            if source is not None and hasattr(source, "aclose"):
                await source.aclose()
