from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from ..models import ContractEvidence, EvidenceClass
from .http import ResilientClient


def _parse_us_date(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    month, day, year = value.split("/")
    return date(int(year), int(month), int(day))


class SAMNormalizer:
    def normalize(self, row: dict[str, Any], *, observed_at: datetime, deleted: bool = False) -> ContractEvidence:
        cid = row.get("contractId", {})
        details = row.get("awardDetails", {})
        awardee = details.get("awardeeData", {})
        header = awardee.get("awardeeHeader", {})
        uei = awardee.get("awardeeUEIInformation", {})
        raw = json.dumps(row, sort_keys=True, default=str)
        agency = (cid.get("subtier") or {}).get("name") or (details.get("contractingDepartment") or {}).get("name") or "Unknown agency"
        return ContractEvidence(
            source="sam_contract_awards",
            source_url="https://sam.gov/data-services/Contract%20Opportunities/Contract%20Awards",
            source_record_id=f"{cid.get('piid','')}:{cid.get('modificationNumber','0')}:{cid.get('transactionNumber','0')}",
            retrieved_at=observed_at,
            published_at=observed_at,
            award_id=str(cid.get("piid", "")),
            modification_number=str(cid.get("modificationNumber", "0")),
            transaction_id=str(cid.get("transactionNumber", "0")),
            status="deleted" if deleted else "awarded",
            award_date=_parse_us_date(details.get("dateSigned"), observed_at.date()),
            agency=agency,
            recipient_name=header.get("awardeeName") or header.get("awardeeNameFromContract") or "",
            recipient_uei=uei.get("uniqueEntityId"),
            recipient_cage=uei.get("cageCode"),
            parent_uei=uei.get("awardeeUltimateParentUniqueEntityId"),
            prime=True,
            obligated_amount=float(details["dollarsObligated"]) if details.get("dollarsObligated") is not None else None,
            current_value=float(details["totalDollarsObligated"]) if details.get("totalDollarsObligated") is not None else None,
            ceiling_amount=float(details["baseAndAllOptionsValue"]) if details.get("baseAndAllOptionsValue") is not None else None,
            award_type=str(details.get("awardOrIDVTypeName", "contract")).lower().replace(" ", "_"),
            pricing_type=(details.get("typeOfContractPricing") or {}).get("name") if isinstance(details.get("typeOfContractPricing"), dict) else details.get("typeOfContractPricingName"),
            description=str(details.get("descriptionOfContractRequirement", "No description supplied")),
            evidence_class=EvidenceClass.A,
            raw_payload_hash=hashlib.sha256(raw.encode()).hexdigest(),
            deleted=deleted,
        )


class SAMCollector:
    URL = "https://api.sam.gov/contract-awards/v1/search"

    def __init__(self, client: ResilientClient, api_key: str):
        self.client = client
        self.api_key = api_key
        self.normalizer = SAMNormalizer()

    async def collect(self, *, observed_at: datetime, last_modified_start: date, deleted: bool = False, limit: int = 100) -> list[ContractEvidence]:
        params = {
            "api_key": self.api_key,
            "limit": limit,
            "offset": 0,
            "lastModifiedDate": f"[{last_modified_start.strftime('%m/%d/%Y')},]",
            "includeSections": "contractId,awardDetails,awardeeData",
        }
        if deleted:
            params["deletedStatus"] = "yes"
        data = await self.client.request_json("GET", self.URL, params=params)
        return [self.normalizer.normalize(row, observed_at=observed_at, deleted=deleted) for row in (data or {}).get("awardSummary", [])]
