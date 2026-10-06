"""Phase T0 edge-discovery run: walk-forward on dev, one pass on validation, gates.

For one strategy (a setup applied to its symbols) this:

1. backtests every grid point over dev + validation at 1.5x spread (signals
   are causal, so one pass sliced by entry time equals per-window runs);
2. walk-forward on dev: each 12-month train window picks the grid point with
   the best expectancy, which then trades the next 3-month test window;
3. picks final parameters on the whole dev period and trades validation once;
4. evaluates the §15 gates plus the §22 robustness checks;
5. appends the experiment, pass or fail, to the registry.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from atlas_engine.features import sessions
from atlas_engine.features.frame import FeatureConfig, build_features
from atlas_engine.setups import EdgeFilters, Setup

from . import metrics
from .backtest import TRADE_COLS, CostModel, ExitPolicy, M1Path, TrailFrame, simulate
from .registry import Registry
from .research_setups import ALL_SETUPS

Window = tuple[pd.Timestamp, pd.Timestamp]


@dataclass
class Market:
    symbol: str
    m1: pd.DataFrame
    features: pd.DataFrame
    costs: CostModel
    paths: dict[float, M1Path]
    trail: TrailFrame | None = None
    all_in: CostModel | None = None  # fills at the mid, one flat all-in round-trip charge (MNQ Stress tier)


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t


def expand_grid(grid: dict) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))] or [{}]


def in_window(trades: pd.DataFrame, w: Window) -> pd.DataFrame:
    t = pd.DatetimeIndex(trades["entry_time"])
    return trades.loc[(t >= w[0]) & (t < w[1])]


def walk_forward_folds(dev: Window, train_months: int, test_months: int) -> list[tuple[Window, Window]]:
    folds = []
    start = dev[0]
    while True:
        split = start + pd.DateOffset(months=train_months)
        end = split + pd.DateOffset(months=test_months)
        if end > dev[1]:
            return folds
        folds.append(((start, split), (split, end)))
        start = start + pd.DateOffset(months=test_months)


class Runner:
    """Backtests one setup across markets, caching by (params, spread multiple)."""

    def __init__(self, setup: Setup, markets: list[Market], exits: ExitPolicy, filters: EdgeFilters):
        self.setup, self.markets, self.exits, self.filters = setup, markets, exits, filters
        self._cache: dict = {}

    def trades(self, params: dict, spread_mult: float, all_in: bool = False) -> pd.DataFrame:
        """Trades of one grid point; ``all_in`` fills at the mid and charges each market's flat all-in round trip."""
        key = (json.dumps(params, sort_keys=True), spread_mult, all_in)
        if key not in self._cache:
            parts = []
            for m in self.markets:
                sig = self.setup.signals(m.features, params, self.filters)
                costs, mult = (m.all_in, 0.0) if all_in else (m.costs.with_spread(spread_mult), spread_mult)
                parts.append(simulate(sig, m.m1, costs, self.exits, m.symbol, self._path(m, mult), self._trail(m)))
            self._cache[key] = _concat(parts)
        return self._cache[key]

    def simulate_signals(self, signals_by_symbol: dict[str, pd.DataFrame], spread_mult: float, all_in: bool = False) -> pd.DataFrame:
        parts = []
        for m in self.markets:
            sig = signals_by_symbol.get(m.symbol)
            if sig is not None and len(sig):
                costs, mult = (m.all_in, 0.0) if all_in else (m.costs.with_spread(spread_mult), spread_mult)
                parts.append(simulate(sig, m.m1, costs, self.exits, m.symbol, self._path(m, mult), self._trail(m)))
        return _concat(parts)

    def _trail(self, m: Market) -> TrailFrame | None:
        if not self.exits.atr_trail_mult:
            return None
        if m.trail is None:
            m.trail = TrailFrame.from_features(m.features)
        return m.trail

    @staticmethod
    def _path(m: Market, spread_mult: float) -> M1Path:
        if spread_mult not in m.paths:
            m.paths[spread_mult] = M1Path(m.m1, spread_mult)
        return m.paths[spread_mult]


def _concat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame(columns=TRADE_COLS)
    return pd.concat(parts, ignore_index=True).sort_values("entry_time", ignore_index=True)


