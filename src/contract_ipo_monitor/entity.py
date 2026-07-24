from __future__ import annotations

import re

from .models import ContractEvidence, EntityMatch, ListingSignal


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    value = value.upper()
    value = re.sub(r"\b(INCORPORATED|INC|CORPORATION|CORP|LIMITED|LLC|LTD|CO|COMPANY)\b", "", value)
    return re.sub(r"[^A-Z0-9]", "", value)


def _normalize_address(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


class EntityResolver:
    """Fail-closed resolver. Similar names alone never create an alertable match."""

    def resolve(self, contract: ContractEvidence, listing: ListingSignal) -> EntityMatch:
        if contract.recipient_uei and contract.recipient_uei in listing.linked_ueis:
            return EntityMatch(matched=True, method="uei", explanation="Award recipient UEI is explicitly linked to the issuer.")
        if contract.parent_uei and contract.parent_uei in listing.linked_ueis:
            return EntityMatch(matched=True, method="parent_uei", explanation="Award recipient parent UEI is explicitly linked to the issuer.")
        if contract.recipient_cage and contract.recipient_cage in listing.linked_cages:
            return EntityMatch(matched=True, method="cage", explanation="Award recipient CAGE code is explicitly linked to the issuer.")

        exact_name = _normalize(contract.recipient_name) == _normalize(listing.issuer_name)
        exact_address = (
            bool(contract.recipient_address and listing.issuer_address)
            and _normalize_address(contract.recipient_address) == _normalize_address(listing.issuer_address)
        )
        if exact_name and exact_address:
            return EntityMatch(matched=True, method="name_address", explanation="Exact normalized legal name and address match.")

        if listing.relationship_verified and contract.recipient_name.upper() in (listing.relationship_description or "").upper():
            return EntityMatch(matched=True, method="documented_relationship", explanation="Primary listing evidence documents the contractor-to-issuer relationship.")

        return EntityMatch(
            matched=False,
            method="none",
            explanation="No deterministic identifier, exact name-and-address match, or documented corporate relationship.",
        )
