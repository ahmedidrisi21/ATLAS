"""NinjaTrader-first execution safety (docs/futures.md): the broker-neutral order, client order IDs and
idempotency, bracket protection that must verify or HALT, malformed answers, foreign orders, restarts,
lifecycle journaling and prop-policy versions. All on the NinjaTrader API stand-in; nothing reaches a network."""

from __future__ import annotations

import math

import pytest

from atlas_engine.adapters.futures_venue import VenueAck
from atlas_engine.adapters.ninjatrader import NinjaTraderError
from atlas_engine.adapters.orders import OrderRequest, closing_order, entry_bracket
from atlas_engine.execution import EntryOrder, ExecutionSettings, FuturesExecutionAdapter
from atlas_engine.journal.audit import reconstruct
from atlas_engine.models import ModelEstimate, ModelRegistry
from atlas_engine.prop_rules import load_prop_rules
from atlas_engine.prop_rules.policy import PolicyContext, PolicyTrade, StandardPropPolicy

from futureshelp import F0, FUTURES_CONFIG, build, venue
from t4help import TEST_PRIOR, Clock

SETTINGS = ExecutionSettings(platform="ninjatrader", contracts={"MES": "MESZ6"})
RULES = FUTURES_CONFIG / "prop_rules" / "fundednext_futures_flex_50k.yaml"
THESIS = "MES held the overnight low and reclaimed VWAP; trend day setup with room to the prior high."


@pytest.fixture
def t(tmp_path):
    clock = Clock(F0)
    v, fake = venue(clock, ledger=tmp_path / "brackets.json")
    v.connect()

    class T:
        pass

    x = T()
    x.clock, x.venue, x.fake, x.path = clock, v, fake, tmp_path / "brackets.json"
    x.ex = FuturesExecutionAdapter(v, SETTINGS, pause=lambda s: None)

    def quote(bid, ask=None):
        ask = bid + 0.25 if ask is None else ask
        fake.set_quote("MESZ6", bid, ask)
        v.quotes.apply(fake.quote_message("MESZ6", bid, ask))
        v.refresh()

    x.quote = quote
    quote(5000.0)
    return x


def order(decision="d1", direction=1, volume=2.0, stop=4990.0, target=5020.0, expected=5000.25):
    return EntryOrder(decision, "MES", "trend_pullback", direction, volume, stop, target, 26_090_001, expected)


def placed(fake):
    return [c for c in fake.calls if c[0] == "POST" and c[1].startswith("/order/place")]


# ---------------------------------------------------------------- the broker-neutral order

@pytest.mark.parametrize("over,problem", [
    ({"quantity": 1.5}, "quantity_not_whole_contracts"),
    ({"quantity": 0}, "quantity_not_whole_contracts"),
    ({"side": "LONG"}, "side_invalid"),
    ({"order_type": "LIMIT", "time_in_force": "GTC"}, "limit_price_invalid"),
    ({"order_type": "STOP", "stop_price": -1.0, "time_in_force": "GTC"}, "stop_price_invalid"),
    ({"order_type": "LIMIT", "limit_price": 5000.0}, "time_in_force_invalid"),
    ({"time_in_force": "GTC"}, "market_order_has_no_time_in_force"),
    ({"stop_loss": 5010.0, "take_profit": 4990.0}, "bracket_stop_and_target_crossed"),
    ({"stop_loss": 4990.0}, "bracket_needs_stop_and_target"),
    ({"stop_loss": 4990.0, "take_profit": 5020.0, "reduce_only": True}, "bracket_cannot_be_reduce_only"),
    ({"limit_price": math.nan, "order_type": "LIMIT", "time_in_force": "DAY"}, "limit_price_invalid"),
    ({"client_order_id": ""}, "client_order_id_invalid"),
])
def test_a_malformed_order_is_named_and_never_sent(t, over, problem):
    base = dict(symbol="MESZ6", side="BUY", quantity=1, order_type="MARKET", client_order_id="ATL-TEST")
    req = OrderRequest(**{**base, **over})
    assert problem in req.problems()
    ack = t.venue.submit(req)
    assert not ack.ok and ack.reason.startswith("invalid_order") and placed(t.fake) == []


