"""Execution and filter settings from ``config/atlas.yaml`` (PRD §27 ``execution:`` and ``filters:``).

Both sections are optional. When a key is missing the PRD's default applies,
and ``status`` reports which defaults are in force, so adding the sections to
``atlas.yaml`` stays an operator decision (a signed commit). Unknown keys are
refused, like every other engine config.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml

from atlas_engine.config import ConfigError

EXECUTION_KEYS = {"max_deviation_points", "reconcile_interval_s", "magic_base", "orphan_policy", "server_tz",
                  "server_offset_hours", "request_budget_share", "require_watchdog", "watchdog_heartbeat_file",
                  "commission_per_lot", "platform", "contracts", "roll_days", "entry_cutoff_min", "flatten_lead_min",
                  "no_trade_days", "broker_poll_s", "demo_only"}
FILTER_KEYS = {"max_spread_to_stop_ratio", "news_blackout_min", "rollover_blackout_ny", "friday_flatten_utc"}
ORPHAN_POLICIES = {"close", "attach_sl"}
PLATFORM_NAMES = {"mt5", "ninjatrader"}
# NinjaTrader's official API is the Tradovate API (docs/futures.md, "One API, two names"), so "tradovate"
# names the same adapter, not a second one.
PLATFORM_ALIASES = {"tradovate": "ninjatrader"}


@dataclass(frozen=True)
class ExecutionSettings:
    max_deviation_points: int = 2  # §21: deviation <= 2 points on majors
    reconcile_interval_s: int = 60  # §21: startup + every 60 s
    magic_base: int = 26_090_000  # strategy magic numbers are magic_base + a per-strategy offset
    orphan_policy: str = "close"  # §21: an unknown ATLAS position is closed (or gets an SL attached)
    server_tz: str = "America/New_York"  # broker server clock = this zone + server_offset_hours
    server_offset_hours: float = 7.0
    request_budget_share: float = 0.90  # stop new entries at this share of the firm's daily request cap
    require_watchdog: bool = True  # a missing watchdog heartbeat is a HALT
    watchdog_heartbeat_file: str | None = None  # the EA's heartbeat file (MT5 Common\Files)
    commission_per_lot: dict | None = None  # round turn, account currency; per symbol
    max_spread_to_stop_ratio: float = 0.20  # §16
    news_blackout_min: tuple[int, int] = (10, 15)  # §16, needs a calendar feed (not wired yet)
    rollover_blackout_ny: tuple[str, str] = ("16:45", "18:15")  # §16
    friday_flatten_utc: str | None = "20:00"  # §18 session exit (forex; futures follow the session calendar)
    # Futures-first (docs/futures.md). platform picks the execution adapter; contracts pins the contract the
    # adapter trades for each product root (MES: MESZ6); no new entries within roll_days of its last trade
    # or first notice day; entry_cutoff_min / flatten_lead_min apply to the prop firm's flat-by time.
    platform: str = "mt5"
    contracts: dict | None = None
    roll_days: int = 5
    entry_cutoff_min: int = 30
    flatten_lead_min: int = 10
    no_trade_days: tuple[str, ...] = ()  # extra exchange no-trade dates (YYYY-MM-DD) on top of the rule set
    broker_poll_s: float = 5.0  # futures REST adapters: reuse a read this long (rate limits)
    demo_only: bool = False  # true: the pipeline refuses every proposal unless the broker reports a demo account
    defaults_used: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def load_execution_settings(root: str | Path = "config") -> ExecutionSettings:
    path = Path(root) / "atlas.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    ex, fl = raw.get("execution") or {}, raw.get("filters") or {}
    for section, allowed, name in ((ex, EXECUTION_KEYS, "execution"), (fl, FILTER_KEYS, "filters")):
        if not isinstance(section, dict):
            raise ConfigError(f"{path}: {name} must be a mapping")
        extra = set(section) - allowed
        if extra:
            raise ConfigError(f"{path}: unknown key(s) {', '.join(f'{name}.{k}' for k in sorted(extra))}")
    given = {**ex, **fl}
    kw = {}
    for f in fields(ExecutionSettings):
        if f.name in given:
            v = given[f.name]
            kw[f.name] = tuple(v) if isinstance(v, list) else v
    kw["defaults_used"] = tuple(sorted((EXECUTION_KEYS | FILTER_KEYS) - set(given)))
    if "platform" in kw:
        kw["platform"] = PLATFORM_ALIASES.get(str(kw["platform"]).lower(), kw["platform"])
    s = ExecutionSettings(**kw)
    checks = [
        (0 <= s.max_deviation_points <= 20, "execution.max_deviation_points must be in 0..20"),
        (10 <= s.reconcile_interval_s <= 300, "execution.reconcile_interval_s must be in 10..300"),
        (s.orphan_policy in ORPHAN_POLICIES, f"execution.orphan_policy must be one of {sorted(ORPHAN_POLICIES)}"),
        (0 < s.request_budget_share <= 1, "execution.request_budget_share must be in (0, 1]"),
        (0 < s.max_spread_to_stop_ratio <= 0.5, "filters.max_spread_to_stop_ratio must be in (0, 0.5]"),
        (s.platform in PLATFORM_NAMES, f"execution.platform must be one of {sorted(PLATFORM_NAMES)}"),
        (0 <= s.roll_days <= 30, "execution.roll_days must be in 0..30"),
        (0 <= s.flatten_lead_min <= s.entry_cutoff_min <= 240,
         "execution.flatten_lead_min <= execution.entry_cutoff_min <= 240"),
        (1 <= s.broker_poll_s <= 60, "execution.broker_poll_s must be in 1..60"),
        (s.contracts is None or isinstance(s.contracts, dict), "execution.contracts must map product roots to contracts"),
        (isinstance(s.demo_only, bool), "execution.demo_only must be true or false"),
    ]
    bad = [m for ok, m in checks if not ok]
    if bad:
        raise ConfigError(f"{path}: " + "; ".join(bad))
    for day in s.no_trade_days:
        try:
            dt.date.fromisoformat(str(day))
        except ValueError:
            raise ConfigError(f"{path}: execution.no_trade_days: {day!r} is not a YYYY-MM-DD date") from None
    return s


def pinned_contracts(s: ExecutionSettings, symbols, today: dt.date) -> dict[str, str]:
    """``{root: contract code}`` for every futures symbol, checked: each root pinned, each code a listed month
    of that root. Raises ConfigError otherwise, so a futures engine never starts without knowing what it trades."""
    from atlas_engine.futures import is_futures, parse_contract

    pins = {str(k).upper(): str(v).upper() for k, v in (s.contracts or {}).items()}
    out, bad = {}, []
    for sym in symbols:
        if not is_futures(sym):
            continue
        code = pins.get(sym)
        if code is None:
            bad.append(f"{sym}: no contract pinned in execution.contracts")
            continue
        try:
            root, _, _ = parse_contract(code, today)
        except (KeyError, ValueError) as e:
            bad.append(f"{sym}: {e}")
            continue
        if root != sym:
            bad.append(f"{sym}: pinned contract {code} is a {root} contract")
            continue
        out[sym] = code
    extra = set(pins) - set(symbols)
    if extra:
        bad.append(f"contracts pinned for symbols not traded: {sorted(extra)}")
    if bad:
        raise ConfigError("execution.contracts: " + "; ".join(bad))
    return out