def prepare_market(symbol: str, m1: pd.DataFrame, cfg: dict, bar: str = "15min") -> Market:
    c = cfg["costs"]["per_symbol"][symbol]
    costs = CostModel(spread_mult=cfg["costs"]["spread_mult"], **c)
    rt = (cfg["costs"].get("all_in_round_trip") or {}).get(symbol)
    all_in = None if rt is None else CostModel(spread_mult=0.0, commission_rt=float(rt), entry_slippage=0.0, stop_slippage=0.0,
                                                swap_per_rollover=0.0, exit_slippage=0.0)
    return Market(symbol, m1, build_features(m1, FeatureConfig(bar=bar)), costs, {}, all_in=all_in)


def select(trades_by_point: list[pd.DataFrame], w: Window, min_trades: int) -> int | None:
    best, best_exp = None, -np.inf
    for i, t in enumerate(trades_by_point):
        tw = in_window(t, w)
        if len(tw) >= min_trades and tw["r"].mean() > best_exp:
            best, best_exp = i, tw["r"].mean()
    return best


def neighbours(params: dict, frac: float) -> list[dict]:
    out = []
    for k, v in params.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v == 0:
            continue
        for sign in (-1, 1):
            nv = v * (1 + sign * frac)
            if isinstance(v, int):
                nv = int(round(nv))
                if nv == v:
                    nv = v + sign
            out.append({**params, k: nv})
    return out


def random_control(runner: Runner, reference: pd.DataFrame, windows: list[Window], runs: int, seed: int) -> np.ndarray:
    """Expectancy of random entries with the same count, direction mix, stop sizes and exits."""
    rng = np.random.default_rng(seed)
    if reference.empty:
        return np.array([])
    stop_atr = (reference["risk"] / reference["atr"]).to_numpy()
    # Setups with time exits: random trades get the same planned holding times.
    holds = (pd.to_datetime(reference["exit_by"], utc=True) - pd.to_datetime(reference["decision_time"], utc=True)).dropna().to_numpy() if "exit_by" in reference else np.array([])
    long_share = float((reference["direction"] == 1).mean())
    per_symbol = reference["symbol"].value_counts().to_dict()
    pools = {}
    for m in runner.markets:
        f = m.features
        ct = pd.DatetimeIndex(f["close_time"])
        ok = f["atr"].notna().to_numpy() & ~f["blk_rollover"].to_numpy() & ~sessions.friday_cutoff(ct, runner.filters.friday_no_entry_after_utc)
        inw = np.zeros(len(f), bool)
        for a, b in windows:
            inw |= (ct >= a) & (ct < b)
        pools[m.symbol] = f.loc[ok & inw]
    out = []
    for _ in range(runs):
        sigs = {}
        for sym, n in per_symbol.items():
            pool = pools.get(sym)
            if pool is None or pool.empty:
                continue
            rows = pool.iloc[np.sort(rng.choice(len(pool), size=min(n, len(pool)), replace=False))]
            d = np.where(rng.random(len(rows)) < long_share, 1, -1)
            k = rng.choice(stop_atr, size=len(rows))
            sig = pd.DataFrame(
                {
                    "decision_time": rows["close_time"].to_numpy(),
                    "direction": d,
                    "stop": rows["close"].to_numpy() - d * k * rows["atr"].to_numpy(),
                    "atr": rows["atr"].to_numpy(),
                    "spread": rows["spread"].to_numpy(),
                    "setup": "random_control",
                }
            )
            if len(holds):
                sig["exit_by"] = sig["decision_time"] + rng.choice(holds, size=len(rows))
            sigs[sym] = sig
        t = runner.simulate_signals(sigs, runner.markets[0].costs.spread_mult)
        out.append(t["r"].mean() if len(t) else 0.0)
    return np.array(out)