def test_reduce_only_never_opens_or_grows_a_position(t):
    exit_ = closing_order("MESZ6", 1, 1, "MARKET", "ATL-X")  # sell 1 to close a long ATLAS does not hold
    ack = t.venue.submit(exit_)
    assert not ack.ok and "reduce_only_would_open_or_grow_a_position" in ack.reason and placed(t.fake) == []
    t.ex.submit_order(order(), F0)  # now ATLAS is long 2
    too_big = closing_order("MESZ6", 1, 3, "MARKET", "ATL-X2")
    assert "reduce_only" in t.venue.submit(too_big).reason
    wrong_side = closing_order("MESZ6", -1, 1, "MARKET", "ATL-X3")  # a buy against a long grows it
    assert "reduce_only" in t.venue.submit(wrong_side).reason


def test_the_bracket_order_carries_client_ids_on_every_leg(t):
    t.ex.submit_order(order(), F0)
    (b,) = t.venue.ledger.active()
    ids = {t.fake.orders[i]["clOrdId"] for i in (b.entry_id, b.stop_id, b.target_id)}
    assert ids == {b.client_id, b.client_id + "-S", b.client_id + "-T"}
    assert t.fake.orders[b.stop_id]["ocoId"] == t.fake.orders[b.target_id]["ocoId"] is not None


def test_entry_bracket_and_closing_orders_are_well_formed():
    assert entry_bracket("MESZ6", 1, 2.0, 4990.0, 5020.0, "ATL-A").problems() == []
    assert closing_order("MESZ6", 1, 2.0, "STOP", "ATL-A-S2", 4990.0).problems() == []
    assert "quantity_not_whole_contracts" in entry_bracket("MESZ6", 1, 2.5, 4990.0, 5020.0, "ATL-A").problems()


def test_an_order_request_cannot_come_from_an_intent():
    from atlas_engine.intents import IntentRejected, parse_intent

    for key in ("quantity", "order_type", "time_in_force", "client_order_id"):
        raw = {"strategy": "hermes", "strategy_version": "1", "symbol": "MES", "side": "BUY",
               "setup": {"stop": 4990.0, "target": 5020.0}, "reason": "x", key: 1}
        with pytest.raises(IntentRejected, match="forbidden_field"):
            parse_intent(raw, now=F0)


# ---------------------------------------------------------------- idempotency: request, timeout, retry

def test_request_timeout_retry_finds_the_order_by_client_id_and_never_sends_twice(t):
    t.fake.lose_answer_next = True  # the bracket reaches the platform; the answer is lost
    first = t.ex.submit_order(order(), F0)
    assert first.status == "filled"  # adopted by its client order ID
    again = t.ex.submit_order(order(), F0)  # the engine retries the same decision
    assert again.status == "duplicate"
    assert len(placed(t.fake)) == 1
    events = [e["event"] for e in t.ex.drain_events()]
    assert events.count("order_submitted") == 1 and "order_acknowledged" in events


def test_adoption_by_client_id_ignores_a_lookalike_order(t):
    t.fake.lose_answer_next = True
    t.fake.fill_market = False
    # Someone else's identical order (same contract, side, size, type) sits on the platform without our ID.
    t.fake._new_order({"symbol": "MESZ6", "action": "Buy", "orderQty": 2, "orderType": "Market"})
    r = t.ex.submit_order(order(), F0)
    (b,) = [x for x in t.venue.ledger.items.values()]
    assert t.fake.orders[b.entry_id]["clOrdId"] == b.client_id  # ours, not the look-alike
    assert r.status == "rejected"  # unfilled, so cancelled with its legs


def test_without_client_ids_the_shape_match_still_works(t):
    t.fake.forget_client_ids = True  # a platform that does not echo clOrdId
    t.fake.lose_answer_next = True
    assert t.ex.submit_order(order(), F0).status == "filled"
    assert len(placed(t.fake)) == 1


def test_a_restart_rebuilds_from_the_ledger_and_the_platform(t):
    t.ex.submit_order(order(), F0)
    v2, _ = venue(t.clock, fake=t.fake, ledger=t.path)
    v2.connect()
    ex2 = FuturesExecutionAdapter(v2, SETTINGS, pause=lambda s: None)
    (p,) = ex2.get_positions()
    assert p.comment == order().client_id and p.sl == 4990.0 and ex2.findings() == []
    assert ex2.submit_order(order(), F0).status == "duplicate"


# ---------------------------------------------------------------- bracket protection

