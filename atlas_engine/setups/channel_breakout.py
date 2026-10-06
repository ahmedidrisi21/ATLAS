"""Channel breakout: close beyond the highest high / lowest low of the last N bars.

The classic trend-following entry (Donchian channel). Meant for H4 decision
bars, where multi-day trends are the effect and costs are small next to the
stop. The stop is a fixed ATR multiple from the decision close. ``sides`` limits it to
``long`` or ``short`` breakouts (default ``both``).
"""

from __future__ import annotations

import pandas as pd

from .base import Setup, emit

DEFAULTS = {
    "channel": 20,
    "sl_atr": 1.5,
    "sides": "both",  # or "long" / "short"
}

SIDES = {"both": (1, -1), "long": (1,), "short": (-1,)}


def detect(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    n = int(p["channel"])
    if p["sides"] not in SIDES:
        raise ValueError(f"sides must be one of {sorted(SIDES)}, not {p['sides']!r}")
    out = []
    for d in SIDES[p["sides"]]:
        edge = f["high"].rolling(n).max().shift(1) if d == 1 else f["low"].rolling(n).min().shift(1)
        beyond = d * (f["close"] - edge) > 0
        fresh = beyond & ~beyond.shift(1, fill_value=False)
        stop = f["close"] - d * p["sl_atr"] * f["atr"]
        out.append(emit(f, fresh, d, stop))
    return pd.concat(out, ignore_index=True)


SETUP = Setup("channel_breakout", "0.1.0", detect, DEFAULTS)
