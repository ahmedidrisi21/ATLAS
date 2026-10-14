"""The demo practice run (docs/demo-practice.md): its config, its demo-only guards, the bars it decides on,
its report card, and an end-to-end rehearsal on the NinjaTrader MCP stand-in.

The opening-range rule is a MACHINERY TEST, not a strategy expected to profit (-0.012R after costs in its
backtest). Nothing here talks to NinjaTrader.
"""

from __future__ import annotations

import argparse
import datetime as dt

import pytest

from atlas_api import engine_cli
from atlas_engine.adapters.broker import AccountInfo
from atlas_engine.config import load_engine_config
from atlas_engine.decisions.ev import EVGate
from atlas_engine.execution import load_execution_settings
from atlas_engine.journal.practice import PASS, practice_report, render
from atlas_engine.models import PracticeModel
from atlas_engine.strategies import Signal, load_strategies, m1_frame

import practice_rehearsal as PR
from fake_mcp import FakeMcpNinjaTrader

UTC = dt.timezone.utc
DAYS = [(dt.date(2026, 10, 6), "target"), (dt.date(2026, 10, 7), "stop"), (dt.date(2026, 10, 8), "flat_by")]
THESIS = "Rehearsal: price stalled under the morning high after the opening-range move; fading it."


# -- the config -------------------------------------------------------------------------------------------

def test_the_practice_config_is_demo_only_one_contract_a_trade_two_in_all():
    cfg = load_engine_config(PR.PRACTICE_CONFIG)
    s = load_execution_settings(PR.PRACTICE_CONFIG)
    assert cfg.symbols == ("MES",) and cfg.mode == "paper" and cfg.initial_balance == 50_000
    assert s.demo_only is True and s.platform == "ninjatrader" and s.contracts == {"MES": "MESZ6"}
    assert cfg.prop.max_lot == 1  # 1 MES per trade, whichever source
    assert cfg.prop.futures.max_minis * cfg.prop.futures.micros_per_mini == pytest.approx(2)  # 2 MES in all
    assert cfg.prop.futures.products == ("MES",)
    # 14:55 Chicago = 15:55 New York, the backtest's exit
    assert (cfg.prop.futures.flat_by, s.flatten_lead_min) == (dt.time(15, 5), 10)
    (orb,) = load_strategies(PR.PRACTICE_CONFIG)
    assert orb.setup.name == "opening_range_breakout" and orb.decision_model == "practice"
    assert orb.params == {"range_minutes": 15, "sl_range_frac": 1.0} and orb.rr == 2.0
    text = (PR.PRACTICE_CONFIG / "strategies" / "mes_orb_practice.yaml").read_text()
    assert "MACHINERY TEST, NOT A STRATEGY EXPECTED TO PROFIT" in text


def test_the_practice_config_lives_outside_config_and_holds_no_secret():
    repo = PR.PRACTICE_CONFIG.parents[1]
    assert PR.PRACTICE_CONFIG.parent.name == "deploy"
    for p in PR.PRACTICE_CONFIG.rglob("*"):
        if p.is_file():
            text = p.read_text().lower()
            assert not any(w in text for w in ("access_token", "refresh_token", "password", "client_secret")), p
    assert not (repo / "config" / "strategies" / "mes_orb_practice.yaml").exists()


# -- the practice model and the demo-only guards ----------------------------------------------------------

@pytest.mark.parametrize("target_r,cost_r", [(2.0, 0.0), (2.0, 0.031), (1.83333, 0.04567), (3.14159, 0.12345)])
def test_the_practice_model_states_just_enough_to_clear_the_gate_and_says_it_is_no_estimate(target_r, cost_r):
    est = PracticeModel(0.15).evaluate_setup({"target_r": round(target_r, 4), "cost_r": round(cost_r, 4)}, {})
    assert est.ok and est.reason_codes == ("MACHINERY_TEST_NOT_AN_ESTIMATE",)
    assert EVGate(0.15).decide(est.p_target_first, target_r, cost_r).take
    assert EVGate(0.15).decide(est.p_target_first, target_r, cost_r).ev_r < 0.151


