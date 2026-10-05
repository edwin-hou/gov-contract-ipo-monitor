from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable
import inspect

from .archive import EvidenceArchive
from .db import Database
from .entity import EntityResolver
from .gate import AlertGate, GateResult
from .models import Candidate, ContractEvidence, ListingSignal, MarketSnapshot


def _stable_contract_payload(evidence: ContractEvidence) -> dict:
    """Return the source-content identity without observation timestamps.

    `retrieved_at`/`published_at` may be synthesized at collection time. The raw payload hash
    and normalized substantive fields are what determine whether a source record changed.
    """
    payload = evidence.model_dump(mode="json")
    payload.pop("retrieved_at", None)
    payload.pop("published_at", None)
    return payload


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

    def _listing_correction(self, signal: ListingSignal, observed: datetime) -> None:
        targets = {signal.related_signal_id or signal.signal_id}
        for previous in self.db.all_listing_signals():
            if self.db.listing_is_superseded(previous, signal):
                targets.add(previous.signal_id)
        for signal_id in targets:
            self.db.enqueue_correction(signal_id=signal_id, reason=f"Listing signal changed to {signal.status}.", source_url=signal.source_url, created_at=observed)

    def _contract_correction(self, evidence: ContractEvidence, observed: datetime) -> None:
        deleted = evidence.deleted or evidence.status.strip().lower() == "deleted"
        self.db.enqueue_correction(
            award_id=evidence.award_id,
            contract_record_id=evidence.source_record_id if deleted else None,
            contract_source=evidence.source if deleted else None,
            reason=f"Contract {'transaction' if deleted else 'status'} changed to {evidence.status}.",
            source_url=evidence.source_url, created_at=observed,
        )

    def ingest_contract(self, evidence: ContractEvidence) -> list[GateResult]:
        observed = self.now()
        source_payload = _stable_contract_payload(evidence)
        inserted = self.db.insert_source_record(evidence.source, evidence.source_record_id, source_payload, observed_at=observed)
        needs_storage = inserted.inserted or not self.db.evidence_record_exists("contract_evidence", inserted.row_id)
        if needs_storage and self.archive:
            self.archive.write(evidence.source, evidence.source_record_id, source_payload, observed_at=observed)
        if needs_storage:
            self.db.store_contract_evidence(evidence, source_record_id=inserted.row_id, created_at=observed)
        if evidence.cancelled or evidence.deleted or evidence.status.lower() in {"cancelled", "deleted", "rescinded"}:
            self._contract_correction(evidence, observed)
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
        needs_storage = inserted.inserted or not self.db.evidence_record_exists("listing_signals", inserted.row_id)
        if needs_storage and self.archive:
            self.archive.write(signal.source, external_id, signal.model_dump(mode="json"), observed_at=observed)
        if needs_storage:
            self.db.store_listing_signal(signal, source_record_id=inserted.row_id, created_at=observed)
        if not signal.active or signal.status.lower() in {"withdrawn", "terminated", "abandoned", "rejected"}:
            self._listing_correction(signal, observed)
            return []
        if not any(item.signal_id == signal.signal_id for item in self.db.load_listing_signals()):
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
        source_payload = _stable_contract_payload(evidence)
        inserted = self.db.insert_source_record(evidence.source, evidence.source_record_id, source_payload, observed_at=observed)
        needs_storage = inserted.inserted or not self.db.evidence_record_exists("contract_evidence", inserted.row_id)
        if needs_storage and self.archive:
            self.archive.write(evidence.source, evidence.source_record_id, source_payload, observed_at=observed)
        if needs_storage:
            self.db.store_contract_evidence(evidence, source_record_id=inserted.row_id, created_at=observed)
        if evidence.cancelled or evidence.deleted or evidence.status.lower() in {"cancelled", "deleted", "rescinded"}:
            self._contract_correction(evidence, observed)
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
        needs_storage = inserted.inserted or not self.db.evidence_record_exists("listing_signals", inserted.row_id)
        if needs_storage and self.archive:
            self.archive.write(signal.source, external_id, signal.model_dump(mode="json"), observed_at=observed)
        if needs_storage:
            self.db.store_listing_signal(signal, source_record_id=inserted.row_id, created_at=observed)
        if not signal.active or signal.status.lower() in {"withdrawn", "terminated", "abandoned", "rejected"}:
            self._listing_correction(signal, observed)
            return []
        if not any(item.signal_id == signal.signal_id for item in self.db.load_listing_signals()):
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
        if inspect.isawaitable(market):
            if inspect.iscoroutine(market):
                market.close()
            raise TypeError("asynchronous market lookup requires ingest_contract_async or ingest_listing_async")
        risks = (
            "Contract obligations may be lower than the potential ceiling and can be modified or terminated.",
            "Listing completion, financing, dilution, liquidity, and execution risks remain material.",
            *signal.risk_findings,
        )
        candidate = Candidate(contract=evidence, listing=signal, market=market, risks=risks)
        return self._gate().evaluate(candidate)
