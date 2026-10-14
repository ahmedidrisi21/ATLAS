"""Daily trend filter against buy-and-hold, for information (NQ review #1, MNQ round 1).

Long the index while the 16:00 New York close is above its ``sma``-session
average and its ``momentum_sessions`` return is positive; flat otherwise. The
signal is decided at a close and executed at the next session's 09:30 open.
One contract's notional, unlevered: each day earns the overnight leg (prior
close to open) and the day leg (open to close) of the mid price when held.
Each switch pays half the round-trip cost in index points at the open.

It trades a few times a year, so it is not put through the T0 gates. It is
compared with buy-and-hold over the same period (long from the first open to
the last close, one round trip) on total return, CAGR, maximum drawdown and
annualized Sharpe of daily returns, and logged as ``kind: information``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Callable

import numpy as np
import pandas as pd

from atlas_engine.features import indicators as ind
from atlas_engine.features import sessions
from atlas_engine.market_data.bars import with_mid

from .data import check_not_holdout
from .registry import Registry
from .research_setups import trading_day

KIND = "information"


def daily_open_close(m1: pd.DataFrame) -> pd.DataFrame:
    """Per New York trading day: the 09:30 open, the 16:00 close and the session's high/low (mid prices).

    The open is the first one-minute mid at or after 09:30 (within 5 minutes);
    the close is the last one-minute mid before 16:00 (within 5 minutes). The
    session high/low run from the prior 16:00 to this 16:00. Days missing
    either price, holidays and early closes are dropped.
    """
    m = with_mid(m1)
    loc = m.index.tz_convert(sessions.NEW_YORK)
    mins = loc.hour * 60 + loc.minute
    date = pd.Index(loc.date)
    op = m.loc[(mins >= 570) & (mins < 575), "open"].groupby(date[(mins >= 570) & (mins < 575)]).first()
    cl = m.loc[(mins >= 955) & (mins < 960), "close"].groupby(date[(mins >= 955) & (mins < 960)]).last()
    sess = pd.Index((loc + pd.Timedelta(minutes=8 * 60)).date)  # bars from 16:00 on belong to the next session
    hl = m.groupby(sess).agg(high=("high", "max"), low=("low", "min"))
    d = pd.DataFrame({"open": op, "close": cl}).join(hl, how="left").dropna(subset=["open", "close"])
    d = d.loc[trading_day(d.index)] if len(d) else d
    d.index.name = "session"
    return d


def signal(d: pd.DataFrame, sma: int, momentum_sessions: int) -> pd.Series:
    """1 while close > SMA and the ``momentum_sessions`` return > 0 (decided at each close), else 0; NaN while warming up."""
    c = d["close"]
    avg = c.rolling(sma).mean()
    mom = c / c.shift(momentum_sessions) - 1
    s = ((c > avg) & (mom > 0)).astype(float)
    return s.where(avg.notna() & mom.notna())


def _in(d: pd.DataFrame, period) -> pd.Index:
    when = pd.to_datetime(d.index)
    return d.index[(when >= period[0].tz_localize(None)) & (when < period[1].tz_localize(None))]


def backtest(d: pd.DataFrame, sig: pd.Series, period: tuple[pd.Timestamp, pd.Timestamp], round_trip_pts: float) -> dict:
    """Daily returns of the filter and of buy-and-hold over ``period`` (dates), after ``round_trip_pts`` per round trip."""
    days = _in(d, period)
    if len(days) < 2:
        raise ValueError("period has fewer than two trading days")
    pos_day = sig.shift(1).reindex(days).fillna(0.0)  # decided at the prior close, entered at this open
    pos_night = pos_day.shift(1).fillna(0.0)          # carried from the prior day leg into this open; flat at the start
    o, c = d.loc[days, "open"], d.loc[days, "close"]
    night = (o / d["close"].shift(1).reindex(days) - 1).fillna(0.0)
    night.iloc[0] = 0.0  # the period starts at its first open
    day = c / o - 1
    switch = (pos_day - pos_night).abs()  # trades at this open
    half = round_trip_pts / 2
    cost = switch * half / o
    cost.iloc[-1] += pos_day.iloc[-1] * half / c.iloc[-1]  # still long at the end: closed at the last close
    ret = (1 + pos_night * night) * (1 + pos_day * day) - 1 - cost
    bh = (1 + night) * (1 + day) - 1
    bh.iloc[0] -= half / o.iloc[0]
    bh.iloc[-1] -= half / c.iloc[-1]
    return {"strategy": ret, "buy_and_hold": bh, "position": pos_day, "round_trips": int((pos_day.diff().fillna(pos_day) > 0).sum())}


def stats(ret: pd.Series) -> dict:
    eq = (1 + ret).cumprod()
    years = max((pd.Timestamp(ret.index[-1]) - pd.Timestamp(ret.index[0])).days / 365.25, 1e-9)
    dd = float((1 - eq / eq.cummax()).max())
    sd = float(ret.std(ddof=1))
    return {"total_return": float(eq.iloc[-1] - 1), "cagr": float(eq.iloc[-1] ** (1 / years) - 1), "max_drawdown": dd,
            "sharpe": float(ret.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0, "days": len(ret)}


def trades_r(d: pd.DataFrame, sig: pd.Series, period, round_trip_pts: float, r_unit_atr: float, atr_len: int = 14) -> pd.DataFrame:
    """Each long spell as a trade, in R with 1R = ``r_unit_atr`` x daily ATR at the decision (no stop is placed).

    A spell enters at the open after the signal turns on and exits at the open
    after it turns off, or at the period's last close if it is still on.
    """
    atr = ind.atr(d["high"], d["low"], d["close"], atr_len)
    days = _in(d, period)
    pos = sig.shift(1).reindex(days).fillna(0.0).to_numpy()
    rows, k = [], 0
    while k < len(days):
        if pos[k] != 1:
            k += 1
            continue
        j = k
        while j + 1 < len(days) and pos[j + 1] == 1:
            j += 1
        start = days[k]
        decided = d.index[d.index.get_loc(start) - 1]
        entry, risk = d.at[start, "open"], r_unit_atr * atr.loc[decided]
        if j + 1 < len(days):
            out_day, px = days[j + 1], d.at[days[j + 1], "open"]
        else:
            out_day, px = days[j], d.at[days[j], "close"]
        rows.append({"entry_day": start, "exit_day": out_day, "entry": entry, "exit": px, "risk": risk,
                     "r_gross": (px - entry) / risk, "r": (px - entry - round_trip_pts) / risk})
        k = j + 1
    return pd.DataFrame(rows)


def run_information(
    name: str,
    cfg: dict,
    load_m1: Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame],
    registry: Registry,
    now: dt.datetime | None = None,
) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    icfg = cfg["information"][name]
    period = tuple(pd.Timestamp(x, tz="UTC") for x in icfg["period"])
    check_not_holdout(period[1], pd.Timestamp(cfg["segments"]["holdout_start"]))
    m1 = load_m1(icfg["symbol"], pd.Timestamp(icfg["warmup_start"], tz="UTC"), period[1])
    d = daily_open_close(m1)
    sig = signal(d, int(icfg["sma"]), int(icfg["momentum_sessions"]))
    segs = {"all": period,
            "dev": tuple(pd.Timestamp(x, tz="UTC") for x in cfg["segments"]["dev"]),
            "validation": tuple(pd.Timestamp(x, tz="UTC") for x in cfg["segments"]["validation"])}
    out = {"tiers": {}}
    for tier, rt in icfg["round_trip_pts"].items():
        res = {}
        for seg, w in segs.items():
            bt = backtest(d, sig, w, float(rt))
            res[seg] = {"strategy": stats(bt["strategy"]), "buy_and_hold": stats(bt["buy_and_hold"]),
                        "exposure": float(bt["position"].mean()), "round_trips": bt["round_trips"]}
            if seg == "all":
                yrs = {}
                for y in sorted(set(pd.to_datetime(bt["strategy"].index).year)):
                    sel = pd.to_datetime(bt["strategy"].index).year == y
                    yrs[int(y)] = {"strategy": float((1 + bt["strategy"][sel]).prod() - 1),
                                   "buy_and_hold": float((1 + bt["buy_and_hold"][sel]).prod() - 1),
                                   "exposure": float(bt["position"][sel].mean())}
                res["by_year"] = yrs
                tr = trades_r(d, sig, w, float(rt), float(icfg["r_unit_atr"]))
                res["trades"] = {"count": len(tr), "expectancy_r": float(tr["r"].mean()) if len(tr) else 0.0,
                                 "gross_r": float(tr["r_gross"].mean()) if len(tr) else 0.0,
                                 "list": tr.assign(entry_day=tr["entry_day"].astype(str), exit_day=tr["exit_day"].astype(str)).to_dict("records") if len(tr) else []}
        out["tiers"][tier] = res
    first_signal = sig.dropna()
    out["first_signal_day"] = str(first_signal.index[0]) if len(first_signal) else None
    out["sessions"] = len(d)
    exp_id = f"{name}-{now:%Y%m%d-%H%M%S}-" + hashlib.sha1(json.dumps([icfg, cfg["segments"]], sort_keys=True, default=str).encode()).hexdigest()[:6]
    base = out["tiers"].get("base", next(iter(out["tiers"].values())))
    entry = {
        "experiment_id": exp_id, "kind": KIND, "strategy": name, "setup": "daily_trend_filter", "bar": "1d",
        "created_at": now.isoformat(), "hypothesis": icfg.get("hypothesis", ""), "symbols": [icfg["symbol"]],
        "data_window": {"period": [str(period[0].date()), str(period[1].date())], "warmup_start": icfg["warmup_start"]},
        "grid": {}, "trial_sharpes": [], "final_params": {k: icfg[k] for k in ("sma", "momentum_sessions")}, "passed": None,
        "summary": {tier: {seg: {k: v for k, v in r[seg].items()} for seg in ("all", "dev", "validation")}
                    | {"trades": {k: v for k, v in r["trades"].items() if k != "list"}} for tier, r in out["tiers"].items()},
        "kanban_metadata": {"experiment_id": exp_id, "strategy_version": "0.1.0", "data_window": "dev+validation",
                            "trades": base["trades"]["count"], "expectancy_r": base["trades"]["expectancy_r"], "artifacts": []},
    }
    registry.append(entry)
    return {"experiment_id": exp_id, **out}
