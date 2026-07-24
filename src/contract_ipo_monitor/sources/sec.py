from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from datetime import UTC, datetime
from typing import Any

from ..models import ListingRoute, ListingSignal
from ..risk import RiskAnalyzer
from .http import ResilientClient


class SECNormalizer:
    INITIAL_RE = re.compile(r"\binitial public offering\b|\binitial offering\b", re.I)
    RESALE_RE = re.compile(r"\bresale\b.*\bselling stockholders?\b|\bselling stockholders?\b.*\bresale\b", re.I | re.S)
    PRICE_RE = re.compile(r"between\s+\$([0-9]+(?:\.[0-9]+)?)\s+and\s+\$([0-9]+(?:\.[0-9]+)?)", re.I)

    def classify_document(self, *, form_type: str, accession: str, issuer_name: str, cik: str | None, filed_at: datetime, source_url: str, text: str) -> ListingSignal | None:
        form = form_type.upper().strip()
        raw_hash = hashlib.sha256(text.encode(errors="ignore")).hexdigest()
        if form in {"RW", "AW"} or "requests withdrawal" in text.lower() or "request withdrawal" in text.lower():
            return ListingSignal(
                source="sec", source_url=source_url, signal_id=accession, issuer_name=issuer_name, cik=cik,
                filed_at=filed_at, active=False, status="withdrawn", route=ListingRoute.S1,
                form_type=form, raw_payload_hash=raw_hash,
            )

        upper = text.upper()
        exchange = None
        for label, needles in {
            "NASDAQ": ("NASDAQ",),
            "NYSE AMERICAN": ("NYSE AMERICAN", "NYSE MKT"),
            "NYSE": ("NEW YORK STOCK EXCHANGE", " NYSE "),
        }.items():
            if any(needle in upper for needle in needles):
                exchange = label
                break
        price = None
        match = self.PRICE_RE.search(text)
        if match:
            price = (float(match.group(1)) + float(match.group(2))) / 2

        ticker_match = re.search(r"under (?:the )?(?:symbol|ticker symbol) [\"‘’']?([A-Z]{1,6})", text, re.I)
        ticker = ticker_match.group(1).upper() if ticker_match else None
        size_match = re.search(r"(?:maximum aggregate offering price|offering size)[^$]{0,80}\$([0-9]+(?:\.[0-9]+)?)\s*(million|billion)?", text, re.I)
        offering_size = None
        if size_match:
            offering_size = float(size_match.group(1)) * ({"million": 1_000_000, "billion": 1_000_000_000}.get((size_match.group(2) or "").lower(), 1))

        risks = tuple(f"{finding.category}: {finding.finding}" for finding in RiskAnalyzer().from_filing_text(text))
        common = dict(
            source="sec", source_url=source_url, signal_id=accession, issuer_name=issuer_name, cik=cik,
            filed_at=filed_at, active=True, status="active", expected_exchange=exchange,
            proposed_price=price, max_offering_size=offering_size, ticker=ticker, external_corroboration=True, risk_findings=risks, raw_payload_hash=raw_hash,
        )
        if form in {"S-1", "S-1/A", "F-1", "F-1/A"}:
            if self.RESALE_RE.search(text) and not self.INITIAL_RE.search(text):
                return None
            if not self.INITIAL_RE.search(text) or not exchange:
                return None
            route = ListingRoute.F1 if form.startswith("F-1") else ListingRoute.S1
            return ListingSignal(**common, route=route, form_type=form, is_initial_listing=True, intends_public_trading=True)
        if form in {"1-A", "1-A/A"}:
            intends = bool(exchange or re.search(r"quotation on|publicly traded|list our", text, re.I))
            if not intends:
                return None
            return ListingSignal(**common, route=ListingRoute.REG_A, form_type=form, is_initial_listing=True, intends_public_trading=True)
        if form in {"8-K", "6-K"}:
            if re.search(r"definitive business combination agreement|entered into a business combination agreement", text, re.I):
                return ListingSignal(**common, route=ListingRoute.DESPAC, form_type=form, definitive_agreement=True, intends_public_trading=True)
            if re.search(r"definitive reverse merger|share exchange agreement", text, re.I):
                return ListingSignal(**common, route=ListingRoute.REVERSE_MERGER, form_type=form, definitive_agreement=True, intends_public_trading=True)
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
            entries.append({
                "form_type": form,
                "issuer_name": issuer,
                "cik": cik_match.group(1) if cik_match else None,
                "accession": accession_match.group(1) if accession_match else href.rstrip("/").split("/")[-1],
                "filed_at": datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else datetime.now(UTC),
                "source_url": href,
            })
        return entries


class SECCollector:
    CURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

    def __init__(self, client: ResilientClient):
        self.client = client
        self.normalizer = SECNormalizer()

    async def current_entries(self, form_type: str, *, count: int = 100) -> list[dict[str, Any]]:
        xml = await self.client.request_text("GET", self.CURRENT_URL, params={"action": "getcurrent", "type": form_type, "count": count, "output": "atom"})
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
