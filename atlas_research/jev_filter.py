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

    python -m atlas_research.jev_filter mes_channel_breakout_long_h4 \\
        --config atlas_research/configs/mes.yaml --out research/runs
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import yaml

from atlas_engine.adapters.jev import JevAdapter, JevResult
from atlas_engine.adapters.jev.adapter import _cache_key, request_hash
from atlas_engine.adapters.jev.questions import QUESTIONS_VERSION
from atlas_engine.decisions.state import payload
from atlas_engine.setups import SETUPS, EdgeFilters

from . import metrics
from .backtest import ExitPolicy
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


def score(adapter: JevAdapter, cands: pd.DataFrame, skips: dict) -> np.ndarray:
    out = np.full(len(cands), np.nan)
    for i, (_, row) in enumerate(cands.iterrows()):
        state = payload(row) | {"recent_signal_r20": row.get("recent_signal_r20")}
        res = adapter.evaluate_setup(row["candidate_id"], state, float(row["target_r"]), float(row["cost_r_est"]))
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
    return {**s, "expectancy_r_gross": float(gross.mean()) if len(gross) else 0.0, "by_year": _years(trades)}


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
    setup = SETUPS[scfg.get("setup", strategy)]
    bar = scfg.get("bar", "15min")
    dev: Window = tuple(_ts(x) for x in cfg["segments"]["dev"])
    val: Window = tuple(_ts(x) for x in cfg["segments"]["validation"])
    markets = [prepare_market(s, load_m1(s, dev[0], val[1]), cfg, bar) for s in scfg["symbols"]]
    ex = {**cfg["exits"], **scfg.get("exits", {})}
    exits = ExitPolicy(rr=ex["rr"], friday_flatten_utc=ex["friday_flatten_utc"],
                       atr_trail_mult=ex.get("atr_trail_mult"), atr_trail_after_r=ex.get("atr_trail_after_r", 1.5))
    filters = EdgeFilters(**{**cfg["filters"], **scfg.get("filters", {})})
    runner = Runner(setup, markets, exits, filters)
    mult = cfg["costs"]["spread_mult"]
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
        c["p_jev"] = score(adapter, c, skips)
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
    rng = np.random.default_rng(seed)
    control_means = []
    for _ in range(controls):
        parts = []
        for w, pick, _ in plan:
            cw = in_decision_window(cands[pick], w)
            k = int(cw["take"].sum())
            if k:
                sub = cw.iloc[np.sort(rng.choice(len(cw), k, replace=False))]
                parts.append(in_window(runner.simulate_signals(signals_of(sub), mult), w))
        t = _concat(parts)
        control_means.append(float(t["r"].mean()) if len(t) else 0.0)
    control_means = np.array(control_means)

    # Does Jev's number track outcomes at all? Out-of-sample candidates of the traded points only.
    oos = pd.concat([in_decision_window(cands[pick], w) for w, pick, _ in plan], ignore_index=True)
    ok = oos["p_jev"].notna()
    varied = ok.sum() > 2 and oos.loc[ok, "p_jev"].nunique() > 1
    spearman = float(oos.loc[ok, "p_jev"].rank().corr(oos.loc[ok, "r"].rank())) if varied else float("nan")
    top, bottom = (oos.loc[ok & oos["take"], "r"], oos.loc[ok & ~oos["take"], "r"])

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
    return {
        "strategy": strategy,
        "test": "jev-filter-v1",
        "model_version": adapter.model_version,
        "questions_version": QUESTIONS_VERSION,
        "skip_frac": skip_frac,
        "min_prior": min_prior,
        "data_window": [str(dev[0]), str(val[1])],
        "t0_trades": {"trades": len(t0_all), "expectancy_r": metrics.summary(t0_all)["expectancy_r"]},
        "plain": {**_brief(plain_all), "dev": metrics.summary(seg(plain_all, "dev")), "validation": metrics.summary(seg(plain_all, "validation"))},
        "jev": {**_brief(jev_all), "dev": metrics.summary(seg(jev_all, "dev")), "validation": metrics.summary(seg(jev_all, "validation"))},
        "p_jev_beats_plain": pb,
        "random_skip_control": {"runs": controls, "mean": float(control_means.mean()) if len(control_means) else None, "p95": rand_p95},
        "score_vs_outcome": {"candidates_scored": int(ok.sum()), "spearman_p_vs_r": spearman,
                             "kept_candidates_mean_r": float(top.mean()) if len(top) else None,
                             "skipped_candidates_mean_r": float(bottom.mean()) if len(bottom) else None,
                             "p_jev_mean": float(oos.loc[ok, "p_jev"].mean()) if ok.any() else None,
                             "target_first_rate": float(oos.loc[ok, "y"].mean()) if ok.any() else None},
        "jev_skips": skips,
        "windows": rows,
        "checks": checks,
        "passed": all(checks.values()),
    }


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
    adapter = JevAdapter(rec, DEFAULT_MODEL, live=False, timeout_ms=args.timeout_ms)
    load = lambda sym, start, end: rdata.load_research_m1(root, cfg, sym, start, end)  # noqa: E731
    try:
        res = run(args.strategy, cfg, load, adapter)
    finally:
        adapter.close()
    now = dt.datetime.now(dt.timezone.utc)
    out = Path(args.out) / f"{args.strategy}+jev-filter-{now:%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(res, indent=2, default=str))
    (out / "jev_answers.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rec.records))
    if hasattr(transport, "usage"):
        (out / "usage.json").write_text(json.dumps(transport.usage, indent=2))
    print(json.dumps({k: res[k] for k in ("t0_trades", "plain", "jev", "p_jev_beats_plain", "random_skip_control", "score_vs_outcome", "jev_skips", "checks", "passed")}, indent=2, default=str))
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
