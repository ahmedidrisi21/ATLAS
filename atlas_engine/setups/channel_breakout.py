"""Channel breakout: close beyond the highest high / lowest low of the last N bars.

The classic trend-following entry (Donchian channel). Meant for H4 decision
bars, where multi-day trends are the effect and costs are small next to the
stop. The stop is a fixed ATR multiple from the decision close.

With ``min_carry`` set (and a ``carry`` feature: base minus quote policy rate,
% a year), only breakouts whose direction earns at least that much carry are
taken, so trend and carry agree.
"""

from __future__ import annotations

import pandas as pd

from .base import Setup, emit

DEFAULTS = {
    "channel": 20,
    "sl_atr": 1.5,
    "min_carry": None,
}


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    n = int(p["channel"])
    out = []
    for d in (1, -1):
        edge = f["high"].rolling(n).max().shift(1) if d == 1 else f["low"].rolling(n).min().shift(1)
        beyond = d * (f["close"] - edge) > 0
        fresh = beyond & ~beyond.shift(1, fill_value=False)
        stop = f["close"] - d * p["sl_atr"] * f["atr"]
        if p["min_carry"] is not None:
            fresh &= d * f["carry"] >= p["min_carry"]
        sig = emit(f, fresh, d, stop)
        if "carry" in f:
            sig["carry"] = f.loc[fresh.fillna(False).astype(bool) & stop.notna(), "carry"].to_numpy()
        out.append(sig)
    return pd.concat(out, ignore_index=True)


SETUP = Setup("channel_breakout", "0.2.0", detect, DEFAULTS)
