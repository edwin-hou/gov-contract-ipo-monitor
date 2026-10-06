from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
import json

import pytest

from contract_ipo_monitor.analyst import (AnalystStore, DEFAULT_MODEL, evaluate, notice_changed,
    notice_snapshot, prepare_candidates, semantic_key, validate_decisions)
from contract_ipo_monitor.quotes import CurrentQuote, quote_to_dict

NOW = datetime(2026, 10, 5, 22, tzinfo=UTC)


def report():
    quote = CurrentQuote("MU", "MU", "yahoo", "https://query1.finance.yahoo.com/v8/finance/chart/MU", 100,
        "USD", "NMS", "America/New_York", NOW.replace(hour=20, second=2), NOW,
        quote_type="session_close", market_phase="closed", delay_seconds=0, status="fresh",
        session_date=NOW.date(), timestamp_precision="second", timestamp_basis="last_trade", provider_market_status="Closed",
        regular_session_start=NOW.replace(hour=13, minute=30), regular_session_end=NOW.replace(hour=20))
    idea = {"symbol":"MU", "company":"Micron", "exchange":"NASDAQ", "currency":"USD", "action":"conditional_buy",
        "entry":101., "invalidation":95., "target":113., "current_quote":quote_to_dict(quote),
        "strategy":{"maximum_entry":102., "setup_valid_through":"2026-10-12"},
        "fundamentals":{"reported_at":"2026-09-30"},
        "evidence_briefs":[{"id":"financial","evidence_type":"financial","claim":"Reported revenue increased.",
            "meaning":"Supports the business thesis, not valuation.","source_urls":["https://investors.micron.com/results"]},
            {"id":"trend","evidence_type":"price_trend","claim":"Completed-bar trend is positive.",
            "meaning":"A conditional breakout is worth reviewing.","source_urls":["https://api.nasdaq.com/history"]}]}
    return {"completed_at":NOW.isoformat(), "trade_ideas":[idea]}


def decisions(symbol="MU", action="notify"):
    return {"decisions":[{"symbol":symbol,"decision":action,"evidence_ids":["financial","trend"],
            "rationale":"A conditional opportunity if the trigger and broker cost checks pass.",
            "counterargument":"Growth may already be reflected in the price."}]}


def infer(request):
    assert request["model"] == DEFAULT_MODEL and request["reasoning_effort"] == "medium"
    assert request["payload"]["account_context"]["holdings"] == []
    return {"status":"ok", "provider":"openai-codex", "model":DEFAULT_MODEL, "text":json.dumps(decisions()), "usage":{}}


def test_fresh_close_is_reference_and_normalized_exchange_matches():
    candidates, withheld = prepare_candidates(report(), now=NOW)
    assert len(candidates) == 1 and not withheld
    assert candidates[0]["current_quote"]["freshness"]["executable"] is False


@pytest.mark.parametrize("change,reason", [
    ({"price":103.}, "price_invalidates_or_exceeds_entry_cap"),
    ({"price":94.}, "price_invalidates_or_exceeds_entry_cap"),
    ({"price":97.}, "price_more_than_3_percent_below_trigger"),
    ({"observed_at":(NOW-timedelta(minutes=6)).isoformat()}, "fresh_current_quote_or_setup_expiry_missing"),
    ({"session_date":"2026-10-02", "quote_at":"2026-10-02T20:00:02+00:00"}, "fresh_current_quote_or_setup_expiry_missing"),
    ({"currency":"HKD"}, "fresh_current_quote_or_setup_expiry_missing"),
])
def test_price_failure_never_calls_model(tmp_path, change, reason):
    data=report(); data["trade_ideas"][0]["current_quote"].update(change)
    value=evaluate(data, AnalystStore(tmp_path/"analyst.db"), lambda _:pytest.fail("model called"),now=NOW)
    assert value["approved"] == [] and value["withheld"]["MU"] == reason


def test_cached_approval_rechecks_freshness_and_sent_baseline(tmp_path):
    calls=[]; store=AnalystStore(tmp_path/"analyst.db"); data=report()
    def model(request): calls.append(request); return infer(request)
    first=evaluate(data,store,model,now=NOW)
    assert len(first["approved"]) == 1
    assert len(evaluate(data,store,model,now=NOW+timedelta(minutes=1))["approved"]) == 1
    assert len(calls) == 1
    store.save_notice(first["approved"][0])
    assert evaluate(data,store,model,now=NOW+timedelta(minutes=2))["approved"] == []
    assert evaluate(data,store,model,now=NOW+timedelta(minutes=6))["status"] == "no_candidates"


def test_receipt_times_do_not_trigger_another_review_but_trigger_crossing_does():
    a=report()["trade_ideas"][0]; b=deepcopy(a)
    b["current_quote"]["observed_at"]=(NOW+timedelta(hours=1)).isoformat()
    assert semantic_key([a],DEFAULT_MODEL) == semantic_key([b],DEFAULT_MODEL)
    b["current_quote"]["price"]=101
    assert semantic_key([a],DEFAULT_MODEL) != semantic_key([b],DEFAULT_MODEL)
    assert notice_changed(notice_snapshot(a),b)


