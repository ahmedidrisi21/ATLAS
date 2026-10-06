"""Jev filter test on one T0 strategy's out-of-sample trades (declared in docs/jev-mes-filter-test.md).

The T2 keep/kill harness can't score a thin strategy: its training folds need
150 candidates and its EV gate assumes every trade ends at the stop or the
target. So this asks a narrower question on exactly the trades T0 counted:

1. Rebuild T0's out-of-sample trades: the walk-forward picks on dev, then the
   final parameters on validation. These are the "plain" trades.
2. Ask Jev ``p_target_first`` for every candidate signal of the grid points
   used, from the start of dev (the earlier ones only set the cut-off).
3. Skip a candidate when Jev's answer is below the ``skip_frac`` quantile of
   Jev's answers on the same parameter set's earlier candidates. Before
   ``min_prior`` earlier answers exist, nothing is skipped. A Jev skip
   (timeout, error) skips the trade.
4. Re-simulate what is left with the one-position rule, window by window.
5. Compare with the plain trades, and with random skips of the same share.

Nothing here reads the holdout: data comes through the guarded loader and the
segments end where the holdout starts.

The Nasdaq noise-area strategy (``setup: noise_area``) has no target and its
own exit clock, so it is asked the research-only ``atlas-jev-noise-q1``
question ("will this trade close in profit?") with its own state (``noise_state``).
Its test is declared in docs/jev-mnq-noise-filter-test.md; ``--check`` adds the
frozen 2018 check's trades as information.

    python -m atlas_research.jev_filter mes_channel_breakout_long_h4 \\
        --config atlas_research/configs/mes.yaml --out research/runs
    python -m atlas_research.jev_filter mnq_noise_area_r2_m5 \\
        --config atlas_research/configs/mnq.yaml --check mnq_noise_area_r2_2018
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import numpy as np
import pandas as pd
import yaml

from atlas_engine.adapters.jev import JevAdapter, JevResult
from atlas_engine.adapters.jev.adapter import NOISE, Q2, QuestionSet, _cache_key, request_hash
from atlas_engine.decisions.state import payload
from atlas_engine.setups import EdgeFilters

from . import metrics
from .backtest import ExitPolicy
from .research_setups import ALL_SETUPS
from .selection.dataset import build_candidates, signals_of
from .selection.keep_kill import in_decision_window, p_better
from .t0 import Runner, _concat, _ts, expand_grid, in_window, prepare_market, select, walk_forward_folds

Window = tuple[pd.Timestamp, pd.Timestamp]


class RecordingTransport:
    """Wraps a transport and keeps every answer, in ``ReplayTransport``'s JSON-lines format."""

    def __init__(self, inner: Callable[[dict], dict]):
        self.inner, self.records = inner, []

    def __call__(self, request: dict) -> dict:
        resp = self.inner(request)
        self.records.append({"request_hash": request_hash(_cache_key(request)), "response": resp})
        return resp


# Which question set each setup is asked; anything not listed gets the engine's atlas-jev-q2.
QUESTIONS_FOR_SETUP: dict[str, QuestionSet] = {"noise_area": NOISE}


def _num(v) -> float | None:
    return None if v is None or not np.isfinite(float(v)) else float(v)


def noise_state(row: pd.Series) -> dict:
    """The ``atlas-jev-noise-q1`` state for one noise-area candidate: ratios, labels and counts only.

    One noise unit is the band half-width at the entry checkpoint, sigma x the band's base price.
    """
    d = float(row["direction"])
    unit = float(row["nz_sigma"]) * float(row["nz_base"])
    return {
        "setup": row["setup"],
        "side": "long" if d > 0 else "short",
        "checkpoint": int(row["nz_checkpoint"]),
        "entry_of_day": int(row["nz_entry_of_day"]),
        "breakout_noise": _num(d * (row["nz_close"] - row["nz_base"]) / unit),
        "gap_noise": _num(d * (row["nz_open"] - row["nz_prior_close"]) / unit),
        "noise_pct": _num(100 * row["nz_sigma"]),
        "stop_noise": _num(abs(row["nz_close"] - row["stop"]) / unit),
        **{k: _num(row[k]) for k in ("stop_atr", "atr_pct", "h1_adx", "htf_aligned", "h1_slope_atr", "h1_slow_dist",
                                     "room_prior_day_atr", "ret_16_atr")},
        "recent_signal_r20": _num(row.get("recent_signal_r20")),
    }


def q2_state(row: pd.Series) -> dict:
    return payload(row) | {"recent_signal_r20": row.get("recent_signal_r20")}