def _signal(r: PR.Rehearsal, direction: int = 1) -> Signal:
    bid, ask, _ = r.fake.quotes[PR.CONTRACT]
    entry = ask if direction == 1 else bid
    return Signal("MES", "opening_range_breakout", "practice-1", direction, entry - direction * 10.0,
                  r.clock().replace(second=0), 2.0, 1)


def _live_account(venue):
    real = venue.account

    def account():
        a = real()
        return AccountInfo(a.login, "ninjatrader-live", a.currency, a.balance, a.equity, a.trade_allowed, False)
    venue.account = account


@pytest.fixture
def quiet_day():
    bars = PR.synthetic_bars([(dt.date(2026, 10, 6), "stop")])
    return bars, PR.ny(dt.date(2026, 10, 6), 10, 30)


def test_the_engine_refuses_every_trade_unless_the_broker_reports_a_demo_account(tmp_path, quiet_day):
    bars, at = quiet_day
    r = PR.build(tmp_path, bars, at)
    _live_account(r.venue)
    r.minute(at + dt.timedelta(minutes=1))
    out = r.engine.submit(_signal(r))
    assert out["decision"] == "REJECT" and "demo_only_needs_demo_account" in out["reasons"]
    assert out["pipeline"]["failed_stage"] == "system_state"
    assert not [t for t, _ in r.fake.calls if t == "place_order"]


def test_without_demo_only_the_practice_model_still_refuses_a_live_account(tmp_path, quiet_day):
    bars, at = quiet_day
    r = PR.build(tmp_path, bars, at, demo_only=False)
    _live_account(r.venue)
    r.minute(at + dt.timedelta(minutes=1))
    out = r.engine.submit(_signal(r))
    assert out["decision"] == "REJECT" and out["reasons"] == ["practice_model_needs_demo_account"]
    assert not [t for t, _ in r.fake.calls if t == "place_order"]


def test_on_the_demo_account_the_practice_trade_goes_out_as_one_protected_contract(tmp_path, quiet_day):
    bars, at = quiet_day
    r = PR.build(tmp_path, bars, at)
    r.minute(at + dt.timedelta(minutes=1))
    out = r.engine.submit(_signal(r))
    assert out["decision"] == "ALLOW" and out["outcome"] == "filled", out["reasons"]
    assert out["pipeline"]["model"]["model"] == "practice" and out["pipeline"]["volume"] == 1
    (pos,) = r.engine.book
    assert pos.volume == 1 and pos.stop < pos.entry < pos.target


