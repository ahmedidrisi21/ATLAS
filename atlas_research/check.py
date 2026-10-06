"""Frozen-parameter confirmation check over a custom period (MES round 3).

A check reruns one strategy with parameters fixed in advance over a period
that its selection never saw. Nothing is selected, so there is no grid, no
walk-forward and no new deflated-Sharpe trial. The check is declared in the
config under ``checks:`` before its data is loaded:

    checks:
      <name>:
        strategy: <a strategy under strategies:>
        params: {...}                  # frozen; each value must be on the strategy's grid
        period: [start, end)           # must end before the holdout
        coverage: {reference_h4_bars_per_year, reference_m1_bars_per_year, min_frac, min_rth_frac}
        price_checks: {YYYY-MM-DD: published close}
        pass_rules: {min_expectancy_r, beat_buy_and_hold, beat_random_p95, min_trades, max_year_share,
                     min_all_in_expectancy_r, beat_random_direction_p95}

A year whose data covers less than ``min_frac`` of either reference is
excluded: its trades are dropped and it is left out of the benchmark windows.
``reference_h4_bars_per_year`` may be omitted (M1 only); ``min_rth_frac``
(optional, MNQ round 2) also excludes a year whose regular-session minutes
(09:30-16:00 New York, trading days) are quoted less than that share. Only
the pass rules that are declared are evaluated, lettered in the order above;
the last two are MNQ round 2's (the 2.0 pt all-in Stress tier, and the
review's random-direction benchmark).
The run is recorded in the registry, pass or fail, as ``kind: frozen_check``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from atlas_engine.market_data.bars import resample
from atlas_engine.setups import EdgeFilters

from . import metrics
from .backtest import ExitPolicy
from .data import check_not_holdout
from .registry import Registry
from .research_setups import ALL_SETUPS
from .t0 import (Runner, Window, _concat, _ts, buy_and_hold_r, gate, in_window, neighbours, points_summary, prepare_market,
                 random_control, random_direction)

KIND = "frozen_check"
NY = "America/New_York"


class PriceScaleError(ValueError):
    pass


def coverage(m1: pd.DataFrame, period: Window, ref_h4: float | None, ref_m1: float, min_frac: float,
             min_rth_frac: float | None = None) -> pd.DataFrame:
    """Per calendar year in ``period``: M1 and non-empty H4 bar counts against the references.

    With ``ref_h4`` None the H4 count is not judged; with ``min_rth_frac`` the
    year's regular-session quote share (``rth_coverage``) must also reach it.
    """
    h4 = resample(m1, "4h") if len(m1) else m1
    years = (period[0].year, (period[1] - pd.Timedelta(seconds=1)).year)
    rth = rth_coverage(m1, years, years, 0.0, 0.0)["rth_frac"] if (min_rth_frac is not None and len(m1)) else None
    rows = []
    for year in range(period[0].year, (period[1] - pd.Timedelta(seconds=1)).year + 1):
        n_m1 = int((m1.index.year == year).sum()) if len(m1) else 0
        n_h4 = int((h4.index.year == year).sum()) if len(h4) else 0
        f_m1 = n_m1 / ref_m1
        f_h4 = n_h4 / ref_h4 if ref_h4 is not None else None
        ok = f_m1 >= min_frac and (f_h4 is None or f_h4 >= min_frac)
        row = {"year": year, "m1_bars": n_m1, "m1_frac": f_m1, "h4_bars": n_h4, "h4_frac": f_h4,
               "first": str(m1.index[m1.index.year == year].min()) if n_m1 else None}
        if min_rth_frac is not None:
            row["rth_frac"] = float(rth.get(year, 0.0)) if rth is not None else 0.0
            ok = ok and row["rth_frac"] >= min_rth_frac
        rows.append({**row, "included": bool(ok)})
    return pd.DataFrame(rows).set_index("year")


def rth_coverage(m1: pd.DataFrame, years: tuple[int, int], reference_years: tuple[int, int], min_frac: float,
                 min_rth_frac: float) -> pd.DataFrame:
    """Per calendar year (MNQ round 1 data rule): M1 bars against the median full reference year, and the
    share of regular-session minutes (09:30-16:00 New York on exchange trading days) that have a quote.

    A year under ``min_frac`` of the median or under ``min_rth_frac`` of its
    regular-session minutes is flagged. Partial years (the first and last of
    the data) are judged on their regular-session share only.
    """
    from .research_setups import trading_day

    loc = m1.index.tz_convert(NY)
    mins = loc.hour * 60 + loc.minute
    dates = pd.Index(loc.date)
    m1_years = m1.index.year
    ref = [int((m1_years == y).sum()) for y in range(reference_years[0], reference_years[1] + 1)]
    median = float(np.median(ref)) if ref else float("nan")
    first, last = m1.index.min(), m1.index.max()
    rows = []
    for y in range(years[0], years[1] + 1):
        lo = max(pd.Timestamp(f"{y}-01-01"), pd.Timestamp(first.tz_convert(NY).date()))
        hi = min(pd.Timestamp(f"{y}-12-31"), pd.Timestamp(last.tz_convert(NY).date()))
        cal = pd.date_range(lo, hi, freq="D").date if lo <= hi else []
        tdays = set(np.asarray(cal, dtype=object)[trading_day(cal)]) if len(cal) else set()
        rth = (mins >= 570) & (mins < 960) & np.isin(np.asarray(dates, dtype=object), list(tdays))
        n_m1 = int((m1_years == y).sum())
        full = lo == pd.Timestamp(f"{y}-01-01") and hi == pd.Timestamp(f"{y}-12-31")
        rth_frac = int(rth.sum()) / (390 * len(tdays)) if tdays else 0.0
        days_with = len(set(np.asarray(dates[rth], dtype=object)))
        frac = n_m1 / median if median else float("nan")
        rows.append({"year": y, "m1_bars": n_m1, "m1_frac_of_median": frac if full else None, "trading_days": len(tdays),
                     "days_with_rth_quotes": days_with, "rth_frac": rth_frac,
                     "flagged": bool(rth_frac < min_rth_frac or (full and frac < min_frac))})
    return pd.DataFrame(rows).set_index("year")


def price_check(m1: pd.DataFrame, closes: dict, tol: float = 0.01) -> list[dict]:
    """Mid price at the last bar by 16:00 New York on each date, against the published close."""
    mid = (m1["bid_c"] + m1["ask_c"]) / 2
    out = []
    for day, published in closes.items():
        cutoff = pd.Timestamp(f"{day} 16:00", tz=NY).tz_convert("UTC")
        same_day = mid.loc[(mid.index < cutoff) & (mid.index >= cutoff - pd.Timedelta(hours=8))]
        got = float(same_day.iloc[-1]) if len(same_day) else float("nan")
        dev = got / float(published) - 1 if len(same_day) else float("nan")
        out.append({"date": str(day), "published": float(published), "cfd_mid": got, "deviation": dev,
                    "ok": bool(len(same_day) and abs(dev) <= tol)})
    return out


def year_table(trades: pd.DataFrame) -> pd.DataFrame:
    """Per entry year: trades, average R, win rate, total R, share of profit, max drawdown (R, in trade order)."""
    if trades.empty:
        return pd.DataFrame()
    t = trades.sort_values("entry_time")
    years = pd.DatetimeIndex(t["entry_time"]).year
    total = t["r"].sum()
    rows = []
    for y, g in t.groupby(years):
        r = g["r"].to_numpy(float)
        rows.append({"year": y, "trades": len(r), "expectancy_r": float(r.mean()), "win_rate": float((r > 0).mean()),
                     "total_r": float(r.sum()), "share_of_profit": float(r.sum() / total) if total > 0 else np.nan,
                     "max_dd_r": max_dd_r(r)})
    return pd.DataFrame(rows).set_index("year")


def max_dd_r(r: np.ndarray) -> float:
    if not len(r):
        return 0.0
    return float(metrics.max_drawdown(np.cumsum(np.asarray(r, float))[None, :])[0])


def run_check(
    name: str,
    cfg: dict,
    load_m1: Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame],
    registry: Registry,
    out_dir: Path | None = None,
    now: dt.datetime | None = None,
    seed: int = 0,
) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    ccfg = cfg["checks"][name]
    strategy = ccfg["strategy"]
    scfg = cfg["strategies"][strategy]
    registry.check_budget(strategy, cfg["budget"]["experiments_per_strategy_per_month"], now)
    setup_name = scfg.get("setup", strategy)
    setup = ALL_SETUPS[setup_name]
    bar = scfg.get("bar", "15min")
    period: Window = tuple(_ts(x) for x in ccfg["period"])
    check_not_holdout(period[1], pd.Timestamp(cfg["segments"]["holdout_start"]))

    # Frozen means frozen: every parameter must be one the strategy's declared grid offered.
    grid = scfg.get("grid", {})
    for k, v in ccfg["params"].items():
        if k in grid and v not in grid[k]:
            raise ValueError(f"{name}: {k}={v!r} is not on {strategy}'s declared grid {grid[k]}")
    params = {**setup.defaults, **ccfg["params"]}

    cov_cfg = ccfg["coverage"]
    markets, cov, prices = [], {}, {}
    for sym in scfg["symbols"]:
        m1 = load_m1(sym, period[0], period[1])
        cov[sym] = coverage(m1, period, cov_cfg.get("reference_h4_bars_per_year"), cov_cfg["reference_m1_bars_per_year"],
                            cov_cfg["min_frac"], cov_cfg.get("min_rth_frac"))
        prices[sym] = price_check(m1, ccfg.get("price_checks", {}))
        bad = [p for p in prices[sym] if not p["ok"]]
        if bad:
            raise PriceScaleError(f"{name}: {sym} prices off the published closes: {bad}")
        markets.append(prepare_market(sym, m1, cfg, bar))
    included = sorted(set.intersection(*(set(c.index[c["included"]]) for c in cov.values())))
    windows: list[Window] = [(max(period[0], pd.Timestamp(f"{y}-01-01", tz="UTC")), min(period[1], pd.Timestamp(f"{y + 1}-01-01", tz="UTC")))
                             for y in included]

    ex = {**cfg["exits"], **scfg.get("exits", {})}
    exits = ExitPolicy(rr=ex["rr"], friday_flatten_utc=ex["friday_flatten_utc"],
                       atr_trail_mult=ex.get("atr_trail_mult"), atr_trail_after_r=ex.get("atr_trail_after_r", 1.5),
                       reenter_at_exit_bar=bool(ex.get("reenter_at_exit_bar", False)))
    filters = EdgeFilters(**{**cfg["filters"], **scfg.get("filters", {})})
    runner = Runner(setup, markets, exits, filters)
    base_mult, stress_mult = cfg["costs"]["spread_mult"], cfg["costs"]["stress_spread_mult"]

    def keep(t: pd.DataFrame) -> pd.DataFrame:
        return _concat([in_window(t, w) for w in windows])

    trades = keep(runner.trades(params, base_mult))
    stress = keep(runner.trades(params, stress_mult))
    r = trades["r"].to_numpy(float)
    s = metrics.summary(trades)
    s["max_dd_r"] = max_dd_r(r)
    years = year_table(trades)
    max_year_share = float(years["share_of_profit"].max()) if len(years) and r.sum() > 0 else 1.0

    g, acct, rules = cfg["gates"], cfg["account"], ccfg["pass_rules"]
    bh = buy_and_hold_r(trades, markets, windows)
    bh_mean = float(bh.mean()) if len(bh) else 0.0
    rand = random_control(runner, trades, windows, g["random_control_runs"], seed)
    rand_p95 = float(np.percentile(rand, 95)) if len(rand) else 0.0
    exp_r = float(r.mean()) if len(r) else 0.0

    has_all_in = all(m.all_in is not None for m in markets) and bool(cfg["costs"].get("all_in_round_trip"))
    all_in = keep(runner.trades(params, base_mult, all_in=True)) if has_all_in else None
    all_in_r = float(all_in["r"].mean()) if all_in is not None and len(all_in) else 0.0
    rand_dir = np.array([])
    if rules.get("beat_random_direction_p95"):
        rand_dir = random_direction(runner, trades, int(g.get("random_direction_runs") or g["random_control_runs"]), seed)
    rd_p95 = float(np.percentile(rand_dir, 95)) if len(rand_dir) else 0.0
    if "min_all_in_expectancy_r" in rules and not has_all_in:
        raise ValueError(f"{name}: min_all_in_expectancy_r needs costs.all_in_round_trip for every symbol")

    src = "declared pass rule"
    random_label = "random long-entry" if (trades["direction"] == 1).all() else "random-entry"
    candidates = [
        ("min_expectancy_r", lambda v: ("Average R after costs", exp_r, ">", v)),
        ("beat_buy_and_hold", lambda v: ("Average R minus exposure-matched buy-and-hold (R)", exp_r - bh_mean if len(r) else 0.0, ">", 0.0)),
        ("beat_random_p95", lambda v: (f"Average R minus {random_label} p95 (R)", exp_r - rand_p95 if len(r) else 0.0, ">", 0.0)),
        ("min_trades", lambda v: ("Trades", len(r), ">=", v)),
        ("max_year_share", lambda v: ("Largest single-year share of profit", max_year_share, "<=", v)),
        ("min_all_in_expectancy_r", lambda v: ("Average R at the all-in Stress round trip", all_in_r, ">", v)),
        ("beat_random_direction_p95", lambda v: ("Average R minus random-direction p95 (R)", exp_r - rd_p95 if len(r) else 0.0, ">", 0.0)),
    ]
    unknown = set(rules) - {k for k, _ in candidates}
    if unknown:
        raise ValueError(f"{name}: unknown pass rules {sorted(unknown)}")
    pass_rules = []
    for key, make in candidates:
        if key in rules and rules[key] is not False:
            label, value, op, threshold = make(rules[key])
            pass_rules.append(gate(f"({'abcdefghij'[len(pass_rules)]}) {label}", value, op, threshold, "check period", src))
    passed = all(x["passed"] for x in pass_rules)

    # The T0 gate table, for information. A frozen run has no dev/validation split or walk-forward.
    risk_pct = acct["risk_pct"]
    mc = metrics.mc_drawdown(r, risk_pct, g["mc_sims"], seed)
    mc_skip = metrics.mc_drawdown(r, risk_pct, g["mc_sims"], seed + 1, skip_frac=0.10)
    breach = metrics.daily_breach_probability(trades, risk_pct, acct["firm_daily_loss_pct"], acct["eval_days"], g["mc_sims"], seed)
    trials = registry.trial_sharpes(setup=setup_name)
    var_trials = float(np.var(trials, ddof=1)) if len(trials) > 1 else 0.0
    dsr = metrics.deflated_sharpe(r, len(trials), var_trials)
    nbr = [keep(runner.trades(p, base_mult))["r"].mean() for p in neighbours(params, g["neighborhood"])]
    nbr = [0.0 if np.isnan(x) else float(x) for x in nbr]
    na = {"value": None, "rule": "n/a", "passed": None, "source": "PRD §15"}
    t0_gates = [
        gate("OOS trades", len(r), ">=", g["min_oos_trades"], "check period"),
        gate("Expectancy after costs (R)", exp_r, ">=", g["min_expectancy_r"], "check period"),
        {"gate": "Expectancy after costs (R)", "scope": "validation", **na},
        gate("Profit factor", s["profit_factor"], ">=", g["min_profit_factor"], "check period"),
        {"gate": "Profit factor", "scope": "validation", **na},
        gate("Max DD, Monte Carlo p95 (% equity)", mc["dd_p95_pct"], "<", g["max_dd_frac_of_firm"] * acct["firm_max_drawdown_pct"], "check period"),
        gate("Daily-loss breach probability", breach, "<", g["max_daily_breach_prob"], "check period"),
        gate("Deflated Sharpe ratio", dsr, ">", g["min_dsr"], f"check period, {len(trials)} prior trials"),
        {"gate": "Walk-forward efficiency", "scope": "no walk-forward", **na},
        gate("Expectancy at 2x spread (R)", float(stress["r"].mean()) if len(stress) else 0.0, ">", 0.0, "check period"),
        gate("Largest single-year share of profit", max_year_share, "<=", g["max_year_share"], "check period", "PRD §22"),
        gate("Skip-10% Monte Carlo expectancy p05 (R)", mc_skip["expectancy_p05"], ">", 0.0, "check period"),
        gate("Expectancy minus random-entry p95 (R)", exp_r - rand_p95 if len(r) else 0.0, ">", 0.0, "check period"),
        gate("Worst ±20% neighbour expectancy (R)", min(nbr) if nbr else 0.0, ">", 0.0, "check period"),
        gate("Expectancy minus exposure-matched buy-and-hold (R)", exp_r - bh_mean if len(r) else 0.0, ">", 0.0, "check period"),
    ]

    exp_id = f"{name}-{now:%Y%m%d-%H%M%S}-" + hashlib.sha1(json.dumps([ccfg, scfg], sort_keys=True, default=str).encode()).hexdigest()[:6]
    result = {
        "experiment_id": exp_id,
        "kind": KIND,
        "check": name,
        "strategy": strategy,
        "setup": setup_name,
        "bar": bar,
        "exits": ex,
        "strategy_version": setup.version,
        "created_at": now.isoformat(),
        "hypothesis": scfg.get("hypothesis", ""),
        "symbols": scfg["symbols"],
        "data_window": {"check": [str(period[0].date()), str(period[1].date())], "included_years": included},
        "grid": {},
        "trial_sharpes": [],  # nothing selected: a confirmation run adds no deflated-Sharpe trial
        "final_params": params,
        "passed": passed,
        "pass_rules": pass_rules,
        "t0_gates": t0_gates,
        "summary": s,
        "years": years.reset_index().to_dict("records"),
        "coverage": {k: v.reset_index().to_dict("records") for k, v in cov.items()},
        "price_checks": prices,
        "benchmark": {"kind": "buy_and_hold", "mean_r": bh_mean},
        "random_control": {"runs": len(rand), "mean": float(rand.mean()) if len(rand) else 0.0, "p95": rand_p95},
        "random_direction": {"runs": len(rand_dir), "mean": float(rand_dir.mean()) if len(rand_dir) else None,
                             "p95": rd_p95 if len(rand_dir) else None},
        "all_in": {"trades": len(all_in), "expectancy_r": all_in_r} if all_in is not None else None,
        "stress_2x_spread_expectancy_r": float(stress["r"].mean()) if len(stress) else 0.0,
        "points": points_summary(trades),
        "neighbours": nbr,
        "monte_carlo": {**mc, "skip10": mc_skip, "daily_breach_prob": breach},
        "dsr": {"value": dsr, "n_trials": len(trials), "var_trials": var_trials},
        "kanban_metadata": {
            "experiment_id": exp_id,
            "strategy_version": setup.version,
            "data_window": f"check {period[0]:%Y-%m-%d}..{period[1]:%Y-%m-%d}",
            "trades": len(r),
            "expectancy_r": exp_r,
            "pf": s["profit_factor"],
            "max_dd_mc95": mc["dd_p95_pct"],
            "dsr": dsr,
            "artifacts": [],
        },
    }
    registry.append({k: result[k] for k in (
        "experiment_id", "kind", "check", "strategy", "setup", "bar", "exits", "strategy_version", "created_at", "hypothesis",
        "symbols", "data_window", "grid", "trial_sharpes", "final_params", "passed", "kanban_metadata")}
        | {"summary": {k: result["summary"][k] for k in ("trades", "expectancy_r", "profit_factor", "win_rate")},
           "points": result["points"], "all_in": result["all_in"], "random_direction": result["random_direction"],
           "random_control": result["random_control"],
           "failed_rules": [x["gate"] for x in pass_rules if not x["passed"]],
           "failed_gates": [x["gate"] + " / " + x["scope"] for x in t0_gates if x["passed"] is False]})

    if out_dir is not None:
        run_dir = Path(out_dir) / exp_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
        trades.to_csv(run_dir / "trades.csv", index=False)
        result["kanban_metadata"]["artifacts"] = [str(run_dir / "summary.json"), str(run_dir / "trades.csv")]
    return result
