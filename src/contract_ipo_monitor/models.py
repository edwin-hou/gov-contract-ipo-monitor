from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvidenceClass(StrEnum):
    A = "A"
    B = "B"


class ListingRoute(StrEnum):
    S1 = "s-1"
    F1 = "f-1"
    REG_A = "1-a"
    DESPAC = "de-spac"
    REVERSE_MERGER = "reverse-merger"
    OPTION_B = "option-b"


class ValidationResult(BaseModel):
    passed: bool
    code: str
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class ContractEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    source_url: str
    source_record_id: str
    retrieved_at: datetime
    published_at: datetime | None = None
    award_id: str
    modification_number: str = "0"
    transaction_id: str | None = None
    status: str
    award_date: date
    agency: str
    subagency: str | None = None
    office: str | None = None
    recipient_name: str
    recipient_uei: str | None = None
    recipient_cage: str | None = None
    recipient_address: str | None = None
    parent_uei: str | None = None
    prime: bool = True
    obligated_amount: float | None = None
    current_value: float | None = None
    ceiling_amount: float | None = None
    award_type: str
    pricing_type: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    option_years: int | None = None
    description: str
    evidence_class: EvidenceClass
    raw_payload_hash: str
    cancelled: bool = False
    deleted: bool = False


class ListingSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    source_url: str
    signal_id: str
    issuer_name: str
    issuer_address: str | None = None
    cik: str | None = None
    filed_at: datetime
    active: bool = True
    status: str = "active"
    route: ListingRoute
    form_type: str | None = None
    is_initial_listing: bool = False
    intends_public_trading: bool = False
    definitive_agreement: bool = False
    expected_exchange: str | None = None
    expected_window_end: date | None = None
    named_underwriter: str | None = None
    listing_application_announced: bool = False
    executed_listing_or_underwriting_agreement: bool = False
    ticker: str | None = None
    proposed_price: float | None = None
    proposed_valuation: float | None = None
    max_offering_size: float | None = None
    transaction_value: float | None = None
    linked_ueis: tuple[str, ...] = ()
    linked_cages: tuple[str, ...] = ()
    external_corroboration: bool = False
    relationship_verified: bool = False
    relationship_description: str | None = None
    related_signal_id: str | None = None
    risk_findings: tuple[str, ...] = ()
    raw_payload_hash: str | None = None


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    venue: str | None = None
    quote_at: datetime
    price: float
    market_cap: float | None = None
    shares_outstanding: float | None = None
    volume: float | None = None
    source: str
    delayed: bool = True


class EntityMatch(BaseModel):
    matched: bool
    method: str
    explanation: str


class Candidate(BaseModel):
    contract: ContractEvidence
    listing: ListingSignal
    market: MarketSnapshot | None = None
    annual_revenue: float | None = None
    risks: tuple[str, ...] = ()


class GateDecision(BaseModel):
    gate: str
    passed: bool
    code: str
    reason: str


class AlertPayload(BaseModel):
    fingerprint: str
    subject: str
    text_body: str
    html_body: str
    company_name: str
    award_id: str
    signal_id: str
