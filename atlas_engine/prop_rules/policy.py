"""Prop-firm policy: may this trade be opened under the account's rules? (PRD §19, futures-first)

``PropPolicy`` is the one interface the risk engine asks. ``StandardPropPolicy``
reads everything from the firm's rule file (``PropRules``), so a new firm or
program is a new YAML file, not new engine code; a firm whose rules can't be
written as data would get its own class here. Deterministic, and nothing in it
reads Hermes memory or asks a model.

Per-trade rules it applies, each only when the rule file sets it:

    prop_news_window              opening inside a restricted news window
    prop_outside_trading_hours    too close to the firm's daily flat-by time, or before it reopens
    prop_correlated_hedge         opposite side of an open position in the same asset group
    prop_bracket_too_tight        stop or target closer than the firm's minimum ticks (scalping rules)
    prop_consistency_cap          today's profit plus this trade's target would pass the consistency share
    prop_contract_limit           (via ``max_contracts``) no room left under the contract limit

``flatten_due`` says when open positions must be closed (the firm's flat-by
time, less ATLAS' margin), so the engine closes them itself rather than leaving
it to the firm's automatic liquidation.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo

from .rules import PropRules, consistency

ENTRY_CUTOFF_MIN = 30  # no new position this close to the firm's flat-by time
FLATTEN_LEAD_MIN = 10  # close ATLAS positions this long before it


@dataclass(frozen=True)
class PolicyTrade:
    symbol: str
    direction: int
    entry: float
    stop: float
    target: float | None
    tick_size: float


@dataclass(frozen=True)
class PolicyContext:
    now: dt.datetime
    mode: str  # evaluation | funded | paper
    phase: str
    initial_balance: float
    day_profit: float  # equity minus the day's starting balance
    positions: tuple  # risk.engine.OpenPosition
    news_events: tuple[dt.datetime, ...] = ()


class PropPolicy(Protocol):
    name: str

    def entry_reasons(self, trade: PolicyTrade, ctx: PolicyContext) -> list[str]: ...
    def max_contracts(self, symbol: str, ctx: PolicyContext) -> float | None: ...
    def sized_reasons(self, trade: PolicyTrade, volume: float, reward_money: float, ctx: PolicyContext) -> list[str]: ...
    def flatten_due(self, now: dt.datetime) -> str | None: ...


def _mini_equivalent(symbol: str) -> float:
    from atlas_engine.futures.contracts import product  # futures catalogue

    return product(symbol).mini_equivalent


def _group(symbol: str) -> str:
    from atlas_engine.sizing.lots import contract

    return contract(symbol).base


class StandardPropPolicy:
    def __init__(self, rules: PropRules, entry_cutoff_min: int = ENTRY_CUTOFF_MIN,
                 flatten_lead_min: int = FLATTEN_LEAD_MIN):
        self.rules = rules
        self.name = f"{rules.firm} {rules.program}".strip()
        self.entry_cutoff = dt.timedelta(minutes=entry_cutoff_min)
        self.flatten_lead = dt.timedelta(minutes=flatten_lead_min)
        self.consistency = consistency(rules.consistency_rule)

    # -- trading hours ------------------------------------------------------------

    def _window(self, now: dt.datetime, lead: dt.timedelta) -> bool:
        """True from ``lead`` before the flat-by time until the reopen, on the exchange clock."""
        f = self.rules.futures
        if f is None or f.flat_by is None:
            return False
        local = now.astimezone(ZoneInfo(f.tz))
        flat = dt.datetime.combine(local.date(), f.flat_by, local.tzinfo) - lead
        reopen = dt.datetime.combine(local.date(), f.reopen, local.tzinfo)
        return flat <= local < reopen

    def flatten_due(self, now: dt.datetime) -> str | None:
        return "prop_flat_by" if self._window(now, self.flatten_lead) else None

    # -- per trade -----------------------------------------------------------------

    def entry_reasons(self, trade: PolicyTrade, ctx: PolicyContext) -> list[str]:
        r, out = self.rules, []
        if r.restrictions_for(ctx.mode).news_blocked(ctx.now, list(ctx.news_events)):
            out.append("prop_news_window")
        f = r.futures
        if f is None:
            return out
        if self._window(ctx.now, self.entry_cutoff):
            out.append("prop_outside_trading_hours")
        if not f.hedging_correlated:
            group = _group(trade.symbol)
            if any(p.direction == -trade.direction and _group(p.symbol) == group for p in ctx.positions):
                out.append("prop_correlated_hedge")
        if f.min_bracket_ticks and trade.tick_size > 0:
            dists = [abs(trade.entry - trade.stop)] + ([abs(trade.target - trade.entry)] if trade.target else [])
            if min(dists) / trade.tick_size + 1e-9 < f.min_bracket_ticks:
                out.append("prop_bracket_too_tight")
        return out

    def max_contracts(self, symbol: str, ctx: PolicyContext) -> float | None:
        """The most this trade may size to: the firm's lot cap, or the room left under a futures contract
        limit counted in minis (a micro is 1 / micros_per_mini of a mini)."""
        f = self.rules.futures
        if f is None or f.max_minis is None:
            return self.rules.max_lot
        def minis(sym: str, n: float) -> float:
            return n * (1.0 if _mini_equivalent(sym) == 1.0 else 1.0 / f.micros_per_mini)
        used = sum(minis(p.symbol, p.volume) for p in ctx.positions)
        room = max(f.max_minis - used, 0.0)
        per = minis(symbol, 1.0)
        cap = math.floor(room / per + 1e-9)
        return float(cap if self.rules.max_lot is None else min(cap, self.rules.max_lot))

    def sized_reasons(self, trade: PolicyTrade, volume: float, reward_money: float, ctx: PolicyContext) -> list[str]:
        c = self.consistency
        if c is None or ctx.mode != "evaluation" or ctx.phase not in c["phases"]:
            return []
        target = self.rules.phases[ctx.phase].profit_target_pct
        if target is None:
            return []
        cap = c["share"] * target / 100 * ctx.initial_balance
        # If this trade reached its target today, would one day carry more than the share? Then skip it.
        if ctx.day_profit + reward_money > cap + 1e-9:
            return ["prop_consistency_cap"]
        return []
