import pytest

from contract_ipo_monitor.entity import EntityResolver
from test_gate import make_candidate


@pytest.mark.parametrize("fallback", ["name_address", "official_name", "cage"])
def test_conflicting_explicit_uei_cannot_be_overridden_by_weaker_identity(fallback):
    candidate = make_candidate(uei="AWARD-RECIPIENT")
    contract_updates = {}
    listing_updates = {"linked_ueis": ("OTHER-LEGAL-ENTITY",)}
    if fallback == "official_name":
        listing_updates["issuer_address"] = None
    elif fallback == "cage":
        contract_updates["recipient_cage"] = "1AB23"
        listing_updates["linked_cages"] = ("1AB23",)
    outcome = EntityResolver().resolve(candidate.contract.model_copy(update=contract_updates),
                                       candidate.listing.model_copy(update=listing_updates))
    assert not outcome.matched and outcome.method == "conflicting_official_identity"


@pytest.mark.parametrize("kind,expected", [("recipient", "uei"), ("parent", "parent_uei"),
                                           ("missing", "official_recipient_identity")])
def test_matching_recipient_parent_or_missing_id_official_name_bridge_still_resolves(kind, expected):
    candidate = make_candidate(uei="AWARD-RECIPIENT")
    contract_updates, listing_updates = {}, {}
    if kind == "parent":
        contract_updates["parent_uei"] = "ISSUER-PARENT"
        listing_updates.update(linked_ueis=("ISSUER-PARENT",), issuer_name="Parent issuer")
    elif kind == "missing":
        contract_updates["recipient_uei"] = None
        listing_updates["issuer_address"] = None
    outcome = EntityResolver().resolve(candidate.contract.model_copy(update=contract_updates),
                                       candidate.listing.model_copy(update=listing_updates))
    assert outcome.matched and outcome.method == expected


def test_verified_corporate_relationship_can_connect_distinct_legal_entities():
    candidate = make_candidate(uei="SUBSIDIARY-UEI")
    listing = candidate.listing.model_copy(update={"linked_ueis": ("PARENT-UEI",),
        "issuer_name": "Public parent", "relationship_verified": True,
        "relationship_description": "Acme Quantum, Inc. is a wholly owned subsidiary of Public parent."})
    outcome = EntityResolver().resolve(candidate.contract, listing)
    assert outcome.matched and outcome.method == "documented_relationship"


def test_corporate_suffixes_alone_are_not_a_legal_name_identity():
    candidate = make_candidate()
    contract = candidate.contract.model_copy(update={"recipient_uei": None, "recipient_name": "Company, Inc."})
    listing = candidate.listing.model_copy(update={"linked_ueis": (), "issuer_name": "LLC"})
    assert not EntityResolver().resolve(contract, listing).matched
