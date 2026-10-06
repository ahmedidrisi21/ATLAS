"""Futures-first, end to end: one engine, one pipeline, the Tradovate adapter behind it (on the stand-in)."""

from __future__ import annotations

import datetime as dt

import pytest
from pathlib import Path

from atlas_engine.journal.audit import reconstruct

from futureshelp import F0, build
from t4help import Clock

UTC = dt.timezone.utc
THESIS = "MES held the overnight low and reclaimed VWAP; trend day setup with room to the prior high."


@pytest.fixture
def rig(tmp_path):
    r = build(tmp_path)
    r.step()
    r.enable()
    return r


def stages(out):
    return [(s["stage"], s["ok"]) for s in out["pipeline"]["stages"]]


def test_a_rule_signal_takes_the_one_pipeline_to_a_bracket(rig):
    out = rig.engine.submit(rig.signal())
    assert out["decision"] == "ALLOW" and out["outcome"] == "filled", out
    assert [s for s, _ in stages(out)] == ["system_state", "setup", "market", "strategy", "model", "ev", "risk",
                                         "sizing", "exposure", "prop", "execution"]
    # 0.25% of $50,000 = $125; a 10-point MES stop risks $50 a contract -> the engine sized 2.
    assert out["pipeline"]["volume"] == 2
    [p] = rig.venue.positions()
    assert (p.symbol, p.volume, p.sl) == ("MES", 2.0, 4990.25)


def test_the_journal_alone_reconstructs_the_futures_decision(rig):
    out = rig.engine.submit(rig.signal())
    a = reconstruct(rig.journal, out["decision_id"])["summary"]
    assert a["complete"] and a["decision"] == "ALLOW" and a["volume"] == 2
    m = a["market_state"]
    assert m["contract"]["symbol"] == "MESZ6" and m["session"]["phase"] == "rth"
    assert (m["bid"], m["ask"], m["tick_size"], m["tick_value"]) == (5000.0, 5000.25, 0.25, 1.25)
    assert a["risk"]["checks"]["prop"]["max_volume"] == 30  # 3 minis = 30 micros under the firm's limit
    assert a["model"]["model"] == "rules" and a["ev"]["ev_r"] > 0
    assert a["order"]["status"] == "filled" and a["fill"]["price"] == 5000.25


def test_closing_the_bracket_records_the_trade_in_r(rig):
    out = rig.engine.submit(rig.signal())
    rig.quote(4989.75)  # the stop is hit at the platform
    rig.step()
    a = reconstruct(rig.journal, out["decision_id"])["summary"]
    [closed] = a["closed"]
    assert closed["exit_reason"] == "sl" and closed["pnl"] == pytest.approx(2 * (4989.75 - 5000.25) * 5)
    assert closed["r"] == pytest.approx(-1.05, abs=0.01)  # a quarter point of slippage past the stop


@pytest.mark.parametrize("when,reason", [
    (dt.datetime(2026, 10, 10, 15, 0, tzinfo=UTC), "market_closed_weekend"),
    (dt.datetime(2026, 10, 6, 21, 30, tzinfo=UTC), "market_closed_maintenance"),
    (dt.datetime(2026, 11, 26, 15, 0, tzinfo=UTC), "exchange_holiday"),
])
def test_the_exchange_calendar_decides_whether_the_market_is_open(tmp_path, when, reason):
    r = build(tmp_path)
    r.step()
    r.enable()
    r.clock.t = when
    out = r.engine.submit(r.signal())
    assert out["decision"] == "REJECT" and out["pipeline"]["failed_stage"] == "market" and reason in out["reasons"]


def test_a_delayed_quote_takes_no_new_entry(rig):
    """A delayed data feed (NinjaTrader's free one is ~10 min behind) must not price a trade."""
    rig.clock.advance(seconds=600)
    out = rig.engine.submit(rig.signal())
    assert out["decision"] == "REJECT" and out["pipeline"]["failed_stage"] == "market"
    assert out["reasons"] == ["quote_not_live"]


def test_a_contract_due_to_roll_takes_no_new_entry(tmp_path):
    r = build(tmp_path, Clock(dt.datetime(2026, 12, 15, 15, 0, tzinfo=UTC)))
    r.step()
    r.enable()
    out = r.engine.submit(r.signal())
    assert out["decision"] == "REJECT" and out["reasons"] == ["contract_roll_due"]


def test_no_entries_close_to_the_firms_flat_by_time(rig):
    rig.clock.t = dt.datetime(2026, 10, 6, 19, 45, tzinfo=UTC)  # 14:45 CT, 25 min before 15:10
    rig.quote(5000.0, 5000.25)  # a live quote at the new time
    out = rig.engine.submit(rig.signal())
    assert out["decision"] == "REJECT" and out["pipeline"]["failed_stage"] == "prop"
    assert "prop_outside_trading_hours" in out["reasons"]


