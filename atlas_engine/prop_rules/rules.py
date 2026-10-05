"""Firm rules as data plus the arithmetic that applies them.

Everything here is the firm's own limit. ATLAS' internal limits, which sit
well inside these, are in ``atlas_engine.risk``. Money is in account currency.

A firm is data, not code: one YAML file per program. Futures programs add a
``futures:`` block (contract limit in minis, the daily flat-by time, the ban on
opposite positions in one asset group, the smallest bracket), and a program
with no daily loss limit sets ``daily_loss.pct: null``.
``atlas_engine.prop_rules.policy`` applies the per-trade rules.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

DAILY_BASES = {"initial_balance", "day_start_balance", "day_start_equity"}
DAILY_REFERENCES = {"day_start_balance", "day_start_equity", "day_start_max", "initial_balance"}
MAX_LOSS_TYPES = {"static", "trailing_eod", "trailing_intraday"}
MODES = {"evaluation", "funded"}
FUTURES_KEYS = {"contract_limit", "tz", "flat_by", "reopen", "hedging_correlated", "min_bracket_ticks"}


@dataclass(frozen=True)
class PhaseRules:
    profit_target_pct: float | None
    min_trading_days: int


@dataclass(frozen=True)
class Restrictions:
    news_window_min: tuple[int, int] | None  # minutes before, after a restricted release
    weekend_holding: bool
    max_market_break_hours: float | None

    def news_blocked(self, ts: dt.datetime, events: list[dt.datetime]) -> bool:
        """True if opening or closing at ``ts`` falls inside a restricted news window."""
        if not self.news_window_min:
            return False
        before, after = (dt.timedelta(minutes=m) for m in self.news_window_min)
        return any(e - before <= ts <= e + after for e in events)


@dataclass(frozen=True)
class FuturesRules:
    """A futures program's per-trade rules (firm facts; ATLAS margins live in the policy)."""

    max_minis: float | None  # contracts held at once, counted in minis
    micros_per_mini: int  # mixing ratio: this many micros count as one mini
    tz: str
    flat_by: dt.time | None  # every position closed by this time each trading day (the firm closes it after)
    reopen: dt.time | None  # trading resumes at this time (the next trading day's session)
    hedging_correlated: bool  # may hold opposite positions in one asset group
    min_bracket_ticks: int  # stop and target at least this many ticks from entry (scalping rules)


@dataclass(frozen=True)
class PropRules:
    firm: str
    program: str
    checked: str
    daily_loss_pct: float | None  # None: the program has no daily loss limit
    daily_loss_base: str
    daily_loss_reference: str
    daily_includes_floating: bool
    reset_time: dt.time
    reset_tz: str
    max_loss_pct: float
    max_loss_type: str
    lock_at_initial: bool
    phases: dict[str, PhaseRules]
    trading_day: str
    restrictions: dict[str, Restrictions]
    consistency_rule: dict | None
    max_lot: float | None
    eas_allowed: bool
    max_server_requests_per_day: int | None
    lock_offset: float = 0.0  # trailing types: the floor stops trailing at initial + this (money)
    futures: FuturesRules | None = None

    # -- server day -------------------------------------------------------

    def server_day(self, ts: dt.datetime) -> dt.date:
        """The firm's trading day containing ``ts`` (daily loss resets at its start)."""
        if ts.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC everywhere, PRD §21)")
        local = ts.astimezone(ZoneInfo(self.reset_tz))
        shift = dt.timedelta(hours=self.reset_time.hour, minutes=self.reset_time.minute)
        return (local - shift).date()

    def next_reset(self, ts: dt.datetime) -> dt.datetime:
        """UTC time the next server day starts."""
        tz = ZoneInfo(self.reset_tz)
        day = self.server_day(ts) + dt.timedelta(days=1)
        return dt.datetime.combine(day, self.reset_time, tz).astimezone(dt.timezone.utc)

    # -- limits in money ---------------------------------------------------

    def daily_loss_amount(self, initial: float, day_start_balance: float, day_start_equity: float) -> float:
        """The firm's daily allowance. With no firm daily limit, ATLAS' own daily stops are measured
        against the max-loss allowance instead (they stay a share of it, so still inside the firm)."""
        if self.daily_loss_pct is None:
            return self.max_loss_amount(initial)
        base = {"initial_balance": initial, "day_start_balance": day_start_balance, "day_start_equity": day_start_equity}
        return self.daily_loss_pct / 100 * base[self.daily_loss_base]

    def daily_reference(self, initial: float, day_start_balance: float, day_start_equity: float) -> float:
        return {
            "day_start_balance": day_start_balance,
            "day_start_equity": day_start_equity,
            "day_start_max": max(day_start_balance, day_start_equity),
            "initial_balance": initial,
        }[self.daily_loss_reference]

    def daily_floor(self, initial: float, day_start_balance: float, day_start_equity: float) -> float:
        """Equity the account may not reach today (no floor when the program has no daily limit)."""
        if self.daily_loss_pct is None:
            return float("-inf")
        ref = self.daily_reference(initial, day_start_balance, day_start_equity)
        return ref - self.daily_loss_amount(initial, day_start_balance, day_start_equity)

    def max_loss_amount(self, initial: float) -> float:
        return self.max_loss_pct / 100 * initial

    def max_loss_reference(self, initial: float, high_water: float) -> float:
        """Level the max-loss allowance is measured from.

        ``high_water`` is the highest end-of-day balance (trailing_eod) or the
        highest equity (trailing_intraday) seen; static ignores it.
        """
        if self.max_loss_type == "static":
            return initial
        ref = max(initial, high_water)
        if self.lock_at_initial:
            ref = min(ref, initial + self.max_loss_amount(initial) + self.lock_offset)
        return ref

    def max_loss_floor(self, initial: float, high_water: float) -> float:
        return self.max_loss_reference(initial, high_water) - self.max_loss_amount(initial)

    def breached(self, equity: float, initial: float, day_start_balance: float, day_start_equity: float,
                 high_water: float) -> list[str]:
        """Firm rules that ``equity`` violates (empty when within limits)."""
        out = []
        if self.daily_loss_pct is not None and equity <= self.daily_floor(initial, day_start_balance, day_start_equity):
            out.append("firm_daily_loss")
        if equity <= self.max_loss_floor(initial, high_water):
            out.append("firm_max_loss")
        return out

    def restrictions_for(self, mode: str) -> Restrictions:
        return self.restrictions["funded" if mode == "funded" else "evaluation"]