def score(adapter: JevAdapter | list[JevAdapter], cands: pd.DataFrame, skips: dict) -> np.ndarray:
    """Jev's probability per candidate (NaN for a skip). Several adapters share the work, one thread each."""
    adapters = adapter if isinstance(adapter, list) else [adapter]
    state_of = noise_state if adapters[0].question_set is NOISE else q2_state
    rows = [row for _, row in cands.iterrows()]

    def one(i: int):
        row = rows[i]
        target = row.get("target_r")
        target = None if target is None or pd.isna(target) else float(target)
        return adapters[i % len(adapters)].evaluate_setup(row["candidate_id"], state_of(row), target, float(row["cost_r_est"]))

    with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
        results = list(pool.map(one, range(len(rows))))
    out = np.full(len(cands), np.nan)
    for i, res in enumerate(results):
        if isinstance(res, JevResult):
            out[i] = res.p_target_first
        else:
            skips[res.reason] = skips.get(res.reason, 0) + 1
    return out


def take_mask(p: np.ndarray, decision_time: pd.Series, skip_frac: float, min_prior: int) -> np.ndarray:
    """Per candidate: keep unless Jev's answer is below the quantile of earlier answers (a skip is never kept)."""
    t = pd.DatetimeIndex(decision_time).as_unit("ns").asi8
    take = np.ones(len(p), bool)
    for i in range(len(p)):
        if not np.isfinite(p[i]):
            take[i] = False
            continue
        prior = p[(t < t[i]) & np.isfinite(p)]
        if len(prior) >= min_prior:
            take[i] = p[i] >= np.quantile(prior, skip_frac)
    return take


def _years(trades: pd.DataFrame) -> dict:
    if not len(trades):
        return {}
    y = pd.DatetimeIndex(trades["entry_time"]).year
    return {int(k): {"trades": int(len(g)), "expectancy_r": float(g["r"].mean())} for k, g in trades.groupby(y)}


def _brief(trades: pd.DataFrame) -> dict:
    s = metrics.summary(trades)
    gross = (trades["r"] + trades["cost_r"]).to_numpy(float) if len(trades) else np.array([])
    out = {**s, "expectancy_r_gross": float(gross.mean()) if len(gross) else 0.0, "by_year": _years(trades)}
    if len(trades) and {"entry", "exit", "direction"} <= set(trades.columns):
        pts = (trades["exit"] - trades["entry"]) * trades["direction"]
        out["gross_points_per_trade"] = float(pts.mean())
    return out


def _random_skips(runner, plan_cands: list[tuple[pd.DataFrame, Window]], mult: float, controls: int, seed: int) -> np.ndarray:
    """Average R of ``controls`` runs that keep, in each window, as many candidates as Jev kept, chosen at random."""
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(controls):
        parts = []
        for cw, w in plan_cands:
            k = int(cw["take"].sum())
            if k:
                sub = cw.iloc[np.sort(rng.choice(len(cw), k, replace=False))]
                parts.append(in_window(runner.simulate_signals(signals_of(sub), mult), w))
        t = _concat(parts)
        means.append(float(t["r"].mean()) if len(t) else 0.0)
    return np.array(means)


def _score_vs_outcome(oos: pd.DataFrame) -> dict:
    ok = oos["p_jev"].notna()
    varied = ok.sum() > 2 and oos.loc[ok, "p_jev"].nunique() > 1
    spearman = float(oos.loc[ok, "p_jev"].rank().corr(oos.loc[ok, "r"].rank())) if varied else float("nan")
    take = oos["take"].astype(bool)
    top, bottom = oos.loc[ok & take, "r"], oos.loc[ok & ~take, "r"]
    out = {"candidates_scored": int(ok.sum()), "spearman_p_vs_r": spearman,
           "kept_candidates_mean_r": float(top.mean()) if len(top) else None,
           "skipped_candidates_mean_r": float(bottom.mean()) if len(bottom) else None,
           "p_jev_mean": float(oos.loc[ok, "p_jev"].mean()) if ok.any() else None,
           "yes_rate": float(oos.loc[ok, "y"].mean()) if ok.any() else None}
    if varied:  # mean R by Jev-score third, lowest first
        q = pd.qcut(oos.loc[ok, "p_jev"].rank(method="first"), 3, labels=False)
        out["mean_r_by_score_third"] = [float(oos.loc[ok, "r"][q == i].mean()) for i in range(3)]
    return out


