"""Common pieces for deterministic setup detectors (PRD §16).

A setup maps the shared feature frame to signals. Each signal is decided at a
bar's close and carries a direction and an initial stop price; targets are
set by the exit policy from the realised entry, so setups stay exit-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from atlas_engine.features import sessions

SIGNAL_COLS = ["decision_time", "direction", "stop", "atr", "spread", "setup"]


@dataclass(frozen=True)
class EdgeFilters:
    """Edge-protection rules shared by every setup (§16)."""

    max_spread_to_stop: float = 0.20
    after_open_minutes: int = 15
    friday_no_entry_after_utc: str | None = "18:00"  # None: Friday entries allowed (intraday setups)


@dataclass(frozen=True)
class Setup:
    name: str
    version: str
    detect: Callable[[pd.DataFrame, dict], pd.DataFrame]
    defaults: dict = field(default_factory=dict)
    session_based: bool = False  # exempt from the after-open blackout

    def signals(self, features: pd.DataFrame, params: dict | None = None, filters: EdgeFilters = EdgeFilters()) -> pd.DataFrame:
        p = {**self.defaults, **(params or {})}
        raw = self.detect(features, p)
        return apply_edge_filters(raw, features, filters, self.session_based).assign(setup=self.name)


def structure_stop(
    close: pd.Series, level: pd.Series, direction: int, atr: pd.Series, buffer_atr: float, min_atr: float, max_atr: float
) -> pd.Series:
    """Stop beyond a structure level, floored at ``min_atr`` and NaN beyond ``max_atr``."""
    raw = level - direction * buffer_atr * atr
    dist = direction * (close - raw)
    dist = dist.where(dist >= min_atr * atr, min_atr * atr)
    return (close - direction * dist).where(dist <= max_atr * atr)


def first_per_day(mask: pd.Series, day: pd.Series) -> pd.Series:
    """Keep only the first True per day."""
    return mask & (mask.astype(int).groupby(day).cumsum() == 1)


def emit(features: pd.DataFrame, mask: pd.Series, direction: int, stop: pd.Series) -> pd.DataFrame:
    m = mask.fillna(False).astype(bool) & stop.notna()
    rows = features.loc[m]
    return pd.DataFrame(
        {
            "decision_time": rows["close_time"].to_numpy(),
            "direction": direction,
            "stop": stop.loc[m].to_numpy(),
            "atr": rows["atr"].to_numpy(),
            "spread": rows["spread"].to_numpy(),
        }
    )


def apply_edge_filters(signals: pd.DataFrame, features: pd.DataFrame, f: EdgeFilters, session_based: bool) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame(columns=SIGNAL_COLS[:-1])
    s = signals.sort_values("decision_time").reset_index(drop=True)  # extra columns (e.g. exit_by) pass through
    t = pd.DatetimeIndex(s["decision_time"])
    close = features.set_index("close_time")["close"].reindex(t).to_numpy()
    stop_dist = np.abs(close - s["stop"].to_numpy())
    keep = s["spread"].to_numpy() <= f.max_spread_to_stop * stop_dist
    keep &= ~sessions.rollover_blackout(t)
    keep &= ~sessions.friday_cutoff(t, f.friday_no_entry_after_utc)
    if not session_based:
        keep &= ~sessions.after_open_blackout(t, f.after_open_minutes)
    return s.loc[keep].reset_index(drop=True)


# New York clock helpers for time-of-day setups (stock-index futures).


def ny_close_clock(features: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Each decision bar's close as New York (date, minutes after midnight), indexed like ``features``."""
    ct = pd.DatetimeIndex(features["close_time"])
    date = pd.Series(np.asarray(sessions.local_date(ct, sessions.NEW_YORK)), index=features.index)
    mins = pd.Series(sessions.local_minutes(ct, sessions.NEW_YORK), index=features.index)
    return date, mins


def close_at(features: pd.DataFrame, hhmm: str) -> pd.Series:
    """Close of the decision bar that closes at ``hhmm`` New York, by New York date."""
    date, mins = ny_close_clock(features)
    at = mins == sessions._hhmm(hhmm)
    return features.loc[at, "close"].groupby(date[at]).last()


def ny_time(days, hhmm: str) -> pd.DatetimeIndex:
    """``hhmm`` New York on each date in ``days``, in UTC (DST-aware)."""
    local = pd.to_datetime(pd.Series(days).astype(str)) + pd.Timedelta(minutes=sessions._hhmm(hhmm))
    return pd.DatetimeIndex(local).tz_localize(sessions.NEW_YORK).tz_convert("UTC")


def timed_signals(features: pd.DataFrame, rows: pd.Series, direction: pd.Series, sl_atr: float, exit_by: pd.Series) -> pd.DataFrame:
    """Signals at the bars flagged in ``rows``: stop ``sl_atr`` x ATR from the close, time exit ``exit_by``."""
    out = []
    for d in (1, -1):
        hit = rows & (direction == d)
        stop = features["close"] - d * sl_atr * features["atr"]
        sig = emit(features, hit, d, stop)
        sig["exit_by"] = pd.DatetimeIndex(exit_by.loc[hit.fillna(False) & stop.notna()]).as_unit("ns")
        out.append(sig)
    return pd.concat(out, ignore_index=True)