def test_a_live_ninjatrader_environment_is_refused_at_start(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_NINJATRADER_ENV", "live")
    args = argparse.Namespace(config=str(PR.PRACTICE_CONFIG), state=str(tmp_path), broker="ninjatrader",
                              allow_writable_config=True, operator_key=None)
    with pytest.raises(SystemExit, match="demo only"):
        engine_cli.build_engine(args)


# -- bars from the MCP server -----------------------------------------------------------------------------

def test_the_mcp_venue_serves_one_minute_bars_quoted_at_one_tick(tmp_path, quiet_day):
    bars, at = quiet_day
    r = PR.build(tmp_path, bars, at, enable=False)
    rates = r.venue.m1_rates("MES", 100_800)
    call = [a for t, a in r.fake.calls if t == "market_history"][-1]
    assert call == {"symbol": "MESZ6", "barType": "Minute", "barSize": 1, "count": 5000}
    assert len(rates) == 5000 or len(rates) == len([b for b in bars if b["t"] < at])
    m1 = m1_frame(rates, None)
    last = [b for b in bars if b["t"] < at][-1]
    assert m1.index[-1] == last["t"]
    assert m1["bid_c"].iloc[-1] == last["close"] - 0.125 and rates["spread"][-1] == 1.0  # mid = traded price
    assert r.venue.bars_feed_mode == "RealTime"


def test_a_signal_source_waits_for_the_closing_bar_before_deciding(tmp_path, quiet_day):
    bars, _ = quiet_day
    at = PR.ny(dt.date(2026, 10, 6), 10, 0)
    r = PR.build(tmp_path, [b for b in bars if b["t"] < at - dt.timedelta(minutes=1)], at, enable=False)
    (src,) = r.engine.sources
    r.clock.t = at + dt.timedelta(seconds=5)
    assert src.poll(r.venue, r.clock()) == [] and src.last_bar == {}  # the 09:59 bar isn't served yet: retry
    r.fake.bars[PR.CONTRACT] = bars
    out = src.poll(r.venue, r.clock())
    assert src.last_bar  # decided once the bar arrived
    assert [s.direction for s in out] == [-1]  # the "stop" day breaks down at the 10:00 close


# -- preflight -------------------------------------------------------------------------------------------

def test_preflight_says_a_delayed_feed_is_not_ready(tmp_path, quiet_day):
    bars, at = quiet_day
    fake = FakeMcpNinjaTrader(lambda: at)
    fake.bars[PR.CONTRACT] = bars
    fake.set_quote(PR.CONTRACT, 6000.0, at=at)
    ok = engine_cli.preflight(fake, PR.CONTRACT, now=at)
    assert ok["ready"] and ok["quote_feed"] == "RealTime" and ok["bars_returned"] >= 1000
    fake.feed_mode = "Delayed"
    fake._market_snapshot = lambda a: {"snapshots": [{"symbol": PR.CONTRACT, "bidPrice": 1, "askPrice": 1.25,
                                                      "timestamp": "2026-10-06T14:20:00Z",
                                                      "dataFeedMode": "Delayed"}]}
    bad = engine_cli.preflight(fake, PR.CONTRACT, now=at)
    assert not bad["ready"] and "quote_not_live" in bad["problems"][0] and bad["quote_age_s"] == 600
    assert not [t for t, _ in fake.calls if t in ("place_order", "modify_order", "cancel_order")]


# -- the rehearsal: three sessions end to end -------------------------------------------------------------

@pytest.fixture(scope="module")
def rehearsal(tmp_path_factory):
    root = tmp_path_factory.mktemp("practice")
    r = PR.build(root, PR.synthetic_bars(DAYS), PR.ny(DAYS[0][0], 8, 55))
    agent: dict = {}

    def hermes(rig: PR.Rehearsal, t: dt.datetime) -> None:
        bid, ask, _ = rig.fake.quotes[PR.CONTRACT]
        local = t.astimezone(PR.NY)
        if (local.date(), local.hour, local.minute) == (DAYS[0][0], 12, 30):  # the rule's trade is done: room
            agent["first"] = rig.engine.agent_submit("atlas-trading", "practice-hermes-1", "MES", "sell",
                                                     bid + 6.0, bid - 12.0, 0.6, THESIS)
        if (local.date(), local.hour, local.minute) == (DAYS[2][0], 10, 30):  # the rule holds MES: no room
            agent["blocked"] = rig.engine.agent_submit("atlas-trading", "practice-hermes-2", "MES", "buy",
                                                       ask - 6.0, ask + 12.0, 0.6, THESIS)

    for day, _ in DAYS:
        r.session(day, hook=hermes)
    return r, agent


def test_the_rehearsal_trades_open_protected_and_close_by_target_stop_and_flat_by(rehearsal):
    r, _ = rehearsal
    trades = [t for t in r.journal.rows("trades", 100) if t["setup"] == "opening_range_breakout"]
    assert [t["exit_reason"] for t in trades] == ["tp", "sl", "expert"]
    assert [t["direction"] for t in trades] == [1, -1, 1]
    assert all(t["volume"] == 1 for t in r.journal.rows("trades", 100))
    flat = dt.datetime.fromisoformat(trades[2]["exit_time"]).astimezone(PR.NY)
    assert (flat.hour, flat.minute) == (15, 55)  # 14:55 Chicago: the backtest's 15:55 New York exit
    events = r.journal.rows("execution_events", 100_000)
    for t in trades:
        mine = [e["event"] for e in events if e.get("decision_id") == t["decision_id"]]
        assert {"order_submitted", "fill_received", "stop_attached", "target_attached", "position_opened"} <= set(mine)
    assert not r.engine.book and not r.venue._net_positions()  # flat at the end of every day
    assert r.engine.health_now["state"] == "NORMAL"
    assert not [t for t, _ in r.fake.calls if t == "close_position"]


def test_hermes_trades_through_the_same_pipeline_one_contract_and_only_when_the_account_has_room(rehearsal):
    r, agent = rehearsal
    first, blocked = agent["first"], agent["blocked"]
    assert first["decision"] == "ALLOW" and first["outcome"] == "filled" and first["volume"] == 1
    assert blocked["outcome"] != "filled"  # the account nets MES: never two positions on one contract
    hermes = [t for t in r.journal.rows("trades", 100) if t["setup"] == "hermes"]
    assert len(hermes) == 1 and hermes[0]["volume"] == 1


def test_the_practice_report_scores_the_rule_and_hermes_separately(rehearsal):
    r, agent = rehearsal
    rep = practice_report(r.journal)
    marks = rep["pass_marks"]
    assert marks["unprotected_positions"] == {"value": 0, "pass": True, "mark": "zero"}
    assert marks["unreconciled_orders"]["value"] == 0 and marks["unreconciled_orders"]["pass"]
    assert marks["completed_trades"]["value"] == 4 and not marks["completed_trades"]["pass"]  # needs 20
    assert rep["verdict"] == "NOT YET"
    # the stand-in slips every market fill by 1 tick, the backtest's own assumption
    assert rep["slippage"]["entry_avg_ticks"] == 1.0 and rep["slippage"]["backtest_assumed_ticks"] == 1.0
    assert marks["average_slippage_ticks"]["pass"]
    assert rep["orders"]["entries_sent"] == 4 and rep["orders"]["filled"] == 4
    assert rep["orders"]["unknown_outcome"] == 0 and rep["orders"]["rejected_by_platform"] == 0
    assert rep["protection"]["could_not_fix_position_closed"] == 0 and rep["protection"]["halts"] == 0
    assert rep["signal_to_fill_s"]["median"] is not None
    orb, hermes = rep["results"]["opening_range_breakout"], rep["results"]["hermes"]
    assert orb["trades"] == 3 and orb["expected"]["expectancy_r"] == -0.012
    assert "machinery test" in orb["note"] and hermes["trades"] == 1 and hermes["expected"]["bar_r"] == 0.0
    assert any(agent["blocked"]["reasons"][0] in reason for reason in rep["refusals"]["hermes"])
    text = render(rep)
    assert "machinery test, not a profit test" in text and "need at least 20" in text
    assert PASS == {"unprotected_positions": 0, "unreconciled_orders": 0, "max_avg_slippage_ticks": 2.0,
                    "min_trades": 20}


def test_the_practice_report_command_reads_the_state_directory(rehearsal, capsys):
    r, _ = rehearsal
    engine_cli.main(["practice-report", "--state", str(r.root / "state")])
    assert "Overall: NOT YET" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="no journal"):
        engine_cli.main(["practice-report", "--state", str(r.root / "nowhere")])


