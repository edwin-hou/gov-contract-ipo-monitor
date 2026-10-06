"""Track frozen research levels against completed closes; never infer a fill."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import date


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def advance_reference_plans(previous: dict[str, dict], report: dict) -> tuple[dict, list[dict]]:
    from .trades import session_count, sessions_after, session_window
    plans, events = copy.deepcopy(previous), []
    ideas = {idea["symbol"]: idea for idea in report.get("trade_ideas", [])}
    for symbol, plan in plans.items():
        if plan.get("state") not in {"awaiting_trigger", "reference_triggered"}:
            continue
        idea = ideas.get(symbol)
        if not idea or idea.get("action") != "conditional_buy":
            events.append({"symbol": symbol, "plan_id": plan["id"], "kind": "evidence_withdrawn",
                           "message": "Required evidence no longer supports this research setup. Cancel the watch; if you entered independently, review the position using a fresh quote."})
            plan["state"] = "closed"
            continue
        if idea.get("exchange") != plan["exchange"] or idea.get("currency") != plan["currency"]:
            events.append({"symbol": symbol, "plan_id": plan["id"], "kind": "listing_changed",
                           "message": "Listing or trading currency changed; the previous levels are withheld pending review."})
            plan["state"] = "closed"
            continue
        priced = idea.get("price_as_of")
        close = (idea.get("indicators") or {}).get("last_close", (idea.get("metrics") or {}).get("close"))
        if not isinstance(priced, str) or not _number(close) or priced <= plan["last_price_date"]:
            continue
        current = date.fromisoformat(priced)
        plan["last_price_date"] = priced
        kind, message = None, None
        if plan["state"] == "awaiting_trigger":
            if priced > plan["valid_through"]:
                kind, message = "setup_expired", "The entry window expired without a verified fill. Cancel or research a new setup."
            elif close <= plan["stop"]:
                kind, message = "setup_invalidated", "The completed close crossed the original invalidation. Cancel the entry watch; no position is assumed."
            elif close > plan["maximum_entry"]:
                kind, message = "do_not_chase", "The completed close is above the entry cap. Skip this research entry and reassess; a fill is not assumed."
            elif close >= plan["entry"]:
                kind, message = "entry_reference_reached", "A completed close reached the frozen entry reference. Verify a fresh quote and the strategy before considering a buy; this is not a recorded fill."
                plan.update(state="reference_triggered", reference_trigger_date=priced)
                try:
                    plan["reference_review"] = session_window(plan["exchange"], sessions_after(current, 5, plan["exchange"]))
                    plan["reference_time_exit"] = session_window(plan["exchange"], sessions_after(current, 15, plan["exchange"]))
                    message += f" Paper-reference review: {plan['reference_review']['session_date']}; time exit: {plan['reference_time_exit']['session_date']}. Real position dates require the actual fill."
                except ValueError:
                    plan["reference_review"], plan["reference_time_exit"] = None, None
        else:
            try:
                age = session_count(date.fromisoformat(plan["reference_trigger_date"]), current, plan["exchange"])
            except ValueError:
                kind, message = "calendar_unavailable", "The session calendar cannot validate this reference horizon. Review manually; no timed trade decision is inferred."
                age = None
            if kind is None:
                if close <= plan["stop"]:
                    kind, message = "invalidation_reference_reached", "The completed close crossed the frozen invalidation. If you entered, review an exit promptly with a fresh quote; otherwise close the watch. Gaps can exceed the planned risk."
                elif close >= plan["target"]:
                    kind, message = "target_reference_reached", "The completed close reached the target reference. If you entered, review taking profit or exiting; no realized return or fill is claimed."
                elif age is not None and age >= 15:
                    kind, message = "time_exit_reference", "Fifteen completed sessions passed since the paper entry reference. Close or reassess the research idea; actual holding age must use your real fill date."
                elif age is not None and age >= 5 and not plan.get("five_session_review"):
                    kind, message = "five_session_review", "Review the research idea after five sessions from the paper entry reference. Verify news, price and risk; any actual holding age depends on your real fill date."
                    plan["five_session_review"] = True
        if kind:
            events.append({"symbol": symbol, "plan_id": plan["id"], "kind": kind, "price_date": priced,
                           "close": close, "currency": plan["currency"], "message": message,
                           "reference_review": plan.get("reference_review"), "reference_time_exit": plan.get("reference_time_exit"),
                           "scope": "paper_reference_only", "assumed_position": False})
            if kind not in {"entry_reference_reached", "five_session_review"}:
                plan["state"] = "closed"
    for symbol, idea in ideas.items():
        strategy = idea.get("strategy") or {}
        values = [idea.get(key) for key in ("entry", "invalidation", "target")]
        maximum, validity, priced = strategy.get("maximum_entry"), strategy.get("setup_valid_through"), idea.get("price_as_of")
        if (idea.get("action") != "conditional_buy" or not all(_number(value) for value in values)
                or not _number(maximum) or not isinstance(validity, str) or not isinstance(priced, str)):
            continue
        old = plans.get(symbol)
        if old and (old.get("state") != "closed" or priced <= old.get("last_price_date", "")):
            continue
        entry, stop, target = values
        if not 0 < stop < entry <= maximum < target:
            continue
        stable = [symbol, idea["exchange"], idea["currency"], entry, stop, target, maximum, validity, priced]
        identity = hashlib.sha256(json.dumps(stable).encode()).hexdigest()
        plans[symbol] = {"id": identity, "symbol": symbol, "exchange": idea["exchange"], "currency": idea["currency"],
                         "entry": entry, "stop": stop, "target": target, "maximum_entry": maximum,
                         "valid_through": validity, "last_price_date": priced, "state": "awaiting_trigger",
                         "scope": "paper_reference_only", "assumed_position": False}
    return plans, events
