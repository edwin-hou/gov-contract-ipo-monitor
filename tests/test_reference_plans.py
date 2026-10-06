from copy import deepcopy

from contract_ipo_monitor.reference_plans import advance_reference_plans


def report(day="2026-10-05", close=99.0, action="conditional_buy", **overrides):
    idea = {"symbol": "TEST", "exchange": "NASDAQ", "currency": "USD", "action": action,
            "price_as_of": day, "entry": 100.0, "invalidation": 95.0, "target": 115.0,
            "indicators": {"last_close": close}, "strategy": {"maximum_entry": 102.0, "setup_valid_through": "2026-10-12"}}
    idea.update(overrides)
    return {"trade_ideas": [idea]}


def seed():
    plans, events = advance_reference_plans({}, report())
    assert not events
    assert not plans["TEST"]["assumed_position"]
    return plans


def test_same_bar_is_quiet_and_does_not_infer_a_fill():
    plans = seed()
    updated, events = advance_reference_plans(plans, report(close=101))
    assert updated == plans and not events
    assert plans["TEST"]["state"] == "awaiting_trigger"


def test_entry_reference_uses_frozen_levels_not_latest_moving_signal():
    plans = seed()
    moved = report("2026-10-06", 101, entry=110, invalidation=105, target=125)
    updated, events = advance_reference_plans(plans, moved)
    assert events[0]["kind"] == "entry_reference_reached"
    assert updated["TEST"]["entry"] == 100
    assert updated["TEST"]["state"] == "reference_triggered"
    assert not updated["TEST"]["assumed_position"]
    assert "not a recorded fill" in events[0]["message"]
    assert plans == seed()  # input state remains immutable


def test_entry_gap_is_not_chased_and_the_same_bar_does_not_reseed():
    plans, events = advance_reference_plans(seed(), report("2026-10-06", 103))
    assert events[0]["kind"] == "do_not_chase"
    assert plans["TEST"]["state"] == "closed"
    assert advance_reference_plans(plans, report("2026-10-06", 103)) == (plans, [])


def test_expired_setup_and_pre_entry_invalidation_are_cancelled():
    plans, events = advance_reference_plans(seed(), report("2026-10-13", 101))
    assert events[0]["kind"] == "setup_expired"
    assert plans["TEST"]["state"] == "closed"
    plans, events = advance_reference_plans(seed(), report("2026-10-06", 94))
    assert events[0]["kind"] == "setup_invalidated"
    assert not plans["TEST"]["assumed_position"]


def test_target_and_invalidation_are_conditional_exit_reviews():
    triggered, _ = advance_reference_plans(seed(), report("2026-10-06", 101))
    for close, kind in [(116, "target_reference_reached"), (94, "invalidation_reference_reached")]:
        plans, events = advance_reference_plans(triggered, report("2026-10-07", close))
        assert events[0]["kind"] == kind
        assert "If you entered" in events[0]["message"]
        assert plans["TEST"]["state"] == "closed"


def test_five_session_review_only_once_and_fifteen_session_exit():
    triggered, _ = advance_reference_plans(seed(), report("2026-10-06", 101))
    reviewed, events = advance_reference_plans(triggered, report("2026-10-13", 106))
    assert events[0]["kind"] == "five_session_review"
    assert advance_reference_plans(reviewed, report("2026-10-14", 107))[1] == []
    closed, events = advance_reference_plans(reviewed, report("2026-10-27", 108))
    assert events[0]["kind"] == "time_exit_reference"
    assert closed["TEST"]["state"] == "closed"


def test_missing_evidence_and_listing_change_withhold_previous_setup():
    for current, kind in [(report(action="wait"), "evidence_withdrawn"), (report(action="reduce_if_owned"), "evidence_withdrawn"), ({"trade_ideas": []}, "evidence_withdrawn"),
                          (report(exchange="NYSE", currency="EUR"), "listing_changed")]:
        plans, events = advance_reference_plans(seed(), current)
        assert events[0]["kind"] == kind
        assert plans["TEST"]["state"] == "closed"


def test_unknown_calendar_has_no_inferred_time_exit():
    triggered, _ = advance_reference_plans(seed(), report("2026-10-06", 101))
    triggered["TEST"]["exchange"] = "HKEX"
    plans, events = advance_reference_plans(triggered, report("2026-10-07", 105, exchange="HKEX"))
    assert events[0]["kind"] == "calendar_unavailable"
    assert plans["TEST"]["state"] == "closed"


def test_invalid_levels_and_wait_cannot_seed_a_reference_plan():
    for candidate in [report(action="wait"), report(invalidation=101), report(target=101), report(entry=float("nan"))]:
        assert advance_reference_plans({}, candidate) == ({}, [])