# -- replayed MES-proxy prices (when the research data is on this machine) --------------------------------

def test_the_rehearsal_on_replayed_mes_proxy_minutes(tmp_path):
    # Validation-period sessions (Tue-Thu 10-12 Sep 2024, never the holdout), moved 108 weeks later so they
    # fall on the pinned MESZ6's trading days with the same weekday and daylight-saving offset.
    src = [dt.date(2024, 9, 10), dt.date(2024, 9, 11), dt.date(2024, 9, 12)]
    shift = 108 * 7
    bars = PR.proxy_bars(src, shift)
    if bars is None:
        pytest.skip("MES-proxy minute data (USA500IDXUSD) is not on this machine")
    days = [d + dt.timedelta(days=shift) for d in src]
    r = PR.build(tmp_path, bars, PR.ny(days[0], 8, 55))
    for d in days:
        r.session(d)
    rep = practice_report(r.journal)
    assert rep["pass_marks"]["unprotected_positions"]["value"] == 0
    assert rep["pass_marks"]["unreconciled_orders"]["value"] == 0
    assert not r.engine.book and not r.venue._net_positions()
    decided = [d for d in r.journal.rows("decisions", 100) if d["setup"] == "opening_range_breakout"]
    trades = r.journal.rows("trades", 100)
    assert len(trades) == sum(d["outcome"] == "filled" for d in decided) <= len(days)
    for t in trades:
        out = dt.datetime.fromisoformat(t["exit_time"]).astimezone(PR.NY)
        assert (out.hour, out.minute) <= (15, 55)
