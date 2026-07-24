from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel

from .db import Database
from .emailer import EmailRenderer
from .entity import EntityResolver
from .models import Candidate, GateDecision, ValidationResult
from .validators import ContractValidator, ListingValidator, SmallCompanyValidator


class GateResult(BaseModel):
    alert_created: bool
    duplicate: bool = False
    fingerprint: str | None = None
    decisions: tuple[GateDecision, ...]


class AlertGate:
    def __init__(self, db: Database, *, now: datetime | None = None, renderer: EmailRenderer | None = None):
        self.db = db
        self.now = now or datetime.now(UTC)
        self.renderer = renderer or EmailRenderer()

    @staticmethod
    def fingerprint(candidate: Candidate) -> str:
        raw = "|".join([
            candidate.listing.issuer_name.strip().upper(),
            candidate.contract.award_id.strip().upper(),
            candidate.listing.signal_id.strip().upper(),
        ])
        return hashlib.sha256(raw.encode()).hexdigest()

    def evaluate(self, candidate: Candidate) -> GateResult:
        contract_result = ContractValidator().validate(candidate.contract)
        entity = EntityResolver().resolve(candidate.contract, candidate.listing)
        listing_result = ListingValidator(now=self.now).validate(candidate.listing)
        small_result = SmallCompanyValidator(now=self.now).validate(candidate.listing, candidate.market)
        decisions = (
            self._decision("contract", contract_result),
            GateDecision(gate="entity", passed=entity.matched, code=entity.method, reason=entity.explanation),
            self._decision("listing", listing_result),
            self._decision("small_company", small_result),
        )
        passed = all(d.passed for d in decisions)
        fingerprint = self.fingerprint(candidate) if passed else None
        payload = self.renderer.render(candidate, entity, fingerprint, validated_at=self.now) if passed and fingerprint else None
        created, duplicate = self.db.save_evaluation(candidate, decisions, payload, created_at=self.now)
        return GateResult(alert_created=created, duplicate=duplicate, fingerprint=fingerprint, decisions=decisions)

    @staticmethod
    def _decision(gate: str, result: ValidationResult) -> GateDecision:
        return GateDecision(gate=gate, passed=result.passed, code=result.code, reason=result.reason)
