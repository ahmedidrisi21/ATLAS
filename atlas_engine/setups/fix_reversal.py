"""Fix reversal: fade the last hour's move into a daily FX fix.

Krohn, Mueller and Whelan (Journal of Finance, 2024) find that the return in
the last hour before each major fix predicts the return over the following
window with a negative sign: dealers hedging ahead of the fix push the price,
and it comes back once the fix flow has cleared. The setup decides at the fix
(M15 bar close), trades against the last hour's move and leaves at the end of
the paper's next window unless the stop or the exit policy's target comes
first. Time-based, so it is exempt from the after-open blackout.

Fix times are local, so daylight saving is handled by the time zone:

- ``tokyo``:  decide 10:00 Tokyo (the 09:55 fix), exit 02:00 New York.
- ``ecb``:    decide 14:15 Frankfurt, exit 16:00 London (the London fix).
- ``london``: decide 16:00 London (WM/R fix), exit 16:30 New York, before
  the rollover blackout.

``min_move_atr`` keeps only fixes whose last-hour move is at least that many
H1 ATRs. The stop is ``sl_atr`` H1 ATRs from the decision close, and the
signal's ``atr`` is the H1 ATR so the random control copies stop sizes in the
same unit.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atlas_engine.features import sessions

from .base import Setup

FIXES = {
    # fix: (decision tz, decision hh:mm, exit tz, exit hh:mm)
    "tokyo": ("Asia/Tokyo", "10:00", sessions.NEW_YORK, "02:00"),
    "ecb": ("Europe/Berlin", "14:15", sessions.LONDON, "16:00"),
    "london": (sessions.LONDON, "16:00", sessions.NEW_YORK, "16:30"),
}

DEFAULTS = {
    "fix": "london",
    "min_move_atr": 0.0,
    "sl_atr": 1.5,
}


def next_local(t: pd.DatetimeIndex, tz: str, hhmm: str) -> pd.DatetimeIndex:
    """The first ``hhmm`` wall-clock time in ``tz`` after each timestamp, in UTC."""
    h, m = map(int, hhmm.split(":"))
    local = t.tz_convert(tz).tz_localize(None)  # wall clock, so a DST change never shifts it
    at = local.normalize() + pd.Timedelta(hours=h, minutes=m)
    at = at.where(at > local, at + pd.Timedelta(days=1))
    return at.tz_localize(tz, ambiguous=True, nonexistent="shift_forward").tz_convert("UTC")


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    dec_tz, dec_hhmm, exit_tz, exit_hhmm = FIXES[p["fix"]]
    close_time = pd.DatetimeIndex(f["close_time"])
    at_fix = (sessions.local_minutes(close_time, dec_tz) == sessions._hhmm(dec_hhmm)) & (close_time.tz_convert(dec_tz).weekday < 5)

    # Last hour's move: decision close minus the close one hour earlier.
    close = pd.Series(f["close"].to_numpy(), index=close_time)
    hour_ago = close.reindex(close_time - pd.Timedelta(hours=1)).to_numpy()
    move = f["close"].to_numpy() - hour_ago
    atr = f["h1_atr"].to_numpy()
    ok = at_fix & np.isfinite(move) & np.isfinite(atr) & (move != 0) & (np.abs(move) >= p["min_move_atr"] * atr)

    rows = f.loc[ok]
    d = -np.sign(move[ok]).astype(int)
    stop = rows["close"].to_numpy() - d * p["sl_atr"] * atr[ok]
    decision = pd.DatetimeIndex(rows["close_time"])
    exit_by = next_local(decision, exit_tz, exit_hhmm)
    return pd.DataFrame(
        {
            "decision_time": decision,
            "direction": d,
            "stop": stop,
            "atr": atr[ok],
            "spread": rows["spread"].to_numpy(),
            "exit_by": exit_by,
        }
    )


SETUP = Setup("fix_reversal", "0.1.0", detect, DEFAULTS, session_based=True)
