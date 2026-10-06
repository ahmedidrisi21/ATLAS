"""Research-only setups for the MNQ round (docs/mnq-candidates.md).

These detectors turn the shared feature frame into signals for the T0 harness,
like ``atlas_engine.setups``, but live on the research side: none of them is
wired into the engine, and a strategy that passed would need engine work
first. Each signal carries ``exit_by``, the clock time at which the backtest
closes it at market unless its protective stop is hit first; that is how the
clock exits, the noise-area trail and the daily exit rules are expressed.

- ``late_day_momentum``: NQ review #2, last half hour in the direction of the
  return from the prior 16:00 close to 15:30.
- ``noise_area``: NQ review #3, half-hour breakouts of the 14-day noise area
  around the 09:30 open, with a band (or band + TWAP) trail checked at the
  half-hour checkpoints and flat at 16:00.
- ``opening_candle``: NQ review #4, the 09:30-09:35 candle's direction, stop
  at its far end.
- ``rsi2_pullback``: NQ review #5, RSI(2) (or three lower closes) pullbacks
  above the 200-day SMA, on a daily series of 16:00 New York closes.

Holidays and early-close days (``atlas_engine.futures.sessions.no_trade_days``)
never trade. All times are New York.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from atlas_engine.features import indicators as ind
from atlas_engine.features import sessions
from atlas_engine.futures.sessions import no_trade_days
from atlas_engine.setups import SETUPS, Setup
from atlas_engine.setups.base import SIGNAL_COLS, close_at, ny_close_clock, ny_time, timed_signals
from atlas_engine.setups.intraday_momentum import prior_close

RTH_OPEN = 9 * 60 + 30
RTH_CLOSE = 16 * 60


def trading_day(days) -> np.ndarray:
    """True for New York dates that are weekdays and not exchange holidays or early closes."""
    arr = np.asarray(list(days), dtype=object)
    if not len(arr):
        return np.zeros(0, bool)
    uniq, inv = np.unique(arr, return_inverse=True)
    ok = np.array([(d := pd.Timestamp(u).date()).weekday() < 5 and d not in no_trade_days(d.year) for u in uniq], bool)
    return ok[inv]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=SIGNAL_COLS[:-1] + ["exit_by"])


# --- review #2: late-day market intraday momentum ---------------------------

LATE_DAY_DEFAULTS = {"confirm": "none", "entry_at": "15:30", "exit_at": "16:00", "sl_atr": 1.0}


def late_day_momentum(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    """Sign of close(15:30) - close(prior 16:00); with ``confirm: last_half_hour`` 15:00-15:30 must agree."""
    date, mins = ny_close_clock(f)
    days = pd.Index(sorted(set(date)))
    at = close_at(f, p["entry_at"])
    sign = np.sign(at - prior_close(f, days).reindex(at.index))
    if p["confirm"] == "last_half_hour":
        half = sessions._hhmm(p["entry_at"]) - 30
        before = close_at(f, f"{half // 60:02d}:{half % 60:02d}").reindex(at.index)
        sign = sign.where(np.sign(at - before) == sign, 0)
    elif p["confirm"] != "none":
        raise ValueError(f"confirm must be 'none' or 'last_half_hour', not {p['confirm']!r}")
    sign = sign[trading_day(sign.index)] if len(sign) else sign
    direction = date.map(sign).fillna(0).astype(int)
    entry = mins == sessions._hhmm(p["entry_at"])
    exit_by = pd.Series(ny_time(date, p["exit_at"]), index=f.index)
    return timed_signals(f, entry & (direction != 0), direction, float(p["sl_atr"]), exit_by)


# --- review #3: noise-area intraday momentum ---------------------------------

NOISE_DEFAULTS = {"lookback": 14, "trail": "band", "first_check": "10:00", "last_check": "15:30", "exit_at": "16:00"}


def noise_area(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    """Half-hour checkpoint breakouts of the gap-adjusted noise area, with a checkpoint trail.

    sigma(t) for a checkpoint t is the mean of |close(t) / open(09:30) - 1| over
    the previous ``lookback`` sessions that have both. Upper band =
    max(open, prior 16:00 close) x (1 + sigma); lower = min(open, prior close)
    x (1 - sigma). At each checkpoint, in order: a long whose close is below
    its trail exits (band: the upper band; band_twap: the higher of the upper
    band and the session TWAP), a short likewise above the lower band (or the
    lower of it and the TWAP); then, if flat, a close above the upper band goes
    long and below the lower band goes short. Every open position is flat at
    ``exit_at``. The protective stop is the opposite band at entry.
    """
    if p["trail"] not in ("band", "band_twap"):
        raise ValueError(f"trail must be 'band' or 'band_twap', not {p['trail']!r}")
    idx = pd.DatetimeIndex(f.index)
    bar_min = int((pd.DatetimeIndex(f["close_time"]) - idx)[0].total_seconds() // 60) if len(f) else 5
    open_min = pd.Series(sessions.local_minutes(idx, sessions.NEW_YORK), index=f.index)
    date = pd.Series(np.asarray(sessions.local_date(idx, sessions.NEW_YORK)), index=f.index)
    close_min = open_min + bar_min
    rth = (open_min >= RTH_OPEN) & (close_min <= RTH_CLOSE)
    first, last = sessions._hhmm(p["first_check"]), sessions._hhmm(p["last_check"])
    checks = list(range(first, last + 1, 30))

    opens = f.loc[open_min == RTH_OPEN].groupby(date[open_min == RTH_OPEN])["open"].first()
    if opens.empty:
        return _empty()
    ck = rth & close_min.isin(checks)
    close_tab = f.loc[ck, "close"].groupby([date[ck], close_min[ck]]).last().unstack()
    close_tab = close_tab.reindex(index=opens.index, columns=checks)
    move = (close_tab.div(opens, axis=0) - 1).abs()
    sigma = pd.DataFrame({c: move[c].dropna().rolling(int(p["lookback"])).mean().shift(1).reindex(move.index)
                          for c in checks})
    pc = prior_close(f, opens.index).reindex(opens.index).fillna(opens)
    upper = sigma.mul(np.maximum(opens, pc), axis=0) + np.maximum(opens, pc).to_numpy()[:, None]
    lower = np.minimum(opens, pc).to_numpy()[:, None] - sigma.mul(np.minimum(opens, pc), axis=0)

    typical = (f["high"] + f["low"] + f["close"]) / 3
    in_rth = typical.where(rth)
    tw = in_rth.fillna(0.0).groupby(date).cumsum() / in_rth.notna().groupby(date).cumsum()
    twap_tab = tw.loc[ck].groupby([date[ck], close_min[ck]]).last().unstack().reindex(index=opens.index, columns=checks)
    row_tab = pd.Series(f.index[ck], index=pd.MultiIndex.from_arrays([date[ck], close_min[ck]])).groupby(level=[0, 1]).last()

    use_twap = p["trail"] == "band_twap"
    exit_at = sessions._hhmm(p["exit_at"])
    ok_day = dict(zip(opens.index, trading_day(opens.index)))
    rows = []
    for day in opens.index:
        if not ok_day[day]:
            continue
        pos, open_trade = 0, None
        for c in checks:
            x, ub, lb = close_tab.at[day, c], upper.at[day, c], lower.at[day, c]
            if not (np.isfinite(x) and np.isfinite(ub) and np.isfinite(lb)):
                continue
            w = twap_tab.at[day, c] if use_twap else np.nan
            when = _ny(day, c)
            if pos == 1 and x < (max(ub, w) if np.isfinite(w) else ub):
                open_trade["exit_by"], pos = when, 0
            elif pos == -1 and x > (min(lb, w) if np.isfinite(w) else lb):
                open_trade["exit_by"], pos = when, 0
            if pos == 0 and (x > ub or x < lb):
                pos = 1 if x > ub else -1
                r = f.loc[row_tab[(day, c)]]
                open_trade = {"decision_time": r["close_time"], "direction": pos, "stop": lb if pos == 1 else ub,
                              "atr": r["atr"], "spread": r["spread"], "exit_by": _ny(day, exit_at)}
                rows.append(open_trade)
    if not rows:
        return _empty()
    out = pd.DataFrame(rows)
    out["exit_by"] = pd.DatetimeIndex(out["exit_by"]).as_unit("ns")
    return out


def _ny(day, minutes: int) -> pd.Timestamp:
    return pd.Timestamp(dt.datetime.combine(pd.Timestamp(day).date(), dt.time(minutes // 60, minutes % 60)),
                        tz=sessions.NEW_YORK).tz_convert("UTC")


# --- review #4: five-minute opening candle -----------------------------------

CANDLE_DEFAULTS = {"doji_pts": 0.25, "min_stop_pts": 4.0, "exit_at": "16:00"}


def opening_candle(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    """Direction of the decision bar that opens at 09:30; stop at its far end, at least ``min_stop_pts`` away."""
    idx = pd.DatetimeIndex(f.index)
    open_min = sessions.local_minutes(idx, sessions.NEW_YORK)
    date = pd.Series(np.asarray(sessions.local_date(idx, sessions.NEW_YORK)), index=f.index)
    first = pd.Series(open_min == RTH_OPEN, index=f.index)
    first &= pd.Series(trading_day(date), index=f.index, dtype=bool)
    body = f["close"] - f["open"]
    direction = pd.Series(np.where(body.abs() >= float(p["doji_pts"]), np.sign(body), 0), index=f.index).astype(int)
    out = []
    m = float(p["min_stop_pts"])
    for d in (1, -1):
        hit = first & (direction == d)
        far = f["low"] if d == 1 else f["high"]
        dist = (d * (f["close"] - far)).clip(lower=m)
        stop = f["close"] - d * dist
        sig = _emit(f, hit, d, stop)
        sig["exit_by"] = pd.DatetimeIndex(ny_time(date.loc[hit], p["exit_at"])).as_unit("ns")
        out.append(sig)
    return pd.concat(out, ignore_index=True)


def _emit(f: pd.DataFrame, mask: pd.Series, d: int, stop: pd.Series) -> pd.DataFrame:
    rows = f.loc[mask]
    return pd.DataFrame({"decision_time": rows["close_time"].to_numpy(), "direction": d, "stop": stop.loc[mask].to_numpy(),
                         "atr": rows["atr"].to_numpy(), "spread": rows["spread"].to_numpy()})


# --- review #5: RSI(2) pullback in an uptrend, daily ---------------------------

RSI2_DEFAULTS = {"trigger": "rsi2", "rsi_len": 2, "rsi_entry": 10.0, "rsi_exit": 70.0, "trend_sma": 200, "exit_sma": 5,
                 "max_sessions": 5, "atr_len": 14, "sl_atr": 3.0}


def daily_bars(f: pd.DataFrame, close_hhmm: str = "16:00") -> pd.DataFrame:
    """One row per New York session that has a decision bar closing at ``close_hhmm``.

    A session runs from the prior day's ``close_hhmm`` to its own (bars closing
    after it belong to the next date). Holidays and early-close days are
    dropped. Columns: open, high, low, close, decision_time, row (the feature
    index of the closing bar).
    """
    ct = pd.DatetimeIndex(f["close_time"]).tz_convert(sessions.NEW_YORK)
    cut = sessions._hhmm(close_hhmm)
    shift = pd.Timedelta(minutes=24 * 60 - cut) - pd.Timedelta(microseconds=1)
    sess = pd.Series(np.asarray((ct + shift).date), index=f.index)
    mins = pd.Series(ct.hour * 60 + ct.minute, index=f.index)
    closing = (mins == cut) & (sess.to_numpy() == np.asarray(ct.date))
    agg = f.groupby(sess).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"))
    last = pd.DataFrame({"close": f.loc[closing, "close"].to_numpy(), "decision_time": f.loc[closing, "close_time"].to_numpy(),
                         "row": f.index[closing]}, index=sess[closing].to_numpy())
    d = last.join(agg, how="left")
    d = d.loc[trading_day(d.index)] if len(d) else d
    d.index.name = "session"
    return d


def rsi(close: pd.Series, length: int) -> pd.Series:
    """Wilder RSI."""
    delta = close.diff()
    up = ind.rma(delta.clip(lower=0), length)
    dn = ind.rma(-delta.clip(upper=0), length)
    out = 100 - 100 / (1 + up / dn)
    return out.where(dn > 0, 100.0).where(up.notna())


def rsi2_pullback(f: pd.DataFrame, p: dict) -> pd.DataFrame:
    d = daily_bars(f)
    if len(d) < 2:
        return _empty()
    c = d["close"]
    r = rsi(c, int(p["rsi_len"]))
    trend = c.rolling(int(p["trend_sma"])).mean()
    fast = c.rolling(int(p["exit_sma"])).mean()
    atr = ind.atr(d["high"], d["low"], c, int(p["atr_len"]))
    if p["trigger"] == "rsi2":
        trig = r < float(p["rsi_entry"])
    elif p["trigger"] == "three_lower_closes":
        trig = (c < c.shift(1)) & (c.shift(1) < c.shift(2)) & (c.shift(2) < c.shift(3))
    else:
        raise ValueError(f"trigger must be 'rsi2' or 'three_lower_closes', not {p['trigger']!r}")
    entry = (trig & (c > trend) & atr.notna()).to_numpy()
    done = ((c > fast) | (r > float(p["rsi_exit"]))).to_numpy()
    times = pd.DatetimeIndex(d["decision_time"])
    rows, n, k = [], len(d), int(p["max_sessions"])
    for i in np.flatnonzero(entry):
        j = next((j for j in range(i + 1, min(i + k, n - 1) + 1) if done[j]), min(i + k, n - 1))
        exit_by = times[j] if j > i else times[i] + pd.Timedelta(days=7)  # no later session: runs to the end of data
        row = f.loc[d["row"].iloc[i]]
        rows.append({"decision_time": times[i], "direction": 1, "stop": c.iloc[i] - float(p["sl_atr"]) * atr.iloc[i],
                     "atr": row["atr"], "spread": row["spread"], "exit_by": exit_by})
    if not rows:
        return _empty()
    out = pd.DataFrame(rows)
    out["exit_by"] = pd.DatetimeIndex(out["exit_by"]).as_unit("ns")
    return out


LATE_DAY_MOMENTUM = Setup("late_day_momentum", "0.1.0", late_day_momentum, LATE_DAY_DEFAULTS, session_based=True)
NOISE_AREA = Setup("noise_area", "0.1.0", noise_area, NOISE_DEFAULTS, session_based=True)
OPENING_CANDLE = Setup("opening_candle", "0.1.0", opening_candle, CANDLE_DEFAULTS, session_based=True)
RSI2_PULLBACK = Setup("rsi2_pullback", "0.1.0", rsi2_pullback, RSI2_DEFAULTS, session_based=True)

RESEARCH_SETUPS: dict[str, Setup] = {s.name: s for s in (LATE_DAY_MOMENTUM, NOISE_AREA, OPENING_CANDLE, RSI2_PULLBACK)}
ALL_SETUPS: dict[str, Setup] = {**SETUPS, **RESEARCH_SETUPS}