def test_unlinked_legs_are_replaced_with_a_linked_pair(t):
    t.fake.unlinked_brackets = True  # the platform's legs come back without a shared OCO id
    r = t.ex.submit_order(order(), F0)
    assert r.status == "filled" and "stop_and_target_not_oco_linked" in r.reason
    (b,) = t.venue.ledger.active()
    assert t.venue.protection_problems(b) == []
    assert ("POST", "/order/placeoco") in t.fake.calls
    # The old legs were cancelled after the new pair was in place: only the linked pair works now.
    working = [o for o in t.ex.get_orders() if o["role"] in ("stop", "target")]
    assert sorted(o["ticket"] for o in working) == sorted([b.stop_id, b.target_id])


def test_leg_quantity_drift_is_found_and_fixed_by_reconciliation(t):
    t.ex.submit_order(order(), F0)
    (b,) = t.venue.ledger.active()
    t.fake.versions[b.stop_id]["orderQty"] = 1  # someone shrank the stop at the platform
    t.venue.refresh()
    assert t.venue.protection_problems(b) == ["stop_qty_1_vs_position_2"]
    assert t.ex.findings() == []  # repaired in place, so nothing is left to HALT on
    assert t.venue.protection_problems(b) == []


def test_protection_that_cannot_be_verified_is_a_halt_finding(t):
    t.ex.submit_order(order(), F0)
    (b,) = t.venue.ledger.active()
    t.fake.orders[b.stop_id]["ordStatus"] = "Canceled"
    t.venue.refresh()
    t.venue._send_oco = lambda stop, target: VenueAck(False, reason="refused")
    (f,) = t.ex.findings()
    assert f["kind"] == "unprotected_position" and "stop_missing" in f["detail"]


def test_a_foreign_working_order_on_a_traded_contract_is_a_finding(t):
    t.fake._new_order({"symbol": "MESZ6", "action": "Sell", "orderQty": 1, "orderType": "Limit", "price": 5100.0})
    (f,) = t.ex.findings()
    assert f["kind"] == "foreign_order" and f["symbol"] == "MES"
    assert t.ex.cancel_order(f["ticket"]).status == "rejected"  # never ATLAS' to cancel
    assert t.ex.cancel_all_orders() == []


# ---------------------------------------------------------------- malformed answers and connection

def test_malformed_platform_rows_raise_broker_unavailable(t):
    t.fake.malformed.add("/order/list")
    t.venue.refresh()
    with pytest.raises(NinjaTraderError, match="malformed order data"):
        t.venue.positions()


def test_a_malformed_command_answer_is_an_unknown_outcome(t):
    real = t.fake._command
    t.fake._command = lambda path, b: {**real(path, b), "orderId": "garbage"}
    r = t.ex.submit_order(order(), F0)  # the answer is unusable, so the order is looked up by its client ID
    assert r.status == "filled" and len(placed(t.fake)) == 1


def test_disconnect_reconnect_and_health(t):
    h = t.venue.health()
    assert h["connected"] and h["platform"] == "ninjatrader" and h["quote_age_s"]["MESZ6"] == 0.0
    assert h["not_front"] == []
    t.venue.disconnect()
    assert not t.venue.connected() and t.venue.client.token is None
    t.venue.reconnect()
    assert t.venue.connected() and t.venue.contracts["MES"].front is True


def test_account_margin_and_balance_come_from_the_snapshot(t):
    t.ex.submit_order(order(), F0)
    assert t.venue.get_balance() == 50_000.0
    assert t.venue.get_margin() == {"initial": None, "maintenance": None, "auto_liquidation_level": None}


def test_credentials_never_appear_in_errors_or_repr(t):
    from futureshelp import CREDS

    assert "secret-sec" not in repr(CREDS) and "pw" not in repr(CREDS).replace("Credentials", "")
    t.fake.malformed.add("/account/list")
    try:
        t.venue.reconnect()
    except Exception as e:  # noqa: BLE001
        assert "secret-sec" not in str(e) and "'pw'" not in str(e)


# ---------------------------------------------------------------- the engine: HALT, journal, policy

@pytest.fixture
def rig(tmp_path):
    r = build(tmp_path)
    r.step()
    r.enable()
    return r


def events(r, decision_id=None):
    rows = r.journal.rows("execution_events", 5000, **({"decision_id": decision_id} if decision_id else {}))
    return [e["event"] for e in rows]


