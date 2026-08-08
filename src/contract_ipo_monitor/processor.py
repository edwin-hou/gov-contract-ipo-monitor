from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable
import inspect

from .archive import EvidenceArchive
from .db import Database
from .entity import EntityResolver
from .gate import AlertGate, GateResult
from .models import Candidate, ContractEvidence, ListingSignal, MarketSnapshot


class EvidenceProcessor:
    def __init__(
        self,
        db: Database,
        *,
        now: Callable[[], datetime] | None = None,
        market_lookup: Callable[[str], MarketSnapshot | None | Awaitable[MarketSnapshot | None]] | None = None,
        archive: EvidenceArchive | None = None,
        max_price: float = 5.0,
        max_market_cap: float = 300_000_000,
        quote_max_age_hours: int = 24,
    ):
        self.db = db
        self.now = now or (lambda: datetime.now(UTC))
        self.market_lookup = market_lookup
        self.archive = archive
        self.max_price = max_price
        self.max_market_cap = max_market_cap
        self.max_quote_age = timedelta(hours=quote_max_age_hours)

    def _gate(self) -> AlertGate:
        return AlertGate(
            self.db,
            now=self.now(),
            max_price=self.max_price,
            max_market_cap=self.max_market_cap,
            max_quote_age=self.max_quote_age,
        )

    def ingest_contract(self, evidence: ContractEvidence) -> list[GateResult]:
        observed = self.now()
        inserted = self.db.insert_source_record(evidence.source, evidence.source_record_id, evidence.model_dump(mode="json"), observed_at=observed)
        if not inserted.inserted:
            return []
        if self.archive:
            self.archive.write(evidence.source, evidence.source_record_id, evidence.model_dump(mode="json"), observed_at=observed)
        self.db.store_contract_evidence(evidence, source_record_id=inserted.row_id, created_at=observed)
        if evidence.cancelled or evidence.deleted or evidence.status.lower() in {"cancelled", "deleted", "rescinded"}:
            self.db.enqueue_correction(award_id=evidence.award_id, reason=f"Contract status changed to {evidence.status}.", source_url=evidence.source_url, created_at=observed)
            return []
        results: list[GateResult] = []
        for signal in self.db.load_listing_signals():
            if not signal.active:
                continue
            if EntityResolver().resolve(evidence, signal).matched:
                results.append(self._evaluate(evidence, signal))
        return results

    def ingest_listing(self, signal: ListingSignal) -> list[GateResult]:
        observed = self.now()
        external_id = f"{signal.signal_id}:{signal.status}"
        inserted = self.db.insert_source_record(signal.source, external_id, signal.model_dump(mode="json"), observed_at=observed)
        if not inserted.inserted:
            return []
        if self.archive:
            self.archive.write(signal.source, external_id, signal.model_dump(mode="json"), observed_at=observed)
        self.db.store_listing_signal(signal, source_record_id=inserted.row_id, created_at=observed)
        if not signal.active or signal.status.lower() in {"withdrawn", "terminated", "abandoned", "rejected"}:
            self.db.enqueue_correction(signal_id=signal.related_signal_id or signal.signal_id, company_name=signal.issuer_name, reason=f"Listing signal changed to {signal.status}.", source_url=signal.source_url, created_at=observed)
            return []
        results: list[GateResult] = []
        for evidence in self.db.load_contracts():
            if evidence.cancelled or evidence.deleted:
                continue
            if EntityResolver().resolve(evidence, signal).matched:
                results.append(self._evaluate(evidence, signal))
        return results

    async def ingest_contract_async(self, evidence: ContractEvidence) -> list[GateResult]:
        observed = self.now()
        inserted = self.db.insert_source_record(evidence.source, evidence.source_record_id, evidence.model_dump(mode="json"), observed_at=observed)
        if not inserted.inserted:
            return []
        if self.archive:
            self.archive.write(evidence.source, evidence.source_record_id, evidence.model_dump(mode="json"), observed_at=observed)
        self.db.store_contract_evidence(evidence, source_record_id=inserted.row_id, created_at=observed)
        if evidence.cancelled or evidence.deleted or evidence.status.lower() in {"cancelled", "deleted", "rescinded"}:
            self.db.enqueue_correction(award_id=evidence.award_id, reason=f"Contract status changed to {evidence.status}.", source_url=evidence.source_url, created_at=observed)
            return []
        results: list[GateResult] = []
        for signal in self.db.load_listing_signals():
            if signal.active and EntityResolver().resolve(evidence, signal).matched:
                results.append(await self._evaluate_async(evidence, signal))
        return results

    async def ingest_listing_async(self, signal: ListingSignal) -> list[GateResult]:
        observed = self.now()
        external_id = f"{signal.signal_id}:{signal.status}"
        inserted = self.db.insert_source_record(signal.source, external_id, signal.model_dump(mode="json"), observed_at=observed)
        if not inserted.inserted:
            return []
        if self.archive:
            self.archive.write(signal.source, external_id, signal.model_dump(mode="json"), observed_at=observed)
        self.db.store_listing_signal(signal, source_record_id=inserted.row_id, created_at=observed)
        if not signal.active or signal.status.lower() in {"withdrawn", "terminated", "abandoned", "rejected"}:
            self.db.enqueue_correction(signal_id=signal.related_signal_id or signal.signal_id, company_name=signal.issuer_name, reason=f"Listing signal changed to {signal.status}.", source_url=signal.source_url, created_at=observed)
            return []
        results: list[GateResult] = []
        for evidence in self.db.load_contracts():
            if not evidence.cancelled and not evidence.deleted and EntityResolver().resolve(evidence, signal).matched:
                results.append(await self._evaluate_async(evidence, signal))
        return results

    async def _evaluate_async(self, evidence: ContractEvidence, signal: ListingSignal) -> GateResult:
        market = None
        if signal.ticker and self.market_lookup:
            value = self.market_lookup(signal.ticker)
            market = await value if inspect.isawaitable(value) else value
        risks = (
            "Contract obligations may be lower than the potential ceiling and can be modified or terminated.",
            "Listing completion, financing, dilution, liquidity, and execution risks remain material.",
            *signal.risk_findings,
        )
        return self._gate().evaluate(Candidate(contract=evidence, listing=signal, market=market, risks=risks))

    def _evaluate(self, evidence: ContractEvidence, signal: ListingSignal) -> GateResult:
        market = self.market_lookup(signal.ticker) if signal.ticker and self.market_lookup else None
        risks = (
            "Contract obligations may be lower than the potential ceiling and can be modified or terminated.",
            "Listing completion, financing, dilution, liquidity, and execution risks remain material.",
            *signal.risk_findings,
        )
        candidate = Candidate(contract=evidence, listing=signal, market=market, risks=risks)
        return self._gate().evaluate(candidate)
