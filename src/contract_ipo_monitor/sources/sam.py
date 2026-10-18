from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from typing import Any

from ..models import ContractEvidence, EvidenceClass
from .http import ResilientClient


def _parse_us_date(value: str | None, fallback: date | None = None) -> date:
    if not value:
        if fallback is None:
            raise ValueError("SAM award is missing its source date signed")
        return fallback
    if "-" in value:
        return date.fromisoformat(value[:10])
    month, day, year = value.split("/")
    return date(int(year), int(month), int(day))


class SAMNormalizer:
    def normalize(self, row: dict[str, Any], *, observed_at: datetime, deleted: bool = False) -> ContractEvidence:
        cid = row.get("contractId", {})
        details = row.get("awardDetails", {})
        core = row.get("coreData") or {}
        dates = details.get("dates") or {}
        dollars = details.get("dollars") or {}
        totals = details.get("totalContractDollars") or {}
        awardee = details.get("awardeeData") or row.get("awardeeData") or {}
        header = awardee.get("awardeeHeader", {})
        uei = awardee.get("awardeeUEIInformation", {})
        raw = json.dumps(row, sort_keys=True, default=str)
        organizations = (core.get("federalOrganization") or {}).get("contractingInformation") or {}
        agency = (organizations.get("contractingDepartment") or {}).get("name") or (cid.get("subtier") or {}).get("name") or (details.get("contractingDepartment") or {}).get("name") or ""
        amount = totals.get("totalActionObligation", dollars.get("actionObligation", details.get("dollarsObligated")))
        value = totals.get("totalBaseAndExercisedOptionsValue", dollars.get("baseAndExercisedOptionsValue", details.get("totalDollarsObligated")))
        ceiling = totals.get("totalBaseAndAllOptionsValue", dollars.get("baseAndAllOptionsValue", details.get("baseAndAllOptionsValue")))
        date_signed = dates.get("dateSigned") or details.get("dateSigned")
        modified = ((core.get("transactionInformation") or {}).get("lastModifiedDate"))
        published = datetime.fromisoformat(str(modified).replace("Z", "+00:00")) if modified else None
        if published is not None and published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        subtype = (core.get("awardOrIDVType") or {}).get("name") or details.get("awardOrIDVTypeName") or "contract"
        if core.get("awardOrIDV") == "IDV":
            subtype = "idv_" + subtype
        location = awardee.get("awardeeLocation") or {}
        address = ", ".join(str(part) for part in [location.get("streetAddress1"), location.get("streetAddress2"), location.get("city"), (location.get("state") or {}).get("code"), location.get("zip")] if part) or None
        authority = (cid.get("subtier") or {}).get("code") or ""
        return ContractEvidence(
            source="sam_contract_awards",
            source_url="https://sam.gov/data-services/Contract%20Opportunities/Contract%20Awards",
            source_record_id=f"{authority}:{cid.get('piid','')}:{cid.get('referencedIDVPiid','')}:{cid.get('modificationNumber','0')}:{cid.get('transactionNumber','0')}",
            retrieved_at=observed_at,
            published_at=published,
            award_id=str(cid.get("piid", "")),
            modification_number=str(cid.get("modificationNumber", "0")),
            transaction_id=str(cid.get("transactionNumber", "0")),
            status="deleted" if deleted else "awarded",
            award_date=_parse_us_date(date_signed, observed_at.date() if deleted else None),
            agency=agency,
            subagency=(organizations.get("contractingSubtier") or {}).get("name"),
            office=(organizations.get("contractingOffice") or {}).get("name"),
            recipient_name=header.get("awardeeName") or header.get("awardeeNameFromContract") or "",
            recipient_uei=uei.get("uniqueEntityId"),
            recipient_cage=uei.get("cageCode"),
            recipient_address=address,
            parent_uei=uei.get("awardeeUltimateParentUniqueEntityId"),
            prime=True,
            obligated_amount=float(amount) if amount not in (None, "") else None,
            current_value=float(value) if value not in (None, "") else None,
            ceiling_amount=float(ceiling) if ceiling not in (None, "") else None,
            award_type=str(subtype).lower().replace(" ", "_"),
            pricing_type=((core.get("acquisitionData") or {}).get("typeOfContractPricing") or {}).get("name") or details.get("typeOfContractPricingName"),
            start_date=_parse_us_date(dates.get("periodOfPerformanceStartDate")) if dates.get("periodOfPerformanceStartDate") else None,
            end_date=_parse_us_date(dates.get("currentCompletionDate")) if dates.get("currentCompletionDate") else None,
            description=str((details.get("productOrServiceInformation") or {}).get("descriptionOfContractRequirement") or details.get("descriptionOfContractRequirement") or "No description supplied"),
            evidence_class=EvidenceClass.A,
            raw_payload_hash=hashlib.sha256(raw.encode()).hexdigest(),
            deleted=deleted,
        )