def _runner(strategy: str, cfg: dict, load_m1: Callable, span: Window):
    """The strategy's setup, exits and filters exactly as T0 builds them, on markets loaded for ``span``."""
    scfg = cfg["strategies"][strategy]
    setup = ALL_SETUPS[scfg.get("setup", strategy)]
    markets = [prepare_market(s, load_m1(s, span[0], span[1]), cfg, scfg.get("bar", "15min")) for s in scfg["symbols"]]
    ex = {**cfg["exits"], **scfg.get("exits", {})}
    exits = ExitPolicy(rr=ex["rr"], friday_flatten_utc=ex["friday_flatten_utc"],
                       atr_trail_mult=ex.get("atr_trail_mult"), atr_trail_after_r=ex.get("atr_trail_after_r", 1.5),
                       reenter_at_exit_bar=bool(ex.get("reenter_at_exit_bar", False)))
    filters = EdgeFilters(**{**cfg["filters"], **scfg.get("filters", {})})
    return setup, Runner(setup, markets, exits, filters), markets, exits, filters


def run(
    strategy: str,
    cfg: dict,
    load_m1: Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame],
    adapter: JevAdapter,
    skip_frac: float = 1 / 3,
    min_prior: int = 30,
    controls: int = 200,
    min_trades: int = 300,
    win_prob: float = 0.95,
    seed: int = 0,
) -> dict:
    scfg = cfg["strategies"][strategy]
    dev: Window = tuple(_ts(x) for x in cfg["segments"]["dev"])
    val: Window = tuple(_ts(x) for x in cfg["segments"]["validation"])
    setup, runner, markets, exits, filters = _runner(strategy, cfg, load_m1, (dev[0], val[1]))
    mult = cfg["costs"]["spread_mult"]
    adapters = adapter if isinstance(adapter, list) else [adapter]
    grid = [{**setup.defaults, **p} for p in expand_grid(scfg.get("grid", {}))]
    by_point = [runner.trades(p, mult) for p in grid]

    # T0's out-of-sample windows and the grid point each one traded.
    wf = cfg["walk_forward"]
    plan: list[tuple[Window, int, str]] = []
    for train, test in walk_forward_folds(dev, wf["train_months"], wf["test_months"]):
        pick = select(by_point, train, wf["min_train_trades"])
        if pick is not None:
            plan.append((test, pick, "dev"))
    final = select(by_point, dev, wf["min_train_trades"])
    plan.append((val, final if final is not None else 0, "validation"))

    used = sorted({pick for _, pick, _ in plan})
    cands, skips = {}, {}
    for i in used:
        c = build_candidates(setup, markets, grid[i], exits, filters, mult)
        c = in_decision_window(c, (dev[0], val[1])).reset_index(drop=True)
        if exits.rr is None:  # no target: the question is "closes in profit after costs"
            c["y"] = (c["r"] > 0).astype(int)
        c["p_jev"] = score(adapters, c, skips)
        c["take"] = take_mask(c["p_jev"].to_numpy(float), c["decision_time"], skip_frac, min_prior)
        cands[i] = c

    def sim(c: pd.DataFrame, w: Window, seg: str) -> pd.DataFrame:
        t = in_window(runner.simulate_signals(signals_of(c), mult), w) if len(c) else _concat([])
        return t.assign(segment=seg)

    # "Plain" is every candidate re-simulated window by window, exactly as the Jev arm is, so the two differ
    # only by Jev's skips. T0's own count differs by a trade or two at window edges (a position carried in).
    t0_parts, plain_parts, jev_parts, rows = [], [], [], []
    for w, pick, seg in plan:
        cw = in_decision_window(cands[pick], w)
        kept = cw.loc[cw["take"].to_numpy(bool)]
        t0_parts.append(in_window(by_point[pick], w))
        plain_parts.append(sim(cw, w, seg))
        jev_parts.append(sim(kept, w, seg))
        rows.append({"window": f"{w[0]:%Y-%m}..{w[1]:%Y-%m}", "params": grid[pick], "segment": seg,
                     "candidates": len(cw), "kept": len(kept), "t0_trades": len(t0_parts[-1]),
                     "plain_trades": len(plain_parts[-1]), "jev_trades": len(jev_parts[-1])})
    t0_all, plain_all, jev_all = _concat(t0_parts), _concat(plain_parts), _concat(jev_parts)
    for t in (plain_all, jev_all):
        if "segment" not in t:
            t["segment"] = pd.Series(dtype=str)

    # Random skips of the same share in each window: the control for "any thinning helps".
    control_means = _random_skips(runner, [(in_decision_window(cands[pick], w), w) for w, pick, _ in plan], mult, controls, seed)

    # Does Jev's number track outcomes at all? Out-of-sample candidates of the traded points only.
    oos = pd.concat([in_decision_window(cands[pick], w) for w, pick, _ in plan], ignore_index=True)

    # The 2.0 pt all-in Stress tier (MNQ): the same kept signals, filled at the mid, one flat charge.
    has_all_in = all(m.all_in is not None for m in markets)
    stress = {}
    if has_all_in:
        def sim_all_in(keep_only: bool) -> pd.DataFrame:
            parts = []
            for w, pick, _ in plan:
                cw = in_decision_window(cands[pick], w)
                cw = cw.loc[cw["take"].to_numpy(bool)] if keep_only else cw
                if len(cw):
                    parts.append(in_window(runner.simulate_signals(signals_of(cw), mult, all_in=True), w))
            return _concat(parts)
        stress = {"plain": metrics.summary(sim_all_in(False)), "jev": metrics.summary(sim_all_in(True))}

    plain_r, jev_r = plain_all["r"].to_numpy(float), jev_all["r"].to_numpy(float)
    pb = p_better(jev_r, plain_r, 10_000, seed)
    rand_p95 = float(np.quantile(control_means, 0.95)) if len(control_means) else float("nan")
    seg = lambda t, s: t.loc[t["segment"] == s]  # noqa: E731
    checks = {
        "jev_trades_at_least_300": len(jev_r) >= min_trades,
        "beats_plain_bootstrap_0.95": pb >= win_prob,
        "beats_random_skips_p95": bool(len(jev_r) and jev_r.mean() > rand_p95),
        "dev_expectancy_at_least_0.10": metrics.summary(seg(jev_all, "dev"))["expectancy_r"] >= 0.10,
        "validation_expectancy_at_least_0.10": metrics.summary(seg(jev_all, "validation"))["expectancy_r"] >= 0.10,
        "dev_profit_factor_at_least_1.25": metrics.summary(seg(jev_all, "dev"))["profit_factor"] >= 1.25,
        "validation_profit_factor_at_least_1.25": metrics.summary(seg(jev_all, "validation"))["profit_factor"] >= 1.25,
    }
    # The strategy's own extra gates (MNQ config): every OOS calendar year positive, and positive at the Stress tier.
    if cfg["gates"].get("same_sign_every_year"):
        yrs = _years(jev_all)
        checks["every_year_positive"] = bool(yrs) and all(v["expectancy_r"] > 0 for v in yrs.values())
    if has_all_in:
        checks["stress_all_in_positive"] = stress["jev"]["expectancy_r"] > 0
    return {
        "strategy": strategy,
        "test": "jev-filter-v1",
        "model_version": adapters[0].model_version,
        "questions_version": adapters[0].question_set.version,
        "skip_frac": skip_frac,
        "min_prior": min_prior,
        "data_window": [str(dev[0]), str(val[1])],
        "t0_trades": {"trades": len(t0_all), "expectancy_r": metrics.summary(t0_all)["expectancy_r"]},
        "plain": {**_brief(plain_all), "dev": metrics.summary(seg(plain_all, "dev")), "validation": metrics.summary(seg(plain_all, "validation"))},
        "jev": {**_brief(jev_all), "dev": metrics.summary(seg(jev_all, "dev")), "validation": metrics.summary(seg(jev_all, "validation"))},
        "p_jev_beats_plain": pb,
        "random_skip_control": {"runs": controls, "mean": float(control_means.mean()) if len(control_means) else None, "p95": rand_p95},
        "stress_all_in": stress,
        "score_vs_outcome": _score_vs_outcome(oos),
        "jev_skips": skips,
        "windows": rows,
        "checks": checks,
        "passed": all(checks.values()),
    }


