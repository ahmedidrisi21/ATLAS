"""Opening gap at the US cash open (stock-index futures).

The gap is the move from the prior day's 16:00 New York close to the close of
the decision bar that ends at 09:30 New York. ``mode: fade`` trades against it,
``follow`` with it, entering at 09:30 and leaving at ``exit_at`` New York the
same day unless the protective stop, ``sl_atr`` x ATR from the decision close,
is hit first. A zero gap, or a day without a prior close, trades nothing.
Needs decision bars that close on the half hour (M15 or M30).

Time-of-day based, so it is exempt from the after-open blackout.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atlas_engine.features import sessions

from .base import Setup, ny_close_clock, ny_time, timed_signals
from .intraday_momentum import prior_close

DEFAULTS = {
    "mode": "fade",  # or "follow"
    "entry_at": "09:30",
    "exit_at": "10:30",
    "sl_atr": 4.0,
}


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    if p["mode"] not in ("fade", "follow"):
        raise ValueError(f"mode must be 'fade' or 'follow', not {p['mode']!r}")
    date, mins = ny_close_clock(f)
    entry = mins == sessions._hhmm(p["entry_at"])
    ref = date.map(prior_close(f, pd.Index(sorted(set(date)))))
    gap = np.sign(f["close"] - ref).fillna(0).astype(int)
    direction = -gap if p["mode"] == "fade" else gap
    exit_by = pd.Series(ny_time(date, p["exit_at"]), index=f.index)
    return timed_signals(f, entry & (direction != 0), direction, float(p["sl_atr"]), exit_by)


SETUP = Setup("opening_gap", "0.1.0", detect, DEFAULTS, session_based=True)
