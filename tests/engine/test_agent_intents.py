"""Hermes trading the demo account: agent intents through the engine, the trading routes and the scorecard."""

from __future__ import annotations

import pytest
import yaml

from atlas_api.auth import TokenStore, hash_token
from atlas_api.http import dispatch
from atlas_api.ops import ENGINE_SCOPES, OPS_ROUTES
from atlas_api.trading import ENGINE_ROUTES, TRADING_ROUTES, EngineService
from atlas_engine import agent_intents as AI
from atlas_engine.adapters.mt5.fake import FakeMT5
from atlas_engine.config import ConfigError, load_engine_config
from atlas_engine.market_data.synthetic import random_walk_m1

from t4help import REPO_CONFIG, Clock, build

THESIS = "EURUSD held the London low twice and the dollar is soft after weak US data; buying the retest."


def intent(r, intent_id="i-0001", direction="buy", stop_pips=15, rr=2.0, symbol="EURUSD", confidence=0.4):
    t = r.adapter.tick(symbol)
    d = 1 if direction == "buy" else -1
    entry = t.ask if d == 1 else t.bid
    pip = 0.0001
    return dict(intent_id=intent_id, symbol=symbol, direction=direction,
                stop=round(entry - d * stop_pips * pip, 5), target=round(entry + d * rr * stop_pips * pip, 5),
                confidence=confidence, thesis=THESIS)


@pytest.fixture
def rig(tmp_path):
    r = build(tmp_path)
    r.step()
    r.enable()
    return r


def test_intent_is_sized_and_filled_by_the_engine(rig):
    out = rig.engine.agent_submit("trader/atlas-trading", **intent(rig))
    assert out["outcome"] == "filled", out
    assert out["volume"] > 0 and out["rr"] == pytest.approx(2.0, abs=0.05)
    [pos] = rig.engine.agent_positions()["positions"]
    assert pos["intent_id"] == "i-0001" and pos["direction"] == "buy"
    # The risk engine sized it: the loss at the stop is about risk_per_trade_pct of equity.
    risk = rig.engine.intents[next(iter(rig.engine.intents))]["risk_amount"]
    assert 0 < risk <= 10_000 * rig.engine.cfg.risk.risk_per_trade_pct / 100 * 1.11
    actions = rig.journal.rows("agent_actions")
    assert actions[-1]["action"] == "submit_trade_intent" and actions[-1]["thesis"] == THESIS


def test_retry_with_the_same_intent_id_sends_nothing_new(rig):
    first = rig.engine.agent_submit("t", **intent(rig))
    again = rig.engine.agent_submit("t", **intent(rig))
    assert again["duplicate"] and again["ticket"] == first["ticket"]
    assert len(rig.engine.agent_positions()["positions"]) == 1


def test_refused_on_a_live_account(tmp_path):
    clock = Clock()
    cfg = load_engine_config(REPO_CONFIG)
    r = build(tmp_path, clock, mt5=FakeMT5(clock, symbols=cfg.symbols, demo=False))
    r.step()
    r.enable()
    out = r.engine.agent_submit("t", **intent(r))
    assert out["outcome"] == "refused" and out["reasons"] == ["not_a_demo_account"]
    assert not r.mt5.requests
    assert "live account" in (tmp_path / "alerts.jsonl").read_text()


def test_refused_while_the_operator_has_not_enabled_trading(tmp_path):
    r = build(tmp_path)
    r.step()
    out = r.engine.agent_submit("t", **intent(r))
    assert out["outcome"] == "skipped" and "trading_disabled" in out["reasons"]
    assert r.engine.agent_day["count"] == 0  # a disabled switch doesn't use up the agent's day
    assert not r.mt5.requests