def random_direction(runner: Runner, reference: pd.DataFrame, runs: int, seed: int) -> np.ndarray:
    """Expectancy of the same trades' signals with a coin-flip direction (NQ review's random-direction benchmark).

    Each reference trade's decision time, stop distance from the decision
    close and planned exit time are kept; only the direction is drawn at
    random (the stop mirrors to the other side). Simulated at the base costs.
    """
    rng = np.random.default_rng(seed)
    if reference.empty:
        return np.array([])
    out = []
    by_symbol = {}
    for m in runner.markets:
        ref = reference.loc[reference["symbol"] == m.symbol]
        if ref.empty:
            continue
        close = m.features.set_index("close_time")["close"]
        dt_ = pd.DatetimeIndex(pd.to_datetime(ref["decision_time"], utc=True))
        c0 = close.reindex(dt_).to_numpy(float)
        by_symbol[m.symbol] = (ref, dt_, c0, np.abs(c0 - ref["stop"].to_numpy(float)))
    for _ in range(runs):
        sigs = {}
        for sym, (ref, dt_, c0, dist) in by_symbol.items():
            d = np.where(rng.random(len(ref)) < 0.5, 1, -1)
            sig = pd.DataFrame({"decision_time": dt_, "direction": d, "stop": c0 - d * dist, "atr": ref["atr"].to_numpy(),
                                "spread": ref["spread_entry"].to_numpy(), "setup": "random_direction"})
            if "exit_by" in ref and pd.to_datetime(ref["exit_by"], utc=True).notna().all():
                sig["exit_by"] = pd.DatetimeIndex(pd.to_datetime(ref["exit_by"], utc=True)).as_unit("ns")
            sigs[sym] = sig.dropna(subset=["stop"])
        t = runner.simulate_signals(sigs, runner.markets[0].costs.spread_mult)
        out.append(t["r"].mean() if len(t) else 0.0)
    return np.array(out)


def buy_and_hold_r(trades: pd.DataFrame, markets: list[Market], windows: list[Window]) -> np.ndarray:
    """Per trade, the R that holding the market long over the same hours would have earned.

    The buy-and-hold rate is the mean mid-price log return per calendar hour over
    ``windows`` (the strategy's out-of-sample windows), charged no costs. Each
    trade's benchmark is that rate x its hours held x its entry price, on its
    own risk, signed by its direction: exposure-matched buy-and-hold.
    """
    if trades.empty:
        return np.array([])
    rate = {}
    for m in markets:
        mid = (m.m1["bid_c"] + m.m1["ask_c"]) / 2
        logret = hours = 0.0
        for a, b in windows:
            w = mid.loc[(mid.index >= a) & (mid.index < b)]
            if len(w) > 1:
                logret += float(np.log(w.iloc[-1] / w.iloc[0]))
                hours += (w.index[-1] - w.index[0]).total_seconds() / 3600
        rate[m.symbol] = logret / hours if hours else 0.0
    held = (pd.to_datetime(trades["exit_time"], utc=True) - pd.to_datetime(trades["entry_time"], utc=True)).dt.total_seconds() / 3600
    mu = trades["symbol"].map(rate).to_numpy(float)
    entry = trades["entry"].to_numpy(float)
    pts = entry * np.expm1(mu * held.to_numpy(float))
    return trades["direction"].to_numpy(float) * pts / trades["risk"].to_numpy(float)


def gate(name: str, value: float, op: str, threshold: float, scope: str, source: str = "PRD §15") -> dict:
    passed = {">=": value >= threshold, ">": value > threshold, "<": value < threshold, "<=": value <= threshold}[op]
    return {"gate": name, "value": value, "rule": f"{op} {threshold:g}", "passed": bool(passed), "scope": scope, "source": source}


def points_summary(trades: pd.DataFrame) -> dict:
    """Net index points (price units) per trade after costs, and the median 1R in points."""
    if trades.empty:
        return {"net_points_per_trade": 0.0, "total_points": 0.0, "median_risk_points": 0.0}
    pts = trades["r"].to_numpy(float) * trades["risk"].to_numpy(float)
    return {"net_points_per_trade": float(pts.mean()), "total_points": float(pts.sum()),
            "median_risk_points": float(np.median(trades["risk"].to_numpy(float)))}


