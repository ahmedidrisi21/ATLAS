"""MNQ round 3: the 2013-2017 frozen checks, the years rule (g), the profit-factor rule and the top-10 share."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from atlas_research.check import losing_years, run_check, top_trades_share, year_table
from atlas_research.cli import load_config
from atlas_research.registry import Registry

from test_mnq_round import _synthetic_mnq

MNQ_CONFIG = Path(__file__).resolve().parents[2] / "atlas_research" / "configs" / "mnq.yaml"
R3 = ("mnq_noise_area_r3_2013_2017_k05", "mnq_noise_area_r3_2013_2017_k10")


def test_round3_config_declares_two_frozen_versions_with_eight_rules():
    c = load_config(MNQ_CONFIG)
    for name, k in zip(R3, (0.5, 1.0)):
        ck = c["checks"][name]
        assert ck["strategy"] == "mnq_noise_area_r2_m5" and ck["params"] == {"stop_k": k}
        assert ck["period"] == ["2013-01-01", "2018-01-01"]
        assert ck["coverage"] == {"reference_m1_bars_per_year": 342192, "min_frac": 0.70, "min_rth_frac": 0.95}
        assert ck["pass_rules"]["max_losing_years"] == 1 and ck["pass_rules"]["min_profit_factor"] == 1.25
        assert len(ck["pass_rules"]) == 8 and len(ck["price_checks"]) == 5
        # The holdout is never touched.
        assert pd.Timestamp(ck["period"][1]) < pd.Timestamp(c["segments"]["holdout_start"])
    for name in R3:
        info = c["checks"][name + "_rth_only_info"]
        assert info["coverage"]["min_frac"] == 0.0 and info["coverage"]["min_rth_frac"] == 0.95


def test_top_trades_share():
    r = np.array([5.0, 3.0, -1.0, -1.0] + [0.1] * 20)
    assert top_trades_share(r, 2) == pytest.approx(8.0 / 8.0)
    assert top_trades_share(r, 10) == pytest.approx((5 + 3 + 0.8) / 8.0)
    assert top_trades_share(np.array([-1.0, 0.5]), 10) is None
    assert top_trades_share(np.array([]), 10) is None


def test_losing_years_counts_included_years_without_a_positive_average():
    t = pd.DataFrame({"entry_time": pd.to_datetime(["2013-03-01", "2014-03-01", "2014-04-01", "2015-03-01"], utc=True),
                      "r": [1.0, -1.0, 0.5, 0.2]})
    years = year_table(t)
    assert losing_years(years, [2013, 2014, 2015]) == 1      # 2014 averages -0.25
    assert losing_years(years, [2013, 2015, 2016]) == 1      # 2016 has no trades: not positive
    assert losing_years(years, [2013, 2015]) == 0
    assert losing_years(pd.DataFrame(), [2013]) == 1


def test_round3_check_letters_the_years_and_profit_factor_rules_g_and_h(tmp_path):
    c = load_config(MNQ_CONFIG)
    c["segments"] = {"dev": ["2019-01-01", "2021-01-01"], "validation": ["2021-01-01", "2021-07-01"], "holdout_start": "2021-07-01"}
    c["gates"].update(mc_sims=200, random_control_runs=3, random_direction_runs=3)
    m1 = _synthetic_mnq()
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    ck = c["checks"]["mnq_noise_area_r3_2013_2017_k10"]
    ck.update(period=["2019-01-01", "2021-01-01"], price_checks={})
    ck["coverage"] = {**ck["coverage"], "reference_m1_bars_per_year": float((m1.index.year == 2019).sum())}
    reg = Registry(tmp_path / "exp.jsonl")
    res = run_check("mnq_noise_area_r3_2013_2017_k10", c, load, reg)
    labels = [g["gate"] for g in res["pass_rules"]]
    assert [x[:3] for x in labels] == ["(a)", "(b)", "(c)", "(d)", "(e)", "(f)", "(g)", "(h)"]
    assert "average R <= 0" in labels[6] and labels[7].endswith("Profit factor")
    g, h = res["pass_rules"][6], res["pass_rules"][7]
    assert g["value"] == losing_years(pd.DataFrame(res["years"]).set_index("year"), res["data_window"]["included_years"])
    assert h["value"] == pytest.approx(res["summary"]["profit_factor"]) and h["rule"] == ">= 1.25"
    assert res["years_all_in"] and {y["year"] for y in res["years_all_in"]} <= {2019, 2020}
    assert res["top10_share_of_r"] is None or res["top10_share_of_r"] > 0
    (entry,) = reg.entries("mnq_noise_area_r2_m5")
    assert entry["kind"] == "frozen_check" and "top10_share_of_r" in entry and entry["years"]