def test_agent_limits(rig):
    rig.engine.agent = AI.AgentIntentSettings(max_intents_per_day=2, max_open_positions=1)
    assert rig.engine.agent_submit("t", **intent(rig, "i-one"))["outcome"] == "filled"
    second = rig.engine.agent_submit("t", **intent(rig, "i-two", symbol="GBPUSD"))
    assert second["outcome"] == "refused" and second["reasons"] == ["agent_open_position_limit"]
    rig.engine.agent = AI.AgentIntentSettings(max_intents_per_day=1, max_open_positions=2)
    third = rig.engine.agent_submit("t", **intent(rig, "i-three", symbol="GBPUSD"))
    assert third["reasons"] == ["agent_daily_intent_limit"]


@pytest.mark.parametrize("change, reason", [
    (dict(rr=0.5), "reward_to_risk"),
    (dict(rr=8), "reward_to_risk"),
    (dict(stop_pips=-10), "stop_on_wrong_side_of_price"),
])
def test_bad_levels_are_refused(rig, change, reason):
    out = rig.engine.agent_submit("t", **intent(rig, **change))
    assert out["outcome"] == "refused" and out["reasons"][0].startswith(reason)
    assert not rig.mt5.requests


@pytest.mark.parametrize("change", [
    dict(intent_id="x"), dict(symbol="USDJPY"), dict(direction="long"), dict(confidence=1.0),
    dict(thesis="because"), dict(stop=-1.0),
])
def test_bad_arguments_raise(rig, change):
    with pytest.raises(AI.IntentRefused):
        rig.engine.agent_submit("t", **{**intent(rig), **change})


def test_agent_closes_and_tightens_only_its_own_positions(rig):
    out = rig.engine.agent_submit("t", **intent(rig))
    ticket = out["ticket"]
    rule = rig.engine.submit(rig.signal("GBPUSD", direction=-1))
    rule_ticket = next(p.ticket for p in rig.engine.book if p.setup != AI.SETUP)
    assert rule["outcome"] == "filled"
    with pytest.raises(AI.IntentRefused):
        rig.engine.agent_close("t", rule_ticket, "not mine to close")
    looser = out["stop"] - 0.0010
    assert rig.engine.agent_tighten("t", ticket, looser, "trying to widen")["status"] == "rejected"
    tighter = round(out["stop"] + 0.0005, 5)
    assert rig.engine.agent_tighten("t", ticket, tighter, "locking in less risk")["status"] == "filled"
    closed = rig.engine.agent_close("t", ticket, "thesis broken by the data")
    assert closed["status"] == "closed" and closed["result"]["r"] is not None
    status = rig.engine.agent_intent_status("i-0001")
    assert status["position"] == "closed" and status["result"]["exit_reason"]


def test_market_returns_closed_bars(rig):
    m1 = random_walk_m1("EURUSD", "2026-08-01", "2026-09-22 10:00", seed=1)
    rig.mt5.load_rates("EURUSD", m1)
    out = rig.engine.agent_market(["EURUSD"], "H1", 24)["symbols"]["EURUSD"]
    assert len(out["bars"]) == 24 and out["bars"][-1]["time"] < "2026-09-22T10:00"
    assert out["ask"] > out["bid"]


def test_scorecard_and_verdict():
    trades = [{"setup": "hermes", "r": r, "decision_id": f"d{i}"} for i, r in enumerate([2.0, -1.0] * 60)]
    trades += [{"setup": "trend_pullback", "r": -0.3}]
    intents = {f"i{i}": {"decision_id": f"d{i}", "confidence": 0.5} for i in range(120)}
    s = AI.scorecard(trades, intents)
    h = s["hermes"]
    assert h["trades"] == 120 and h["expectancy_r"] == 0.5 and h["verdict"] == "passing"
    assert h["calibration"]["brier"] == 0.25 and s["setups"]["trend_pullback"]["trades"] == 1
    assert AI.verdict(AI._stats([0.1, -0.1] * 10)) == "too_early"
    assert AI.verdict(AI._stats([-1.0, -0.9] * 60)) == "losing"