def load_prop_rules(path: str | Path) -> PropRules:
    raw = yaml.safe_load(Path(path).read_text())
    return parse_prop_rules(raw, str(path))


def parse_prop_rules(raw: dict, where: str = "prop rules") -> PropRules:
    def need(d: dict, key: str, ctx: str):
        if key not in d:
            raise ValueError(f"{where}: missing {ctx}{key}")
        return d[key]

    _only(raw, {"firm", "program", "checked", "daily_loss", "max_loss", "phases", "trading_day", "restrictions",
                "consistency_rule", "max_lot", "eas_allowed", "max_server_requests_per_day", "futures"}, where, "")
    dl, ml = need(raw, "daily_loss", ""), need(raw, "max_loss", "")
    _only(dl, {"pct", "base", "reference", "includes_floating", "reset_time", "reset_tz"}, where, "daily_loss.")
    _only(ml, {"pct", "type", "lock_at_initial", "lock_offset"}, where, "max_loss.")
    raw_daily = need(dl, "pct", "daily_loss.")
    daily_pct, max_pct = None if raw_daily is None else float(raw_daily), float(need(ml, "pct", "max_loss."))
    if not 0 < (max_pct if daily_pct is None else daily_pct) <= max_pct <= 100:
        raise ValueError(f"{where}: need 0 < daily_loss.pct <= max_loss.pct <= 100 (daily_loss.pct may be null)")
    if daily_pct is None:
        base, ref = "initial_balance", "day_start_balance"  # unused without a daily limit
    else:
        base, ref = need(dl, "base", "daily_loss."), need(dl, "reference", "daily_loss.")
    if base not in DAILY_BASES or ref not in DAILY_REFERENCES:
        raise ValueError(f"{where}: daily_loss.base must be one of {sorted(DAILY_BASES)}, reference one of {sorted(DAILY_REFERENCES)}")
    mtype = need(ml, "type", "max_loss.")
    if mtype not in MAX_LOSS_TYPES:
        raise ValueError(f"{where}: max_loss.type must be one of {sorted(MAX_LOSS_TYPES)}")
    tz = need(dl, "reset_tz", "daily_loss.")
    ZoneInfo(tz)  # raises on an unknown zone
    h, m = str(need(dl, "reset_time", "daily_loss.")).split(":")
    phases = {}
    for name, p in need(raw, "phases", "").items():
        _only(p, {"profit_target_pct", "min_trading_days"}, where, f"phases.{name}.")
        tgt = p.get("profit_target_pct")
        phases[name] = PhaseRules(None if tgt is None else float(tgt), int(p.get("min_trading_days", 0)))
    restrictions = {}
    for mode in MODES:
        r = need(need(raw, "restrictions", ""), mode, "restrictions.")
        _only(r, {"news_window_min", "weekend_holding", "max_market_break_hours"}, where, f"restrictions.{mode}.")
        win = r.get("news_window_min")
        restrictions[mode] = Restrictions(
            None if win is None else (int(win[0]), int(win[1])),
            bool(r.get("weekend_holding", True)),
            None if r.get("max_market_break_hours") is None else float(r["max_market_break_hours"]),
        )
    consistency(raw.get("consistency_rule"))  # validate now, not at the first trade
    lock_offset = float(ml.get("lock_offset", 0.0))
    if lock_offset < 0:
        raise ValueError(f"{where}: max_loss.lock_offset must be >= 0")
    return PropRules(
        firm=str(need(raw, "firm", "")), program=str(raw.get("program", "")), checked=str(raw.get("checked", "")),
        daily_loss_pct=daily_pct, daily_loss_base=base, daily_loss_reference=ref,
        daily_includes_floating=bool(dl.get("includes_floating", True)),
        reset_time=dt.time(int(h), int(m)), reset_tz=tz,
        max_loss_pct=max_pct, max_loss_type=mtype, lock_at_initial=bool(ml.get("lock_at_initial", False)),
        phases=phases, trading_day=str(raw.get("trading_day", "position_opened")), restrictions=restrictions,
        consistency_rule=raw.get("consistency_rule"),
        max_lot=None if raw.get("max_lot") is None else float(raw["max_lot"]),
        eas_allowed=bool(need(raw, "eas_allowed", "")),
        max_server_requests_per_day=raw.get("max_server_requests_per_day"),
        lock_offset=lock_offset,
        futures=_futures(raw.get("futures"), where),
    )


