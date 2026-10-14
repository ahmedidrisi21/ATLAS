"""Frozen-parameter confirmation checks (MES round 3 harness) on synthetic data."""

import json

import pandas as pd
import pytest

from atlas_engine.market_data import synthetic
from atlas_research.check import PriceScaleError, coverage, run_check, year_table
from atlas_research.cli import DEFAULT_CONFIG, load_config
from atlas_research.data import HoldoutAccessError
from atlas_research.registry import Registry


@pytest.fixture(scope="module")
def m1():
    return synthetic.random_walk_m1("EURUSD", "2016-01-01", "2019-01-01", seed=5)


@pytest.fixture()
def cfg(m1):
    c = load_config(DEFAULT_CONFIG)
    c["segments"] = {"dev": ["2019-01-01", "2020-01-01"], "validation": ["2020-01-01", "2020-07-01"], "holdout_start": "2020-07-01"}
    c["gates"]["mc_sims"] = 500
    c["gates"]["random_control_runs"] = 10
    c["strategies"] = {"cb_long": {"setup": "channel_breakout", "bar": "4h", "symbols": ["EURUSD"], "benchmark": "buy_and_hold",
                                   "grid": {"channel": [20, 55], "sl_atr": [1.5, 2.5], "sides": ["long"]}}}
    h4 = synthetic_refs(m1)
    c["checks"] = {"cb_old": {
        "strategy": "cb_long", "params": {"channel": 20, "sl_atr": 1.5, "sides": "long"}, "period": ["2016-01-01", "2019-01-01"],
        "coverage": {"reference_h4_bars_per_year": h4[0], "reference_m1_bars_per_year": h4[1], "min_frac": 0.70},
        "pass_rules": {"min_expectancy_r": 0.10, "beat_buy_and_hold": True, "beat_random_p95": True, "min_trades": 150, "max_year_share": 0.50},
    }}
    return c


def synthetic_refs(m1):
    from atlas_engine.market_data.bars import resample

    return float(resample(m1, "4h").groupby(lambda t: t.year).size().median()), float(m1.groupby(m1.index.year).size().median())


def _loader(m1):
    return lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]


def test_frozen_check_runs_once_and_is_recorded(cfg, m1, tmp_path):
    reg = Registry(tmp_path / "exp.jsonl")
    res = run_check("cb_old", cfg, _loader(m1), reg, out_dir=tmp_path / "runs")
    assert not res["passed"]  # a random walk has no edge
    assert len(res["pass_rules"]) == 5
    assert res["final_params"]["channel"] == 20 and res["final_params"]["sides"] == "long"
    assert (res["summary"]["trades"] > 0) and set(res["data_window"]["included_years"]) == {2016, 2017, 2018}
    assert sum(y["trades"] for y in res["years"]) == res["summary"]["trades"]
    # Long-only: every trade, and so every random-control entry, is long.
    trades = pd.read_csv(tmp_path / "runs" / res["experiment_id"] / "trades.csv")
    assert (trades["direction"] == 1).all()
    # The T0 table is informational; the walk-forward / validation rows are n/a.
    assert sum(g["passed"] is None for g in res["t0_gates"]) == 3
    (entry,) = reg.entries("cb_long")
    assert entry["kind"] == "frozen_check" and entry["trial_sharpes"] == [] and entry["passed"] is False
    assert entry["data_window"]["check"] == ["2016-01-01", "2019-01-01"]
    json.dumps(res, default=str)


def test_thin_year_is_excluded_and_its_trades_dropped(cfg, m1, tmp_path):
    thin = m1.loc[(m1.index.year != 2017) | (m1.index.month == 1)]  # 2017 keeps only January
    res = run_check("cb_old", cfg, _loader(thin), Registry(tmp_path / "exp.jsonl"))
    cov = {c["year"]: c for c in res["coverage"]["EURUSD"]}
    assert not cov[2017]["included"] and cov[2016]["included"] and cov[2018]["included"]
    assert res["data_window"]["included_years"] == [2016, 2018]
    assert {y["year"] for y in res["years"]} <= {2016, 2018}


def test_coverage_counts_against_references(m1):
    cov = coverage(m1, (pd.Timestamp("2016-01-01", tz="UTC"), pd.Timestamp("2019-01-01", tz="UTC")), 1e9, 1.0, 0.7)
    assert list(cov.index) == [2016, 2017, 2018] and not cov["included"].any()


def test_off_grid_params_are_refused(cfg, m1, tmp_path):
    cfg["checks"]["cb_old"]["params"]["channel"] = 30
    with pytest.raises(ValueError, match="not on"):
        run_check("cb_old", cfg, _loader(m1), Registry(tmp_path / "exp.jsonl"))
    assert not (tmp_path / "exp.jsonl").exists()


def test_period_reaching_the_holdout_is_refused(cfg, m1, tmp_path):
    cfg["checks"]["cb_old"]["period"] = ["2016-01-01", "2021-01-01"]
    with pytest.raises(HoldoutAccessError):
        run_check("cb_old", cfg, _loader(m1), Registry(tmp_path / "exp.jsonl"))


def test_wrong_price_scale_stops_the_run(cfg, m1, tmp_path):
    cfg["checks"]["cb_old"]["price_checks"] = {"2016-03-01": 1150.0}
    with pytest.raises(PriceScaleError):
        run_check("cb_old", cfg, _loader(m1), Registry(tmp_path / "exp.jsonl"))
    assert not (tmp_path / "exp.jsonl").exists()


def test_year_table_drawdown_and_share():
    t = pd.DataFrame({"entry_time": pd.to_datetime(["2016-02-01", "2016-03-01", "2016-04-01", "2017-02-01"], utc=True),
                      "r": [2.0, -1.0, -1.0, 2.0]})
    y = year_table(t)
    assert y.loc[2016, "max_dd_r"] == 2.0 and y.loc[2016, "total_r"] == 0.0
    assert y.loc[2017, "share_of_profit"] == 1.0 and y.loc[2016, "win_rate"] == pytest.approx(1 / 3)


def test_mes_round3_declaration_is_frozen_on_round2s_final_params():
    from pathlib import Path

    mes = load_config(Path(DEFAULT_CONFIG).parent / "mes.yaml")
    c = mes["checks"]["mes_channel_breakout_long_h4_2013_2018"]
    assert c["params"] == {"channel": 20, "sl_atr": 1.5, "sides": "long"}
    assert c["period"] == ["2013-01-01", "2019-01-01"]
    assert pd.Timestamp(c["period"][1]) <= pd.Timestamp(mes["segments"]["dev"][0])