def fixed_point_report(runner: "Runner", params: dict, dev_span: Window, val: Window, cfg: dict, has_all_in: bool,
                       n_trials: int, var_trials: float, seed: int) -> dict:
    """One grid point held fixed over the out-of-sample span (dev test span + validation), for information.

    Not a verdict: nothing is selected, so walk-forward efficiency doesn't
    apply. Returns the same figures and the gates a fixed point can be judged on.
    """
    g, acct = cfg["gates"], cfg["account"]
    span = (dev_span[0], val[1])
    t = in_window(runner.trades(params, cfg["costs"]["spread_mult"]), span)
    td, tv = in_window(t, dev_span), in_window(t, val)
    stress = in_window(runner.trades(params, cfg["costs"]["stress_spread_mult"]), span)
    all_in = in_window(runner.trades(params, cfg["costs"]["spread_mult"], all_in=True), span) if has_all_in else None
    r = t["r"].to_numpy(float)
    exp_r = float(r.mean()) if len(r) else 0.0
    years = metrics.breakdown(t, "year")
    max_year_share = float(years["share_of_profit"].max()) if len(years) and r.sum() > 0 else 1.0
    risk_pct = acct["risk_pct"]
    mc = metrics.mc_drawdown(r, risk_pct, g["mc_sims"], seed)
    mc_skip = metrics.mc_drawdown(r, risk_pct, g["mc_sims"], seed + 1, skip_frac=0.10)
    breach = metrics.daily_breach_probability(t, risk_pct, acct["firm_daily_loss_pct"], acct["eval_days"], g["mc_sims"], seed)
    dsr = metrics.deflated_sharpe(r, n_trials, var_trials)
    rand = random_control(runner, t, [dev_span, val], g["random_control_runs"], seed)
    rand_p95 = float(np.percentile(rand, 95)) if len(rand) else 0.0
    rand_dir = random_direction(runner, t, int(g.get("random_direction_runs") or 0), seed) if g.get("random_direction_runs") else np.array([])
    rd_p95 = float(np.percentile(rand_dir, 95)) if len(rand_dir) else None
    nbr = [in_window(runner.trades(p, cfg["costs"]["spread_mult"]), span)["r"].mean() for p in neighbours(params, g["neighborhood"])]
    nbr = [0.0 if np.isnan(x) else float(x) for x in nbr]
    ds, vs = metrics.summary(td), metrics.summary(tv)
    scope = "fixed point, OOS span"
    gates = [
        gate("OOS trades", len(t), ">=", g["min_oos_trades"], scope),
        gate("Expectancy after costs (R)", ds["expectancy_r"], ">=", g["min_expectancy_r"], "fixed point, dev test span"),
        gate("Expectancy after costs (R)", vs["expectancy_r"], ">=", g["min_expectancy_r"], "fixed point, validation"),
        gate("Profit factor", ds["profit_factor"], ">=", g["min_profit_factor"], "fixed point, dev test span"),
        gate("Profit factor", vs["profit_factor"], ">=", g["min_profit_factor"], "fixed point, validation"),
        gate("Max DD, Monte Carlo p95 (% equity)", mc["dd_p95_pct"], "<", g["max_dd_frac_of_firm"] * acct["firm_max_drawdown_pct"], scope),
        gate("Daily-loss breach probability", breach, "<", g["max_daily_breach_prob"], scope),
        gate("Deflated Sharpe ratio", dsr, ">", g["min_dsr"], f"{scope}, {n_trials} trials"),
        gate("Expectancy at 2x spread (R)", float(stress["r"].mean()) if len(stress) else 0.0, ">", 0.0, scope),
        gate("Largest single-year share of profit", max_year_share, "<=", g["max_year_share"], scope, "PRD §22"),
        gate("Skip-10% Monte Carlo expectancy p05 (R)", mc_skip["expectancy_p05"], ">", 0.0, scope),
        gate("Expectancy minus random-entry p95 (R)", exp_r - rand_p95 if len(r) else 0.0, ">", 0.0, scope),
        gate("Worst ±20% neighbour expectancy (R)", min(nbr) if nbr else 0.0, ">", 0.0, scope),
    ]
    if all_in is not None:
        gates.append(gate("Expectancy at all-in Stress round trip (R)", float(all_in["r"].mean()) if len(all_in) else 0.0, ">", 0.0, scope))
    if g.get("same_sign_every_year"):
        gates.append(gate("Worst OOS year's average R after costs", float(years["expectancy_r"].min()) if len(years) else 0.0, ">", 0.0, scope))
    if rd_p95 is not None:
        gates.append(gate("Expectancy minus random-direction p95 (R)", exp_r - rd_p95 if len(r) else 0.0, ">", 0.0, scope))
    by_year = {int(y): {"trades": int(row["trades"]), "expectancy_r": float(row["expectancy_r"])} for y, row in years.iterrows()}
    if all_in is not None and len(all_in):
        for y, row in metrics.breakdown(all_in, "year").iterrows():
            by_year.setdefault(int(y), {})["all_in_expectancy_r"] = float(row["expectancy_r"])
    return {
        "params": params,
        "span": [str(span[0].date()), str(span[1].date())],
        "trades": len(t),
        "expectancy_r": exp_r,
        "gross_expectancy_r": float(t["r_gross"].mean()) if len(t) else 0.0,
        "all_in_expectancy_r": float(all_in["r"].mean()) if all_in is not None and len(all_in) else None,
        "stress_2x_spread_expectancy_r": float(stress["r"].mean()) if len(stress) else 0.0,
        "profit_factor": metrics.profit_factor(r),
        "win_rate": float((r > 0).mean()) if len(r) else 0.0,
        "dev_span": ds,
        "validation": vs,
        **points_summary(t),
        "by_year": by_year,
        "exit_reasons": t["exit_reason"].value_counts().to_dict() if len(t) else {},
        "random_entry": {"runs": len(rand), "mean": float(rand.mean()) if len(rand) else None, "p95": rand_p95},
        "random_direction": {"runs": len(rand_dir), "mean": float(rand_dir.mean()) if len(rand_dir) else None, "p95": rd_p95},
        "neighbours": nbr,
        "dsr": dsr,
        "monte_carlo_dd_p95_pct": mc["dd_p95_pct"],
        "gates": gates,
        "failed_gates": [x["gate"] + " / " + x["scope"] for x in gates if not x["passed"]],
    }