class IncompleteSAMCollection(RuntimeError):
    """A failed source scan with validated observations but no success cursor."""

    def __init__(self, reason: str, *, records: list[ContractEvidence], page: int):
        super().__init__(reason)
        self.reason = reason
        self.records = tuple(records)
        self.page = page


class SAMCollector:
    URL = "https://api.sam.gov/contract-awards/v1/search"

    def __init__(self, client: ResilientClient, api_key: str):
        self.client = client
        self.api_key = api_key
        self.normalizer = SAMNormalizer()
        self.partial_records: list[ContractEvidence] = []

    async def collect(self, *, observed_at: datetime, last_modified_start: date, deleted: bool = False, limit: int = 100, max_pages: int = 250) -> list[ContractEvidence]:
        # One call covers either active or deleted records. The service adapter
        # combines the two calls and retains this list on failure/cancellation.
        self.partial_records = []
        if observed_at.utcoffset() is None:
            raise ValueError("SAM observation timestamp must include a timezone")
        observed_at = observed_at.astimezone(UTC)
        if not 1 <= limit <= 100 or max_pages < 1 or last_modified_start > observed_at.date():
            raise ValueError("invalid SAM collection window or pagination limits")
        params = {
            "api_key": self.api_key,
            "limit": limit,
            "offset": 0,
            "lastModifiedDate": f"[{last_modified_start.strftime('%m/%d/%Y')},{observed_at:%m/%d/%Y}]",
            "includeSections": "contractId,coreData,awardDetails,awardeeData",
        }
        if deleted:
            params["deletedStatus"] = "yes"
        records = self.partial_records
        seen: set[tuple[str, str | None]] = set()
        first_total: int | None = None
        total_presence: bool | None = None
        offset = 0
        try:
            for offset in range(max_pages):
                params["offset"] = offset  # SAM offset is a page index, not a row offset.
                data = await self.client.request_json("GET", self.URL, params=params)
                if not isinstance(data, dict) or not isinstance(data.get("awardSummary"), list):
                    raise ValueError("SAM response does not contain an awardSummary list")
                rows = data["awardSummary"]
                before = len(records)
                for row in rows:
                    record = self.normalizer.normalize(row, observed_at=observed_at, deleted=deleted)
                    key = (record.source_record_id, record.raw_payload_hash)
                    if key not in seen:
                        seen.add(key)
                        records.append(record)
                raw_total = data.get("totalRecords")
                total = None
                if raw_total is not None:
                    if (type(raw_total) is not int and not (isinstance(raw_total, str) and re.fullmatch(r"\d+", raw_total))) or int(raw_total) < 0:
                        raise ValueError("SAM totalRecords must be an exact nonnegative integer")
                    total = int(raw_total)
                if total_presence is None:
                    total_presence = total is not None
                    first_total = total
                elif (total is not None) != total_presence or total != first_total:
                    raise RuntimeError("SAM totalRecords changed during pagination; source completeness is unproven")
                if len(rows) > limit:
                    raise RuntimeError("SAM pagination returned more rows than the requested page limit")
                if rows and len(records) == before:
                    raise RuntimeError("SAM pagination repeated previously observed rows instead of advancing")
                if total is not None and len(records) > total:
                    raise RuntimeError("SAM totalRecords is smaller than the validated source observations")
                if total is not None and len(records) == total:
                    return records
                if not rows or len(rows) < limit:
                    if total is not None and len(records) < total:
                        raise RuntimeError("SAM returned an incomplete page before totalRecords was reached")
                    return records
            raise RuntimeError(f"SAM pagination exceeded max_pages={max_pages}; refusing to truncate silently")
        except Exception as exc:
            if records:
                raise IncompleteSAMCollection(str(exc), records=records, page=offset) from exc
            raise
