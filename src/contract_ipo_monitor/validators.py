from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from .models import ContractEvidence, ListingRoute, ListingSignal, MarketSnapshot, ValidationResult


class ContractValidator:
    REJECTED_STATUSES = {"solicitation", "presolicitation", "sources_sought", "rfi", "rfq", "rfp", "cancelled", "rescinded", "deleted"}
    REJECTED_TYPES = {"grant", "loan", "subsidy", "vendor_registration", "forecast"}

    def validate(self, evidence: ContractEvidence) -> ValidationResult:
        status = evidence.status.strip().lower().replace(" ", "_")
        award_type = evidence.award_type.strip().lower().replace(" ", "_")
        if evidence.cancelled or evidence.deleted or status in self.REJECTED_STATUSES:
            return ValidationResult(passed=False, code="not_an_active_award", reason=f"Record is a {status or 'cancelled'} rather than an active award.")
        if award_type in self.REJECTED_TYPES:
            return ValidationResult(passed=False, code="excluded_award_type", reason=f"{award_type} is outside the contract-only scope.")
        if not evidence.award_id or not evidence.recipient_name or not evidence.agency:
            return ValidationResult(passed=False, code="missing_contract_identity", reason="Official award identifier, recipient, and agency are required.")
        if evidence.evidence_class.value == "B":
            host = (urlparse(evidence.source_url).hostname or "").lower()
            if not evidence.source_url.lower().startswith("https://") or not (host.endswith(".gov") or host.endswith(".us")):
                return ValidationResult(passed=False, code="weak_documentary_source", reason="Class B evidence must be hosted on an official HTTPS government domain.")
        if "idv" in award_type and (evidence.obligated_amount or 0) <= 0 and (evidence.current_value or 0) <= 0:
            return ValidationResult(passed=False, code="unfunded_idv", reason="An IDV requires a funded task/order before alerting.")
        if not evidence.prime:
            return ValidationResult(passed=False, code="unconfirmed_subcontract", reason="Subcontract claims require separate official confirmation.")
        return ValidationResult(
            passed=True,
            code="actual_contract_confirmed",
            reason=f"Official Class {evidence.evidence_class.value} evidence confirms an awarded contract.",
            details={"obligated_amount": evidence.obligated_amount, "ceiling_amount": evidence.ceiling_amount},
        )


class ListingValidator:
    def __init__(self, *, now: datetime | None = None):
        self.now = now or datetime.now(UTC)

    def validate(self, signal: ListingSignal) -> ValidationResult:
        if not signal.active or signal.status.lower() in {"withdrawn", "terminated", "abandoned", "rejected", "closed"}:
            return ValidationResult(passed=False, code="inactive_listing_signal", reason=f"Listing signal is {signal.status}.")
        if self.now - signal.filed_at > timedelta(days=365):
            return ValidationResult(passed=False, code="stale_listing_signal", reason="Listing signal has had no confirming event for more than 365 days.")

        if signal.route in {ListingRoute.S1, ListingRoute.F1}:
            if not signal.is_initial_listing or not signal.intends_public_trading:
                return ValidationResult(passed=False, code="not_initial_listing", reason="Registration does not establish an initial public listing.")
            return ValidationResult(passed=True, code="primary_sec_registration", reason="Active initial-listing registration statement.")

        if signal.route == ListingRoute.REG_A:
            if signal.form_type != "1-A" or not signal.intends_public_trading:
                return ValidationResult(passed=False, code="reg_a_not_public_listing", reason="Form 1-A does not state an intended publicly traded security.")
            return ValidationResult(passed=True, code="primary_reg_a", reason="Active Regulation A public-listing offering statement.")

        if signal.route in {ListingRoute.DESPAC, ListingRoute.REVERSE_MERGER}:
            if not signal.definitive_agreement:
                return ValidationResult(passed=False, code="non_definitive_merger", reason="Merger route lacks a definitive executed agreement.")
            return ValidationResult(passed=True, code="definitive_public_transaction", reason="Active definitive public-market transaction agreement.")

        if signal.route == ListingRoute.OPTION_B:
            factors = [
                bool(signal.named_underwriter),
                bool(signal.expected_exchange),
                bool(signal.listing_application_announced),
                bool(signal.expected_window_end and signal.expected_window_end <= (self.now + timedelta(days=180)).date()),
                bool(signal.executed_listing_or_underwriting_agreement),
            ]
            if sum(factors) < 2:
                return ValidationResult(passed=False, code="insufficient_option_b_factors", reason="Option B requires at least two documented listing factors.")
            if not signal.external_corroboration:
                return ValidationResult(passed=False, code="issuer_only_option_b", reason="Option B requires at least one non-issuer official source.")
            return ValidationResult(passed=True, code="high_confidence_option_b", reason="At least two high-confidence factors include independent corroboration.")

        return ValidationResult(passed=False, code="unsupported_listing_route", reason="Listing route is not supported.")


class SmallCompanyValidator:
    def __init__(self, *, now: datetime | None = None, max_price: float = 5.0, max_market_cap: float = 300_000_000, max_quote_age: timedelta = timedelta(days=1)):
        self.now = now or datetime.now(UTC)
        self.max_price = max_price
        self.max_market_cap = max_market_cap
        self.max_quote_age = max_quote_age

    def validate(self, signal: ListingSignal, market: MarketSnapshot | None) -> ValidationResult:
        if signal.ticker:
            if market is None:
                return ValidationResult(passed=False, code="missing_market_data", reason="Public candidate requires lawful current price and market-cap evidence.")
            if self.now - market.quote_at > self.max_quote_age:
                return ValidationResult(passed=False, code="stale_market_data", reason="Market quote is older than the configured maximum staleness.")
            if market.price >= self.max_price:
                return ValidationResult(passed=False, code="price_too_high", reason=f"Share price ${market.price:.2f} is not below ${self.max_price:.2f}.")
            if market.market_cap is None or market.market_cap >= self.max_market_cap:
                return ValidationResult(
                    passed=False,
                    code="market_cap_too_high_or_missing",
                    reason=f"Market capitalization is missing or not below ${self.max_market_cap:,.0f}.",
                )
            return ValidationResult(passed=True, code="public_small_company", reason="Fresh quote is below both configured price and market-cap thresholds.")

        proxy_name: str | None = None
        proxy_value: float | None = None
        if signal.proposed_valuation is not None:
            proxy_name, proxy_value = "proposed valuation", signal.proposed_valuation
        elif signal.transaction_value is not None:
            proxy_name, proxy_value = "transaction value", signal.transaction_value
        elif signal.max_offering_size is not None:
            proxy_name, proxy_value = "maximum offering size", signal.max_offering_size

        if proxy_value is None:
            return ValidationResult(passed=False, code="missing_private_valuation", reason="Private candidate lacks a primary-source valuation, transaction value, or offering-size proxy.")
        if proxy_value >= self.max_market_cap:
            return ValidationResult(
                passed=False,
                code="private_valuation_too_high",
                reason=f"Primary-source {proxy_name} of ${proxy_value:,.0f} is not below ${self.max_market_cap:,.0f}.",
            )
        if signal.proposed_price is not None and signal.proposed_price >= self.max_price:
            return ValidationResult(
                passed=False,
                code="proposed_price_too_high",
                reason=f"Disclosed proposed share price ${signal.proposed_price:.2f} is not below ${self.max_price:.2f}.",
            )
        return ValidationResult(
            passed=True,
            code="private_small_company",
            reason=f"Primary filing {proxy_name} is below the configured ${self.max_market_cap:,.0f} threshold.",
            details={"proxy_name": proxy_name, "proxy_value": proxy_value},
        )
