"""The NinjaTrader MCP venue (the free demo route), on a stand-in answering in the recorded shapes."""

import datetime as dt

import pytest

from atlas_engine.adapters.ninjatrader import mcp
from atlas_engine.adapters.ninjatrader.mcp_venue import NinjaTraderMcpAdapter
from atlas_engine.adapters.orders import closing_order, entry_bracket
from atlas_engine.alerts import AlertOutbox
from atlas_engine.config import load_engine_config
from atlas_engine.execution import FuturesExecutionAdapter, load_execution_settings
from atlas_engine.journal import Journal
from atlas_engine.runtime import TradingEngine
from atlas_engine.strategies import Signal

from fake_mcp import ACCOUNT, FakeMcpNinjaTrader
from futureshelp import F0, FUTURES_CONFIG
from t4help import KEY, Clock, stand_in_models


def make(clock, tmp_path, slip_ticks=0):
    fake = FakeMcpNinjaTrader(clock, slip_ticks=slip_ticks)
    fake.set_quote("MESZ6", 5000.0, 5000.25)
    v = NinjaTraderMcpAdapter(fake, {"MES": "MESZ6"}, ledger_path=tmp_path / "brackets.json", poll_s=0, quote_s=0,
                              leg_wait_s=0, sleep=lambda s: None, today=lambda: clock().date())
    v.connect()
    return v, fake


class Rig:
    def __init__(self, tmp_path, slip_ticks=0):
        self.clock = Clock(F0)
        self.venue, self.fake = make(self.clock, tmp_path, slip_ticks)
        cfg = load_engine_config(FUTURES_CONFIG)
        settings = load_execution_settings(FUTURES_CONFIG)
        self.engine = TradingEngine(cfg, settings, self.venue, Journal(tmp_path / "journal.db", "run-test"),
                                    tmp_path / "state", now=self.clock,
                                    alerts=AlertOutbox(tmp_path / "alerts.jsonl", senders=[]), operator_key=KEY,
                                    broker_label="ninjatrader-mcp-demo", models=stand_in_models(),
                                    execution=FuturesExecutionAdapter(self.venue, settings, pause=lambda s: None))
        self.engine.start()

    def step(self, seconds=1.0):
        self.clock.advance(seconds=seconds)
        bid, ask, _ = self.fake.quotes["MESZ6"]
        self.fake.set_quote("MESZ6", bid, ask)
        return self.engine.step()

    def enable(self):
        import json

        from atlas_engine.operator import sign
        cmd = sign(KEY, "enable_trading", "yahye", "operator drill in the test suite", self.clock())
        (self.engine.inbox / f"{cmd['nonce']}.json").write_text(json.dumps(cmd))
        self.step()
        assert self.engine.trading["enabled"]

    def signal(self, direction=1, stop_points=10.0):
        bid, ask, _ = self.fake.quotes["MESZ6"]
        entry = ask if direction == 1 else bid
        return Signal("MES", "trend_pullback", "1", direction, entry - direction * stop_points,
                      self.clock().replace(second=0))


def test_it_reads_the_account_contract_and_quote_in_the_recorded_shapes(tmp_path):
    clock = Clock(F0)
    v, fake = make(clock, tmp_path)
    assert v.account_spec == ACCOUNT and v.contracts["MES"].symbol == "MESZ6"
    assert v.contracts["MES"].last_trade == dt.datetime(2026, 12, 18, 13, 30, tzinfo=dt.timezone.utc)
    a = v.account()
    assert a.demo is True and a.balance == a.equity == 50_000 and a.server == "ninjatrader-mcp-demo"
    t = v.tick("MES")
    assert (t.bid, t.ask, t.time) == (5000.0, 5000.25, F0)


def test_the_venue_refuses_a_read_only_client(tmp_path):
    clock = Clock(F0)
    with pytest.raises(ValueError, match="order-capable"):
        NinjaTraderMcpAdapter(FakeMcpNinjaTrader(clock, orders=False), {"MES": "MESZ6"})


def test_a_bracket_is_sent_as_offsets_from_the_quote_and_its_legs_are_found_linked(tmp_path):
    clock = Clock(F0)
    v, fake = make(clock, tmp_path)
    ack = v.submit(entry_bracket("MESZ6", 1, 1, 4990.25, 5020.25, "ATL-0000000000000001"))
    assert ack.ok and set(ack.ids) == {"entry", "stop", "target"}
    sent = [a for t, a in fake.calls if t == "place_order"][0]
    assert sent["brackets"] == [{"qty": 1, "stopLoss": -10.0, "profitTarget": 20.0}] and sent["timeInForce"] == "Day"
    orders = {o.id: o for o in v._orders()}
    stop, target = orders[ack.ids["stop"]], orders[ack.ids["target"]]
    assert (stop.type, stop.stop_price, target.type, target.price) == ("STOP", 4990.25, "LIMIT", 5020.25)
    assert stop.oco_id == target.oco_id is not None and stop.parent_id == ack.ids["entry"]


def test_a_short_brackets_offsets_are_signed_the_other_way(tmp_path):
    clock = Clock(F0)
    v, fake = make(clock, tmp_path)
    assert v.submit(entry_bracket("MESZ6", -1, 1, 5010.0, 4980.0, "ATL-0000000000000002")).ok
    sent = [a for t, a in fake.calls if t == "place_order"][0]
    assert sent["action"] == "Sell" and sent["brackets"] == [{"qty": 1, "stopLoss": 10.0, "profitTarget": -20.0}]


