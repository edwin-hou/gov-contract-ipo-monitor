from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from contract_ipo_monitor.db import Database
from contract_ipo_monitor.tracking import IPOEvidence, IPOTracker


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def event(event_id="one", event_type="registration", *, at=NOW, registration="333-123", **kwargs):
    defaults = dict(
        event_id=event_id, event_type=event_type, issuer_name="Large Company Inc", cik="42",
        source="sec", source_kind="regulatory", source_url=f"https://www.sec.gov/Archives/{event_id}.htm",
        registration_id=registration, filed_at=at,
        is_ipo=event_type in {"registration", "amendment"}, offering_kind="ipo",
        proposed_price=250.0,
    )
    defaults.update(kwargs)
    return IPOEvidence(**defaults)


@pytest.fixture
def tracker(tmp_path):
    db = Database(tmp_path / "monitor.db")
    db.initialize()
    tracker = IPOTracker(db)
    tracker.initialize()
    return tracker


def test_tracks_large_ipo_without_contract_or_market_gate_and_deduplicates(tracker):
    evidence = event()
    assert tracker.record(evidence, observed_at=NOW)
    assert not tracker.record(evidence, observed_at=NOW + timedelta(hours=1))
    candidates = tracker.candidates()
    assert len(candidates) == 1
    assert candidates[0]["status"] == "filed"
    assert candidates[0]["ipo_confirmed"] is True
    assert candidates[0]["proposed_price"] == 250
    assert candidates[0]["cik"] == "0000000042"
    assert tracker.summary()["active_ipos"] == 1


def test_lifecycle_replay_uses_filed_order_instead_of_ingestion_order(tracker):
    # The monitor can encounter an EFFECT before downloading the original filing.
    tracker.record(event("effect", "effective", at=NOW + timedelta(days=1), is_ipo=False))
    tracker.record(event("first"))
    tracker.record(event("amendment", "amendment", at=NOW + timedelta(hours=1)))
    tracker.record(event("prospectus", "prospectus", at=NOW + timedelta(days=2), is_ipo=False))
    result = tracker.candidates()[0]
    assert result["status"] == "prospectus_filed"
    assert result["evidence_count"] == 4
    assert "completed" in result["limitations"][0]


def test_effectiveness_alone_does_not_make_ipo_candidate(tracker):
    tracker.record(event("effect", "effective", is_ipo=False))
    assert tracker.candidates() == []
    assert tracker.summary()["evidence"] == 1


def test_unrelated_resale_withdrawal_does_not_withdraw_ipo(tracker):
    tracker.record(event())
    tracker.record(event("resale", registration="333-999", is_ipo=False, offering_kind="resale"))
    tracker.record(event("rw", "withdrawn", registration="333-999", at=NOW + timedelta(days=2), is_ipo=False))
    result = tracker.candidates()
    assert len(result) == 1
    assert result[0]["status"] == "filed"
    assert result[0]["active"] is True


def test_scoped_withdrawal_does_not_resurrect_on_later_amendment(tracker):
    tracker.record(event())
    tracker.record(event("rw", "withdrawn", at=NOW + timedelta(days=1), is_ipo=False))
    tracker.record(event("late", "amendment", at=NOW + timedelta(days=2)))
    result = tracker.candidates()[0]
    assert result["status"] == "withdrawn"
    assert result["active"] is False
    assert tracker.summary()["withdrawn_ipos"] == 1


def test_amendment_withdrawal_does_not_withdraw_offering(tracker):
    tracker.record(event())
    tracker.record(event("aw", "amendment_withdrawn", at=NOW + timedelta(days=1), is_ipo=False))
    assert tracker.candidates()[0]["status"] == "filed"


def test_missing_registration_number_never_applies_issuer_wide_withdrawal(tracker):
    tracker.record(event())
    tracker.record(event("rw", "withdrawn", registration=None, at=NOW + timedelta(days=1), is_ipo=False))
    assert tracker.candidates()[0]["active"] is True
    assert tracker.summary()["evidence"] == 2


def test_distinct_ciks_share_name_without_collapsing_issuers(tracker):
    tracker.record(event("first", cik="42"))
    tracker.record(event("second", cik="43"))
    assert len(tracker.candidates()) == 2
    assert tracker.summary()["companies"] == 2


def test_commentary_cannot_upgrade_rumor_even_when_labeled_ipo(tracker):
    tracker.record(event(
        source="youtube", source_kind="commentary", source_url="https://youtu.be/example",
        event_type="rumor", cik=None, is_ipo=True,
    ))
    result = tracker.candidates()[0]
    assert result["status"] == "rumored"
    assert result["ipo_confirmed"] is False
    assert result["confidence"] == "unverified"


def test_non_sec_url_does_not_gain_regulatory_authority(tracker):
    tracker.record(event(source_url="https://sec.gov.evil.example/filing"))
    assert tracker.candidates()[0]["status"] == "rumored"


def test_naive_timestamps_rejected_instead_of_assuming_freshness():
    with pytest.raises(ValidationError, match="timezone"):
        event(at=datetime(2026, 10, 5))


def test_unclassified_registration_remains_uncertain_after_effect(tracker):
    tracker.record(event(is_ipo=False, offering_kind="unclassified"))
    tracker.record(event("effect", "effective", at=NOW + timedelta(days=1), is_ipo=False))
    result = tracker.candidates()[0]
    assert result["status"] == "registration_observed"
    assert result["ipo_confirmed"] is False


def test_unclassified_withdrawal_keeps_uncertainty(tracker):
    tracker.record(event(is_ipo=False, offering_kind="unclassified"))
    tracker.record(event("rw", "withdrawn", at=NOW + timedelta(days=1), is_ipo=False))
    assert tracker.candidates()[0]["status"] == "withdrawn_unclassified_registration"


def test_known_follow_on_is_retained_as_evidence_but_not_ipo_candidate(tracker):
    tracker.record(event(is_ipo=False, offering_kind="follow_on"))
    assert tracker.candidates() == []
    assert tracker.summary()["evidence"] == 1