@pytest.mark.parametrize("alter", [
    lambda v:v["decisions"].append(deepcopy(v["decisions"][0])),
    lambda v:v["decisions"][0].update(symbol="UNKNOWN"),
    lambda v:v["decisions"][0].update(evidence_ids=["made-up"]),
    lambda v:v["decisions"][0].update(evidence_ids=[["unhashable"]]),
    lambda v:v["decisions"][0].update(evidence_ids=["financial"]),
    lambda v:v["decisions"][0].update(rationale="guaranteed profit"),
    lambda v:v["decisions"][0].update(entry=999),
])
def test_unverified_model_output_never_approves(tmp_path, alter):
    value=decisions(); alter(value)
    def model(_): return {**infer({"model":DEFAULT_MODEL,"reasoning_effort":"medium","payload":{"account_context":{"holdings":[]}}}),"text":json.dumps(value)}
    result=evaluate(report(),AnalystStore(tmp_path/"analyst.db"),model,now=NOW)
    assert result["approved"] == [] and result["status"] == "invalid_response"


def test_provider_failure_is_silent_and_crash_claim_does_not_repeat(tmp_path):
    store=AnalystStore(tmp_path/"analyst.db"); data=report(); calls=[]
    def failed(_): calls.append(1); raise TimeoutError()
    assert evaluate(data,store,failed,now=NOW)["approved"] == []
    evaluate(data,store,failed,now=NOW)
    assert len(calls) == 1
    changed=deepcopy(data); changed["trade_ideas"][0]["target"]=114
    key=semantic_key(changed["trade_ideas"],DEFAULT_MODEL)
    assert store.claim(key,DEFAULT_MODEL,NOW)["cached"] is False
    assert evaluate(changed,store,lambda _:pytest.fail("repeated ambiguous call"),now=NOW)["approved"] == []


def test_daily_budget_bounds_calls_across_separate_store_instances(tmp_path):
    path=tmp_path/"analyst.db"
    first=AnalystStore(path); second=AnalystStore(path)
    assert first.claim("one",DEFAULT_MODEL,NOW,daily_limit=1)["cached"] is False
    assert second.claim("two",DEFAULT_MODEL,NOW,daily_limit=1)["status"] == "daily_limit"
    assert second.claim("two",DEFAULT_MODEL,NOW+timedelta(days=1),daily_limit=1)["cached"] is False


@pytest.mark.parametrize("offset", [-5, 9])
def test_daily_claim_budget_uses_utc_day_for_aware_local_input(tmp_path, offset):
    store = AnalystStore(tmp_path / "analyst.db")
    at = datetime(2026, 10, 6, tzinfo=UTC).astimezone(timezone(timedelta(hours=offset)))
    assert store.claim("one", DEFAULT_MODEL, at, daily_limit=1)["cached"] is False
    assert store.claim("two", DEFAULT_MODEL, at, daily_limit=1)["status"] == "daily_limit"
    with store.connect() as connection:
        rows = connection.execute("SELECT claimed_at FROM analyst_calls").fetchall()
    assert [row["claimed_at"] for row in rows] == ["2026-10-06T00:00:00+00:00"]


@pytest.mark.parametrize("recorded_at,expected", [
    ("2026-10-05T19:00:00-05:00", "daily_limit"),
    ("2026-10-06T09:00:00+09:00", "daily_limit"),
    ("2026-10-05T18:59:59.999999-05:00", "claimed"),
    ("2026-10-06T18:59:59.999999-05:00", "daily_limit"),
    ("2026-10-06T19:00:00-05:00", "claimed"),
])
def test_existing_offset_claims_count_exact_utc_instants_without_rewriting_history(tmp_path, recorded_at, expected):
    store = AnalystStore(tmp_path / "analyst.db")
    with store.connect() as connection:
        connection.execute("INSERT INTO analyst_calls(identity,model,claimed_at,status) VALUES(?,?,?,?)",
                           ("legacy-offset", DEFAULT_MODEL, recorded_at, "unavailable"))
        original = dict(connection.execute("SELECT * FROM analyst_calls WHERE identity='legacy-offset'").fetchone())
    result = store.claim("new", DEFAULT_MODEL, datetime(2026, 10, 6, tzinfo=UTC), daily_limit=1)
    assert result["status"] == expected
    with store.connect() as connection:
        assert dict(connection.execute("SELECT * FROM analyst_calls WHERE identity='legacy-offset'").fetchone()) == original


def test_naive_claim_clock_cannot_spend_or_renew_budget(tmp_path):
    store = AnalystStore(tmp_path / "analyst.db")
    with pytest.raises(ValueError, match="Aware timestamp required"):
        store.claim("unknown-timezone", DEFAULT_MODEL, datetime(2026, 10, 6), daily_limit=1)
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM analyst_calls").fetchone()[0] == 0


