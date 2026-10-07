"""Durable discovery from official SEC daily indexes and exact Atom entries.

Catalogue discovery is independent of document processing. A bounded poll may
leave a visible backlog, but never forgets a validated filing or invents a
complete day from a missing URL. The checkpoint contains the original indexes.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import zlib
from datetime import UTC, date, datetime, timedelta
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from ..db import Database

_NEW_YORK = ZoneInfo("America/New_York")
_MAX_INDEX_BYTES = 20 * 1024 * 1024
_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}\Z")
_MASTER_NAME = re.compile(r"master\.(\d{8})\.idx\Z")
_FORM = re.compile(r"[A-Z0-9][A-Z0-9 /-]{0,39}\Z")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sec_catalog_state(
 id INTEGER PRIMARY KEY CHECK(id=1), scope_start TEXT NOT NULL,
 captured_through TEXT, published_through TEXT, listed_through TEXT, scan_from TEXT,
 last_sync_at TEXT, last_error TEXT, enabled_forms_json TEXT);
CREATE TABLE IF NOT EXISTS sec_index_days(
 day TEXT PRIMARY KEY, source_url TEXT NOT NULL, sha256 TEXT NOT NULL,
 gzip_blob BLOB NOT NULL, original_bytes INTEGER NOT NULL,
 entry_count INTEGER NOT NULL, captured_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sec_pending_filings(
 accession TEXT PRIMARY KEY, form TEXT NOT NULL, entry_json TEXT NOT NULL,
 discovered_at TEXT NOT NULL, processed_at TEXT);
CREATE INDEX IF NOT EXISTS sec_pending_form
 ON sec_pending_filings(form,processed_at,discovered_at,accession);
"""


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("SEC catalogue requires an aware observation timestamp")
    return value.astimezone(UTC)