def run_check(
    check: str,
    cfg: dict,
    load_m1: Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame],
    adapter: JevAdapter | list[JevAdapter],
    skip_frac: float = 1 / 3,
    min_prior: int = 30,
    controls: int = 200,
    seed: int = 0,
) -> dict:
    """Information only: the same Jev rule on a frozen check's trades (e.g. MNQ round 2's 2018 year).

    The cut-off still uses only earlier answers from the same period, so the first ``min_prior`` candidates are
    never skipped. Nothing here is a pass mark; it reports plain, Jev and random skips side by side.
    """
    ccfg = cfg["checks"][check]
    period: Window = (_ts(ccfg["period"][0]), _ts(ccfg["period"][1]))
    setup, runner, markets, exits, filters = _runner(ccfg["strategy"], cfg, load_m1, period)
    mult = cfg["costs"]["spread_mult"]
    params = {**setup.defaults, **ccfg["params"]}
    adapters = adapter if isinstance(adapter, list) else [adapter]
    skips: dict = {}
    c = in_decision_window(build_candidates(setup, markets, params, exits, filters, mult), period).reset_index(drop=True)
    if exits.rr is None:
        c["y"] = (c["r"] > 0).astype(int)
    c["p_jev"] = score(adapters, c, skips)
    c["take"] = take_mask(c["p_jev"].to_numpy(float), c["decision_time"], skip_frac, min_prior)
    plain = in_window(runner.simulate_signals(signals_of(c), mult), period)
    kept = c.loc[c["take"].to_numpy(bool)]
    jev = in_window(runner.simulate_signals(signals_of(kept), mult), period) if len(kept) else _concat([])
    ctrl = _random_skips(runner, [(c, period)], mult, controls, seed)
    out = {"check": check, "params": params, "period": [str(period[0]), str(period[1])],
           "questions_version": adapters[0].question_set.version, "candidates": len(c), "kept": len(kept),
           "plain": _brief(plain), "jev": _brief(jev),
           "random_skip_control": {"runs": controls, "mean": float(ctrl.mean()), "p95": float(np.quantile(ctrl, 0.95))},
           "score_vs_outcome": _score_vs_outcome(c), "jev_skips": skips}
    if all(m.all_in is not None for m in markets):
        out["stress_all_in"] = {
            "plain": metrics.summary(in_window(runner.simulate_signals(signals_of(c), mult, all_in=True), period)),
            "jev": metrics.summary(in_window(runner.simulate_signals(signals_of(kept), mult, all_in=True), period))
            if len(kept) else metrics.summary(_concat([]))}
    return out


