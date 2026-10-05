from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zlib
from contextlib import closing
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from ..archive import EvidenceArchive
from ..models import ListingRoute, ListingSignal
from ..risk import RiskAnalyzer
from ..tracking import IPOEvidence
from .http import ResilientClient


class _DocumentHTML(HTMLParser):
    """Extract rendered words and filing-index table rows without script content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.words: list[str] = []
        self.rows: list[list[dict[str, Any]]] = []
        self.links: list[str] = []
        self._skip = 0
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self._skip += 1
        if tag == "tr":
            self._row = []
        if tag in {"td", "th"} and self._row is not None:
            self._cell = {"words": [], "links": []}
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
            if self._cell is not None:
                self._cell["links"].append(attributes["href"])

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._cell["text"] = " ".join(self._cell.pop("words"))
            self._row.append(self._cell)
            self._cell = None
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._skip:
            return
        if data.strip():
            self.words.append(data.strip())
            if self._cell is not None:
                self._cell["words"].append(data.strip())


def document_text(raw: str) -> str:
    if not re.search(r"<(?:html|body|div|p|table|span)\b", raw, re.I):
        return re.sub(r"\s+", " ", raw).strip()
    parser = _DocumentHTML()
    parser.feed(raw)
    return re.sub(r"\s+", " ", " ".join(parser.words)).strip()


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
    REGISTRATION_FORMS = {"S-1", "S-1/A", "F-1", "F-1/A", "S-11", "S-11/A"}
    CURRENT_INITIAL_RE = re.compile(
        r"\bthis is (?:an?|our|the) initial public offering\b|"
        r"\b(?:we|the company) (?:are|is) (?:offering|selling)[^.]{0,250}\binitial public offering\b",
        re.I,
    )
    SOLE_RESALE_RE = re.compile(
        r"\b(?:solely|exclusively)[^.]{0,120}\bresale\b|"
        r"\bthis (?:prospectus|registration statement)[^.]{0,220}\bresale\b|"
        r"\b(?:we are not|we will not be) (?:offering|selling) (?:any )?(?:shares|securities)\b",
        re.I,
    )
    ALREADY_PUBLIC_RE = re.compile(
        r"\b(?:our|the company's) (?:common stock|ordinary shares|shares|units) (?:is|are) "
        r"(?:currently |already )?(?:listed|traded|quoted)\b|"
        r"\b(?:completed|consummated|closed) our initial public offering\b|"
        r"\bsince our initial public offering\b",
        re.I,
    )

    @staticmethod
    def registration_number(raw: str) -> str | None:
        # EFFECT uses XML. Other filings and index pages have labelled file numbers.
        patterns = (
            r"<fileNumber>\s*((?:333|33)-\d+(?:-\d+)?)\s*</fileNumber>",
            r"(?:registration|file)(?: statement)?(?: (?:no\.?|number))?[\s:#.]{0,15}((?:333|33)-\d+(?:-\d+)?)",
            r"(?:filenum|fileNumber)=((?:333|33)-\d+(?:-\d+)?)",
        )
        matches = {match for pattern in patterns for match in re.findall(pattern, raw, re.I)}
        return next(iter(matches)) if len(matches) == 1 else None

    @staticmethod
    def effectiveness_metadata(raw: str) -> dict[str, Any]:
        """Notice dates describe effectiveness, distinct from EDGAR posting time."""
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return {}
        if root.tag.rsplit("}", 1)[-1] != "edgarSubmission":
            return {}
        values = {element.tag.rsplit("}", 1)[-1]: (element.text or "").strip() for element in root.iter()}
        metadata: dict[str, Any] = {}
        if values.get("cik", "").isdigit():
            metadata["cik"] = values["cik"]
        if values.get("entityName"):
            metadata["issuer_name"] = values["entityName"]
        if values.get("form"):
            metadata["underlying_form"] = values["form"]
        if values.get("finalEffectivenessDispDate"):
            metadata["effective_date"] = date.fromisoformat(values["finalEffectivenessDispDate"])
        return metadata

    def offering_classification(self, text: str) -> tuple[bool, str, str]:
        cover = text[:30000]
        initial = self.CURRENT_INITIAL_RE.search(cover)
        primary = self.PRIMARY_OFFERING_RE.search(cover)
        # Explicit resale on the cover remains resale even if IPO history is discussed.
        if self.SOLE_RESALE_RE.search(cover):
            return False, "resale", "Cover describes a resale registration, not a new IPO."
        if self.ALREADY_PUBLIC_RE.search(cover) and not initial:
            return False, "follow_on", "Issuer is already publicly traded or describes a completed historical IPO."
        if initial:
            return True, "ipo", "Current offering is explicitly described as an initial public offering."
        if primary and self.NO_PUBLIC_MARKET_RE.search(cover):
            return True, "ipo", "Primary offering and no previous public trading market are stated on the filing cover."
        if self.RESALE_RE.search(cover) and not primary:
            return False, "resale", "Selling stockholders and resale language without a current issuer offering."
        return False, "unclassified", "Registration form alone does not establish an IPO; review the offering document."

    def tracking_document(
        self, *, form_type: str, accession: str, issuer_name: str, cik: str | None,
        filed_at: datetime, source_url: str, text: str, registration_id: str | None = None,
    ) -> IPOEvidence | None:
        form = form_type.upper().strip()
        clean = document_text(text)
        metadata = self.effectiveness_metadata(text) if form == "EFFECT" else {}
        issuer_name = metadata.get("issuer_name", issuer_name)
        cik = metadata.get("cik", cik)
        registration = registration_id or self.registration_number(text) or self.registration_number(clean[:30000])
        if form in self.REGISTRATION_FORMS or form == "424B4":
            is_ipo, kind, reason = self.offering_classification(clean)
            event_type = "prospectus" if form == "424B4" else "amendment" if form.endswith("/A") else "registration"
        elif form in {"EFFECT", "RW", "AW"}:
            event_type = {"EFFECT": "effective", "RW": "withdrawn", "AW": "amendment_withdrawn"}[form]
            is_ipo, kind = False, "lifecycle_notice"
            reason = "Lifecycle notice applies only to its referenced SEC registration; it does not identify an IPO by itself."
        else:
            return None
        return IPOEvidence(
            event_id=accession, issuer_name=issuer_name, cik=cik, source="sec", source_kind="regulatory",
            source_url=source_url, filed_at=filed_at, form_type=form, registration_id=registration,
            underlying_form=metadata.get("underlying_form"), effective_date=metadata.get("effective_date"),
            event_type=event_type, is_ipo=is_ipo, offering_kind=kind,
            ticker=self._ticker(clean[:30000]), exchange=self._exchange(clean[:30000]),
            proposed_price=self._price(clean[:30000]), evidence_excerpt=clean[:2500],
            classification_reason=reason, raw_payload_hash=hashlib.sha256(text.encode()).hexdigest(),
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
            r"under (?:the )?(?:symbol|ticker symbol|trading symbol)\s*[\"'‘’“”:]?\s*([A-Z]{1,6})\b",
            r"(?:symbol|ticker symbol|trading symbol)\s*[\"'‘’“”:]\s*([A-Z]{1,6})\b",
            r"(?:Nasdaq|NYSE(?: American)?)[^.]{0,120}?\b(?:symbol|ticker)\s*[\"'‘’“”:]?\s*([A-Z]{1,6})\b",
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
        registration_id: str | None = None,
    ) -> ListingSignal | None:
        form = form_type.upper().strip()
        raw_hash = hashlib.sha256(text.encode(errors="ignore")).hexdigest()
        registration_id = registration_id or self.registration_number(text)
        text = document_text(text)
        if form == "RW":
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
                registration_id=registration_id,
                raw_payload_hash=raw_hash,
            )
        if form == "AW":
            # AW withdraws an amendment, not the registration statement itself.
            return None

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
            registration_id=registration_id,
        )

        if form in {"S-1", "S-1/A", "F-1", "F-1/A"}:
            initial, _kind, _reason = self.offering_classification(text)
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

    def primary_document_url(self, index_url: str, index_html: str, form_type: str | None = None) -> str:
        parser = _DocumentHTML()
        parser.feed(index_html)

        def safe_document(href: str) -> str | None:
            joined = urljoin(index_url, href)
            parsed = urlparse(joined)
            if parsed.path.startswith("/ixviewer/") or parsed.path == "/ix":
                query = parse_qs(parsed.query)
                joined = urljoin(index_url, (query.get("doc") or [""])[0])
                parsed = urlparse(joined)
            if parsed.scheme != "https" or parsed.hostname not in {"sec.gov", "www.sec.gov"}:
                return None
            if not parsed.path.startswith("/Archives/edgar/data/"):
                return None
            if not parsed.path.lower().endswith((".htm", ".html", ".txt", ".xml")) or "-index." in parsed.path:
                return None
            # Fetch raw EFFECT XML instead of the XSL presentation wrapper.
            return re.sub(r"/xsl[^/]+/", "/", joined, flags=re.I)

        typed: list[tuple[int, str]] = []
        for row in parser.rows:
            if len(row) < 4 or not row[0]["text"].isdigit():
                continue
            filing_type = row[3]["text"].strip().upper()
            if form_type and filing_type != form_type.strip().upper():
                continue
            if filing_type.startswith("EX-") or filing_type in {"GRAPHIC", "XML", "ZIP"}:
                continue
            for href in row[2]["links"]:
                document = safe_document(href)
                if document:
                    typed.append((int(row[0]["text"]), document))
        if typed:
            return min(typed)[1]
        if parser.rows and form_type:
            return index_url
        for href in parser.links:
            document = safe_document(href)
            if document:
                return document
        return index_url

    def parse_atom(self, xml_text: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries: list[dict[str, Any]] = []
        for entry in root.findall("a:entry", ns):
            title = entry.findtext("a:title", default="", namespaces=ns)
            updated = entry.findtext("a:updated", default="", namespaces=ns)
            link = next((link for link in entry.findall("a:link", ns) if link.get("rel", "alternate") == "alternate"), None)
            category = entry.find("a:category", ns)
            form = category.get("term") if category is not None else title.split(" - ", 1)[0]
            href = link.get("href") if link is not None else ""
            issuer_part = title.split(" - ", 1)[-1] if " - " in title else title
            issuer_match = re.match(r"^(.*?)\s*\((\d{1,10})\)(?:\s+\([^)]*\))*\s*$", issuer_part)
            issuer = issuer_match.group(1).strip() if issuer_match else issuer_part.strip()
            accession_match = re.search(r"(\d{10}-\d{2}-\d{6})", href or entry.findtext("a:id", default="", namespaces=ns))
            if not href or not accession_match or not updated:
                raise ValueError("SEC Atom entry missing accession, document URL, or published timestamp")
            timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if timestamp.utcoffset() is None:
                raise ValueError("SEC Atom timestamp is missing timezone")
            entries.append(
                {
                    "form_type": form,
                    "issuer_name": issuer,
                    "cik": f"{int(issuer_match.group(2)):010d}" if issuer_match else None,
                    "accession": accession_match.group(1),
                    "filed_at": timestamp.astimezone(UTC),
                    "source_url": href,
                }
            )
        return entries


class SECCollector:
    CURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

    def __init__(self, client: ResilientClient, *, max_pages: int = 3, request_interval: float = 0.15, db=None, archive: EvidenceArchive | None = None, max_document_bytes: int = 20 * 1024 * 1024):
        if max_pages < 1 or request_interval < 0 or max_document_bytes < 1:
            raise ValueError("SEC pagination and request interval must be positive")
        self.client = client
        self.normalizer = SECNormalizer()
        self.max_pages = max_pages
        self.request_interval = request_interval
        self.last_feed_truncated = False
        self.db = db
        self.archive = archive
        self.max_document_bytes = max_document_bytes
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0
        self._addresses: dict[str, str | None] = {}
        if db is not None:
            with closing(db.connect()) as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS sec_processed_filings(
                     accession TEXT PRIMARY KEY, processed_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS sec_raw_documents(
                     sha256 TEXT PRIMARY KEY, source_url TEXT NOT NULL, gzip_blob BLOB NOT NULL,
                     original_bytes INTEGER NOT NULL, archived_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS sec_raw_filing_archives(
                     accession TEXT NOT NULL, document_sha256 TEXT NOT NULL REFERENCES sec_raw_documents(sha256),
                     index_sha256 TEXT NOT NULL REFERENCES sec_raw_documents(sha256),
                     document_url TEXT NOT NULL, index_url TEXT NOT NULL, archived_at TEXT NOT NULL,
                     PRIMARY KEY(accession,document_sha256,index_sha256));
                """)

    async def _text(self, url: str, **kwargs) -> str:
        await self._throttle()
        text = await self.client.request_text("GET", url, **kwargs)
        if len(text.encode("utf-8")) > self.max_document_bytes:
            raise ValueError("SEC source document exceeds the configured byte limit")
        return text

    def _archive_documents(self, *, accession: str, index_url: str, index_html: str, document_url: str, document: str) -> str | None:
        """Raw evidence travels with the SQLite checkpoint on hosted runners."""
        timestamp = datetime.now(UTC)
        reference = None
        if self.db is not None:
            documents = []
            for source_url, raw in ((document_url, document), (index_url, index_html)):
                content = raw.encode("utf-8")
                if len(content) > self.max_document_bytes:
                    raise ValueError("SEC source document exceeds the configured byte limit")
                digest = hashlib.sha256(content).hexdigest()
                documents.append((digest, source_url, gzip.compress(content, compresslevel=6, mtime=0), len(content), timestamp.isoformat()))
            document_digest, index_digest = documents[0][0], documents[1][0]
            with self.db.transaction() as conn:
                conn.executemany(
                    """INSERT INTO sec_raw_documents(sha256,source_url,gzip_blob,original_bytes,archived_at) VALUES(?,?,?,?,?)
                       ON CONFLICT(sha256) DO UPDATE SET gzip_blob=excluded.gzip_blob,original_bytes=excluded.original_bytes""",
                    documents,
                )
                conn.execute(
                    """INSERT OR IGNORE INTO sec_raw_filing_archives
                       (accession,document_sha256,index_sha256,document_url,index_url,archived_at) VALUES(?,?,?,?,?,?)""",
                    (accession, document_digest, index_digest, document_url, index_url, timestamp.isoformat()),
                )
            reference = f"sqlite:sec_raw_documents/{document_digest}"
        if self.archive is not None:
            archive_path = self.archive.write(
                "sec-raw", accession,
                {"index_url": index_url, "document_url": document_url, "index_html": index_html, "document": document},
                observed_at=timestamp,
            )
            if reference is None:
                reference = str(archive_path)
        return reference

    async def _throttle(self) -> None:
        async with self._request_lock:
            delay = self.request_interval - (monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = monotonic()

    def is_processed(self, accession: str) -> bool:
        if self.db is None:
            return False
        with closing(self.db.connect()) as conn:
            if conn.execute("SELECT 1 FROM sec_processed_filings WHERE accession=?", (accession,)).fetchone() is None:
                return False
            # Receipts created before raw archiving was added need a one-time replay.
            # The latest version supersedes older missing references after recovery.
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ipo_evidence'").fetchone():
                row = conn.execute(
                    "SELECT evidence_json FROM ipo_evidence WHERE source='sec' AND event_id=? ORDER BY id DESC LIMIT 1",
                    (accession,),
                ).fetchone()
                if row:
                    try:
                        evidence = json.loads(row["evidence_json"])
                    except (ValueError, TypeError):
                        return False
                    return self._has_durable_archive(conn, accession, evidence)
            # Alternate listing routes (e.g. an 8-K business combination) can have
            # a legacy signal without an IPO event. Ordinary ignored 8-Ks do not.
            row = conn.execute(
                "SELECT version_json FROM listing_signals WHERE signal_id=? ORDER BY id DESC LIMIT 1", (accession,),
            ).fetchone()
            if row:
                try:
                    evidence = json.loads(row["version_json"])
                except (ValueError, TypeError):
                    return False
                if evidence.get("source") == "sec":
                    return self._has_durable_archive(conn, accession, evidence)
            return True

    def _has_durable_archive(self, conn, accession: str, evidence: dict[str, Any]) -> bool:
        reference = evidence.get("raw_archive_path")
        expected_digest = evidence.get("raw_payload_hash")
        # The source hash ties archive bytes to this precise classification version.
        if (not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
                or (reference is not None and not isinstance(reference, str))):
            return False
        if reference and not reference.startswith("sqlite:"):
            # Only an explicitly configured filesystem backend can satisfy a local
            # archive receipt. Hosted default storage must travel inside SQLite.
            if self.archive is None:
                return False
            try:
                path = Path(reference).resolve()
                if not path.is_relative_to(self.archive.root.resolve()) or path.stat().st_size > 12 * self.max_document_bytes + 10000:
                    return False
                archived = json.loads(path.read_text(encoding="utf-8"))
                payload = archived["payload"]
                document = payload["document"].encode("utf-8")
                index = payload["index_html"].encode("utf-8")
                return (
                    archived.get("external_id") == accession and archived.get("source") == "sec-raw"
                    and payload.get("document_url") == evidence.get("source_url")
                    and bool(payload.get("index_url"))
                    and len(document) <= self.max_document_bytes and len(index) <= self.max_document_bytes
                    and hashlib.sha256(document).hexdigest() == expected_digest
                )
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                return False
        if reference and reference != f"sqlite:sec_raw_documents/{expected_digest}":
            return False
        manifest = conn.execute(
            """SELECT document_sha256,index_sha256 FROM sec_raw_filing_archives
               WHERE accession=? AND document_sha256=? AND document_url=? LIMIT 1""",
            (accession, expected_digest, evidence.get("source_url")),
        ).fetchone()
        if manifest is None:
            return False
        for digest in {manifest["document_sha256"], manifest["index_sha256"]}:
            row = conn.execute("SELECT gzip_blob,original_bytes FROM sec_raw_documents WHERE sha256=?", (digest,)).fetchone()
            if row is None or not 0 <= row["original_bytes"] <= self.max_document_bytes:
                return False
            try:
                decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                content = decompressor.decompress(row["gzip_blob"], self.max_document_bytes + 1)
                if (not decompressor.eof or decompressor.unconsumed_tail or decompressor.unused_data
                        or len(content) != row["original_bytes"] or hashlib.sha256(content).hexdigest() != digest):
                    return False
            except (zlib.error, TypeError):
                return False
        return True

    def mark_processed(self, accession: str, *, observed_at: datetime) -> None:
        if self.db is None:
            return
        with closing(self.db.connect()) as conn:
            conn.execute("INSERT OR IGNORE INTO sec_processed_filings(accession,processed_at) VALUES(?,?)", (accession, observed_at.isoformat()))

    async def current_entries(self, form_type: str, *, count: int = 100) -> list[dict[str, Any]]:
        if count < 1 or count > 100:
            raise ValueError("SEC Atom page size must be between 1 and 100")
        self.last_feed_truncated = False
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(self.max_pages):
            xml = await self._text(
                self.CURRENT_URL,
                params={"action": "getcurrent", "type": form_type, "count": count, "start": page * count, "output": "atom", "owner": "exclude"},
            )
            parsed = self.normalizer.parse_atom(xml)
            new = [entry for entry in parsed if entry["accession"] not in seen]
            matching = [entry for entry in parsed if entry["form_type"].strip().upper() == form_type.strip().upper()]
            for entry in new:
                seen.add(entry["accession"])
                if entry["form_type"].strip().upper() == form_type.strip().upper():
                    entries.append(entry)
            if matching and all(self.is_processed(entry["accession"]) for entry in matching):
                break
            if len(parsed) < count:
                break
            if not new:
                # The upstream may ignore the requested pagination offset.
                self.last_feed_truncated = any(not self.is_processed(entry["accession"]) for entry in matching)
                break
            if page == self.max_pages - 1:
                self.last_feed_truncated = any(not self.is_processed(entry["accession"]) for entry in matching)
        return entries

    async def issuer_address(self, cik: str | None) -> str | None:
        if not cik:
            return None
        if cik in self._addresses:
            return self._addresses[cik]
        await self._throttle()
        data = await self.client.request_json("GET", f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
        business = ((data or {}).get("addresses") or {}).get("business") or {}
        parts = [business.get("street1"), business.get("street2"), business.get("city"), business.get("stateOrCountry"), business.get("zipCode")]
        address = ", ".join(str(part).strip() for part in parts if part) or None
        self._addresses[cik] = address
        return address

    async def classify_entry(self, entry: dict[str, Any]) -> ListingSignal | None:
        _event, signal = await self.collect_entry(entry)
        return signal

    async def collect_entry(self, entry: dict[str, Any]) -> tuple[IPOEvidence | None, ListingSignal | None]:
        parsed_url = urlparse(entry["source_url"])
        if (parsed_url.scheme != "https" or parsed_url.hostname not in {"sec.gov", "www.sec.gov"}
                or not parsed_url.path.startswith("/Archives/edgar/data/") or parsed_url.username is not None):
            raise ValueError("SEC entry does not reference an official EDGAR archive document")
        index_html = await self._text(entry["source_url"])
        document_url = self.normalizer.primary_document_url(entry["source_url"], index_html, entry["form_type"])
        if document_url == entry["source_url"] and "-index." in document_url:
            raise ValueError("No primary filing document found in SEC filing index")
        text = index_html if document_url == entry["source_url"] else await self._text(document_url)
        enriched = {key: entry[key] for key in ("form_type", "accession", "issuer_name", "cik", "filed_at")}
        enriched["source_url"] = document_url
        enriched["registration_id"] = entry.get("registration_id") or self.normalizer.registration_number(text) or self.normalizer.registration_number(index_html)
        event = self.normalizer.tracking_document(text=text, **enriched)
        signal = self.normalizer.classify_document(text=text, **enriched)
        if event is not None or signal is not None:
            reference = self._archive_documents(
                accession=entry["accession"], index_url=entry["source_url"], index_html=index_html,
                document_url=document_url, document=text,
            )
            if event is not None and reference is not None:
                event = event.model_copy(update={"raw_archive_path": reference})
        if signal is not None and signal.issuer_address is None:
            try:
                address = await self.issuer_address(signal.cik)
            except Exception:
                address = None
            if address:
                signal = signal.model_copy(update={"issuer_address": address})
        return event, signal
