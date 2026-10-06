"""Hermes as a trader on the demo account: agent trade intents (PRD §6 ``atlas-trading``, §11).

The ``atlas-trading`` Hermes profile decides trades itself and sends them as
intents through the ``atlas-trading`` MCP server. The engine stays the only
thing that touches MT5, and an intent gets no shortcut: it goes through the
same health check, T3 risk engine, sizing and execution rules as a rule-based
signal (``TradingEngine.submit``). The agent says where its stop and target
are and why; it never sets a lot size, and it can't move a stop away from
the price.

Three rules the agent can't change, because they are code, not config:

- **Demo only.** An intent is refused unless the broker reports a demo
  account. ``agent_intents.enabled_modes`` may hold ``paper`` and ``demo``;
  ``live`` is refused at load. Letting Hermes trade real money is a code
  change plus the promotion gate, never a setting (AGENTS.md, PRD §11).
- **The operator enables trading.** An intent is refused while trading is
  disabled, exactly like a rule-based signal. Only a signed operator command
  turns it on.
- **Its own positions only.** The agent can close or tighten the stop of a
  position it opened (setup ``hermes``), and nothing else.

``scorecard`` measures the agent's demo record in R after costs, by setup, so
Hermes and any rule-based strategy on the same account are scored the same way.
See docs/hermes-trader.md.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd
import yaml

from atlas_engine.config import ConfigError

SETUP = "hermes"  # the setup name on every agent decision, position and trade
MODES = {"paper", "demo"}
KEYS = {"enabled_modes", "max_intents_per_day", "max_open_positions", "min_rr", "max_rr", "magic_offset"}
MIN_THESIS, MAX_THESIS = 40, 2000
INTENT_ID = re.compile(r"^[A-Za-z0-9_.-]{4,48}$")
TIMEFRAMES = {"M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D"}
MAX_M1_BARS = 100_000  # D1 x 60 is 86,400 M1 bars; one copy_rates call, not an order request

# The pre-declared gate for Hermes's demo record (docs/hermes-trader.md). Fixed before any trade.
GATE_MIN_TRADES = 100
GATE_MIN_EXPECTANCY_R = 0.10


class IntentRefused(ValueError):
    """An intent the desk will not pass to the engine (bad arguments or an agent limit)."""


@dataclass(frozen=True)
class AgentIntentSettings:
    enabled_modes: tuple[str, ...] = ("paper", "demo")  # PRD §27 default
    max_intents_per_day: int = 4  # per server day, sent or refused by the engine
    max_open_positions: int = 2
    min_rr: float = 1.0  # target distance over stop distance
    max_rr: float = 5.0
    magic_offset: int = 900  # magic = execution.magic_base + this; strategies use 0..899
    defaults_used: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.enabled_modes)

    def to_dict(self) -> dict:
        return asdict(self)


def load_agent_settings(root: str | Path = "config") -> AgentIntentSettings:
    """``agent_intents:`` from config/atlas.yaml. Optional; missing keys take the PRD §27 defaults."""
    path = Path(root) / "atlas.yaml"
    raw = (yaml.safe_load(path.read_text()) or {}).get("agent_intents") or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: agent_intents must be a mapping")
    extra = set(raw) - KEYS
    if extra:
        raise ConfigError(f"{path}: unknown key(s) {', '.join(f'agent_intents.{k}' for k in sorted(extra))}")
    kw = {k: (tuple(v) if isinstance(v, list) else v) for k, v in raw.items()}
    s = AgentIntentSettings(**kw, defaults_used=tuple(sorted(KEYS - set(raw))))
    if "live" in s.enabled_modes:
        raise ConfigError(f"{path}: agent_intents.enabled_modes may not include live: an agent never trades a "
                          "live account in v3 (AGENTS.md, PRD §11)")
    checks = [
        (set(s.enabled_modes) <= MODES, f"agent_intents.enabled_modes must be a subset of {sorted(MODES)}"),
        (1 <= s.max_intents_per_day <= 20, "agent_intents.max_intents_per_day must be in 1..20"),
        (1 <= s.max_open_positions <= 5, "agent_intents.max_open_positions must be in 1..5"),
        (0.5 <= s.min_rr <= s.max_rr <= 10, "agent_intents needs 0.5 <= min_rr <= max_rr <= 10"),
        (900 <= s.magic_offset <= 999, "agent_intents.magic_offset must be in 900..999"),
    ]
    bad = [m for ok, m in checks if not ok]
    if bad:
        raise ConfigError(f"{path}: " + "; ".join(bad))
    return s


def check_intent(intent_id, symbol, direction, stop, target, confidence, thesis, symbols) -> tuple[str, int]:
    """Argument checks that need no market data. Returns (symbol, direction as +1/-1)."""
    if not isinstance(intent_id, str) or not INTENT_ID.match(intent_id):
        raise IntentRefused("intent_id must be 4-48 characters of letters, digits, '.', '_' or '-'")
    sym = str(symbol).upper()
    if sym not in symbols:
        raise IntentRefused(f"the engine does not trade {symbol}; symbols: {', '.join(symbols)}")
    d = {"buy": 1, "sell": -1}.get(str(direction).lower())
    if d is None:
        raise IntentRefused("direction must be 'buy' or 'sell'")
    for name, v in (("stop", stop), ("target", target)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise IntentRefused(f"{name} must be a positive price")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 < confidence < 1:
        raise IntentRefused("confidence must be your probability (0-1, exclusive) that the target fills before the stop")
    if not isinstance(thesis, str) or not MIN_THESIS <= len(thesis.strip()) <= MAX_THESIS:
        raise IntentRefused(f"thesis must be {MIN_THESIS}-{MAX_THESIS} characters: why this trade, now")
    return sym, d


def resample_bars(m1: pd.DataFrame, timeframe: str, count: int, now: dt.datetime) -> list[dict]:
    """Closed bars of ``timeframe`` from bid M1 bars (``m1_frame``), newest last."""
    rule = TIMEFRAMES[timeframe]
    agg = m1.resample(rule, label="left", closed="left").agg(
        {"bid_o": "first", "bid_h": "max", "bid_l": "min", "bid_c": "last", "volume": "sum"}).dropna()
    step = pd.Timedelta(rule)
    agg = agg[agg.index + step <= pd.Timestamp(now)]  # the forming bar is left out
    return [{"time": t.isoformat(), "open": r.bid_o, "high": r.bid_h, "low": r.bid_l, "close": r.bid_c,
             "tick_volume": int(r.volume)} for t, r in agg.tail(count).iterrows()]


def m1_needed(timeframe: str, count: int) -> int:
    per = {"M15": 15, "H1": 60, "H4": 240, "D1": 1440}[timeframe]
    # x1.6 covers weekends and the forming bar.
    return min(int(per * (count + 1) * 1.6) + 60, MAX_M1_BARS)


# --------------------------------------------------------------------------- the record


def scorecard(trades: list[dict], intents: dict[str, dict] | None = None) -> dict:
    """Closed trades in R after costs, grouped by setup; Hermes's calibration from its confidences.

    ``trades`` are rows of the journal's ``trades`` table. ``r`` is net P&L
    (spread, commission and swap included, as the broker booked it) over the
    money at risk when the order was sized.
    """
    by_setup: dict[str, list[dict]] = {}
    for t in trades:
        if t.get("r") is not None:
            by_setup.setdefault(t.get("setup") or "unknown", []).append(t)
    out = {"gate": {"min_trades": GATE_MIN_TRADES, "min_expectancy_r": GATE_MIN_EXPECTANCY_R,
                    "rule": "expectancy >= +0.10R and the 95% interval's low end above 0, over at least 100 trades"},
           "setups": {s: _stats([t["r"] for t in ts]) for s, ts in sorted(by_setup.items())}}
    mine = by_setup.get(SETUP, [])
    out["hermes"] = {**(out["setups"].get(SETUP) or _stats([])), "verdict": verdict(out["setups"].get(SETUP)),
                     "calibration": _calibration(mine, intents or {})}
    return out


def verdict(stats: dict | None) -> str:
    if not stats or stats["trades"] < GATE_MIN_TRADES:
        return "too_early"
    if stats["ci95_r"][0] > 0 and stats["expectancy_r"] >= GATE_MIN_EXPECTANCY_R:
        return "passing"
    if stats["ci95_r"][1] < 0:
        return "losing"
    return "no_edge_shown"


def _stats(rs: list[float]) -> dict:
    n = len(rs)
    if not n:
        return {"trades": 0, "expectancy_r": None, "ci95_r": None, "total_r": 0.0, "win_rate": None,
                "profit_factor": None, "worst_losing_streak": 0}
    mean = sum(rs) / n
    sd = (sum((r - mean) ** 2 for r in rs) / (n - 1)) ** 0.5 if n > 1 else 0.0
    half = 1.96 * sd / n ** 0.5 if n > 1 else float("inf")
    wins, losses = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r < 0 else 0
        worst = max(worst, streak)
    return {"trades": n, "expectancy_r": round(mean, 3),
            "ci95_r": [round(mean - half, 3), round(mean + half, 3)] if n > 1 else None,
            "total_r": round(sum(rs), 2), "win_rate": round(sum(r > 0 for r in rs) / n, 3),
            "profit_factor": round(wins / losses, 2) if losses else None, "worst_losing_streak": worst}


def _calibration(trades: list[dict], intents: dict[str, dict]) -> dict:
    """Brier score of the confidences Hermes gave, against always guessing the observed win rate."""
    by_decision = {i.get("decision_id"): i for i in intents.values() if i.get("decision_id")}
    pairs = [(by_decision[t["decision_id"]]["confidence"], 1.0 if t["r"] > 0 else 0.0)
             for t in trades if t.get("decision_id") in by_decision]
    if not pairs:
        return {"trades": 0, "brier": None, "brier_base_rate": None, "mean_confidence": None, "win_rate": None}
    n = len(pairs)
    base = sum(o for _, o in pairs) / n
    return {"trades": n, "brier": round(sum((c - o) ** 2 for c, o in pairs) / n, 4),
            "brier_base_rate": round(sum((base - o) ** 2 for _, o in pairs) / n, 4),
            "mean_confidence": round(sum(c for c, _ in pairs) / n, 3), "win_rate": round(base, 3)}