def _time(v, where: str, key: str) -> dt.time | None:
    if v is None:
        return None
    try:
        return dt.time.fromisoformat(str(v))
    except ValueError:
        raise ValueError(f"{where}: futures.{key} must be HH:MM") from None


def _futures(f, where: str) -> FuturesRules | None:
    if f is None:
        return None
    _only(f, FUTURES_KEYS, where, "futures.")
    lim = f.get("contract_limit") or {}
    _only(lim, {"minis", "micros_per_mini"}, where, "futures.contract_limit.")
    tz = str(f.get("tz", "America/Chicago"))
    ZoneInfo(tz)
    minis = lim.get("minis")
    ratio = int(lim.get("micros_per_mini", 10))
    ticks = int(f.get("min_bracket_ticks", 0))
    if (minis is not None and float(minis) <= 0) or ratio < 1 or ticks < 0:
        raise ValueError(f"{where}: futures.contract_limit.minis must be > 0, micros_per_mini >= 1, "
                         "min_bracket_ticks >= 0")
    flat_by, reopen = _time(f.get("flat_by"), where, "flat_by"), _time(f.get("reopen"), where, "reopen")
    if (flat_by is None) != (reopen is None) or (flat_by is not None and not flat_by < reopen):
        raise ValueError(f"{where}: futures.flat_by and futures.reopen go together, flat_by first")
    return FuturesRules(None if minis is None else float(minis), ratio, tz, flat_by, reopen,
                        bool(f.get("hedging_correlated", True)), ticks)


def _only(d: dict, allowed: set[str], where: str, ctx: str) -> None:
    if not isinstance(d, dict):
        raise ValueError(f"{where}: {ctx or 'top level'} must be a mapping")
    extra = set(d) - allowed
    if extra:
        raise ValueError(f"{where}: unknown key(s) {', '.join(ctx + k for k in sorted(extra))}")


def consistency(raw) -> dict | None:
    """The consistency rule as {share, phases}, or None. Raises on a malformed rule."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"max_day_profit_share", "phases"} or "max_day_profit_share" not in raw:
        raise ValueError("consistency_rule needs max_day_profit_share (and optional phases)")
    share = float(raw["max_day_profit_share"])
    if not 0 < share <= 1:
        raise ValueError("consistency_rule.max_day_profit_share must be in (0, 1]")
    return {"share": share, "phases": tuple(raw.get("phases") or ("challenge",))}