def main(argv: list[str] | None = None) -> None:
    from . import data as rdata
    from atlas_engine.adapters.jev.typesafe import DEFAULT_MODEL, TypeSafeTransport

    ap = argparse.ArgumentParser(prog="python -m atlas_research.jev_filter")
    ap.add_argument("strategy")
    ap.add_argument("--config", required=True)
    ap.add_argument("--data-root")
    ap.add_argument("--out", default="research/runs")
    ap.add_argument("--replay", help="answer from a recorded jev_answers.jsonl instead of calling TypeSafe")
    ap.add_argument("--timeout-ms", type=float, default=5000.0, help="research only; live stays at 500 ms")
    ap.add_argument("--workers", type=int, default=4, help="parallel Jev requests (research only)")
    ap.add_argument("--check", help="also run this frozen check's trades through the same rule (information only)")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(Path(args.config).read_text())
    root = Path(args.data_root or cfg["data"]["root"])
    if args.replay:
        from atlas_engine.adapters.jev import ReplayTransport
        transport = ReplayTransport(path=Path(args.replay))
    else:
        transport = TypeSafeTransport.from_env(timeout_s=args.timeout_ms / 1000)
        if transport is None:
            raise SystemExit("no TypeSafe access: set TYPESAFE_API_KEY, or ATLAS_TYPESAFE_PROXY_AUTH=1 behind a proxy that adds it")
    rec = RecordingTransport(transport)
    scfg = cfg["strategies"][args.strategy]
    qs = QUESTIONS_FOR_SETUP.get(scfg.get("setup", args.strategy), Q2)
    adapters = [JevAdapter(rec, DEFAULT_MODEL, live=False, timeout_ms=args.timeout_ms, question_set=qs)
                for _ in range(max(1, args.workers))]
    load = lambda sym, start, end: rdata.load_research_m1(root, cfg, sym, start, end)  # noqa: E731
    try:
        res = run(args.strategy, cfg, load, adapters)
        if args.check:
            res["check"] = run_check(args.check, cfg, load, adapters)
    finally:
        for a in adapters:
            a.close()
    now = dt.datetime.now(dt.timezone.utc)
    out = Path(args.out) / f"{args.strategy}+jev-filter-{now:%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(res, indent=2, default=str))
    (out / "jev_answers.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rec.records))
    if hasattr(transport, "usage"):
        (out / "usage.json").write_text(json.dumps(transport.usage, indent=2))
    keys = ("t0_trades", "plain", "jev", "p_jev_beats_plain", "random_skip_control", "stress_all_in", "score_vs_outcome",
            "jev_skips", "checks", "passed", "check")
    print(json.dumps({k: res[k] for k in keys if k in res}, indent=2, default=str))
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