def test_the_engine_moves_slipped_legs_onto_the_decided_prices(tmp_path):
    rig = Rig(tmp_path, slip_ticks=2)  # the entry fills 2 ticks worse, so offsets land 2 ticks off
    rig.enable()
    out = rig.engine.submit(rig.signal())
    assert out["decision"] == "ALLOW", out
    b = rig.venue.ledger.active()[0]
    assert rig.venue.protection_problems(b) == []
    modified = [a for t, a in rig.fake.calls if t == "modify_order"]
    assert modified, "the legs were not moved onto the decided prices"
    legs = {o["orderType"]: o for o in rig.fake.book.values() if o.get("parent") and o["ordStatus"] == "Working"}
    assert legs["Stop"]["stopPrice"] == b.stop and legs["Limit"]["price"] == b.target
    assert not [t for t, _ in rig.fake.calls if t in ("close_position", "update_risk_settings")]


def test_a_stop_out_closes_the_position_and_cancels_the_target(tmp_path):
    rig = Rig(tmp_path)
    rig.enable()
    assert rig.engine.submit(rig.signal())["decision"] == "ALLOW"
    b = rig.venue.ledger.active()[0]
    rig.fake.set_quote("MESZ6", b.stop - 0.25, b.stop)
    rig.step()
    assert rig.venue.positions() == [] and rig.venue.ledger.active() == []
    assert not [o for o in rig.fake.book.values() if o["ordStatus"] == "Working"]


def test_legs_that_show_up_late_are_picked_up_not_replaced(tmp_path):
    clock = Clock(F0)
    v, fake = make(clock, tmp_path)
    fake.legs_hidden = 1
    ack = v.submit(entry_bracket("MESZ6", 1, 1, 4990.25, 5020.25, "ATL-0000000000000003"))
    assert ack.ok and ack.ids["stop"] is None and ack.ids["target"] is None
    from atlas_engine.adapters.futures_venue import Bracket
    b = Bracket("ATL-0000000000000003", "MES", "MESZ6", 1, 1, 0, 4990.25, 5020.25, F0.isoformat(), "live",
                ack.ids["entry"])
    v.ledger.put(b)
    assert v.protection_problems(b) == [] and b.stop_id and b.target_id


def test_a_lost_answer_is_found_by_shape_and_a_refusal_is_a_refusal(tmp_path):
    clock = Clock(F0)
    v, fake = make(clock, tmp_path)
    fake.lose_answer.add("place_order")
    ack = v.submit(entry_bracket("MESZ6", 1, 1, 4990.25, 5020.25, "ATL-0000000000000004"))
    assert not ack.ok and ack.unknown
    fake.status_override["place_order"] = {"status": "rejected", "error": {"reason": "RiskLimit",
                                                                            "message": "max exposure 2"}}
    ack = v.submit(entry_bracket("MESZ6", 1, 1, 4990.25, 5020.25, "ATL-0000000000000005"))
    assert not ack.ok and not ack.unknown and "max exposure" in ack.reason
    fake.status_override["place_order"] = {"status": "failed", "error": {"reason": "TrackingTimeout"}}
    assert v.submit(entry_bracket("MESZ6", 1, 1, 4990.25, 5020.25, "ATL-0000000000000006")).unknown


def test_an_exit_is_atlas_own_reduce_only_order_never_close_position(tmp_path):
    clock = Clock(F0)
    v, fake = make(clock, tmp_path)
    from atlas_engine.adapters.futures_venue import Bracket
    ack = v.submit(entry_bracket("MESZ6", 1, 1, 4990.25, 5020.25, "ATL-0000000000000007"))
    v.ledger.put(Bracket("ATL-0000000000000007", "MES", "MESZ6", 1, 1, 0, 4990.25, 5020.25, F0.isoformat(), "live",
                         ack.ids["entry"], ack.ids["stop"], ack.ids["target"]))
    assert not v.submit(closing_order("MESZ6", 1, 2, "MARKET", "ATL-0000000000000007-X1")).ok  # larger than held
    assert v.submit(closing_order("MESZ6", 1, 1, "MARKET", "ATL-0000000000000007-X1")).ok
    assert not [t for t, _ in fake.calls if t == "close_position"]


def test_no_standalone_oco_so_an_unprotected_position_is_closed(tmp_path):
    clock = Clock(F0)
    v, _ = make(clock, tmp_path)
    stop = closing_order("MESZ6", 1, 1, "STOP", "x-S2", 4990.0)
    target = closing_order("MESZ6", 1, 1, "LIMIT", "x-T2", 5020.0)
    ack = v._send_oco(stop, target)
    assert not ack.ok and not ack.unknown


def test_a_delayed_quote_from_the_venue_is_refused_by_the_pipeline(tmp_path):
    rig = Rig(tmp_path)
    rig.enable()
    rig.fake.set_quote("MESZ6", 5000.0, 5000.25, at=rig.clock() - dt.timedelta(minutes=10))
    out = rig.engine.submit(rig.signal())
    assert out["decision"] == "REJECT" and out["reasons"] == ["quote_not_live"]
    assert not [t for t, _ in rig.fake.calls if t == "place_order"]


def test_the_mcp_client_reaches_only_the_demo_server():
    assert set(mcp.SERVERS) == {"demo"}
    fake = FakeMcpNinjaTrader(lambda: F0)
    fake.oauth.resource = "https://mcp-live.tradovateapi.com/mcp"
    with pytest.raises(ValueError, match="demo server only"):
        NinjaTraderMcpAdapter(fake, {"MES": "MESZ6"})
