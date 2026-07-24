from __future__ import annotations

import hashlib
import json
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


class USAspendingNormalizer:
    def normalize(self, row: dict[str, Any], *, observed_at: datetime) -> ContractEvidence:
        award_id = str(_first(row, "Award ID", "award_id", "piid", "generated_unique_award_id"))
        external_id = str(_first(row, "generated_unique_award_id", "internal_id", default=award_id))
        raw = json.dumps(row, sort_keys=True, default=str)
        obligation = _first(row, "Total Obligation", "total_obligation", "Award Amount", "award_amount")
        value = _first(row, "Award Amount", "award_amount", "generated_subawards")
        recipient = str(_first(row, "Recipient Name", "recipient_name", default=""))
        return ContractEvidence(
            source="usaspending",
            source_url=f"https://www.usaspending.gov/award/{external_id}",
            source_record_id=external_id,
            retrieved_at=observed_at,
            published_at=observed_at,
            award_id=award_id,
            modification_number=str(_first(row, "Modification Number", "modification_number", default="0")),
            status="awarded",
            award_date=_date(_first(row, "Start Date", "start_date", "Award Date", "award_date"), observed_at.date()),
            agency=str(_first(row, "Awarding Agency", "awarding_agency", default="Unknown agency")),
            subagency=_first(row, "Awarding Sub Agency", "awarding_subagency"),
            office=_first(row, "Awarding Office", "awarding_office"),
            recipient_name=recipient,
            recipient_uei=_first(row, "Recipient UEI", "recipient_uei", "recipient_unique_id"),
            recipient_address=_first(row, "Recipient Address", "recipient_address") or ", ".join(str(x) for x in [
                _first(row, "Recipient Address Line 1"), _first(row, "Recipient City"),
                _first(row, "Recipient State"), _first(row, "Recipient Zip Code")
            ] if x),
            prime=True,
            obligated_amount=float(obligation) if obligation is not None else None,
            current_value=float(value) if value is not None else None,
            ceiling_amount=float(_first(row, "Potential Award Amount", "potential_award_amount")) if _first(row, "Potential Award Amount", "potential_award_amount") is not None else None,
            award_type=str(_first(row, "Award Type", "award_type", default="contract")).lower().replace(" ", "_"),
            start_date=_date(_first(row, "Start Date", "start_date"), observed_at.date()),
            end_date=_date(_first(row, "End Date", "end_date"), observed_at.date()) if _first(row, "End Date", "end_date") else None,
            description=str(_first(row, "Description", "description", default="No description supplied")),
            evidence_class=EvidenceClass.A,
            raw_payload_hash=hashlib.sha256(raw.encode()).hexdigest(),
        )


class USAspendingCollector:
    URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

    def __init__(self, client: ResilientClient):
        self.client = client
        self.normalizer = USAspendingNormalizer()

    async def collect(self, *, observed_at: datetime, lookback: timedelta = timedelta(days=3), page: int = 1, limit: int = 100) -> list[ContractEvidence]:
        payload = {
            "filters": {
                "time_period": [{"start_date": (observed_at.date() - lookback).isoformat(), "end_date": observed_at.date().isoformat()}],
                "award_type_codes": ["A", "B", "C", "D"],
            },
            "fields": [
                "Award ID", "Recipient Name", "Award Amount", "Potential Award Amount",
                "Awarding Agency", "Awarding Sub Agency", "Awarding Office", "Start Date", "End Date",
                "Description", "Contract Award Type", "Recipient Address Line 1", "Recipient City",
                "Recipient State", "Recipient Zip Code",
            ],
            "page": page,
            "limit": limit,
            "subawards": False,
            "sort": "Start Date",
            "order": "desc",
        }
        data = await self.client.request_json("POST", self.URL, json=payload)
        rows = (data or {}).get("results", [])
        return [self.normalizer.normalize(row, observed_at=observed_at) for row in rows]