def test_every_step_of_a_trade_is_journaled_in_order(rig):
    out = rig.engine.submit(rig.signal())
    ev = events(rig, out["decision_id"])
    want = ["trade_intent_created", "validation_started", "validation_completed", "probability_requested",
            "probability_returned", "ev_calculated", "risk_calculated", "size_calculated", "exposure_checked",
            "prop_policy_checked", "execution_requested", "order_submitted", "order_acknowledged", "fill_received",
            "stop_attached", "target_attached", "position_opened"]
    assert ev == want
    rig.quote(4989.75)  # the stop fills at the platform
    rig.step()
    assert events(rig, out["decision_id"])[-1] == "position_closed"


def test_reconciliation_and_halts_are_journaled(rig):
    ev = events(rig)
    assert "reconciliation_started" in ev and "reconciliation_completed" in ev
    rig.fake._new_order({"symbol": "MESZ6", "action": "Buy", "orderQty": 1, "orderType": "Limit", "price": 4900.0})
    rig.venue.refresh()
    rig.engine.reconcile()
    h = rig.step()
    assert h["state"] == "HALT"
    halted = [e for e in rig.journal.rows("execution_events", 5000) if e["event"] == "halted"]
    assert halted and "reconciliation_mismatch" in halted[-1]["reasons"]
    assert rig.engine.submit(rig.signal())["decision"] == "HALT"


def test_an_unprotected_fill_is_closed_reconciled_at_once_and_alerted(rig):
    rig.fake.drop_stop_after_fill = True
    rig.venue._send_oco = lambda stop, target: VenueAck(False, reason="refused")
    before = rig.engine.last_reconcile
    out = rig.engine.submit(rig.signal())
    assert out["outcome"] == "unprotected_closed"
    assert rig.venue.positions() == [] and rig.engine.last_reconcile != before
    ev = events(rig, out["decision_id"])
    assert "protection_failed" in ev and "position_closed" in ev and "position_opened" not in ev


def test_unreadable_broker_state_halts_instead_of_reusing_a_stale_copy(rig):
    rig.fake.malformed.add("/order/list")
    rig.venue.refresh()
    report = rig.engine.reconcile()
    assert [m["kind"] for m in report["mismatches"]] == ["broker_state_unreadable"]
    assert rig.step()["state"] == "HALT"
    rig.fake.malformed.clear()
    rig.venue.refresh()
    rig.engine.reconcile()
    assert rig.step()["state"] != "HALT"  # it clears once the platform answers properly again


def test_a_dead_quote_stream_halts_after_the_disconnect_limit(rig):
    class Dead:
        error = "ConnectionResetError"

        def alive(self):
            return False

        def stop(self):
            pass

    rig.venue.stream = Dead()
    rig.step()
    rig.clock.advance(seconds=61)
    h = rig.step()
    assert h["state"] == "HALT", h


def test_a_kill_is_journaled_and_no_agent_can_lift_it(rig):
    rig.engine.submit(rig.signal())
    rig.operator("kill", "drill: emergency flatten")
    assert rig.venue.positions() == [] and not rig.engine.trading["enabled"]
    assert "killed" in events(rig)
    out = rig.engine.agent_submit("atlas-trading/atlas-trading", intent_id="i-after-kill", symbol="MES",
                                  direction="buy", stop=4990.25, target=5020.25, confidence=0.5, thesis=THESIS)
    assert out["outcome"] != "filled"


def test_the_prop_policy_version_is_journaled_and_changes_with_the_rules(rig, tmp_path):
    out = rig.engine.submit(rig.signal())
    a = reconstruct(rig.journal, out["decision_id"])["summary"]
    v = a["risk"]["checks"]["prop"]["version"]
    assert v.startswith("2026-10-05:") and len(v.split(":")[1]) == 12
    edited = tmp_path / "rules.yaml"
    edited.write_text(RULES.read_text().replace("min_bracket_ticks: 6", "min_bracket_ticks: 8"))
    assert StandardPropPolicy(load_prop_rules(edited)).version != v
    assert StandardPropPolicy(load_prop_rules(RULES)).version == v  # deterministic