def _quarter(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3 + 1


def _next_quarter(value: tuple[int, int]) -> tuple[int, int]:
    year, quarter = value
    return (year + 1, 1) if quarter == 4 else (year, quarter + 1)


def _index_url(day: date) -> str:
    year, quarter = _quarter(day)
    return f"https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/master.{day:%Y%m%d}.idx"


def _directory_index_days(listing: Any, *, quarter: tuple[int, int]) -> list[date]:
    """Adapt SEC directory identities while validating the complete listing.

    SEC daily-index JSON names its directory relative to Archives/edgar,
    e.g. daily-index/2026/QTR4/. Also accept the equivalent absolute archive
    path; neither variant can change the requested year or quarter. Download
    URLs are built from validated filenames, never directory href values.
    """
    year, number = quarter
    relative_path = f"daily-index/{year}/QTR{number}"
    directory_path = f"/Archives/edgar/{relative_path}"
    directory = listing.get("directory") if isinstance(listing, dict) else None
    if not isinstance(directory, dict):
        raise ValueError("SEC daily-index directory listing has an unexpected shape")
    name = directory.get("name")
    if (not isinstance(name, str) or name.removesuffix("/") not in {relative_path, directory_path}
            or not isinstance(directory.get("item"), list)):
        raise ValueError("SEC daily-index directory listing has an unexpected shape")
    seen = set()
    days = []
    for item in directory["item"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("SEC daily-index directory has an invalid item")
        filename = item["name"]
        if filename in seen:
            raise ValueError("SEC daily-index directory has duplicate filenames")
        seen.add(filename)
        match = _MASTER_NAME.fullmatch(filename)
        if not match:
            if filename.startswith("master.") and filename.endswith(".idx"):
                raise ValueError("SEC daily-index directory has a malformed master filename")
            continue
        day = datetime.strptime(match[1], "%Y%m%d").date()
        if _quarter(day) != quarter or item.get("type", "file") != "file":
            raise ValueError("SEC daily index is outside its declared quarter")
        if "href" in item and item["href"] != filename:
            raise ValueError("SEC daily index href and filename disagree")
        days.append(day)
    return days


def _forms(values: Iterable[str]) -> set[str]:
    result = {str(value).strip().upper() for value in values}
    if not result or any(not _FORM.fullmatch(value) for value in result):
        raise ValueError("SEC catalogue requires valid enabled form types")
    return result


def _entry(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Invalid SEC catalogue entry")
    accession = value.get("accession")
    cik = str(value.get("cik", ""))
    form = str(value.get("form_type", "")).strip().upper()
    issuer = value.get("issuer_name")
    if (not isinstance(accession, str) or not _ACCESSION.fullmatch(accession)
            or not re.fullmatch(r"\d{1,10}", cik) or int(cik) <= 0
            or not _FORM.fullmatch(form) or not isinstance(issuer, str) or not issuer.strip()):
        raise ValueError("Invalid SEC filing accession, issuer, form, or CIK")
    stamp = value.get("filed_at")
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    stamp = _aware(stamp)
    precision = value.get("filed_at_precision", "second")
    if precision not in {"date", "second"}:
        raise ValueError("Invalid SEC filing date precision")
    if precision == "date" and stamp.time() != datetime.min.time():
        raise ValueError("Date-only SEC filing must use a midnight placeholder")
    parsed = urlparse(str(value.get("source_url", "")))
    prefix = f"/Archives/edgar/data/{int(cik)}/"
    suffix = f"{accession}-index."
    if (parsed.scheme != "https" or parsed.hostname not in {"www.sec.gov", "sec.gov"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or not parsed.path.startswith(prefix)
            or parsed.path.rsplit("/", 1)[-1] not in {suffix + "htm", suffix + "html"}
            or ".." in parsed.path.split("/")):
        raise ValueError("SEC catalogue entry must reference its official filing index")
    relative = parsed.path.removeprefix(prefix)
    middle = relative.rsplit("/", 1)[0] if "/" in relative else ""
    if middle not in {"", accession.replace("-", "")}:
        raise ValueError("SEC filing index path and accession disagree")
    result = dict(value)
    result.update(accession=accession, cik=f"{int(cik):010d}", issuer_name=issuer.strip(),
                  form_type=form, filed_at=stamp.isoformat(), filed_at_precision=precision)
    if "catalogue_filers" in value:
        filers = value["catalogue_filers"]
        if not isinstance(filers, list) or not filers:
            raise ValueError("Invalid SEC catalogue filer identities")
        normalized = []
        seen_filers = set()
        for filer in filers:
            if not isinstance(filer, dict) or set(filer) != {"cik", "issuer_name", "source_url"}:
                raise ValueError("Invalid SEC catalogue filer identity")
            identity = _entry({**{key: item for key, item in result.items() if key != "catalogue_filers"}, **filer})
            if identity["cik"] in seen_filers:
                raise ValueError("Duplicate SEC catalogue filer identity")
            seen_filers.add(identity["cik"])
            normalized.append({key: identity[key] for key in ("cik", "issuer_name", "source_url")})
        result["catalogue_filers"] = normalized
    return result


def parse_master_index(text: str, *, day: date, enabled_forms: Iterable[str]) -> list[dict[str, Any]]:
    """Validate every source row before selecting the configured form sample."""
    allowed = _forms(enabled_forms)
    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_INDEX_BYTES:
        raise ValueError("SEC daily index exceeds its byte limit")
    if re.search(r"<(?:html|body|script|!doctype)\b", text, re.I):
        raise ValueError("SEC daily index returned HTML instead of an index")
    lines = text.splitlines()
    headers = {"CIK|Company Name|Form Type|Date Filed|File Name",
               "CIK|Company Name|Form Type|Date Filed|Filename"}
    matches = [index for index, line in enumerate(lines) if line.strip() in headers]
    if len(matches) != 1 or matches[0] > 100:
        raise ValueError("SEC daily index has no unique expected header")
    submissions = {}
    for raw in lines[matches[0] + 1:]:
        line = raw.strip()
        if not line or set(line) == {"-"}:
            continue
        columns = line.split("|")
        if len(columns) != 5:
            raise ValueError("Malformed SEC daily index row")
        cik, issuer, form, filed, filename = (value.strip() for value in columns)
        form = form.upper()
        if not re.fullmatch(r"\d{1,10}", cik) or int(cik) <= 0 or not issuer or not _FORM.fullmatch(form):
            raise ValueError("Invalid SEC daily index identity")
        filed_day = date.fromisoformat(filed)
        if filed_day > day:
            raise ValueError("SEC daily index row has a future filing date")
        path = re.fullmatch(r"edgar/data/(\d{1,10})/(\d{10}-\d{2}-\d{6})\.txt", filename)
        if not path or int(path[1]) != int(cik):
            raise ValueError("SEC daily index contains an invalid archive path or CIK")
        accession = path[2]
        entry = _entry({
            "accession": accession, "cik": cik, "issuer_name": issuer, "form_type": form,
            "filed_at": datetime.combine(filed_day, datetime.min.time(), UTC), "filed_at_precision": "date",
            "source_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm",
            "catalogue_source_url": _index_url(day),
            "catalogue_index_day": day.isoformat(),
        })
        previous = submissions.get(accession)
        filer = {key: entry[key] for key in ("cik", "issuer_name", "source_url")}
        if previous is None:
            entry["catalogue_filers"] = [filer]
            entry["catalogue_index_row_count"] = 1
            submissions[accession] = entry
            continue
        if previous["form_type"] != form or previous["filed_at"] != entry["filed_at"]:
            raise ValueError("SEC daily index has conflicting form or date for one accession")
        existing = next((identity for identity in previous["catalogue_filers"] if identity["cik"] == entry["cik"]), None)
        if existing is not None and existing != filer:
            raise ValueError("SEC daily index has conflicting filer identity for one accession")
        # One accepted submission can appear under several filers, and SEC
        # also publishes exact repeated rows. Keep their source identities and
        # row count while enqueueing the accession once. The first source row
        # is a representative identity, not an inferred primary registrant.
        if existing is None:
            previous["catalogue_filers"].append(filer)
        previous["catalogue_index_row_count"] += 1
    return [_entry(entry) for entry in submissions.values() if entry["form_type"] in allowed]


class SECFilingCatalog:
    def __init__(self, db: Database):
        self.db = db
        with db.connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sec_catalog_state)")}
            for name in ("listed_through", "scan_from", "enabled_forms_json"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE sec_catalog_state ADD COLUMN {name} TEXT")

    @staticmethod
    def _capture(conn, entries: list[dict], observed: str, *, requeue: bool = False) -> int:
        inserted = 0
        for entry in entries:
            old = conn.execute("SELECT entry_json FROM sec_pending_filings WHERE accession=?", (entry["accession"],)).fetchone()
            retained = dict(entry)
            reopen = requeue
            if old:
                previous = json.loads(old["entry_json"])
                if previous.get("filed_at_precision") == "second" and entry["filed_at_precision"] == "date":
                    retained = dict(previous)
                # Atom may supply a more precise timestamp and a different
                # representative filer. Keep the observed daily-index filer
                # identities regardless of which timestamp version is retained.
                catalogue = entry if "catalogue_filers" in entry else previous
                for key in ("catalogue_filers", "catalogue_source_url", "catalogue_index_day", "catalogue_index_row_count"):
                    if key in catalogue:
                        retained[key] = catalogue[key]
                for key in ("issuer_review", "issuer_review_history"):
                    if key in previous and key not in retained:
                        retained[key] = previous[key]
                old_review = previous.get("issuer_review", {})
                changed_exact_observation = (entry["filed_at_precision"] == "second"
                    and (entry["filed_at"] != previous.get("filed_at")
                         or entry["cik"] != previous.get("cik")))
                changed_filers = ("catalogue_filers" in entry
                                 and entry["catalogue_filers"] != previous.get("catalogue_filers"))
                if old_review.get("status") == "unresolved" and (requeue or changed_exact_observation or changed_filers):
                    history = list(retained.get("issuer_review_history", []))
                    if old_review not in history:
                        history.append(old_review)
                    retained["issuer_review_history"] = history
                    retained["issuer_review"] = {**old_review, "status": "retry_requested"}
                    reopen = True
            if len(retained.get("catalogue_filers", [])) > 1 and "issuer_review" not in retained:
                retained["issuer_review"] = {"status": "retry_requested",
                    "reason": "Published SEC index identifies multiple filers; primary issuer authority must be checked before classification."}
                reopen = True
            inserted += conn.execute(
                "INSERT OR IGNORE INTO sec_pending_filings VALUES(?,?,?,?,NULL)",
                (entry["accession"], entry["form_type"], json.dumps(retained, sort_keys=True), observed),
            ).rowcount
            conn.execute("UPDATE sec_pending_filings SET form=?,entry_json=? WHERE accession=?",
                         (retained["form_type"], json.dumps(retained, sort_keys=True), entry["accession"]))
            if reopen:
                conn.execute("UPDATE sec_pending_filings SET processed_at=NULL WHERE accession=?", (entry["accession"],))
        return inserted

    def capture(self, entries: Iterable[dict[str, Any]], *, observed_at: datetime, requeue: bool = False) -> int:
        observed = _aware(observed_at).isoformat()
        validated = [_entry(value) for value in entries]
        if any(value["filed_at"] > observed for value in validated):
            raise ValueError("SEC catalogue filing timestamp is in the future")
        with self.db.transaction() as conn:
            return self._capture(conn, validated, observed, requeue=requeue)

    def pending_entries(self, form: str, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("SEC pending filing limit must be a bounded integer")
        with self.db.connect() as conn:
            rows = conn.execute("""SELECT entry_json FROM sec_pending_filings
              WHERE form=? AND processed_at IS NULL
              ORDER BY json_extract(entry_json,'$.filed_at'),discovered_at,accession LIMIT ?""",
                                (form.strip().upper(), limit)).fetchall()
        result = []
        for row in rows:
            entry = _entry(json.loads(row["entry_json"]))
            entry["filed_at"] = datetime.fromisoformat(entry["filed_at"])
            result.append(entry)
        return result

    def note_processed(self, accession: str, *, observed_at: datetime | None = None) -> None:
        if not _ACCESSION.fullmatch(accession):
            raise ValueError("Invalid processed SEC accession")
        stamp = _aware(observed_at or datetime.now(UTC)).isoformat()
        with self.db.connect() as conn:
            conn.execute("UPDATE sec_pending_filings SET processed_at=? WHERE accession=?", (stamp, accession))

    def record_issuer_review(self, entry: dict[str, Any], review: dict[str, Any]) -> None:
        """Keep completed source processing separate from issuer classification."""
        if (review.get("status") not in {"unresolved", "resolved"}
                or not isinstance(review.get("reason"), str) or not 1 <= len(review["reason"]) <= 1000):
            raise ValueError("Invalid SEC issuer review status or reason")
        observed = _aware(datetime.fromisoformat(review["processed_document_at"]))
        self.capture([entry], observed_at=observed)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT entry_json FROM sec_pending_filings WHERE accession=?", (entry["accession"],)).fetchone()
            current = json.loads(row["entry_json"])
            previous = current.get("issuer_review")
            history = current.get("issuer_review_history", [])
            if previous and previous != review and previous not in history:
                history.append(previous)
            if history:
                current["issuer_review_history"] = history
            current["issuer_review"] = review
            conn.execute("UPDATE sec_pending_filings SET entry_json=? WHERE accession=?",
                         (json.dumps(current, sort_keys=True), entry["accession"]))

    def _state(self) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM sec_catalog_state WHERE id=1").fetchone()
        return dict(row) if row else None

    def coverage(self) -> dict[str, Any]:
        state = self._state() or {}
        enabled = sorted(_forms(json.loads(state["enabled_forms_json"]))) if state.get("enabled_forms_json") else []
        with self.db.connect() as conn:
            total_pending = conn.execute("SELECT COUNT(*) FROM sec_pending_filings WHERE processed_at IS NULL").fetchone()[0]
            pending = (conn.execute(f"SELECT COUNT(*) FROM sec_pending_filings WHERE processed_at IS NULL AND form IN ({','.join('?' for _ in enabled)})", enabled).fetchone()[0]
                       if enabled else total_pending)
            days = conn.execute("SELECT COUNT(*) FROM sec_index_days").fetchone()[0]
            review_rows = conn.execute("""SELECT accession,form,entry_json FROM sec_pending_filings
                WHERE json_extract(entry_json,'$.issuer_review.status') IN ('unresolved','retry_requested')
                ORDER BY discovered_at,accession""").fetchall()
        reviews = [row for row in review_rows if not enabled or row["form"] in enabled]
        latest_closed_day = ((datetime.fromisoformat(state["last_sync_at"]).astimezone(_NEW_YORK).date() - timedelta(days=1)).isoformat()
                             if state.get("last_sync_at") else None)
        catchup_complete = bool(state.get("listed_through") and latest_closed_day
                                and state["listed_through"] >= latest_closed_day and not state.get("last_error")
                                and (not state.get("published_through") or state.get("captured_through")
                                     and state["captured_through"] >= state["published_through"]))
        return {**{key: value for key, value in state.items() if key != "id"},
                "source": "sec_daily_index", "scope": "Configured forms in published daily indexes from the frozen initial seven-day window onward; delayed releases may have older filing dates. Earlier index history remains incomplete.",
                "captured_index_days": days, "pending_filings": pending, "pending_filings_total": total_pending,
                "configured_forms": enabled,
                "issuer_review_count": len(reviews), "issuer_review_count_total": len(review_rows),
                "issuer_reviews": [{"accession": row["accession"], "form": row["form"],
                                    "reason": json.loads(row["entry_json"])["issuer_review"]["reason"]} for row in reviews[:20]],
                "status": "error" if state.get("last_error") else "review_required" if reviews else "pending" if state and (pending or not catchup_complete) else "ok" if state else "not_attempted",
                "error": state.get("last_error"),
                "index_catchup_complete": catchup_complete,
                "catchup_complete": catchup_complete and not reviews,
                "limitations": ["Directory availability establishes published daily indexes, not real-time coverage; SEC builds indexes nightly after 10 p.m. Eastern.",
                                "An index queue entry is not a processed filing or verified IPO; document processing receipts are independent.",
                                *([f"{len(reviews)} SEC submissions require issuer review; their source evidence cannot authorize an IPO or listing signal until attribution is resolved."] if reviews else [])]}

    async def sync(self, fetch_text: Callable[[str], Awaitable[str]], enabled_forms: Iterable[str], *,
                   observed_at: datetime, max_indexes: int = 3) -> dict[str, Any]:
        observed = _aware(observed_at)
        allowed = _forms(enabled_forms)
        if isinstance(max_indexes, bool) or not isinstance(max_indexes, int) or not 1 <= max_indexes <= 100:
            raise ValueError("SEC index download budget must be a bounded integer")
        last_day = observed.astimezone(_NEW_YORK).date() - timedelta(days=1)
        with self.db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO sec_catalog_state(id,scope_start) VALUES(1,?)",
                         ((observed.astimezone(_NEW_YORK).date() - timedelta(days=7)).isoformat(),))
        state = self._state()
        scope = date.fromisoformat(state["scope_start"])
        cursor = date.fromisoformat(state["captured_through"]) if state["captured_through"] else scope
        scan_from = date.fromisoformat(state["scan_from"]) if state.get("scan_from") else cursor
        quarter = _quarter(max(scope, scan_from))
        available = []
        published = state.get("published_through")
        try:
            previous_forms = set(json.loads(state["enabled_forms_json"])) if state.get("enabled_forms_json") else set()
            if previous_forms != allowed:
                # The source archive is complete for each captured day, even
                # when its original configured form sample was narrower.
                # Enabling a form replays those primary bytes once; disabling
                # a form preserves its evidence without counting it as active.
                with self.db.connect() as conn:
                    archived = conn.execute("SELECT * FROM sec_index_days ORDER BY day").fetchall()
                for row in archived:
                    raw = self._index_content(row)
                    entries = parse_master_index(raw.decode("utf-8"), day=date.fromisoformat(row["day"]), enabled_forms=allowed)
                    with self.db.transaction() as conn:
                        self._capture(conn, entries, observed.isoformat())
                with self.db.connect() as conn:
                    conn.execute("UPDATE sec_catalog_state SET enabled_forms_json=? WHERE id=1", (json.dumps(sorted(allowed)),))
            for _ in range(2):
                if quarter > _quarter(last_day):
                    break
                year, number = quarter
                directory_path = f"/Archives/edgar/daily-index/{year}/QTR{number}"
                url = f"https://www.sec.gov{directory_path}/index.json"
                listing = json.loads(await fetch_text(url))
                for day in _directory_index_days(listing, quarter=quarter):
                    if not scope <= day <= last_day:
                        continue
                    published = max(published or day.isoformat(), day.isoformat())
                    with self.db.connect() as conn:
                        exists = conn.execute("SELECT 1 FROM sec_index_days WHERE day=?", (day.isoformat(),)).fetchone()
                    if exists is None:
                        available.append(day)
                next_year, next_number = _next_quarter(quarter)
                quarter_end = date(next_year, (next_number - 1) * 3 + 1, 1) - timedelta(days=1)
                with self.db.connect() as conn:
                    conn.execute("UPDATE sec_catalog_state SET listed_through=? WHERE id=1",
                                 (min(last_day, quarter_end).isoformat(),))
                quarter = _next_quarter(quarter)
            with self.db.connect() as conn:
                conn.execute("UPDATE sec_catalog_state SET published_through=?,last_sync_at=?,last_error=NULL WHERE id=1",
                             (published, observed.isoformat()))
            for day in sorted(set(available))[:max_indexes]:
                url = _index_url(day)
                text = await fetch_text(url)
                entries = parse_master_index(text, day=day, enabled_forms=allowed)
                raw = text.encode("utf-8")
                with self.db.transaction() as conn:
                    conn.execute("INSERT INTO sec_index_days VALUES(?,?,?,?,?,?,?)",
                                 (day.isoformat(), url, hashlib.sha256(raw).hexdigest(), gzip.compress(raw, mtime=0),
                                  len(raw), len(entries), observed.isoformat()))
                    self._capture(conn, entries, observed.isoformat())
                    conn.execute("UPDATE sec_catalog_state SET captured_through=CASE WHEN captured_through IS NULL OR captured_through<? THEN ? ELSE captured_through END WHERE id=1",
                                 (day.isoformat(), day.isoformat()))
            remaining = sorted(set(available))[max_indexes:]
            # Resume complete closed quarters even when a directory had no
            # index files in scope. Always revisit the current quarter so a
            # newly published daily index remains discoverable next poll.
            next_quarter = _quarter(remaining[0]) if remaining else min(quarter, _quarter(last_day))
            next_start = date(next_quarter[0], (next_quarter[1] - 1) * 3 + 1, 1)
            with self.db.connect() as conn:
                conn.execute("UPDATE sec_catalog_state SET scan_from=? WHERE id=1",
                             (max(scope, next_start).isoformat(),))
        except Exception as exc:
            with self.db.connect() as conn:
                conn.execute("UPDATE sec_catalog_state SET last_sync_at=?,last_error=? WHERE id=1",
                             (observed.isoformat(), f"{type(exc).__name__}: {exc}"))
            raise
        result = self.coverage()
        # Cursor equality alone cannot prove all quarters have been examined.
        result["index_catchup_complete"] = result["index_catchup_complete"] and quarter > _quarter(last_day) and len(available) <= max_indexes
        result["catchup_complete"] = result["catchup_complete"] and result["index_catchup_complete"]
        result["indexes_captured_this_run"] = min(len(available), max_indexes)
        return result

    @staticmethod
    def _index_content(row) -> bytes:
        if not 0 <= row["original_bytes"] <= _MAX_INDEX_BYTES:
            raise ValueError("Invalid SEC index archive byte count")
        try:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = decoder.decompress(row["gzip_blob"], _MAX_INDEX_BYTES + 1)
        except (zlib.error, TypeError) as exc:
            raise ValueError("SEC daily-index archive integrity check failed") from exc
        if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                or len(raw) != row["original_bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]
                or row["source_url"] != _index_url(date.fromisoformat(row["day"]))):
            raise ValueError("SEC daily-index archive integrity check failed")
        return raw

    def verify_index_receipts(self) -> int:
        """Read back every portable source receipt before trusting the catalogue."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM sec_index_days ORDER BY day").fetchall()
        for row in rows:
            self._index_content(row)
        return len(rows)
