"""Intraday momentum in stock-index futures (Gao, Han, Li & Zhou 2018).

The S&P 500's return over the first half hour of the session, measured from
the prior day's 16:00 New York close (``signal_from: prev_close``) or from the
09:30 open (``open``) to ``signal_end`` (10:00), predicts the sign of the last
half hour. At the decision bar that closes at ``entry_at`` (15:30) New York the
setup enters in the direction of that return and leaves at ``exit_at`` (16:00)
the same day unless the protective stop, ``sl_atr`` x ATR from the decision
close, is hit first. A zero return, or a day missing any of the bars, trades
nothing. Needs decision bars that close on the half hour (M15 or M30).

Time-of-day based, so it is exempt from the after-open blackout.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atlas_engine.features import sessions

from .base import Setup, close_at, ny_close_clock, ny_time, timed_signals

DEFAULTS = {
    "signal_from": "prev_close",  # or "open" (09:30 New York)
    "signal_end": "10:00",
    "entry_at": "15:30",
    "exit_at": "16:00",
    "sl_atr": 2.0,
}


def prior_close(f: pd.DataFrame, days: pd.Index) -> pd.Series:
    """For each New York date, the latest 16:00 New York close on an earlier date."""
    closes = close_at(f, "16:00")
    pos = np.searchsorted(closes.index.to_numpy(), days.to_numpy(), side="left") - 1
    vals = closes.to_numpy(float)[np.clip(pos, 0, None)] if len(closes) else np.full(len(days), np.nan)
    return pd.Series(np.where(pos >= 0, vals, np.nan), index=days)


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    date, mins = ny_close_clock(f)
    if p["signal_from"] == "prev_close":
        ref = prior_close(f, pd.Index(sorted(set(date))))
    elif p["signal_from"] == "open":
        ref = close_at(f, "09:30")
    else:
        raise ValueError(f"signal_from must be 'prev_close' or 'open', not {p['signal_from']!r}")
    ret = close_at(f, p["signal_end"]) - ref
    direction = date.map(np.sign(ret)).fillna(0).astype(int)
    entry = mins == sessions._hhmm(p["entry_at"])
    exit_by = pd.Series(ny_time(date, p["exit_at"]), index=f.index)
    return timed_signals(f, entry & (direction != 0), direction, float(p["sl_atr"]), exit_by)


SETUP = Setup("intraday_momentum", "0.1.0", detect, DEFAULTS, session_based=True)