def _registry_extra(extra: dict) -> dict:
    """The registry line keeps each fixed point's figures and failed gates, not its full gate table."""
    if "fixed_points" not in extra:
        return extra
    return {**extra, "fixed_points": [{k: v for k, v in fp.items() if k != "gates"} for fp in extra["fixed_points"]]}


def run_t0(
    strategy: str,
    cfg: dict,
    load_m1: Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame],
    registry: Registry,
    out_dir: Path | None = None,
    now: dt.datetime | None = None,
    seed: int = 0,
) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    scfg = cfg["strategies"][strategy]
    registry.check_budget(strategy, cfg["budget"]["experiments_per_strategy_per_month"], now)
    # A strategy is a setup plus its decision timeframe; variants share a setup.
    setup_name = scfg.get("setup", strategy)
    setup = ALL_SETUPS[setup_name]
    bar = scfg.get("bar", "15min")
    g = cfg["gates"]
    acct = cfg["account"]
    dev: Window = tuple(_ts(x) for x in cfg["segments"]["dev"])
    val: Window = tuple(_ts(x) for x in cfg["segments"]["validation"])

    # Daily-indicator strategies may load earlier data to warm up; no trade before dev[0] is ever in a window.
    load_from = dev[0] - pd.Timedelta(days=int(scfg.get("warmup_days", 0)))
    markets = [prepare_market(s, load_m1(s, load_from, val[1]), cfg, bar) for s in scfg["symbols"]]
    # A strategy may override the T0 baseline exits (e.g. trend exits for trend following).
    ex = {**cfg["exits"], **scfg.get("exits", {})}
    exits = ExitPolicy(rr=ex["rr"], friday_flatten_utc=ex["friday_flatten_utc"],
                       atr_trail_mult=ex.get("atr_trail_mult"), atr_trail_after_r=ex.get("atr_trail_after_r", 1.5),
                       reenter_at_exit_bar=bool(ex.get("reenter_at_exit_bar", False)))
    filters = EdgeFilters(**{**cfg["filters"], **scfg.get("filters", {})})
    runner = Runner(setup, markets, exits, filters)
    base_mult = cfg["costs"]["spread_mult"]
    stress_mult = cfg["costs"]["stress_spread_mult"]
    has_all_in = all(m.all_in is not None for m in markets) and bool(cfg["costs"].get("all_in_round_trip"))

    grid = [{**setup.defaults, **p} for p in expand_grid(scfg.get("grid", {}))]
    by_point = [runner.trades(p, base_mult) for p in grid]
    trial_sharpes = [metrics.sharpe(in_window(t, dev)["r"].to_numpy()) for t in by_point]

    # Walk-forward on dev.
    wf = cfg["walk_forward"]
    fold_rows, oos_parts, oos_stress_parts, oos_windows, oos_all_in_parts = [], [], [], [], []
    is_r = is_years = oos_r = oos_years = 0.0
    for train, test in walk_forward_folds(dev, wf["train_months"], wf["test_months"]):
        pick = select(by_point, train, wf["min_train_trades"])
        row = {"train": f"{train[0]:%Y-%m}..{train[1]:%Y-%m}", "test": f"{test[0]:%Y-%m}..{test[1]:%Y-%m}", "pick": pick}
        if pick is not None:
            tr, te = in_window(by_point[pick], train), in_window(by_point[pick], test)
            oos_parts.append(te)
            oos_windows.append(test)
            oos_stress_parts.append(in_window(runner.trades(grid[pick], stress_mult), test))
            if has_all_in:
                oos_all_in_parts.append(in_window(runner.trades(grid[pick], base_mult, all_in=True), test))
            is_r += tr["r"].sum()
            is_years += (train[1] - train[0]).days / 365.25
            oos_r += te["r"].sum()
            oos_years += (test[1] - test[0]).days / 365.25
            row.update(train_expectancy=tr["r"].mean(), test_trades=len(te), test_expectancy=te["r"].mean() if len(te) else 0.0)
        fold_rows.append(row)
    wf_oos = _concat(oos_parts)
    wfe = (oos_r / oos_years) / (is_r / is_years) if is_years and oos_years and is_r > 0 else 0.0

    # Final parameters from the whole dev period, traded once on validation.
    final_idx = select(by_point, dev, wf["min_train_trades"])
    final = grid[final_idx] if final_idx is not None else grid[0]
    val_trades = in_window(runner.trades(final, base_mult), val)
    val_stress = in_window(runner.trades(final, stress_mult), val)

    oos = _concat([wf_oos, val_trades])
    oos_stress = _concat(oos_stress_parts + [val_stress])
    oos_all_in = _concat(oos_all_in_parts + [in_window(runner.trades(final, base_mult, all_in=True), val)]) if has_all_in else None
    r = oos["r"].to_numpy(float)

    # Every variant of a setup counts toward its deflated Sharpe trials.
    # ``dsr_pool`` adds earlier trials of related setups (e.g. the S&P version of the same idea).
    prior = [x for name in [setup_name, *scfg.get("dsr_pool", [])] for x in registry.trial_sharpes(setup=name)]
    all_trials = prior + trial_sharpes
    n_trials = len(all_trials)
    var_trials = float(np.var(all_trials, ddof=1)) if n_trials > 1 else 0.0
    dsr = metrics.deflated_sharpe(r, n_trials, var_trials)

    risk_pct = acct["risk_pct"]
    mc = metrics.mc_drawdown(r, risk_pct, g["mc_sims"], seed)
    mc_skip = metrics.mc_drawdown(r, risk_pct, g["mc_sims"], seed + 1, skip_frac=0.10)
    breach = metrics.daily_breach_probability(oos, risk_pct, acct["firm_daily_loss_pct"], acct["eval_days"], g["mc_sims"], seed)
    years = metrics.breakdown(oos, "year")
    max_year_share = float(years["share_of_profit"].max()) if len(years) and r.sum() > 0 else 1.0

    rand = random_control(runner, oos, oos_windows + [val], g["random_control_runs"], seed)
    rand_p95 = float(np.percentile(rand, 95)) if len(rand) else 0.0
    nbr = [in_window(runner.trades(p, base_mult), val)["r"].mean() for p in neighbours(final, g["neighborhood"])]
    nbr = [0.0 if np.isnan(x) else float(x) for x in nbr]

    dev_s, val_s = metrics.summary(wf_oos), metrics.summary(val_trades)
    ours = "ATLAS default (PRD names the check, not a threshold)"
    gates = [
        gate("OOS trades", len(oos), ">=", g["min_oos_trades"], "dev walk-forward + validation"),
        gate("Expectancy after costs (R)", dev_s["expectancy_r"], ">=", g["min_expectancy_r"], "dev walk-forward OOS"),
        gate("Expectancy after costs (R)", val_s["expectancy_r"], ">=", g["min_expectancy_r"], "validation"),
        gate("Profit factor", dev_s["profit_factor"], ">=", g["min_profit_factor"], "dev walk-forward OOS"),
        gate("Profit factor", val_s["profit_factor"], ">=", g["min_profit_factor"], "validation"),
        gate("Max DD, Monte Carlo p95 (% equity)", mc["dd_p95_pct"], "<", g["max_dd_frac_of_firm"] * acct["firm_max_drawdown_pct"], "all OOS"),
        gate("Daily-loss breach probability", breach, "<", g["max_daily_breach_prob"], "all OOS"),
        gate("Deflated Sharpe ratio", dsr, ">", g["min_dsr"], f"all OOS, {n_trials} trials"),
        gate("Walk-forward efficiency", wfe, ">=", g["min_wfe"], "dev"),
        gate("Expectancy at 2x spread (R)", float(oos_stress["r"].mean()) if len(oos_stress) else 0.0, ">", 0.0, "all OOS"),
        gate("Largest single-year share of profit", max_year_share, "<=", g["max_year_share"], "all OOS", "PRD §22"),
        gate("Skip-10% Monte Carlo expectancy p05 (R)", mc_skip["expectancy_p05"], ">", 0.0, "all OOS", ours),
        gate("Expectancy minus random-entry p95 (R)", float(r.mean() - rand_p95) if len(r) else 0.0, ">", 0.0, "all OOS", ours),
        gate("Worst ±20% neighbour expectancy (R)", min(nbr) if nbr else 0.0, ">", 0.0, "validation", ours),
    ]
    review = "NQ review (MNQ round 1 declaration)"
    rand_dir = np.array([])
    if has_all_in:
        gates.append(gate("Expectancy at all-in Stress round trip (R)", float(oos_all_in["r"].mean()) if len(oos_all_in) else 0.0,
                          ">", 0.0, "all OOS", review))
    if g.get("same_sign_every_year"):
        gates.append(gate("Worst OOS year's average R after costs", float(years["expectancy_r"].min()) if len(years) else 0.0,
                          ">", 0.0, "all OOS, by calendar year", review))
    if g.get("random_direction_runs"):
        rand_dir = random_direction(runner, oos, int(g["random_direction_runs"]), seed)
        rd_p95 = float(np.percentile(rand_dir, 95)) if len(rand_dir) else 0.0
        gates.append(gate("Expectancy minus random-direction p95 (R)", float(r.mean() - rd_p95) if len(r) else 0.0,
                          ">", 0.0, "all OOS", review))
    bh = buy_and_hold_r(oos, markets, oos_windows + [val])
    bh_mean = float(bh.mean()) if len(bh) else 0.0
    benchmark = None
    if scfg.get("benchmark") == "buy_and_hold":
        benchmark = {"kind": "buy_and_hold", "mean_r": bh_mean}
        gates.append(gate("Expectancy minus exposure-matched buy-and-hold (R)", float(r.mean() - bh.mean()) if len(r) else 0.0,
                          ">", 0.0, "all OOS", "MES round 2 declaration"))
    elif scfg.get("benchmark") is not None:
        raise ValueError(f"{strategy}: unknown benchmark {scfg['benchmark']!r}")
    passed = all(x["passed"] for x in gates)

    # Per grid point, for the write-up: dev (whole period) and validation, after costs and gross of commission.
    grid_points = []
    for p_, t in zip(grid, by_point):
        td, tv = in_window(t, dev), in_window(t, val)
        grid_points.append({"params": p_, "dev_trades": len(td), "dev_expectancy_r": float(td["r"].mean()) if len(td) else 0.0,
                            "dev_gross_r": float(td["r_gross"].mean()) if len(td) else 0.0, "val_trades": len(tv),
                            "val_expectancy_r": float(tv["r"].mean()) if len(tv) else 0.0})
    by_year = {int(y): {"trades": int(row["trades"]), "expectancy_r": float(row["expectancy_r"])} for y, row in years.iterrows()}
    if oos_all_in is not None and len(oos_all_in):
        for y, row in metrics.breakdown(oos_all_in, "year").iterrows():
            by_year.setdefault(int(y), {})["all_in_expectancy_r"] = float(row["expectancy_r"])
    extra = {
        "gross_expectancy_r": float(oos["r_gross"].mean()) if len(oos) else 0.0,
        "all_in_expectancy_r": float(oos_all_in["r"].mean()) if oos_all_in is not None and len(oos_all_in) else None,
        "all_in_trades": len(oos_all_in) if oos_all_in is not None else None,
        "stress_2x_spread_expectancy_r": float(oos_stress["r"].mean()) if len(oos_stress) else 0.0,
        "buy_and_hold_r": bh_mean,
        "random_entry_p95": rand_p95,
        "random_direction": {"runs": len(rand_dir), "mean": float(rand_dir.mean()) if len(rand_dir) else None,
                             "p95": float(np.percentile(rand_dir, 95)) if len(rand_dir) else None},
        "by_year": by_year,
        "exit_reasons": oos["exit_reason"].value_counts().to_dict() if len(oos) else {},
        "long_short": {int(k): {"trades": int(v.size), "expectancy_r": float(v.mean())} for k, v in oos.groupby("direction")["r"]} if len(oos) else {},
        **points_summary(oos),
    }
    # Information (declared per strategy): every grid point held fixed over the OOS span. Never part of the verdict.
    if scfg.get("report_grid_points"):
        folds = walk_forward_folds(dev, wf["train_months"], wf["test_months"])
        if folds:
            dev_span = (folds[0][1][0], folds[-1][1][1])
            extra["fixed_points"] = [fixed_point_report(runner, p_, dev_span, val, cfg, has_all_in, n_trials, var_trials, seed)
                                     for p_ in grid]

    exp_id = f"{strategy}-{now:%Y%m%d-%H%M%S}-" + hashlib.sha1(json.dumps([scfg, cfg["segments"]], sort_keys=True, default=str).encode()).hexdigest()[:6]
    result = {
        "experiment_id": exp_id,
        "strategy": strategy,
        "setup": setup_name,
        "bar": bar,
        "exits": ex,
        "strategy_version": setup.version,
        "created_at": now.isoformat(),
        "hypothesis": scfg.get("hypothesis", ""),
        "symbols": scfg["symbols"],
        "data_window": {"dev": [str(dev[0].date()), str(dev[1].date())], "validation": [str(val[0].date()), str(val[1].date())]},
        "grid": scfg.get("grid", {}),
        "trial_sharpes": trial_sharpes,
        "final_params": final,
        "passed": passed,
        "gates": gates,
        "dev_oos": dev_s,
        "validation": val_s,
        "walk_forward": {"efficiency": wfe, "folds": fold_rows},
        "monte_carlo": {**mc, "skip10": mc_skip, "daily_breach_prob": breach},
        "dsr": {"value": dsr, "n_trials": n_trials, "var_trials": var_trials},
        "random_control": {"runs": len(rand), "mean": float(rand.mean()) if len(rand) else 0.0, "p95": rand_p95},
        "neighbours": nbr,
        "benchmark": benchmark,
        "extra": extra,
        "grid_points": grid_points,
        # Kanban handoff shape from PRD §4.
        "kanban_metadata": {
            "experiment_id": exp_id,
            "strategy_version": setup.version,
            "data_window": "dev+validation",
            "trades": len(oos),
            "expectancy_r": float(r.mean()) if len(r) else 0.0,
            "pf": metrics.profit_factor(r),
            "max_dd_mc95": mc["dd_p95_pct"],
            "dsr": dsr,
            "artifacts": [],
        },
    }
    registry.append({k: result[k] for k in (
        "experiment_id", "strategy", "setup", "bar", "exits", "strategy_version", "created_at", "hypothesis", "symbols", "data_window",
        "grid", "trial_sharpes", "final_params", "passed", "kanban_metadata")} | ({"extra": _registry_extra(extra)} if has_all_in or g.get("random_direction_runs") else {}) | {"failed_gates": [x["gate"] + " / " + x["scope"] for x in gates if not x["passed"]]})

    if out_dir is not None:
        from .report import write_report

        run_dir = Path(out_dir) / exp_id
        result["kanban_metadata"]["artifacts"] = write_report(run_dir, result, oos, years, metrics.breakdown(oos, "session"), metrics.breakdown(oos, "symbol"))
    return result
