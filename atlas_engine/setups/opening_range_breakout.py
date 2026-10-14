"""Opening-range breakout at the US cash open (stock-index futures, e.g. MES).

The opening range is the high and low of the decision bars that open in the
first ``range_minutes`` after 09:30 New York. The first decision-bar close
beyond either edge, between the end of the range and ``entry_end`` New York,
enters in the direction of the break; one trade per day. The stop sits
``sl_range_frac`` x the range width back from the broken edge (1.0 = the far
edge, 0.5 = the middle of the range). The trade leaves at ``exit_at`` New York
unless the stop or the exit policy's target comes first, so it never holds
overnight. Weekends and days without a 09:30 bar have no range and no trades.

Time-of-day based, so it is exempt from the after-open blackout.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atlas_engine.features import sessions

from .base import Setup, emit

DEFAULTS = {
    "range_start": "09:30",  # New York clock
    "range_minutes": 15,
    "entry_end": "12:00",
    "exit_at": "15:55",
    "sl_range_frac": 1.0,
}


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    idx = pd.DatetimeIndex(f.index)
    ny_min = pd.Series(sessions.local_minutes(idx, sessions.NEW_YORK), index=f.index)
    ny_date = pd.Series(np.asarray(sessions.local_date(idx, sessions.NEW_YORK)), index=f.index)
    start = sessions._hhmm(p["range_start"])
    range_end = start + int(p["range_minutes"])
    # Range bars: open inside [start, start + range_minutes). Each has closed by
    # the close of the first eligible bar, which opens at or after range_end.
    in_range = (ny_min >= start) & (ny_min < range_end)
    rng = f.loc[in_range].groupby(ny_date[in_range]).agg(or_high=("high", "max"), or_low=("low", "min"))
    has_open = f.loc[ny_min == start].groupby(ny_date[ny_min == start]).size()
    rng = rng.loc[rng.index.isin(has_open.index)]
    or_high = ny_date.map(rng["or_high"])
    or_low = ny_date.map(rng["or_low"])
    width = or_high - or_low

    # Eligible bars must also close by entry_end.
    bar = (pd.DatetimeIndex(f["close_time"]) - idx).to_numpy()
    close_min = ny_min + pd.Series(bar, index=f.index).dt.total_seconds().div(60).astype(int)
    window = (ny_min >= range_end) & (close_min <= sessions._hhmm(p["entry_end"])) & (width > 0)

    up = window & (f["close"] > or_high)
    dn = window & (f["close"] < or_low)
    first = (up | dn) & ((up | dn).astype(int).groupby(ny_date).cumsum() == 1)

    exit_hhmm = sessions._hhmm(p["exit_at"])
    out = []
    for d, hit, level in ((1, first & up, or_high), (-1, first & dn, or_low)):
        stop = level - d * float(p["sl_range_frac"]) * width
        sig = emit(f, hit, d, stop)
        days = ny_date.loc[hit.fillna(False) & stop.notna()]
        exit_local = pd.to_datetime(days.astype(str)) + pd.Timedelta(minutes=exit_hhmm)
        sig["exit_by"] = pd.DatetimeIndex(exit_local).tz_localize(sessions.NEW_YORK).tz_convert("UTC")
        out.append(sig)
    return pd.concat(out, ignore_index=True)


SETUP = Setup("opening_range_breakout", "0.1.0", detect, DEFAULTS, session_based=True)