def test_a_product_the_firm_does_not_list_is_refused(tmp_path):
    f = tmp_path / "rules.yaml"
    f.write_text(RULES.read_text().replace("  min_bracket_ticks: 6", "  min_bracket_ticks: 6\n  products: [ES, NQ]"))
    pol = StandardPropPolicy(load_prop_rules(f))
    ctx = PolicyContext(F0, "evaluation", "challenge", 50_000.0, 0.0, ())
    assert "prop_instrument_not_allowed" in pol.entry_reasons(PolicyTrade("MES", 1, 5000.0, 4990.0, 5020.0, 0.25), ctx)
    assert "prop_instrument_not_allowed" not in pol.entry_reasons(PolicyTrade("ES", 1, 5000.0, 4990.0, 5020.0, 0.25),
                                                                  ctx)


def test_market_states_record_where_the_price_came_from(rig):
    out = rig.engine.submit(rig.signal())
    m = reconstruct(rig.journal, out["decision_id"])["summary"]["market_state"]
    d = m["data"]
    assert (d["source"], d["mode"], d["contract"], d["adjustment"], d["quality"]) == \
        ("fake-ninjatrader", "paper", "MESZ6", "none", "ok")


# ---------------------------------------------------------------- end to end

class JevStandIn:
    """Jev's bounded answer only: p_target_first, regime, reason codes, model version. No quantity."""

    name, version = "jev", "jev-1.13.0"

    def evaluate_setup(self, setup, state):
        return ModelEstimate(0.62, "jev", self.version, "iso-test", regime="trend_normal_vol",
                             reason_codes=("TREND_ALIGNMENT",))


def test_hermes_idea_to_reconciled_journal_end_to_end(tmp_path):
    from atlas_engine.setups import SETUPS

    models = ModelRegistry.default({s: TEST_PRIOR for s in [*SETUPS, "hermes"]}, {"hermes": "jev"},
                                   extra=[JevStandIn()])
    r = build(tmp_path, models=models)
    r.step()
    r.enable()
    out = r.engine.agent_submit("atlas-trading/atlas-trading", intent_id="i-e2e-1", symbol="MES", direction="buy",
                                stop=4990.25, target=5020.25, confidence=0.9, thesis=THESIS)
    assert out["outcome"] == "filled", out
    did = out["decision_id"]
    a = reconstruct(r.journal, did)
    s = a["summary"]
    # Jev gave the probability; the engine chose the contracts from the risk limit and the stop in ticks.
    assert s["model"]["model"] == "jev" and s["model"]["p_target_first"] == pytest.approx(0.62)
    assert s["volume"] == 2  # $125 risk / (40 ticks x $1.25) = 2.5 -> 2 contracts
    assert s["risk"]["checks"]["prop"]["version"]
    assert s["order"]["status"] == "filled" and s["fill"]["volume"] == 2.0
    report = r.engine.reconcile()
    assert report["status"] == "clean", report
    ev = events(r, did)
    assert ev[0] == "trade_intent_created" and "prop_policy_checked" in ev and ev[-1] == "position_opened"
    submitted = next(e for e in r.journal.rows("execution_events", 5000, decision_id=did)
                     if e["event"] == "order_submitted")
    req = submitted["request"]
    assert (req["symbol"], req["side"], req["quantity"], req["order_type"]) == ("MESZ6", "BUY", 2, "MARKET")
    assert req["trade_intent_id"] == did and req["stop_loss"] == 4990.25
    assert "password" not in str(a) and "secret-sec" not in str(a)
    r.quote(5020.25)  # the target fills at the platform
    r.step()
    assert events(r, did)[-1] == "position_closed"
    assert reconstruct(r.journal, did)["summary"]["closed"][0]["exit_reason"] == "tp"


def test_a_jev_scored_hermes_idea_is_still_demo_only(tmp_path):
    from atlas_engine.intents import TradeIntent
    from atlas_engine.setups import SETUPS

    models = ModelRegistry.default({s: TEST_PRIOR for s in [*SETUPS, "hermes"]}, {"hermes": "jev"},
                                   extra=[JevStandIn()])
    r = build(tmp_path, env="live", models=models)
    r.step()
    r.enable()
    bid, ask = r.fake.quotes["MESZ6"]
    intent = TradeIntent("hermes", "1", "MES", 1, 4990.25, r.clock().replace(microsecond=0), source="agent",
                         target=5020.25, reason=THESIS, p_estimate=0.9)
    out = r.engine.submit(intent)  # straight into the engine, past the agent route's own demo check
    assert out["decision"] == "REJECT" and out["reasons"] == ["agent_trade_needs_demo_account"]
    assert placed(r.fake) == []
