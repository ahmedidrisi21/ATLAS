"""Overnight drift in stock-index futures (Cooper, Cliff & Gulen 2008; Boyarchenko, Larsen & Whelan 2023).

Most of the S&P 500's return accrues outside US cash hours. At the decision bar
that closes at ``entry_at`` (16:00) New York the setup buys and leaves at
``exit_at`` New York on the next weekday (09:30 = the next open; 04:00 = a short
hold through the European open) unless the protective stop, ``sl_atr`` x ATR
from the decision close, is hit first. Long only. Friday entries are left to
the edge filters (the Friday entry cut-off refuses them, so nothing is held
over the weekend); a holiday on the exit day exits at the next bar quoted.

Time-of-day based, so it is exempt from the after-open blackout.
"""

from __future__ import annotations

import pandas as pd

from atlas_engine.features import sessions

from .base import Setup, ny_close_clock, ny_time, timed_signals

DEFAULTS = {
    "entry_at": "16:00",  # New York
    "exit_at": "09:30",  # New York, next weekday
    "sl_atr": 3.0,
}


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    date, mins = ny_close_clock(f)
    entry = mins == sessions._hhmm(p["entry_at"])
    next_day = (pd.to_datetime(date.astype(str)) + pd.offsets.BDay(1)).dt.date
    exit_by = pd.Series(ny_time(next_day, p["exit_at"]), index=f.index)
    return timed_signals(f, entry, pd.Series(1, index=f.index), float(p["sl_atr"]), exit_by)


SETUP = Setup("overnight_drift", "0.1.0", detect, DEFAULTS, session_based=True)