def test_the_engine_flattens_before_the_firm_does(rig):
    rig.engine.submit(rig.signal())
    rig.clock.t = dt.datetime(2026, 10, 6, 20, 0, tzinfo=UTC)  # 15:00 CT: 10 min before the firm's 15:10
    rig.step()
    assert rig.venue.positions() == []
    assert any(e["kind"] == "flattened" and e["reason"] == "prop_flat_by" for e in rig.engine.events()["events"])


def test_hermes_trades_mes_on_demo_through_the_same_pipeline(rig):
    out = rig.engine.agent_submit("atlas-trading/atlas-trading", intent_id="i-mes-1", symbol="MES", direction="buy",
                                  stop=4990.25, target=5020.25, confidence=0.5, thesis=THESIS)
    assert out["outcome"] == "filled", out
    assert out["volume"] == 2  # the engine chose the contracts, not the agent


def test_hermes_is_refused_on_a_live_futures_account(tmp_path):
    r = build(tmp_path, env="live")
    r.step()
    r.enable()
    out = r.engine.agent_submit("atlas-trading/atlas-trading", intent_id="i-mes-1", symbol="MES", direction="buy",
                                stop=4990.25, target=5020.25, confidence=0.5, thesis=THESIS)
    assert out["outcome"] == "refused" and out["reasons"] == ["not_a_demo_account"]
    assert rig_orders(r) == []


def rig_orders(r):
    return [c for c in r.fake.calls if c[0] == "POST" and c[1].startswith("/order/place")]


def test_a_kill_flattens_atlas_positions_and_leaves_the_operators(rig):
    rig.engine.submit(rig.signal())
    rig.fake.open_foreign("MESZ6", -1, 5001.0)
    rig.operator("kill", "drill: emergency flatten")
    left = rig.venue.positions()
    assert len(left) == 1 and left[0].magic == 0 and left[0].direction == -1  # the operator's own short
    assert not rig.engine.trading["enabled"]


@pytest.mark.parametrize("field", ["orderType", "orderQty", "contractId", "accountId", "accountSpec", "clOrdId",
                                   "isAutomated", "timeInForce", "bracket1", "stopPrice", "contracts", "qty"])
def test_an_intent_cannot_carry_a_broker_order_command(field):
    from atlas_engine.intents import IntentRejected, parse_intent

    raw = {"strategy": "trend_pullback", "strategy_version": "1", "symbol": "MES", "side": "BUY",
           "setup": {"entry": 5000.25, "stop": 4990.25, "target": 5020.25}, "reason": "test", field: 1}
    with pytest.raises(IntentRejected) as e:
        parse_intent(raw, now=F0, symbols=("MES",))
    assert e.value.code == "forbidden_field"


def test_the_engine_host_builds_ninjatrader_only_for_a_ninjatrader_config(tmp_path):
    import argparse

    from atlas_api import engine_cli
    from futureshelp import FUTURES_CONFIG

    args = argparse.Namespace(config=str(FUTURES_CONFIG), state=str(tmp_path), broker="fake", operator_key=None,
                              allow_writable_config=True)
    with pytest.raises(SystemExit, match="execution.platform: mt5"):
        engine_cli.build_engine(args)
    args.broker = "fake-ninjatrader"
    eng = engine_cli.build_engine(args)
    assert type(eng.execution).__name__ == "FuturesExecutionAdapter" and eng.execution.platform == "ninjatrader"
    assert eng.source == "engine:fake-ninjatrader"
    eng.journal.close()
    args.broker = "fake-tradovate"  # the same API under its other name: the same adapter, not a second one
    eng = engine_cli.build_engine(args)
    assert type(eng.broker).__name__ == "NinjaTraderAdapter"
    eng.journal.close()


def test_tradovate_is_an_alias_of_ninjatrader_in_the_config(tmp_path):
    from atlas_engine.execution import load_execution_settings
    from atlas_engine.execution.broker_api import PLATFORMS

    (tmp_path / "atlas.yaml").write_text((Path(__file__).resolve().parents[2] / "deploy" / "futures-config" / "atlas.yaml")
                                         .read_text().replace("platform: ninjatrader", "platform: tradovate"))
    assert load_execution_settings(tmp_path).platform == "ninjatrader"
    assert set(PLATFORMS) == {"mt5", "ninjatrader"}  # one futures adapter


def test_a_live_ninjatrader_run_needs_credentials_from_the_environment(tmp_path):
    from atlas_api.engine_cli import ninjatrader_venue
    from atlas_engine.config import load_engine_config
    from atlas_engine.execution import load_execution_settings
    from futureshelp import FUTURES_CONFIG

    cfg, s = load_engine_config(FUTURES_CONFIG), load_execution_settings(FUTURES_CONFIG)
    with pytest.raises(SystemExit, match="ATLAS_NINJATRADER_PASSWORD"):
        ninjatrader_venue("ninjatrader", cfg, s, tmp_path, env={})
