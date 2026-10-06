"""Bounded primary-SEC issuer discovery, separate from executable trade ideas.

Current 10-Q/10-K pages select issuers; standard entity-wide companyfacts supply
comparable reported quarters. Neither a ticker directory nor financial growth
establishes a trading currency, a security class, or a buying opportunity.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from ..fundamentals import normalize_companyfacts
from .discourse import SourceCoverage, plain_text
from .http import ResilientClient
from .sec import SECCollector

DIRECTORY_URL = "https://www.sec.gov/files/company_tickers_exchange.json"


@dataclass(frozen=True)
class ListedDiscoveryBatch:
    candidates: tuple[dict[str, Any], ...]
    coverage: tuple[SourceCoverage, ...]


def ticker_directory(data: Any) -> dict[str, tuple[dict[str, str], ...]]:
    """Validate SEC listing identities without guessing a venue or currency."""
    if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("data"), list):
        raise ValueError("SEC ticker directory requires fields and data arrays")
    fields = data["fields"]
    if len(fields) != len(set(fields)) or not {"cik", "name", "ticker", "exchange"}.issubset(fields) or len(data["data"]) > 25_000:
        raise ValueError("SEC ticker directory fields or row count are invalid")
    identities: dict[str, list[dict[str, str]]] = {}
    for row in data["data"]:
        if not isinstance(row, list) or len(row) != len(fields):
            raise ValueError("SEC ticker directory has malformed rows")
        value = dict(zip(fields, row))
        if not re.fullmatch(r"[1-9]\d{0,9}", str(value["cik"])):
            raise ValueError("SEC ticker directory has an invalid issuer identifier")
        symbol, venue = value["ticker"], value["exchange"]
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", symbol):
            continue
        # Directory entries without an exchange are not exchange identity proof.
        if venue not in {"Nasdaq", "NYSE", "Cboe", "NYSE American", "NYSE Arca"}:
            continue
        name = plain_text(str(value["name"]), limit=300)
        if not name:
            continue
        cik = str(value["cik"]).zfill(10)
        identity = {"symbol": symbol, "exchange": venue, "name": name, "source_url": DIRECTORY_URL}
        if identity not in identities.setdefault(cik, []):
            identities[cik].append(identity)
    return {cik: tuple(values) for cik, values in identities.items()}


class ListedDiscoveryCollector:
    def __init__(self, client: ResilientClient, *, max_new_ciks: int = 5, request_interval: float = .5):
        if not 1 <= max_new_ciks <= 5 or request_interval < .15:
            raise ValueError("Discovery permits 1-5 new issuer fact requests and paced SEC access")
        self.client = client
        self.max_new_ciks = max_new_ciks
        self.request_interval = request_interval
        self.sec = SECCollector(client, max_pages=1, request_interval=request_interval)
        self.partial_batch = ListedDiscoveryBatch((), ())

    async def collect(self, *, observed_at: datetime, processed_accessions: Iterable[str] = (),
                      excluded_ciks: Iterable[str] = (), latest_filed_at: dict[str, str] | None = None) -> ListedDiscoveryBatch:
        if observed_at.utcoffset() is None:
            raise ValueError("Discovery observation timestamp requires a timezone")
        observed_at = observed_at.astimezone(UTC)
        coverage, candidates, entries = [], [], []
        self.partial_batch = ListedDiscoveryBatch((), ())
        processed = set(processed_accessions)
        excluded = {str(int(value)).zfill(10) for value in excluded_ciks}
        latest_filed_at = latest_filed_at or {}
        limitations = ("One current SEC feed page per 10-Q/10-K form; no exhaustive equity or global-market scan.",
                       f"At most {self.max_new_ciks} issuer companyfacts requests in this collection, including rechecks of a missing comparable context.",
                       "Discovery is a research/watch finding. Listing currency, share class, price and company-news gates remain unverified.")
        try:
            try:
                directory_data = await self.client.request_json("GET", DIRECTORY_URL)
                identities = ticker_directory(directory_data)
                directory_hash = hashlib.sha256(json.dumps(directory_data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                coverage.append(SourceCoverage("listed_discovery:directory", DIRECTORY_URL, observed_at, "ok", len(identities), limitations=limitations))
            except Exception as exc:
                coverage.append(SourceCoverage("listed_discovery:directory", DIRECTORY_URL, observed_at, "error", error=f"{type(exc).__name__}: listing directory unavailable", limitations=limitations))
                return ListedDiscoveryBatch((), tuple(coverage))
            self.partial_batch = ListedDiscoveryBatch((), tuple(coverage))
            for form in ("10-Q", "10-K"):
                try:
                    values = await self.sec.current_entries(form, count=40)
                    entries.extend(values)
                    coverage.append(SourceCoverage("listed_discovery:" + form, SECCollector.CURRENT_URL, observed_at,
                                                   "ok", len(values), limitations=limitations + (f"Examined at most 40 current {form} filings; older pages were not downloaded.",)))
                except Exception as exc:
                    coverage.append(SourceCoverage("listed_discovery:" + form, SECCollector.CURRENT_URL, observed_at,
                                                   "error", error=f"{type(exc).__name__}: current filing page unavailable", limitations=limitations))
                self.partial_batch = ListedDiscoveryBatch(tuple(candidates), tuple(coverage))
            seen = set()
            selected = []
            for entry in sorted(entries, key=lambda item: item["filed_at"], reverse=True):
                cik, accession = entry.get("cik"), entry.get("accession")
                if (not cik or cik in excluded or cik in seen or accession in processed or cik not in identities
                        or entry.get("form_type") not in {"10-Q", "10-K"}
                        or not observed_at - timedelta(days=30) <= entry["filed_at"] <= observed_at):
                    continue
                if cik in latest_filed_at and entry["filed_at"] <= datetime.fromisoformat(latest_filed_at[cik]):
                    continue
                seen.add(cik)
                selected.append(entry)
                if len(selected) == self.max_new_ciks:
                    break
            for entry in selected:
                cik, accession = entry["cik"], entry["accession"]
                listing = identities[cik]
                # Multiple classes are retained explicitly; choosing one is review work.
                symbol = listing[0]["symbol"]
                facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
                await asyncio.sleep(self.request_interval)
                try:
                    raw = await self.client.request_json("GET", facts_url)
                    fact = normalize_companyfacts(raw, cik=cik, symbol=symbol, now=observed_at)
                    raw_hash = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                    eligible = bool(fact and fact.period_type == "quarter" and fact.accounting_standard == "US GAAP"
                                    and fact.is_fresh(observed_at) and fact.growth >= .10 and fact.net_income > 0)
                    reasons = []
                    if fact is None:
                        reasons.append("A complete comparable entity-wide SEC revenue/profit context is unavailable; custom tags and YTD differences are not inferred.")
                    else:
                        if fact.period_type != "quarter": reasons.append("A comparable fresh quarter is required; annual growth is not quarterly growth.")
                        if fact.accounting_standard != "US GAAP": reasons.append("This discovery policy requires reported US GAAP profit.")
                        if not fact.is_fresh(observed_at): reasons.append("Financial evidence is expired or its reporting period is too old.")
                        if fact.growth < .10: reasons.append("Comparable quarterly total revenue growth is below 10% year over year.")
                        if fact.net_income <= 0: reasons.append("Reported net income is not positive.")
                    if len(listing) != 1:
                        reasons.append("Multiple SEC-listed ticker classes require a specific security selection.")
                    reasons.append("WAIT: trading currency/share class, a completed price history, company-news evidence, valuation and trade gates require review; this is not a buy/sell instruction.")
                    # Preserve the exact original standard-tag contexts used in
                    # the comparison inside the portable checkpoint. This is
                    # a source subset, not an invented filing or API receipt.
                    context_receipt = None
                    if fact:
                        taxonomy = "us-gaap" if fact.accounting_standard == "US GAAP" else "ifrs-full"
                        tags = {fact.revenue_tag, fact.net_income_tag}
                        context_receipt = {"cik": raw["cik"], "entityName": raw.get("entityName"), "facts": {taxonomy: {}}}
                        for tag in tags:
                            concept = raw["facts"][taxonomy][tag]
                            units = {unit: [row for row in rows if row.get("accn") == fact.accession]
                                     for unit, rows in concept.get("units", {}).items()}
                            context_receipt["facts"][taxonomy][tag] = {"label": concept.get("label"), "units": units}
                    candidate = {"cik": cik, "accession": accession, "name": listing[0]["name"], "symbol": symbol,
                                 "exchange": listing[0]["exchange"], "listings": list(listing), "trading_currency": None,
                                 "reporting_currency": fact.currency if fact else None, "filing_form": entry["form_type"],
                                 "filed_at": entry["filed_at"].isoformat(), "discovered_at": observed_at.isoformat(),
                                 "filing_source_url": entry["source_url"], "listing_source_url": DIRECTORY_URL,
                                 "financials": fact.to_dict() if fact else None, "financial_eligible": eligible,
                                 "status": "qualified_review" if eligible else "wait" if fact is None else "rejected",
                                 "reasons": reasons, "facts_source_url": facts_url, "facts_sha256": raw_hash,
                                 "directory_sha256": directory_hash,
                                 "companyfacts_context_receipt": context_receipt,
                                 "raw_archive_status": "exact_source_context_subset_archived; full_API_body_hash_only" if context_receipt else "no_comparable_context; full_API_body_hash_only"}
                    candidates.append(candidate)
                    coverage.append(SourceCoverage("listed_discovery:companyfacts:" + cik, facts_url, observed_at, "ok", 1, limitations=limitations))
                except Exception as exc:
                    coverage.append(SourceCoverage("listed_discovery:companyfacts:" + cik, facts_url, observed_at, "error",
                                                   error=f"{type(exc).__name__}: issuer facts unavailable or failed identity/comparability validation", limitations=limitations))
                self.partial_batch = ListedDiscoveryBatch(tuple(candidates), tuple(coverage))
        except asyncio.CancelledError:
            coverage.append(SourceCoverage("listed_discovery:interrupted", SECCollector.CURRENT_URL, observed_at, "error",
                                           error="Collection deadline interrupted discovery; finished candidates remain saved", limitations=limitations))
            raise
        finally:
            self.partial_batch = ListedDiscoveryBatch(tuple(candidates), tuple(coverage))
        return self.partial_batch
