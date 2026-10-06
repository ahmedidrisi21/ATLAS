"""Where the engine's trade signals come from. Strategy-agnostic by design.

The engine doesn't know which setup T0 will pick. It takes any number of
signal sources; each returns ``Signal``s decided at a closed bar. The one
source built here, ``SetupSource``, runs a setup from the shared library
(atlas_engine.setups) on the same feature frame the backtester uses, built
from the broker's M1 bars, so live and backtest decide from the same code.

Which setups trade is config, not code: one YAML file per strategy in
``config/strategies/`` (a signed operator commit, AGENTS.md). None exists
until a strategy passes its gates, so the engine starts with no sources and
only monitors. The file format:

    setup: trend_pullback        # a name in atlas_engine.setups.SETUPS
    version: "1"                 # bump on any change; part of every decision ID
    enabled: true
    symbols: [EURUSD]
    params: {}                   # setup parameters, as in the research registry
    magic_offset: 1              # magic = execution.magic_base + this (0..999)
    exit: {rr: 2.0}              # fixed target in R until T1 picks an exit policy
    decision:                    # PRD v3 §8: the strategy's decision model (optional)
      model: rules               # rules | gbm | jev; default rules
      p_target_first: 0.42       # rules prior: the validated out-of-sample hit rate. Without one,
                                 # every trade of this strategy is rejected (no estimate, no guess).

Each signal carries its normalised decision state (atlas_engine.decisions.state),
built from the same feature frame, so GBM and Jev see what research saw.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pandas as pd
import yaml

from atlas_engine.config import ConfigError
from atlas_engine.decisions.state import payload, state_frame
from atlas_engine.features.frame import FeatureConfig, build_features
from atlas_engine.market_data.bars import BAR_COLS
from atlas_engine.setups import SETUPS, Setup

M15 = dt.timedelta(minutes=15)


@dataclass(frozen=True)
class Signal:
    symbol: str
    setup: str
    version: str
    direction: int
    stop: float
    decision_time: dt.datetime  # the bar close it was decided at (UTC)
    rr: float = 2.0
    magic_offset: int = 0
    ev_scale: float = 1.0
    features: dict = field(default_factory=dict, compare=False)  # normalised decision state for the model

    @property
    def decision_id(self) -> str:
        return f"{self.setup}:{self.version}:{self.symbol}:{self.decision_time.isoformat()}:{self.direction:+d}"


class SignalSource(Protocol):
    name: str

    def poll(self, broker, now: dt.datetime) -> list[Signal]: ...


def m1_frame(rates, clock) -> pd.DataFrame:
    """MT5 M1 rates (bid OHLC + spread in points) as a bid/ask bar frame indexed by UTC open time.
    MT5 reports one spread per bar, so the ask side is the bid shifted by it."""
    idx = pd.DatetimeIndex([clock.to_utc(int(t)) for t in rates["time"]], name="time")
    df = pd.DataFrame(index=idx)
    return df.assign(**{f"bid_{c}": rates[k] for c, k in zip("ohlc", ("open", "high", "low", "close"))},
                     volume=rates["tick_volume"].astype(float))


@dataclass
class SetupSource:
    setup: Setup
    version: str
    symbols: tuple[str, ...]
    params: dict = field(default_factory=dict)
    rr: float = 2.0
    magic_offset: int = 0
    history_bars: int = 60 * 24 * 70  # M1 bars; the ATR percentile needs 60 days of M15
    features: FeatureConfig = field(default_factory=FeatureConfig)
    last_bar: dict = field(default_factory=dict)
    decision_model: str = "rules"
    p_target_first: float | None = None

    @property
    def name(self) -> str:
        return f"{self.setup.name}:{self.version}"

    def poll(self, broker, now: dt.datetime) -> list[Signal]:
        out = []
        for sym in self.symbols:
            closed = pd.Timestamp(now).floor("15min")  # the latest M15 close at or before now
            if self.last_bar.get(sym) == closed:
                continue
            rates = broker.m1_rates(sym, self.history_bars)
            point = broker.symbol_rules(sym).point
            m1 = m1_frame(rates, broker.clock)
            spread = rates["spread"].astype(float) * point
            for c in "ohlc":
                m1[f"ask_{c}"] = m1[f"bid_{c}"].to_numpy() + spread
            m1 = m1[BAR_COLS]
            m1 = m1[m1.index + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]  # drop the forming bar
            self.last_bar[sym] = closed
            if len(m1) < 1000:
                continue
            frame = build_features(m1, self.features)
            sig = self.setup.signals(frame, self.params)
            if sig.empty:
                continue
            fresh = sig[pd.DatetimeIndex(sig["decision_time"]) == closed].reset_index(drop=True)
            states = state_frame(frame, fresh) if not fresh.empty else None
            for i, r in fresh.iterrows():
                out.append(Signal(sym, self.setup.name, self.version, int(r["direction"]), float(r["stop"]),
                                  closed.to_pydatetime(), self.rr, self.magic_offset,
                                  features=payload(states.iloc[i]) if states is not None else {}))
        return out


DECISION_MODELS = {"rules", "gbm", "jev"}


def decision_config(sources: list[SetupSource]) -> tuple[dict[str, float], dict[str, str]]:
    """(rules priors keyed "setup:version", strategy -> model name) for ``ModelRegistry.default``."""
    priors = {f"{s.setup.name}:{s.version}": s.p_target_first for s in sources if s.p_target_first is not None}
    return priors, {s.setup.name: s.decision_model for s in sources}


def load_strategies(root: str | Path = "config") -> list[SetupSource]:
    d = Path(root) / "strategies"
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text()) or {}
        extra = set(raw) - {"setup", "version", "enabled", "symbols", "params", "magic_offset", "exit", "decision"}
        if extra:
            raise ConfigError(f"{path}: unknown key(s) {', '.join(sorted(extra))}")
        if not raw.get("enabled", False):
            continue
        if raw.get("setup") not in SETUPS:
            raise ConfigError(f"{path}: setup must be one of {sorted(SETUPS)}")
        dec = raw.get("decision") or {}
        if not isinstance(dec, dict) or set(dec) - {"model", "p_target_first"}:
            raise ConfigError(f"{path}: decision takes only model and p_target_first")
        if dec.get("model", "rules") not in DECISION_MODELS:
            raise ConfigError(f"{path}: decision.model must be one of {sorted(DECISION_MODELS)}")
        p = dec.get("p_target_first")
        if p is not None and not (isinstance(p, (int, float)) and 0 < p < 1):
            raise ConfigError(f"{path}: decision.p_target_first must be a probability strictly between 0 and 1")
        off = int(raw.get("magic_offset", 0))
        if not 0 <= off < 1000:
            raise ConfigError(f"{path}: magic_offset must be in 0..999")
        out.append(SetupSource(SETUPS[raw["setup"]], str(raw.get("version", "1")),
                               tuple(s.upper() for s in raw.get("symbols") or ()), dict(raw.get("params") or {}),
                               float((raw.get("exit") or {}).get("rr", 2.0)), off,
                               decision_model=str(dec.get("model", "rules")),
                               p_target_first=None if p is None else float(p)))
    offsets = [s.magic_offset for s in out]
    if len(offsets) != len(set(offsets)):
        raise ConfigError(f"{d}: two strategies share a magic_offset")
    return out
