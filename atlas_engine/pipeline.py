"""The decision pipeline: one trade intent in, one explicit decision out (PRD v3 §9, §11, §12, §26).

Every proposal, from a rule strategy or from Hermes, takes these stages in
order and stops at the first that fails:

    1. system_state   NORMAL trades; DEGRADED blocks the affected symbol;
                      HALT and KILL block everything (atlas_engine.ops.health)
    2. setup          stop and target on the right side of the live quote,
                      planned reward inside the allowed range of R
    3. market         a live quote, the market open, spread small next to the stop
    4. strategy       a known strategy; agents only under their own strategy name
    5. model          the strategy's decision model through SafeModel (timeout,
                      schema, range); any failure is REJECT, never a guess
    6. ev             EV_R = p x R_target - (1 - p) - C_R >= ev_min (0.15 R)
    7. risk           the T3 risk engine: account latches, risk per trade, daily
       sizing         and drawdown limits, lot size, open risk, currency and
       exposure       correlated exposure, prop-firm rules. Its reasons are
       prop           filed under these four stages for the audit record.

The result is ALLOW, REJECT, HALT or KILL with every stage's inputs and
outputs, so the journal can reconstruct why (§31). ALLOW only means the
engine may send the order: execution checks run in the executor right
before the send, against a fresh quote, and the runtime appends that stage.

Nothing here can be configured to skip a stage, and no stage reads Hermes
memory: the thresholds come from ``DecisionSettings`` (engine config) and the
risk engine's own config (§29).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from atlas_engine.config import ConfigError
from atlas_engine.decisions.ev import EVGate
from atlas_engine.intents import TradeIntent
from atlas_engine.models import DEFAULT_TIMEOUT_MS, ModelRegistry
from atlas_engine.ops import health as H
from atlas_engine.risk import AccountSnapshot, RiskEngine, TradeProposal
from atlas_engine.setups import SETUPS
from atlas_engine.sizing.lots import risk_per_lot

ALLOW, REJECT, HALT, KILL = "ALLOW", "REJECT", "HALT", "KILL"
AGENT_STRATEGY = "hermes"  # the one strategy name an agent may propose under (atlas_engine.agent_intents.SETUP)

# T3 risk-engine reasons, filed by stage for the audit record. Anything unlisted is "risk".
_SIZING = ("sizing_failed", "no_risk_budget", "invalid_stop_distance", "invalid_tick_value", "min_volume_exceeds")
_EXPOSURE = ("max_open_risk", "currency_risk_", "correlated_one_bet", "position_exists_for_setup")
_PROP = ("prop_", "firm_limit_headroom", "mode_not_tradeable", "account_firm_breached")


def risk_stage(reason: str) -> str:
    if reason.startswith(_SIZING):
        return "sizing"
    if reason.startswith(_EXPOSURE):
        return "exposure"
    if reason.startswith(_PROP):
        return "prop"
    return "risk"


@dataclass(frozen=True)
class DecisionSettings:
    ev_min_r: float = 0.15  # §12: initial minimum; a model can't override it
    model_timeout_ms: float = DEFAULT_TIMEOUT_MS  # §25
    min_target_r: float = 0.5
    max_target_r: float = 10.0
    max_spread_to_stop: float = 0.20  # §16 edge filter, as in research
    extra_cost_r: float = 0.0  # slippage allowance added to commission in C_R
    jev_model: str = "jev-1.13.0"  # §7: a pinned TypeSafe model ID, never an alias
    defaults_used: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


KEYS = {"ev_min_r", "model_timeout_ms", "min_target_r", "max_target_r", "max_spread_to_stop", "extra_cost_r",
        "jev_model"}
TEXT_KEYS = {"jev_model"}
CALIBRATED_MODELS = {"gbm", "jev"}  # models whose raw output must be calibrated before a real-money trade (§24)


def load_decision_settings(root: str | Path = "config") -> DecisionSettings:
    """``decision:`` in config/atlas.yaml. Optional; missing keys take the PRD defaults. Refuses a weaker gate
    than the PRD's floor: EV_min below 0 or a model timeout above 5 s."""
    path = Path(root) / "atlas.yaml"
    raw = (yaml.safe_load(path.read_text()) or {}).get("decision") or {} if path.exists() else {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: decision must be a mapping")
    extra = set(raw) - KEYS
    if extra:
        raise ConfigError(f"{path}: unknown key(s) {', '.join(f'decision.{k}' for k in sorted(extra))}")
    s = DecisionSettings(**{k: str(v) if k in TEXT_KEYS else float(v) for k, v in raw.items()},
                         defaults_used=tuple(sorted(KEYS - set(raw))))
    checks = [(s.ev_min_r >= 0.0, "decision.ev_min_r must be >= 0"),
              (0 < s.model_timeout_ms <= 5000, "decision.model_timeout_ms must be in (0, 5000]"),
              (0 < s.min_target_r <= s.max_target_r <= 20, "decision needs 0 < min_target_r <= max_target_r <= 20"),
              (0 < s.max_spread_to_stop <= 0.5, "decision.max_spread_to_stop must be in (0, 0.5]"),
              (s.extra_cost_r >= 0, "decision.extra_cost_r must be >= 0"),
              (bool(re.fullmatch(r"jev-\d+\.\d+\.\d+", s.jev_model)),
               "decision.jev_model must be a pinned version such as jev-1.13.0, not an alias")]
    bad = [m for ok, m in checks if not ok]
    if bad:
        raise ConfigError(f"{path}: " + "; ".join(bad))
    return s


@dataclass
class Context:
    """What the engine knows at decision time. Built by the runtime; the pipeline only reads it."""

    now: dt.datetime
    health: dict
    trading_enabled: bool
    connected: bool
    journal_ok: bool
    demo_account: bool | None
    tick: object | None  # adapters.broker.Tick
    rules: object | None  # adapters.broker.SymbolRules
    snapshot: AccountSnapshot | None
    rates: dict[str, float]
    account_currency: str
    already_handled: bool = False
    market_open: bool = True
    market_closed_reason: str | None = None  # why not, from the session calendar (futures) or the FX week
    session: dict | None = None  # futures: SessionState.to_dict(), journaled with the decision
    roll_days: int = 0  # futures: refuse a contract this many days before its last trade / first notice


@dataclass
class EngineDecision:
    decision: str  # ALLOW | REJECT | HALT | KILL
    intent: TradeIntent
    stages: list[dict] = field(default_factory=list)  # {"stage", "ok", ...detail}
    reasons: list[str] = field(default_factory=list)
    failed_stage: str | None = None
    entry: float | None = None
    target: float | None = None
    target_r: float | None = None
    cost_r: float | None = None
    model: dict | None = None
    ev: dict | None = None
    risk: dict | None = None  # the T3 RiskDecision
    volume: float = 0.0
    risk_amount: float = 0.0
    system_state: str = H.NORMAL

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    @property
    def outcome(self) -> str:
        """The engine's older outcome words, kept for the operations API and the T4 gate."""
        if self.allowed:
            return "allowed"
        if self.failed_stage in ("system_state", "strategy"):
            return "skipped"
        if self.failed_stage in ("risk", "sizing", "exposure", "prop"):
            return "risk_denied"
        return "rejected"

    def to_dict(self) -> dict:
        return {"decision": self.decision, "intent": self.intent.to_dict(), "stages": self.stages,
                "reasons": self.reasons, "failed_stage": self.failed_stage, "entry": self.entry, "target": self.target,
                "target_r": self.target_r, "cost_r": self.cost_r, "model": self.model, "ev": self.ev,
                "risk": self.risk, "volume": self.volume, "risk_amount": self.risk_amount,
                "system_state": self.system_state}


class DecisionPipeline:
    def __init__(self, settings: DecisionSettings, models: ModelRegistry, risk: RiskEngine):
        self.settings, self.models, self.risk = settings, models, risk
        self.gate = EVGate(settings.ev_min_r)

    def evaluate(self, intent: TradeIntent, ctx: Context) -> EngineDecision:
        d = EngineDecision(REJECT, intent, system_state=ctx.health.get("state", H.NORMAL))
        for stage in (self._system, self._setup, self._market, self._strategy, self._model, self._ev, self._risk):
            if not stage(intent, ctx, d):
                return d
        d.decision = ALLOW
        return d

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _pass(d: EngineDecision, stage: str, **detail) -> bool:
        d.stages.append({"stage": stage, "ok": True, **detail})
        return True

    @staticmethod
    def _fail(d: EngineDecision, stage: str, reasons: list[str], decision: str = REJECT, **detail) -> bool:
        d.stages.append({"stage": stage, "ok": False, "reasons": list(reasons), **detail})
        d.decision, d.reasons, d.failed_stage = decision, list(reasons), stage
        return False

    # -- stages ------------------------------------------------------------------

    def _system(self, i: TradeIntent, ctx: Context, d: EngineDecision) -> bool:
        state = ctx.health.get("state", H.NORMAL)
        if state == H.KILL:
            return self._fail(d, "system_state", ["system_kill", *ctx.health.get("reasons", [])], KILL, state=state)
        if state == H.HALT:
            return self._fail(d, "system_state", ["system_halt", *ctx.health.get("reasons", [])], HALT, state=state)
        reasons = []
        if not ctx.trading_enabled:
            reasons.append("trading_disabled")
        sym = ctx.health.get("symbols", {}).get(i.symbol)
        if sym is None:
            reasons.append("symbol_not_traded")
        elif sym["state"] != H.NORMAL and ctx.trading_enabled:
            reasons.append(f"health_{sym['state'].lower()}")
        if not ctx.connected or ctx.snapshot is None:
            reasons.append("broker_unavailable")
        if not ctx.journal_ok:
            reasons.append("journal_unavailable")  # never trade what the journal can't record (§31)
        if ctx.already_handled:
            reasons.append("decision_already_handled")
        if reasons:
            return self._fail(d, "system_state", reasons, state=state)
        return self._pass(d, "system_state", state=state)

    def _setup(self, i: TradeIntent, ctx: Context, d: EngineDecision) -> bool:
        if ctx.tick is None:
            return self._fail(d, "setup", ["no_quote"])
        entry = ctx.tick.price(i.direction)
        risk = i.direction * (entry - i.stop)
        d.entry = entry
        if risk <= 0:
            return self._fail(d, "setup", ["stop_on_wrong_side_of_price"], entry=entry, stop=i.stop)
        target = i.target if i.target is not None else entry + i.direction * i.rr * risk
        if ctx.rules is not None:
            target = ctx.rules.round_price(target)
        reward = i.direction * (target - entry)
        if reward <= 0:
            return self._fail(d, "setup", ["target_on_wrong_side_of_price"], entry=entry, target=target)
        d.target, d.target_r = target, reward / risk
        s = self.settings
        if not s.min_target_r <= d.target_r <= s.max_target_r:
            return self._fail(d, "setup", [f"target_r_outside_{s.min_target_r:g}-{s.max_target_r:g}"],
                              target_r=round(d.target_r, 3))
        return self._pass(d, "setup", entry=entry, stop=i.stop, target=target, target_r=round(d.target_r, 3))

    def _market(self, i: TradeIntent, ctx: Context, d: EngineDecision) -> bool:
        if not ctx.market_open:
            return self._fail(d, "market", [ctx.market_closed_reason or "market_closed"], session=ctx.session)
        contract = getattr(ctx.rules, "contract", None)
        if contract is not None:
            # Futures: never open a position in a contract that has expired or is due to roll.
            block = contract.entry_block(ctx.now, ctx.roll_days)
            if block:
                return self._fail(d, "market", [block], contract=contract.to_dict())
        stop_dist = abs(d.entry - i.stop)
        spread = ctx.tick.spread
        detail = {"bid": ctx.tick.bid, "ask": ctx.tick.ask, "spread_to_stop": round(spread / stop_dist, 4)}
        if ctx.session is not None:
            detail["session"] = ctx.session
        if contract is not None:
            detail["contract"] = contract.to_dict()
        if spread > self.settings.max_spread_to_stop * stop_dist + 1e-12:
            return self._fail(d, "market", ["spread_too_wide_for_stop"], **detail)
        return self._pass(d, "market", **detail)

    def _strategy(self, i: TradeIntent, ctx: Context, d: EngineDecision) -> bool:
        if i.source == "agent":
            ok = i.strategy == AGENT_STRATEGY
        else:
            ok = i.strategy in SETUPS and i.strategy != AGENT_STRATEGY
        if not ok:
            return self._fail(d, "strategy", ["unknown_strategy_for_source"], source=i.source)
        return self._pass(d, "strategy", strategy=i.strategy, version=i.strategy_version, source=i.source)

    def _cost_r(self, i: TradeIntent, ctx: Context, entry: float) -> float:
        """C_R: round-turn commission over the money at risk to the stop, plus the slippage allowance.
        Spread is already in the geometry: the stop and target are measured from the fill side of the quote."""
        stop_dist = abs(entry - i.stop)
        spec = ctx.rules.spec if ctx.rules is not None else None
        if spec is None:
            return self.settings.extra_cost_r
        per_lot = risk_per_lot(spec, stop_dist, ctx.account_currency, ctx.rates)
        price_risk = per_lot - spec.commission_per_lot
        return (spec.commission_per_lot / price_risk if price_risk > 0 else float("inf")) + self.settings.extra_cost_r

    def _model(self, i: TradeIntent, ctx: Context, d: EngineDecision) -> bool:
        # An agent's idea is scored by its own stated probability (demo only), unless the operator assigned its
        # strategy to a model (e.g. hermes: jev), which then scores it like any rule signal.
        assigned = i.strategy in self.models.assignment
        name = "agent_stated" if i.source == "agent" and not assigned else self.models.model_for(i.strategy)
        if i.source == "agent" and assigned and ctx.demo_account is not True:
            # AGENTS.md: agent trades are demo only, whichever model scores them.
            return self._fail(d, "model", ["agent_trade_needs_demo_account"], model=self.models.model_for(i.strategy))
        if name == "agent_stated" and ctx.demo_account is not True:
            return self._fail(d, "model", ["stated_probability_needs_demo_account"], model=name)
        model = self.models.get(name)
        if model is None:
            return self._fail(d, "model", ["model_unavailable"], model=name)
        d.cost_r = self._cost_r(i, ctx, d.entry)
        setup = {"strategy": i.strategy, "strategy_version": i.strategy_version, "target_r": round(d.target_r, 4),
                 "cost_r": round(d.cost_r, 4)}
        state = dict(i.features)
        if name == "agent_stated":
            state = {"stated_p": i.p_estimate}
        res = model.evaluate_setup(setup, state)
        d.model = {**res.to_dict(), "ok": res.ok, "requested": name}
        if not res.ok:
            return self._fail(d, "model", [f"model_failure:{res.reason}"], model=name)
        if name in CALIBRATED_MODELS and res.calibration_version == "none" and ctx.demo_account is not True:
            # §24: a raw GBM or Jev probability is not a calibrated one; it may only trade a demo account.
            return self._fail(d, "model", ["uncalibrated_model_needs_demo_account"], model=name)
        return self._pass(d, "model", **d.model)

    def _ev(self, i: TradeIntent, ctx: Context, d: EngineDecision) -> bool:
        res = self.gate.decide(d.model["p_target_first"], d.target_r, d.cost_r)
        d.ev = {"p": d.model["p_target_first"], "target_r": round(d.target_r, 4), "cost_r": round(d.cost_r, 4),
                "ev_r": None if res.ev_r is None else round(res.ev_r, 4), "ev_min_r": self.settings.ev_min_r,
                "formula": "p*R_target - (1-p) - C_R"}
        if not res.take:
            return self._fail(d, "ev", [res.reason], **d.ev)
        return self._pass(d, "ev", **d.ev)

    def _risk(self, i: TradeIntent, ctx: Context, d: EngineDecision) -> bool:
        prop = TradeProposal(i.symbol, i.strategy, i.direction, d.entry, i.stop, 1.0, ctx.rates,
                             spec=ctx.rules.spec if ctx.rules is not None else None, target=d.target)
        r = self.risk.check_entry(prop, ctx.snapshot)
        d.risk = r.to_dict()
        if not r.allowed:
            by_stage: dict[str, list[str]] = {}
            for reason in r.reasons:
                by_stage.setdefault(risk_stage(reason), []).append(reason)
            first = next(s for s in ("risk", "sizing", "exposure", "prop") if s in by_stage)
            for s in ("risk", "sizing", "exposure", "prop"):
                if s != first:
                    d.stages.append({"stage": s, "ok": s not in by_stage, "reasons": by_stage.get(s, [])})
            self._fail(d, first, list(r.reasons), checks=r.checks)
            d.failed_stage = first
            return False
        d.volume, d.risk_amount = r.volume, r.risk_amount
        self._pass(d, "risk", risk_pct=round(r.risk_pct, 4), multiplier=r.multiplier)
        self._pass(d, "sizing", volume=r.volume, risk_amount=round(r.risk_amount, 2), sizing=r.checks.get("sizing"))
        self._pass(d, "exposure", open_risk_pct=r.checks.get("open_risk_pct"),
                   currency_risk_pct=r.checks.get("currency_risk_pct"), correlation=r.checks.get("correlation"))
        return self._pass(d, "prop", worst_case_equity=r.checks.get("worst_case_equity"))
