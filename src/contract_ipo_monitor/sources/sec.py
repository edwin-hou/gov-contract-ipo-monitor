from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from ..models import ListingRoute, ListingSignal
from ..risk import RiskAnalyzer
from .http import ResilientClient


class SECNormalizer:
    INITIAL_RE = re.compile(
        r"\binitial public offering\b|\binitial offering\b|\bthis is our initial public offering\b",
        re.I,
    )
    NO_PUBLIC_MARKET_RE = re.compile(
        r"no (?:established |existing )?public (?:trading )?market .*?(?:common stock|ordinary shares|securities)|"
        r"prior to this offering[^.]{0,200}no public market",
        re.I | re.S,
    )
    PRIMARY_OFFERING_RE = re.compile(
        r"\bwe are offering\b|\bwe are selling\b|\bshares offered by (?:us|the company)\b|"
        r"\bcompany is offering\b",
        re.I,
    )
    RESALE_RE = re.compile(
        r"\bresale\b.*\bselling stockholders?\b|\bselling stockholders?\b.*\bresale\b",
        re.I | re.S,
    )
    PRICE_RANGE_RE = re.compile(
        r"(?:price|offering price)[^.]{0,120}?between\s+\$([0-9][0-9,]*(?:\.[0-9]+)?)\s+and\s+\$([0-9][0-9,]*(?:\.[0-9]+)?)",
        re.I | re.S,
    )
    SINGLE_PRICE_RE = re.compile(
        r"(?:initial public offering price|public offering price|assumed offering price|price to the public)[^$]{0,100}\$([0-9][0-9,]*(?:\.[0-9]+)?)",
        re.I | re.S,
    )
    OFFERING_SIZE_RE = re.compile(
        r"(?:maximum aggregate offering price|aggregate offering price|offering size)[^$]{0,120}\$([0-9][0-9,]*(?:\.[0-9]+)?)\s*(million|billion)?",
        re.I | re.S,
    )
    POST_OFFERING_SHARES_RE = re.compile(
        r"(?:shares of (?:our )?(?:common stock|ordinary shares) (?:to be )?outstanding (?:immediately )?after (?:this|the) offering|"
        r"outstanding immediately after (?:this|the) offering)[^0-9]{0,100}([0-9][0-9,]*)",
        re.I | re.S,
    )

    @staticmethod
    def _number(value: str) -> float:
        return float(value.replace(",", ""))

    @staticmethod
    def _scaled_money(value: str, scale: str | None) -> float:
        multiplier = {"million": 1_000_000, "billion": 1_000_000_000}.get((scale or "").lower(), 1)
        return SECNormalizer._number(value) * multiplier

    @staticmethod
    def _exchange(text: str) -> str | None:
        patterns = (
            ("NASDAQ CAPITAL MARKET", r"(?:list|listing|quoted|traded)[^.]{0,140}\bNasdaq Capital Market\b"),
            ("NASDAQ GLOBAL MARKET", r"(?:list|listing|quoted|traded)[^.]{0,140}\bNasdaq Global Market\b"),
            ("NASDAQ GLOBAL SELECT MARKET", r"(?:list|listing|quoted|traded)[^.]{0,140}\bNasdaq Global Select Market\b"),
            ("NASDAQ", r"(?:list|listing|quoted|traded)[^.]{0,140}\bNasdaq\b"),
            ("NYSE AMERICAN", r"(?:list|listing|quoted|traded)[^.]{0,140}\bNYSE American\b"),
            ("NYSE", r"(?:list|listing|quoted|traded)[^.]{0,140}\b(?:New York Stock Exchange|NYSE)\b"),
        )
        for label, pattern in patterns:
            if re.search(pattern, text, re.I | re.S):
                return label
        return None

    @staticmethod
    def _ticker(text: str) -> str | None:
        patterns = (
            r"under (?:the )?(?:symbol|ticker symbol|trading symbol)\s*[\"'‘’:]?\s*([A-Z]{1,6})\b",
            r"(?:symbol|ticker symbol|trading symbol)\s*[\"'‘’:]\s*([A-Z]{1,6})\b",
            r"(?:Nasdaq|NYSE(?: American)?)[^.]{0,120}?\b(?:symbol|ticker)\s*[\"'‘’:]?\s*([A-Z]{1,6})\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I | re.S)
            if match:
                return match.group(1).upper()
        return None

    def _price(self, text: str) -> float | None:
        match = self.PRICE_RANGE_RE.search(text)
        if match:
            return (self._number(match.group(1)) + self._number(match.group(2))) / 2
        match = self.SINGLE_PRICE_RE.search(text)
        return self._number(match.group(1)) if match else None

    def classify_document(
        self,
        *,
        form_type: str,
        accession: str,
        issuer_name: str,
        cik: str | None,
        filed_at: datetime,
        source_url: str,
        text: str,
    ) -> ListingSignal | None:
        form = form_type.upper().strip()
        raw_hash = hashlib.sha256(text.encode(errors="ignore")).hexdigest()
        lower = text.lower()
        if form in {"RW", "AW"} or "requests withdrawal" in lower or "request withdrawal" in lower:
            return ListingSignal(
                source="sec",
                source_url=source_url,
                signal_id=accession,
                issuer_name=issuer_name,
                cik=cik,
                filed_at=filed_at,
                active=False,
                status="withdrawn",
                route=ListingRoute.S1,
                form_type=form,
                raw_payload_hash=raw_hash,
            )

        exchange = self._exchange(text)
        price = self._price(text)
        ticker = self._ticker(text)

        offering_size = None
        size_match = self.OFFERING_SIZE_RE.search(text)
        if size_match:
            offering_size = self._scaled_money(size_match.group(1), size_match.group(2))

        proposed_valuation = None
        shares_match = self.POST_OFFERING_SHARES_RE.search(text)
        if shares_match and price is not None:
            shares = self._number(shares_match.group(1))
            if shares >= 1_000:
                proposed_valuation = shares * price

        risks = tuple(f"{finding.category}: {finding.finding}" for finding in RiskAnalyzer().from_filing_text(text))
        common = dict(
            source="sec",
            source_url=source_url,
            signal_id=accession,
            issuer_name=issuer_name,
            cik=cik,
            filed_at=filed_at,
            active=True,
            status="active",
            expected_exchange=exchange,
            proposed_price=price,
            proposed_valuation=proposed_valuation,
            max_offering_size=offering_size,
            ticker=ticker,
            external_corroboration=True,
            risk_findings=risks,
            raw_payload_hash=raw_hash,
        )

        if form in {"S-1", "S-1/A", "F-1", "F-1/A"}:
            primary = bool(self.PRIMARY_OFFERING_RE.search(text))
            initial = bool(self.INITIAL_RE.search(text)) or (primary and bool(self.NO_PUBLIC_MARKET_RE.search(text)))
            if self.RESALE_RE.search(text) and not primary:
                return None
            if not initial or not exchange:
                return None
            route = ListingRoute.F1 if form.startswith("F-1") else ListingRoute.S1
            return ListingSignal(
                **common,
                route=route,
                form_type=form,
                is_initial_listing=True,
                intends_public_trading=True,
            )

        if form in {"1-A", "1-A/A"}:
            intends = bool(exchange or re.search(r"quotation on|publicly traded|list our", text, re.I))
            if not intends:
                return None
            return ListingSignal(
                **common,
                route=ListingRoute.REG_A,
                form_type=form,
                is_initial_listing=True,
                intends_public_trading=True,
            )

        if form in {"8-K", "6-K"}:
            if re.search(r"definitive business combination agreement|entered into a business combination agreement", text, re.I):
                return ListingSignal(
                    **common,
                    route=ListingRoute.DESPAC,
                    form_type=form,
                    definitive_agreement=True,
                    intends_public_trading=True,
                )
            if re.search(r"definitive reverse merger|share exchange agreement", text, re.I):
                return ListingSignal(
                    **common,
                    route=ListingRoute.REVERSE_MERGER,
                    form_type=form,
                    definitive_agreement=True,
                    intends_public_trading=True,
                )
        return None

    def primary_document_url(self, index_url: str, index_html: str) -> str:
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', index_html, flags=re.I)
        candidates = []
        for href in hrefs:
            lower = href.lower()
            if not lower.endswith((".htm", ".html")):
                continue
            if "-index." in lower or "xsl" in lower or "ixviewer" in lower:
                continue
            candidates.append(urljoin(index_url, href))
        return candidates[0] if candidates else index_url

    def parse_atom(self, xml_text: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries: list[dict[str, Any]] = []
        for entry in root.findall("a:entry", ns):
            title = entry.findtext("a:title", default="", namespaces=ns)
            updated = entry.findtext("a:updated", default="", namespaces=ns)
            link = entry.find("a:link", ns)
            category = entry.find("a:category", ns)
            form = category.get("term") if category is not None else title.split(" - ", 1)[0]
            href = link.get("href") if link is not None else ""
            issuer = title.split(" - ", 1)[-1].rsplit(" (", 1)[0] if " - " in title else title
            cik_match = re.search(r"\((\d{10})\)", title)
            accession_match = re.search(r"(\d{10}-\d{2}-\d{6})", href)
            entries.append(
                {
                    "form_type": form,
                    "issuer_name": issuer,
                    "cik": cik_match.group(1) if cik_match else None,
                    "accession": accession_match.group(1) if accession_match else href.rstrip("/").split("/")[-1],
                    "filed_at": datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else datetime.now(UTC),
                    "source_url": href,
                }
            )
        return entries


class SECCollector:
    CURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

    def __init__(self, client: ResilientClient):
        self.client = client
        self.normalizer = SECNormalizer()

    async def current_entries(self, form_type: str, *, count: int = 100) -> list[dict[str, Any]]:
        xml = await self.client.request_text(
            "GET",
            self.CURRENT_URL,
            params={"action": "getcurrent", "type": form_type, "count": count, "output": "atom"},
        )
        return self.normalizer.parse_atom(xml)

    async def issuer_address(self, cik: str | None) -> str | None:
        if not cik:
            return None
        data = await self.client.request_json("GET", f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
        business = ((data or {}).get("addresses") or {}).get("business") or {}
        parts = [business.get("street1"), business.get("street2"), business.get("city"), business.get("stateOrCountry"), business.get("zipCode")]
        return ", ".join(str(part).strip() for part in parts if part) or None

    async def classify_entry(self, entry: dict[str, Any]) -> ListingSignal | None:
        index_html = await self.client.request_text("GET", entry["source_url"])
        document_url = self.normalizer.primary_document_url(entry["source_url"], index_html)
        text = index_html if document_url == entry["source_url"] else await self.client.request_text("GET", document_url)
        enriched = dict(entry)
        enriched["source_url"] = document_url
        signal = self.normalizer.classify_document(text=text, **enriched)
        if signal is not None and signal.issuer_address is None:
            try:
                address = await self.issuer_address(signal.cik)
            except Exception:
                address = None
            if address:
                signal = signal.model_copy(update={"issuer_address": address})
        return signal
