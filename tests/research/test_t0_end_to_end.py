"""End-to-end T0 runs on synthetic data: no false pass on noise, and a planted edge is found."""

from pathlib import Path

import pytest

from atlas_engine.market_data import synthetic
from atlas_research.cli import DEFAULT_CONFIG, load_config
from atlas_research.registry import Registry
from atlas_research.t0 import run_t0


@pytest.fixture()
def cfg():
    c = load_config(DEFAULT_CONFIG)
    c["segments"] = {"dev": ["2019-01-01", "2022-01-01"], "validation": ["2022-01-01", "2023-01-01"], "holdout_start": "2023-01-01"}
    c["gates"]["mc_sims"] = 2000
    c["gates"]["random_control_runs"] = 20
    c["strategies"]["session_breakout"]["symbols"] = ["EURUSD"]
    return c


def _run(cfg, tmp_path: Path, momentum: float) -> dict:
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2023-01-01", seed=5, momentum=momentum)
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    return run_t0("session_breakout", cfg, load, Registry(tmp_path / "exp.jsonl"), out_dir=tmp_path / "runs")


def _gate(res: dict, name: str, scope: str) -> dict:
    return next(g for g in res["gates"] if g["gate"] == name and g["scope"].startswith(scope))


def test_random_walk_never_passes(cfg, tmp_path):
    res = _run(cfg, tmp_path, momentum=0.0)
    assert not res["passed"]
    assert not _gate(res, "Expectancy after costs (R)", "dev")["passed"]
    assert not _gate(res, "Expectancy after costs (R)", "validation")["passed"]
    assert not _gate(res, "Expectancy minus random-entry p95 (R)", "all")["passed"]
    # Failures are recorded too, with the handoff shape and report artifacts.
    entries = Registry(tmp_path / "exp.jsonl").entries("session_breakout")
    assert len(entries) == 1 and entries[0]["passed"] is False
    assert len(entries[0]["trial_sharpes"]) == 4
    assert all(Path(p).exists() for p in res["kanban_metadata"]["artifacts"])


def test_planted_momentum_edge_is_found(cfg, tmp_path):
    res = _run(cfg, tmp_path, momentum=0.35)
    assert _gate(res, "Expectancy after costs (R)", "dev")["passed"]
    assert _gate(res, "Expectancy after costs (R)", "validation")["passed"]
    assert _gate(res, "Expectancy minus random-entry p95 (R)", "all")["passed"]
    assert _gate(res, "Walk-forward efficiency", "dev")["passed"]
    assert _gate(res, "Expectancy at 2x spread (R)", "all")["passed"]


def test_h1_variant_runs_and_shares_the_setups_trials(cfg, tmp_path):
    cfg["strategies"]["session_breakout_h1"]["symbols"] = ["EURUSD"]
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2023-01-01", seed=5)
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    reg = Registry(tmp_path / "exp.jsonl")
    run_t0("session_breakout", cfg, load, reg)
    res = run_t0("session_breakout_h1", cfg, load, reg)
    assert not res["passed"]
    assert res["setup"] == "session_breakout" and res["bar"] == "1h"
    assert res["dsr"]["n_trials"] == 8
    entry = reg.entries("session_breakout_h1")[0]
    assert entry["setup"] == "session_breakout" and entry["bar"] == "1h"


def test_strategy_exit_overrides_reach_the_backtest(cfg, tmp_path):
    cfg["strategies"]["channel_breakout_h4_trend"]["symbols"] = ["EURUSD"]
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2023-01-01", seed=5)
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    res = run_t0("channel_breakout_h4_trend", cfg, load, Registry(tmp_path / "exp.jsonl"))
    assert not res["passed"]
    assert res["exits"]["rr"] is None and res["exits"]["atr_trail_mult"] == 2.0


def test_time_exit_strategy_with_buy_and_hold_benchmark_and_filter_override(cfg, tmp_path):
    cfg["strategies"] = {"ovn": {"setup": "overnight_drift", "bar": "1h", "symbols": ["EURUSD"], "benchmark": "buy_and_hold",
                                 "exits": {"rr": None, "friday_flatten_utc": None},
                                 "filters": {"friday_no_entry_after_utc": None}, "grid": {"exit_at": ["09:30", "04:00"]}}}
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2023-01-01", seed=5)
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    res = run_t0("ovn", cfg, load, Registry(tmp_path / "exp.jsonl"))
    assert not res["passed"]
    bh = _gate(res, "Expectancy minus exposure-matched buy-and-hold (R)", "all")
    assert bh["value"] == pytest.approx(res["kanban_metadata"]["expectancy_r"] - res["benchmark"]["mean_r"])
    assert len(res["gates"]) == 15


def test_buy_and_hold_r_is_the_hourly_drift_over_the_hours_held():
    import numpy as np
    import pandas as pd

    from atlas_research.t0 import Market, buy_and_hold_r

    idx = pd.date_range("2020-01-06", periods=101, freq="1h", tz="UTC")
    mid = 100 * np.exp(0.001 * np.arange(101))  # +0.1% an hour
    m1 = pd.DataFrame({"bid_c": mid - 0.01, "ask_c": mid + 0.01}, index=idx)
    trades = pd.DataFrame({"symbol": ["X", "X"], "direction": [1, -1], "entry": [100.0, 100.0], "risk": [1.0, 2.0],
                           "entry_time": [idx[0], idx[0]], "exit_time": [idx[10], idx[10]]})
    r = buy_and_hold_r(trades, [Market("X", m1, pd.DataFrame(), None, {})], [(idx[0], idx[-1] + pd.Timedelta(hours=1))])
    assert r == pytest.approx([100 * np.expm1(0.01), -100 * np.expm1(0.01) / 2])
