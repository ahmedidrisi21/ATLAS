"""PRD v3: trade intents, the decision pipeline, decision models, fail-safe and the audit trail."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from atlas_engine.intents import IntentRejected, TradeIntent, parse_intent
from atlas_engine.journal.audit import reconstruct
from atlas_engine.models import (
    AgentStatedModel, ModelEstimate, ModelFailure, ModelRegistry, RulesModel, SafeModel, validate,
)
from atlas_engine.ops import health as H
from atlas_engine.pipeline import ConfigError, DecisionSettings, load_decision_settings, risk_stage

from t4help import T0, build, stand_in_models

UTC = dt.timezone.utc


@pytest.fixture
def rig(tmp_path):
    r = build(tmp_path)
    r.step()
    r.enable()
    return r


def raw_intent(**over):
    raw = {"strategy": "trend_pullback", "strategy_version": "12", "symbol": "EURUSD", "side": "BUY",
           "setup": {"entry": 1.1, "stop": 1.098, "target": 1.104}, "reason": "Trend continuation after a pullback"}
    raw.update(over)
    return raw


# ---------------------------------------------------------------- the intent schema (§10)

def test_the_prd_example_parses():
    i = parse_intent(raw_intent(), now=T0, source="rules", symbols=("EURUSD",))
    assert (i.strategy, i.strategy_version, i.side, i.stop, i.target) == ("trend_pullback", "12", "BUY", 1.098, 1.104)
    assert i.decision_id.startswith("trend_pullback:12:EURUSD:")


@pytest.mark.parametrize("field", ["volume", "lots", "risk", "risk_pct", "exposure", "prop_override", "password",
                                   "api_key", "magic", "ticket"])
def test_an_intent_may_not_set_size_risk_overrides_or_credentials(field):
    with pytest.raises(IntentRejected) as e:
        parse_intent(raw_intent(**{field: 1}), now=T0)
    assert e.value.code == "forbidden_field"
    with pytest.raises(IntentRejected) as e:  # nested too
        parse_intent(raw_intent(setup={"stop": 1.098, "target": 1.104, field: 1}), now=T0)
    assert e.value.code == "forbidden_field"


@pytest.mark.parametrize("over,code", [
    ({"side": "LONG"}, "invalid_side"), ({"symbol": "BTCUSD"}, "symbol_not_traded"),
    ({"setup": {"stop": 1.098}}, "target_or_rr"), ({"setup": {"stop": 1.098, "target": 1.1, "rr": 2}}, "target_or_rr"),
    ({"setup": {"stop": -1, "target": 1.1}}, "invalid_price"), ({"reason": ""}, "invalid_reason"),
    ({"p_estimate": 1.0}, "invalid_p_estimate"), ({"colour": "red"}, "unknown_field"),
])
def test_malformed_intents_are_rejected_not_repaired(over, code):
    with pytest.raises(IntentRejected) as e:
        parse_intent(raw_intent(**over), now=T0, symbols=("EURUSD",))
    assert e.value.code == code


# ---------------------------------------------------------------- the pipeline (§9, §11)

def test_allow_journals_every_stage_and_the_trade_is_reconstructable(rig):
    out = rig.engine.submit(rig.signal())
    assert out["decision"] == "ALLOW" and out["outcome"] == "filled", out
    stages = [s["stage"] for s in out["pipeline"]["stages"]]
    assert stages == ["system_state", "setup", "market", "strategy", "model", "ev", "risk", "sizing", "exposure",
                      "prop", "execution"]
    ev = out["pipeline"]["ev"]
    assert ev["ev_r"] == pytest.approx(ev["p"] * ev["target_r"] - (1 - ev["p"]) - ev["cost_r"], abs=1e-3)
    audit = reconstruct(rig.journal, out["decision_id"])["summary"]
    assert audit["complete"] and audit["decision"] == "ALLOW" and audit["volume"] > 0
    assert audit["model"]["model"] == "rules" and audit["model"]["calibration_version"].startswith("priors-")
    assert audit["model"]["feature_schema_version"] == "atlas-state-1"


def test_no_validated_prior_means_reject_never_a_guess(tmp_path):
    r = build(tmp_path, models=ModelRegistry.default())  # rules with no priors at all
    r.step()
    r.enable()
    out = r.engine.submit(r.signal())
    assert out["decision"] == "REJECT" and out["reasons"] == ["model_failure:no_validated_prior"]
    assert not r.adapter.positions()


def test_ev_below_the_minimum_is_rejected(tmp_path):
    r = build(tmp_path, models=ModelRegistry.default(priors={"trend_pullback": 0.30}))  # 0.3x2 - 0.7 < 0.15
    r.step()
    r.enable()
    out = r.engine.submit(r.signal())
    assert out["decision"] == "REJECT" and out["pipeline"]["failed_stage"] == "ev"
    assert out["pipeline"]["ev"]["ev_r"] < 0.15


def test_halt_and_kill_are_explicit_decisions(rig):
    rig.engine.health_now = {**rig.engine.health_now, "state": H.HALT, "reasons": ["reconciliation_mismatch"]}
    out = rig.engine.submit(rig.signal())
    assert out["decision"] == "HALT" and "reconciliation_mismatch" in out["reasons"]
    rig.engine.health_now = {**rig.engine.health_now, "state": H.KILL, "reasons": ["manual_kill"]}
    assert rig.engine.submit(rig.signal(minute=15))["decision"] == "KILL"
    assert not rig.adapter.positions()


def test_risk_reasons_are_filed_by_stage():
    assert risk_stage("min_volume_exceeds_budget") == "sizing"
    assert risk_stage("currency_risk_EUR") == "exposure"
    assert risk_stage("firm_limit_headroom") == "prop"
    assert risk_stage("account_day_stopped") == "risk"


def test_correlated_second_trade_fails_at_exposure(rig):
    assert rig.engine.submit(rig.signal())["outcome"] == "filled"
    out = rig.engine.submit(rig.signal(setup="session_breakout"))
    assert out["decision"] == "REJECT" and out["outcome"] == "risk_denied"
    assert out["pipeline"]["failed_stage"] in ("exposure", "risk")


def test_an_agent_cannot_propose_under_a_rule_strategy_name(rig):
    t = rig.adapter.tick("EURUSD")
    i = TradeIntent("trend_pullback", "1", "EURUSD", 1, round(t.ask - 0.0015, 5), T0, source="agent",
                    target=round(t.ask + 0.003, 5), reason="x", p_estimate=0.6)
    out = rig.engine.submit(i)
    assert out["decision"] == "REJECT" and out["reasons"] == ["unknown_strategy_for_source"]


def test_agent_stated_probability_must_clear_the_ev_gate(rig):
    from test_agent_intents import intent

    low = rig.engine.agent_submit("t", **intent(rig, "i-low", confidence=0.3))
    assert low["decision"] == "REJECT" and low["reasons"] == ["ev_below_min"], low
    ok = rig.engine.agent_submit("t", **intent(rig, "i-ok", confidence=0.55))
    assert ok["decision"] == "ALLOW" and ok["outcome"] == "filled"


def test_stated_probability_is_refused_on_a_live_account(rig):
    rig.mt5.demo = False
    rig.step()
    t = rig.adapter.tick("EURUSD")
    i = TradeIntent("hermes", "1", "EURUSD", 1, round(t.ask - 0.0015, 5), rig.clock(), source="agent",
                    target=round(t.ask + 0.003, 5), reason="x", p_estimate=0.9, magic_offset=900)
    out = rig.engine.submit(i)
    assert out["decision"] == "REJECT" and out["reasons"] == ["stated_probability_needs_demo_account"]


# ---------------------------------------------------------------- models and fail-safe (§7, §8, §25)

class Slow:
    name, version = "slow", "slow-1"

    def evaluate_setup(self, setup, state):
        time.sleep(0.3)
        return ModelEstimate(0.9, "slow", "slow-1")


class Broken:
    name, version = "broken", "broken-1"

    def __init__(self, out):
        self.out = out

    def evaluate_setup(self, setup, state):
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


SETUP = {"strategy": "trend_pullback", "strategy_version": "1", "target_r": 2.0, "cost_r": 0.05}


def test_a_timeout_is_a_failure_and_the_limit_is_configurable():
    m = SafeModel(Slow(), timeout_ms=50)
    assert m.evaluate_setup(SETUP, {}).reason == "timeout"
    assert SafeModel(Slow(), timeout_ms=1000).evaluate_setup(SETUP, {}).ok
    assert m.failure_frac() == 1.0


@pytest.mark.parametrize("out,reason", [
    (RuntimeError("down"), "exception:RuntimeError"), ({"p": 0.6}, "schema:not_an_estimate"),
    (ModelEstimate(1.2, "x", "v1"), "out_of_range:p_target_first"),
    (ModelEstimate(float("nan"), "x", "v1"), "schema:p_target_first"),
    (ModelEstimate(0.6, "x", "jev-latest"), "schema:model_version"),
    (ModelEstimate(0.6, "x", "v1", regime="moon"), "schema:regime"),
])
def test_invalid_model_output_fails_safe(out, reason):
    assert SafeModel(Broken(out)).evaluate_setup(SETUP, {}).reason == reason


@pytest.mark.parametrize("key", ["balance", "equity", "symbol", "price", "decision_time", "password", "volume"])
def test_models_never_receive_account_prices_or_credentials(key):
    res = SafeModel(RulesModel({"trend_pullback": 0.5})).evaluate_setup(SETUP, {key: 1})
    assert not res.ok and res.reason == f"forbidden_input:{key}"


def test_model_failures_degrade_the_system():
    h = H.evaluate({"model_failure_frac": 0.3, "symbols": {"EURUSD": {}}})
    assert h["state"] == H.DEGRADED and h["symbols"]["EURUSD"]["reasons"] == ["model_failures"]
    assert not H.new_trades_allowed(h, True)["EURUSD"]


def test_declining_a_strategy_is_not_a_model_failure():
    m = SafeModel(RulesModel({}))
    for _ in range(5):
        m.evaluate_setup(SETUP, {})
    assert m.failure_frac() == 0.0


def test_registry_assigns_models_by_strategy_and_defaults_to_rules():
    reg = ModelRegistry.default({"a": 0.5}, {"b": "jev"})
    assert reg.model_for("a") == "rules" and reg.model_for("b") == "jev" and reg.get("jev") is None
    assert isinstance(reg.get("agent_stated").model, AgentStatedModel)


def test_jev_model_wraps_the_adapter_with_versions():
    from atlas_engine.adapters.jev import JevAdapter
    from atlas_engine.calibration import Calibrator
    from atlas_engine.models import JevModel

    state = {k: 0.1 for k in ("stop_atr", "spread_r", "dist_ema_atr", "h1_fast_dist", "h1_slow_dist", "h1_slope_atr",
                              "h1_adx", "htf_aligned", "atr_pct", "atr_ratio", "room_prior_day_atr",
                              "behind_prior_day_atr", "ret_1_atr", "ret_4_atr", "ret_16_atr")}
    state |= {"session": "london", "regime": "trend_normal_vol", "setup": "trend_pullback"}

    def transport(req):
        return {"setup_id": req["setup_id"], "model_version": "jev-0.8.2", "p_target_first": 0.67,
                "regime": "trend_normal_vol", "reason_codes": ["trend_alignment"]}

    cal = Calibrator.fit([0.2, 0.4, 0.6, 0.8], [0, 0, 1, 1])
    m = JevModel(JevAdapter(transport, "jev-0.8.2"), cal)
    res = SafeModel(m).evaluate_setup(SETUP, state)
    assert res.ok and res.model_version == "jev-0.8.2" and res.calibration_version.startswith("iso-")
    assert res.reason_codes == ("TREND_ALIGNMENT",)
    bad = SafeModel(JevModel(JevAdapter(transport, "jev-0.8.2"))).evaluate_setup(SETUP, {})
    assert not bad.ok and bad.reason.startswith("leakage:")


# ---------------------------------------------------------------- settings

def test_decision_settings_default_to_the_prd(tmp_path):
    (tmp_path / "atlas.yaml").write_text("mode: paper\n")
    s = load_decision_settings(tmp_path)
    assert (s.ev_min_r, s.model_timeout_ms) == (0.15, 500.0) and "ev_min_r" in s.defaults_used
    (tmp_path / "atlas.yaml").write_text("decision: {ev_min_r: -0.1}\n")
    with pytest.raises(ConfigError):
        load_decision_settings(tmp_path)
    (tmp_path / "atlas.yaml").write_text("decision: {skip_ev: true}\n")
    with pytest.raises(ConfigError):
        load_decision_settings(tmp_path)
    assert DecisionSettings().ev_min_r == 0.15