def test_settings_refuse_live(tmp_path):
    (tmp_path / "atlas.yaml").write_text(yaml.safe_dump({"agent_intents": {"enabled_modes": ["demo", "live"]}}))
    with pytest.raises(ConfigError, match="live"):
        AI.load_agent_settings(tmp_path)
    (tmp_path / "atlas.yaml").write_text(yaml.safe_dump({"agent_intents": {"max_lots": 5}}))
    with pytest.raises(ConfigError, match="unknown"):
        AI.load_agent_settings(tmp_path)
    assert AI.load_agent_settings(REPO_CONFIG).enabled_modes == ("paper", "demo")


# ---------------------------------------------------------------------------- the trading routes


def tokens(**named):
    return TokenStore([{"name": n, "sha256": hash_token(n), "scopes": s} for n, s in named.items()], ENGINE_SCOPES)


def test_routes_check_scopes(rig):
    svc = EngineService(rig.engine)
    store = tokens(trader=["trading:read", "trading:demo"], reader=["trading:read"], ops=["ops:read"])
    body = intent(rig)
    status, out = dispatch(svc, store, "trading/submit_intent", "reader", body, ENGINE_ROUTES)
    assert status == 403
    status, out = dispatch(svc, store, "trading/submit_intent", "ops", body, ENGINE_ROUTES)
    assert status == 403
    status, out = dispatch(svc, store, "trading/submit_intent", "trader", body, ENGINE_ROUTES)
    assert status == 200 and out["outcome"] == "filled"
    assert dispatch(svc, store, "trading/positions", "reader", {}, ENGINE_ROUTES)[0] == 200
    assert dispatch(svc, store, "trading/track_record", "reader", {}, ENGINE_ROUTES)[0] == 200
    assert dispatch(svc, store, "trading/intent_status", "reader", {"intent_id": "nope"}, ENGINE_ROUTES)[0] == 404
    bad = dispatch(svc, store, "trading/submit_intent", "trader", {**body, "intent_id": "i-two", "volume": 5}, ENGINE_ROUTES)
    assert bad[0] == 400  # no argument sets a lot size


def test_no_trading_route_can_enable_or_change_risk():
    assert set(ENGINE_ROUTES) == set(OPS_ROUTES) | set(TRADING_ROUTES)
    for route in TRADING_ROUTES:
        assert not any(w in route for w in ("enable", "flatten", "kill", "risk", "limit", "live", "volume", "lot"))


def test_mcp_to_engine_end_to_end(rig):
    """atlas-trading over the real HTTP API with a trader token, as bootstrap issues it."""
    import asyncio
    import json
    import threading

    from mcp.client import Client

    from atlas_api.http import make_server
    from atlas_mcp.servers import SERVERS, ApiClient

    store = tokens(**{"trader/atlas-trading": ["trading:read", "trading:demo"],
                      "operations-monitor/atlas-operations": ["ops:read", "ops:disable_trading"]})
    httpd = make_server(EngineService(rig.engine), store, "127.0.0.1", 0, routes=ENGINE_ROUTES)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"

    async def call(token, tool, args):
        async with Client(SERVERS["atlas-trading"](ApiClient(url, token, timeout=30))) as c:
            r = await c.call_tool(tool, args)
            return r.is_error, r.content[0].text

    try:
        err, text = asyncio.run(call("trader/atlas-trading", "submit_trade_intent", intent(rig)))
        assert not err and json.loads(text)["outcome"] == "filled", text
        err, text = asyncio.run(call("operations-monitor/atlas-operations", "submit_trade_intent",
                                     intent(rig, "i-other")))
        assert err and "trading:demo" in text
        err, text = asyncio.run(call("trader/atlas-trading", "my_track_record", {}))
        assert not err and json.loads(text)["hermes"]["verdict"] == "too_early"
    finally:
        httpd.shutdown()
