from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote
from typing import Any

from ..models import ContractEvidence, EvidenceClass
from .http import ResilientClient, TransientHTTPError


def _first(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return default


def _date(value: Any, fallback: date) -> date:
    if not value:
        return fallback
    return date.fromisoformat(str(value)[:10])


def _normalize_name(value: str | None) -> str:
    if not value:
        return ""
    value = value.upper()
    value = re.sub(r"\b(INCORPORATED|INC|CORPORATION|CORP|LIMITED|LLC|LTD|CO|COMPANY)\b", "", value)
    return re.sub(r"[^A-Z0-9]", "", value)


def _next_source_page(data: dict[str, Any], *, current_page: int, row_count: int,
                      page_limit: int, require_full_page_probe: bool = False) -> int | None:
    """Completion is an explicit source receipt, never a missing field default.

    The award endpoint uses integer page numbers. A jump or a coercion such as
    ``int(2.9)`` could silently skip awards while advancing the durable success
    watermark. Keep already validated rows, but fail the scan on that ambiguity.
    """
    metadata = data.get("page_metadata")
    if not isinstance(metadata, dict) or type(metadata.get("hasNext")) is not bool:
        raise ValueError("USAspending pagination lacks an explicit boolean hasNext receipt")
    receipt_page = metadata.get("page")
    if receipt_page is not None and (type(receipt_page) is not int or receipt_page != current_page):
        raise ValueError("USAspending pagination response page does not match the requested page")
    next_page = metadata.get("next")
    if not metadata["hasNext"]:
        if next_page is not None:
            raise ValueError("USAspending pagination reports conflicting completion and next-page receipts")
        # Offset responses derive hasNext from an OpenSearch total capped at
        # 10,000. A full offset page is not end-of-results proof. Prefer the
        # documented search_after cursor; a legacy response needs a tail read.
        # Cursor responses use an actual limit+1 look-ahead, unlike offsets.
        if row_count == page_limit and require_full_page_probe:
            return current_page + 1
        return None
    if next_page is None:
        # Some API responses omit `next`; hasNext still proves continuation.
        next_page = current_page + 1
    if type(next_page) is not int or next_page != current_page + 1 or not row_count:
        raise ValueError("USAspending pagination did not advance to the immediate next nonempty page")
    return next_page


def _award_search_cursor(metadata: dict[str, Any]) -> tuple[int, str] | None:
    """Read the paired, documented source cursor without inventing sort values.

    https://github.com/fedspendingtransparency/usaspending-api/blob/master/usaspending_api/search/v2/views/spending_by_award.py
    Cursor paging uses the source's stable sort plus unique award-ID tie break,
    and avoids the capped hit-count/offset window reported in upstream #4793.
    """
    keys = {"last_record_unique_id", "last_record_sort_value"}
    present = keys.intersection(metadata)
    if not present:
        return None
    if present != keys:
        raise ValueError("USAspending pagination cursor requires both source fields")
    identity, value = metadata["last_record_unique_id"], metadata["last_record_sort_value"]
    # The upstream endpoint serializes its absent terminal sort value as "None".
    if identity is None and value in (None, "None") and metadata["hasNext"] is False:
        return None
    if type(identity) is not int or identity <= 0 or not isinstance(value, str) or not value or len(value) > 1024 or value == "None":
        raise ValueError("USAspending pagination cursor fields are malformed or unavailable")
    return identity, value


class USAspendingRecipientResolver:
    """Resolve an exact recipient legal name to a unique UEI using USAspending's recipient API.

    The lookup is deliberately fail-closed: ambiguous exact-name matches return no UEI.
    """

    URL = "https://api.usaspending.gov/api/v2/recipient/"

    def __init__(self, client: ResilientClient, *, max_pages: int = 5):
        if type(max_pages) is not int or not 1 <= max_pages <= 20:
            raise ValueError("Recipient identity lookup permits 1-20 source pages")
        self.client = client
        self.max_pages = max_pages
        self._cache: dict[str, str | None] = {}

    async def resolve_uei(self, legal_name: str) -> str | None:
        key = _normalize_name(legal_name)
        if not key:
            return None
        if key in self._cache:
            return self._cache[key]

        payload = {
            "keyword": legal_name,
            "award_type": "contracts",
            "page": 1,
            "limit": 100,
            "sort": "name",
            "order": "asc",
        }
        exact_ueis: set[str] = set()
        for page in range(1, self.max_pages + 1):
            payload["page"] = page
            data = await self.client.request_json("POST", self.URL, json=payload)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ValueError("USAspending recipient response does not contain a results list")
            rows = data["results"]
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                    raise ValueError("USAspending recipient response has an invalid identity")
                if _normalize_name(row["name"]) == key and row.get("uei"):
                    exact_ueis.add(str(row["uei"]).strip())
            next_page = _next_source_page(data, current_page=page, row_count=len(rows), page_limit=100)
            if len(exact_ueis) > 1:
                # Ambiguity is already proven; further pages cannot make two
                # independently observed identities unique.
                self._cache[key] = None
                return None
            if next_page is None:
                resolved = next(iter(exact_ueis), None)
                self._cache[key] = resolved
                return resolved
        # A positive identity and a negative cache entry both require a complete
        # lookup. A later attempt can retry instead of freezing this partial view.
        raise RuntimeError("USAspending recipient pagination reached its page budget; identity remains unresolved")


class USAspendingNormalizer:
    def normalize(self, row: dict[str, Any], *, observed_at: datetime) -> ContractEvidence:
        award_id = str(_first(row, "Award ID", "award_id", "piid", "generated_unique_award_id", default=""))
        external_id = str(_first(row, "generated_internal_id", "generated_unique_award_id", "internal_id", default=award_id))
        raw = json.dumps(row, sort_keys=True, default=str)
        obligation = _first(row, "Total Obligation", "total_obligation", "Award Amount", "award_amount")
        value = _first(row, "Award Amount", "award_amount")
        recipient = str(_first(row, "Recipient Name", "recipient_name", default=""))
        address_parts = [
            _first(row, "Recipient Address Line 1"),
            _first(row, "Recipient Address Line 2"),
            _first(row, "Recipient Address Line 3"),
            _first(row, "Recipient City", "Recipient City Name"),
            _first(row, "Recipient State", "Recipient State Code"),
            _first(row, "Recipient Zip Code", "Recipient Zip5"),
        ]
        location = _first(row, "Recipient Location")
        if isinstance(location, dict):
            address_parts = [location.get(key) for key in ("address_line1", "address_line2", "city_name", "state_code", "zip5", "country_name")]
        modified = _first(row, "Last Modified Date", "last_modified_date")
        published = datetime.fromisoformat(str(modified).replace("Z", "+00:00")) if modified else None
        if published is not None and published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        signed = _first(row, "Base Obligation Date", "Signed Date", "Award Date", "award_date", "Start Date", "start_date")
        if not signed:
            raise ValueError("USAspending award is missing its source award date")
        return ContractEvidence(
            source="usaspending",
            source_url=f"https://www.usaspending.gov/award/{quote(external_id, safe='')}",
            source_record_id=external_id,
            retrieved_at=observed_at,
            published_at=published,
            award_id=award_id,
            modification_number=str(_first(row, "Modification Number", "modification_number", default="0")),
            status="awarded",
            award_date=_date(signed, observed_at.date()),
            agency=str(_first(row, "Awarding Agency", "awarding_agency", default="Unknown agency")),
            subagency=_first(row, "Awarding Sub Agency", "awarding_subagency"),
            office=_first(row, "Awarding Office", "awarding_office"),
            recipient_name=recipient,
            recipient_uei=_first(row, "Recipient UEI", "recipient_uei", "recipient_unique_id"),
            recipient_address=_first(row, "Recipient Address", "recipient_address") or ", ".join(str(x) for x in address_parts if x) or None,
            prime=True,
            obligated_amount=float(obligation) if obligation is not None else None,
            current_value=float(value) if value is not None else None,
            ceiling_amount=float(_first(row, "Potential Award Amount", "potential_award_amount")) if _first(row, "Potential Award Amount", "potential_award_amount") is not None else None,
            award_type=str(_first(row, "Contract Award Type", "Award Type", "award_type", default="contract")).lower().replace(" ", "_"),
            start_date=_date(_first(row, "Start Date", "start_date"), observed_at.date()) if _first(row, "Start Date", "start_date") else None,
            end_date=_date(_first(row, "End Date", "end_date"), observed_at.date()) if _first(row, "End Date", "end_date") else None,
            description=str(_first(row, "Contract Description", "Description", "description", default="No description supplied")),
            evidence_class=EvidenceClass.A,
            raw_payload_hash=hashlib.sha256(raw.encode()).hexdigest(),
        )


class IncompleteUSAspendingCollection(RuntimeError):
    """Incomplete source scan with usable records and no successful watermark."""

    def __init__(self, reason: str, *, records: list[ContractEvidence], page: int):
        super().__init__(reason)
        self.reason = reason
        self.records = tuple(records)
        self.page = page


class USAspendingCollector:
    URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

    def __init__(self, client: ResilientClient, *, recipient_resolver: USAspendingRecipientResolver | None = None):
        self.client = client
        self.normalizer = USAspendingNormalizer()
        self.recipient_resolver = recipient_resolver
        self.partial_records: list[ContractEvidence] = []
        self.pagination_warnings: list[str] = []

    async def collect(
        self,
        *,
        observed_at: datetime,
        lookback: timedelta = timedelta(days=3),
        start_date: date | None = None,
        end_date: date | None = None,
        page: int = 1,
        limit: int = 100,
        max_pages: int = 250,
        enrich_uei: bool = False,
    ) -> list[ContractEvidence]:
        # Retain completed observations even if a later page fails or the
        # caller's deadline cancels the request. This is never a success cursor.
        self.partial_records = []
        self.pagination_warnings = []
        start = start_date or (observed_at.date() - lookback)
        end = end_date or observed_at.date()
        if start > end or page < 1 or not 1 <= limit <= 100 or max_pages < 1:
            raise ValueError("invalid USAspending collection window or pagination limits")
        current_page = page
        records = self.partial_records
        seen: set[tuple[str, str]] = set()
        search_cursor: tuple[int, str] | None = None
        seen_cursors: set[tuple[int, str]] = set()
        cursor_disabled = False

        try:
            while True:
                payload = {
                    "filters": {
                        "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
                        "award_type_codes": ["A", "B", "C", "D"],
                    },
                    "fields": [
                        "Award ID", "Recipient Name", "Award Amount", "Recipient UEI", "Recipient Location",
                        "Awarding Agency", "Awarding Sub Agency", "Start Date", "End Date",
                        "Base Obligation Date", "Last Modified Date", "Description", "Contract Award Type", "generated_internal_id",
                    ],
                    "page": current_page,
                    "limit": limit,
                    "subawards": False,
                    "sort": "Start Date",
                    "order": "desc",
                }
                cursor_used = search_cursor is not None
                if cursor_used:
                    payload.update(last_record_unique_id=search_cursor[0], last_record_sort_value=search_cursor[1])
                try:
                    data = await self.client.request_json("POST", self.URL, json=payload)
                except TransientHTTPError as exc:
                    if not cursor_used:
                        raise
                    # The documented cursor route can be unavailable while the
                    # same authorized endpoint still serves offset pages. This
                    # is an independent bounded traversal, not a success claim
                    # for the failed request. Authentication/403 is never retried
                    # through a different request shape.
                    self.pagination_warnings.append(
                        f"USAspending search_after request failed ({exc}); using offset pagination with full-page tail verification for this scan."
                    )
                    cursor_disabled = True
                    cursor_used = False
                    search_cursor = None
                    payload.pop("last_record_unique_id", None)
                    payload.pop("last_record_sort_value", None)
                    data = await self.client.request_json("POST", self.URL, json=payload)
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    raise ValueError("USAspending response does not contain a results list")
                rows = data["results"]
                before = len(records)
                for row in rows:
                    record = self.normalizer.normalize(row, observed_at=observed_at)
                    if not record.award_id or not record.recipient_name:
                        raise ValueError("USAspending returned an award without an identifier or recipient")
                    if enrich_uei and record.recipient_uei is None and self.recipient_resolver is not None:
                        uei = await self.recipient_resolver.resolve_uei(record.recipient_name)
                        if uei:
                            record = record.model_copy(update={"recipient_uei": uei})
                    key = (record.source_record_id, record.raw_payload_hash)
                    if key not in seen:
                        seen.add(key)
                        records.append(record)

                next_page = _next_source_page(data, current_page=current_page, row_count=len(rows),
                                              page_limit=limit, require_full_page_probe=not cursor_used)
                next_cursor = _award_search_cursor(data["page_metadata"])
                if len(rows) > limit:
                    raise RuntimeError("USAspending pagination returned more rows than the requested page limit")
                if rows and len(records) == before:
                    raise RuntimeError("USAspending pagination repeated previously observed rows instead of advancing")
                if next_page is None:
                    break
                if current_page - page + 1 >= max_pages:
                    raise RuntimeError(
                        f"USAspending pagination exceeded max_pages={max_pages} for {start.isoformat()}..{end.isoformat()}; refusing to truncate silently."
                    )
                if search_cursor is not None and next_cursor is None:
                    raise ValueError("USAspending pagination lost its search_after continuation cursor")
                if next_cursor is not None:
                    if next_cursor in seen_cursors:
                        raise RuntimeError("USAspending pagination repeated its source cursor instead of advancing")
                    seen_cursors.add(next_cursor)
                search_cursor = None if cursor_disabled else next_cursor
                current_page = next_page

        except Exception as exc:
            if records:
                raise IncompleteUSAspendingCollection(
                    str(exc), records=records, page=current_page,
                ) from exc
            raise

        return records
