from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from ..models import ContractEvidence, EvidenceClass
from .http import ResilientClient


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


class USAspendingRecipientResolver:
    """Resolve an exact recipient legal name to a unique UEI using USAspending's recipient API.

    The lookup is deliberately fail-closed: ambiguous exact-name matches return no UEI.
    """

    URL = "https://api.usaspending.gov/api/v2/recipient/"

    def __init__(self, client: ResilientClient):
        self.client = client
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
        data = await self.client.request_json("POST", self.URL, json=payload)
        exact_ueis = {
            str(row.get("uei")).strip()
            for row in (data or {}).get("results", [])
            if _normalize_name(row.get("name")) == key and row.get("uei")
        }
        resolved = next(iter(exact_ueis)) if len(exact_ueis) == 1 else None
        self._cache[key] = resolved
        return resolved


class USAspendingNormalizer:
    def normalize(self, row: dict[str, Any], *, observed_at: datetime) -> ContractEvidence:
        award_id = str(_first(row, "Award ID", "award_id", "piid", "generated_unique_award_id", default=""))
        external_id = str(_first(row, "generated_unique_award_id", "internal_id", default=award_id))
        raw = json.dumps(row, sort_keys=True, default=str)
        obligation = _first(row, "Total Obligation", "total_obligation", "Award Amount", "award_amount")
        value = _first(row, "Award Amount", "award_amount", "generated_subawards")
        recipient = str(_first(row, "Recipient Name", "recipient_name", default=""))
        address_parts = [
            _first(row, "Recipient Address Line 1"),
            _first(row, "Recipient Address Line 2"),
            _first(row, "Recipient Address Line 3"),
            _first(row, "Recipient City", "Recipient City Name"),
            _first(row, "Recipient State", "Recipient State Code"),
            _first(row, "Recipient Zip Code", "Recipient Zip5"),
        ]
        return ContractEvidence(
            source="usaspending",
            source_url=f"https://www.usaspending.gov/award/{external_id}",
            source_record_id=external_id,
            retrieved_at=observed_at,
            published_at=observed_at,
            award_id=award_id,
            modification_number=str(_first(row, "Modification Number", "modification_number", default="0")),
            status="awarded",
            award_date=_date(_first(row, "Signed Date", "Award Date", "award_date", "Start Date", "start_date"), observed_at.date()),
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
            start_date=_date(_first(row, "Start Date", "start_date"), observed_at.date()),
            end_date=_date(_first(row, "End Date", "end_date"), observed_at.date()) if _first(row, "End Date", "end_date") else None,
            description=str(_first(row, "Contract Description", "Description", "description", default="No description supplied")),
            evidence_class=EvidenceClass.A,
            raw_payload_hash=hashlib.sha256(raw.encode()).hexdigest(),
        )


class USAspendingCollector:
    URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

    def __init__(self, client: ResilientClient, *, recipient_resolver: USAspendingRecipientResolver | None = None):
        self.client = client
        self.normalizer = USAspendingNormalizer()
        self.recipient_resolver = recipient_resolver

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
    ) -> list[ContractEvidence]:
        start = start_date or (observed_at.date() - lookback)
        end = end_date or observed_at.date()
        current_page = page
        records: list[ContractEvidence] = []
        seen: set[tuple[str, str]] = set()

        while True:
            payload = {
                "filters": {
                    "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
                    "award_type_codes": ["A", "B", "C", "D"],
                },
                "fields": [
                    "Award ID", "Recipient Name", "Award Amount", "Potential Award Amount",
                    "Awarding Agency", "Awarding Sub Agency", "Awarding Office", "Start Date", "End Date",
                    "Signed Date", "Contract Description", "Contract Award Type", "Recipient Address Line 1",
                ],
                "page": current_page,
                "limit": limit,
                "subawards": False,
                "sort": "Start Date",
                "order": "desc",
            }
            data = await self.client.request_json("POST", self.URL, json=payload)
            rows = (data or {}).get("results", [])
            for row in rows:
                record = self.normalizer.normalize(row, observed_at=observed_at)
                if not record.award_id or not record.recipient_name:
                    continue
                if record.recipient_uei is None and self.recipient_resolver is not None:
                    uei = await self.recipient_resolver.resolve_uei(record.recipient_name)
                    if uei:
                        record = record.model_copy(update={"recipient_uei": uei})
                key = (record.source_record_id, record.raw_payload_hash)
                if key not in seen:
                    seen.add(key)
                    records.append(record)

            metadata = (data or {}).get("page_metadata") or {}
            if not metadata.get("hasNext"):
                break
            if current_page - page + 1 >= max_pages:
                raise RuntimeError(
                    f"USAspending pagination exceeded max_pages={max_pages} for {start.isoformat()}..{end.isoformat()}; refusing to truncate silently."
                )
            next_page = metadata.get("next")
            current_page = int(next_page) if next_page is not None else current_page + 1

        return records