@pytest.mark.parametrize("recorded_at", ["2026-10-06T00:00:00", "invalid"])
def test_unverifiable_durable_claim_times_cannot_authorize_another_attempt(tmp_path, recorded_at):
    store = AnalystStore(tmp_path / "analyst.db")
    with store.connect() as connection:
        connection.execute("INSERT INTO analyst_calls(identity,model,claimed_at,status) VALUES(?,?,?,?)",
                           ("unknown-time", DEFAULT_MODEL, recorded_at, "claimed"))
    with pytest.raises(ValueError, match="Invalid durable analyst claim timestamp"):
        store.claim("new", DEFAULT_MODEL, datetime(2026, 10, 6, tzinfo=UTC), daily_limit=1)
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM analyst_calls").fetchone()[0] == 1


def test_wrong_current_quote_symbol_never_calls_model(tmp_path):
    data = report(); data['trade_ideas'][0]['current_quote']['symbol'] = 'NVDA'
    result = evaluate(data, AnalystStore(tmp_path/'a.db'), lambda _:pytest.fail('identity mismatch called model'), now=NOW)
    assert result['approved'] == []


@pytest.mark.parametrize('field,value', [('world_context',[{'title':'New issuer-specific export restriction'}]),
    ('sentiment',{'label':'unknown','bias_flags':['new_source_gap']}), ('risks',['New counterevidence'])])
def test_material_context_changes_invalidate_cached_review(field,value):
    before = report()['trade_ideas'][0]; after = deepcopy(before); after[field] = value
    assert semantic_key([before], DEFAULT_MODEL) != semantic_key([after], DEFAULT_MODEL)


def test_uncitable_unknown_commentary_does_not_replace_required_evidence(tmp_path):
    data = report(); data['trade_ideas'][0]['evidence_briefs'].append({'id':'sample','evidence_type':'sampled_commentary',
        'claim':'Commentary is insufficient.','meaning':'No sentiment direction is established.','source_urls':[]})
    data['trade_ideas'][0]['sentiment'] = {'label':'unknown','score':None}
    candidates, _ = prepare_candidates(data,now=NOW)
    assert len(candidates[0]['evidence_briefs']) == 2 and candidates[0]['sentiment']['label'] == 'unknown'


def test_duplicate_json_keys_and_padded_text_cannot_approve(tmp_path):
    for index, raw in enumerate(['{"decisions":[],"decisions":'+json.dumps(decisions()['decisions'])+'}',
            json.dumps({'decisions':[{**decisions()['decisions'][0],'rationale':' '*1000+'A setup.'}]})]):
        result = evaluate(report(),AnalystStore(tmp_path/f'{index}.db'),lambda request:{'status':'ok','provider':'openai-codex',
            'model':DEFAULT_MODEL,'text':raw,'usage':{}},now=NOW)
        assert result['status'] == 'invalid_response' and result['approved'] == []


def test_cached_review_expiry_never_reposts_unchanged_evidence(tmp_path):
    store = AnalystStore(tmp_path/'a.db'); calls=[]
    def model(request): calls.append(1); return infer(request)
    assert evaluate(report(),store,model,now=NOW)['approved']
    later = NOW+timedelta(hours=6,seconds=1); data = report(); data['completed_at']=later.isoformat()
    data['trade_ideas'][0]['current_quote']['observed_at']=later.isoformat()
    result = evaluate(data,store,model,now=later)
    assert result['status']=='review_expired' and not result['approved'] and len(calls)==1


def test_packet_removes_repeated_calendar_windows_and_retains_provenance():
    from contract_ipo_monitor.analyst import packet
    idea = report()['trade_ideas'][0]
    idea['strategy']['timing']={'status':'conditional_window','entry_window':{'session_date':'2026-10-06'},
        'setup_expiry':{'session_date':'2026-10-12','repeated':'not needed'},
        'illustrative_review':{'session_date':'2026-10-13'},'illustrative_time_exit':{'session_date':'2026-10-27'},
        'anchor':'Recalculate after actual fill.'}
    value = packet([idea])['candidates'][0]
    assert value['strategy']['timing']['setup_expiry']=='2026-10-12'
    assert value['current_quote']['quote_at']==idea['current_quote']['quote_at']
    assert value['current_quote']['source_url']==idea['current_quote']['source_url']
    assert 'freshness' not in value['current_quote']


def test_oversized_packet_does_not_claim_usage_or_call_model(tmp_path):
    data=report(); data['trade_ideas'][0]['risks']=['x'*50000]
    store=AnalystStore(tmp_path/'a.db')
    value=evaluate(data,store,lambda _:pytest.fail('oversized input posted'),now=NOW)
    assert value['status']=='packet_oversized' and value['model_called'] is False
    with store.connect() as connection: assert connection.execute('SELECT COUNT(*) FROM analyst_calls').fetchone()[0]==0
