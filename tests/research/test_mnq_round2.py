"""MNQ round 2: the noise area's volatility stop, the per-grid-point report and the generalized frozen check."""

from pathlib import Path

import pandas as pd
import pytest

from atlas_engine.features import sessions
from atlas_research.check import coverage, run_check
from atlas_research.cli import load_config
from atlas_research.registry import Registry
from atlas_research.t0 import points_summary, run_t0

from test_mnq_round import NOISE, _synthetic_mnq, frame, noise_day

MNQ_CONFIG = Path(__file__).resolve().parents[2] / "atlas_research" / "configs" / "mnq.yaml"


def _breakout_frame():
    days = pd.bdate_range("2023-05-01", periods=16)
    bars = []
    for d in days[:-1]:
        bars += noise_day(str(d.date()), {})  # every checkpoint 0.5% above the open: sigma = 0.005
    bars += noise_day(str(days[-1].date()), {30: 101.5, 60: 100.2, 90: 99.0, 120: 100.0}, quiet=100.0)
    return frame(bars, 5), days[-1]


def test_volatility_stop_keeps_the_entries_and_sits_k_half_widths_from_the_entry_close():
    f, day = _breakout_frame()
    band = NOISE.detect(f, NOISE.defaults)
    for k in (0.5, 1.0, 1.5, 2.0):
        sig = NOISE.detect(f, {**NOISE.defaults, "stop_k": k})
        # Same entries and exits as round 1; only the stop moves.
        assert list(sig["decision_time"]) == list(band["decision_time"])
        assert list(sig["direction"]) == list(band["direction"]) == [1, -1]
        assert list(sig["exit_by"]) == list(band["exit_by"])
        # Long at 101.5: the upper band is built on max(open 100, prior close 100.5), half-width 100.5 x 0.005.
        assert sig.iloc[0]["stop"] == pytest.approx(101.5 - k * 100.5 * 0.005)
        # Short at 99.0: the lower band is built on min(open, prior close) = 100, half-width 0.5.
        assert sig.iloc[1]["stop"] == pytest.approx(99.0 + k * 100.0 * 0.005)
        # Never wider than round 1's opposite-band stop.
        for i in range(2):
            close = 101.5 if i == 0 else 99.0
            assert abs(close - sig.iloc[i]["stop"]) <= abs(close - band.iloc[i]["stop"]) + 1e-9


def test_volatility_stop_rejects_a_non_positive_k():
    f, _ = _breakout_frame()
    with pytest.raises(ValueError):
        NOISE.detect(f, {**NOISE.defaults, "stop_k": 0})


def test_points_summary_is_r_times_risk():
    t = pd.DataFrame({"r": [1.0, -0.5, 0.25], "risk": [10.0, 20.0, 40.0]})
    assert points_summary(t) == {"net_points_per_trade": pytest.approx(10 / 3), "total_points": pytest.approx(10.0),
                                 "median_risk_points": 20.0}
    assert points_summary(t.iloc[:0])["net_points_per_trade"] == 0.0


@pytest.fixture()
def mnq_cfg():
    c = load_config(MNQ_CONFIG)
    c["segments"] = {"dev": ["2019-01-01", "2021-01-01"], "validation": ["2021-01-01", "2021-07-01"], "holdout_start": "2021-07-01"}
    c["gates"].update(mc_sims=200, random_control_runs=3, random_direction_runs=3)
    return c


def test_round2_config_declares_four_stop_points_and_the_2018_check():
    c = load_config(MNQ_CONFIG)
    s = c["strategies"]["mnq_noise_area_r2_m5"]
    assert s["setup"] == "noise_area" and s["grid"] == {"stop_k": [0.5, 1.0, 1.5, 2.0]}
    assert c["checks"]["mnq_noise_area_r2_2018"]["period"] == ["2018-01-01", "2019-01-01"]
    assert c["checks"]["mnq_noise_area_r1_2018_info"]["strategy"] == "mnq_noise_area_m5"


def test_round2_run_reports_every_grid_point_fixed_without_changing_the_verdict(mnq_cfg, tmp_path):
    m1 = _synthetic_mnq()
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    mnq_cfg["strategies"]["mnq_noise_area_r2_m5"]["grid"] = {"stop_k": [1.0, 2.0]}
    reg = Registry(tmp_path / "exp.jsonl")
    res = run_t0("mnq_noise_area_r2_m5", mnq_cfg, load, reg)
    fps = res["extra"]["fixed_points"]
    assert [fp["params"]["stop_k"] for fp in fps] == [1.0, 2.0]
    assert len(res["gates"]) == 17 and len(res["trial_sharpes"]) == 2
    for fp in fps:
        assert fp["span"] == ["2020-01-01", "2021-07-01"]
        assert fp["trades"] == fp["dev_span"]["trades"] + fp["validation"]["trades"]
        assert {"net_points_per_trade", "median_risk_points", "random_direction", "random_entry", "by_year"} <= set(fp)
        assert any(g["gate"] == "Expectancy at all-in Stress round trip (R)" for g in fp["gates"])
        assert all(y >= 2020 for y in fp["by_year"])
    # The tighter stop has a smaller 1R in points.
    assert fps[0]["median_risk_points"] < fps[1]["median_risk_points"]
    (entry,) = reg.entries("mnq_noise_area_r2_m5")
    assert "gates" not in entry["extra"]["fixed_points"][0] and entry["extra"]["fixed_points"][0]["failed_gates"] is not None
    assert "net_points_per_trade" in entry["extra"]


def test_frozen_check_runs_a_research_setup_with_the_review_rules(mnq_cfg, tmp_path):
    m1 = _synthetic_mnq()
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    ck = mnq_cfg["checks"]["mnq_noise_area_r2_2018"]
    ck.update(params={"stop_k": 1.0}, period=["2019-01-01", "2020-01-01"], price_checks={})
    ck["coverage"]["reference_m1_bars_per_year"] = float((m1.index.year == 2019).sum())
    reg = Registry(tmp_path / "exp.jsonl")
    res = run_check("mnq_noise_area_r2_2018", mnq_cfg, load, reg)
    labels = [g["gate"] for g in res["pass_rules"]]
    assert [x[:3] for x in labels] == ["(a)", "(b)", "(c)", "(d)", "(e)", "(f)"]
    assert "all-in Stress" in labels[4] and "random-direction" in labels[5]
    assert res["final_params"]["stop_k"] == 1.0 and res["final_params"]["trail"] == "band"
    assert res["all_in"]["trades"] > 0 and res["random_direction"]["runs"] == 3
    assert res["points"]["median_risk_points"] > 0
    cov = res["coverage"]["MNQ"][0]
    assert cov["h4_frac"] is None and cov["rth_frac"] > 0.95 and cov["included"]
    (entry,) = reg.entries("mnq_noise_area_r2_m5")
    assert entry["kind"] == "frozen_check" and entry["trial_sharpes"] == []
    # Off-grid frozen parameters are refused.
    ck["params"] = {"stop_k": 0.75}
    with pytest.raises(ValueError):
        run_check("mnq_noise_area_r2_2018", mnq_cfg, load, Registry(tmp_path / "x.jsonl"))


def test_coverage_can_require_the_regular_session_share():
    m1 = _synthetic_mnq("2019-01-01", "2020-01-01")
    ny = m1.index.tz_convert(sessions.NEW_YORK)
    mins = ny.hour * 60 + ny.minute
    thin = m1.loc[~((mins >= 570) & (mins < 960) & (ny.month <= 2))]  # Jan-Feb regular sessions missing
    period = (pd.Timestamp("2019-01-01", tz="UTC"), pd.Timestamp("2020-01-01", tz="UTC"))
    ref = float(len(m1))
    assert coverage(m1, period, None, ref, 0.5, 0.95).loc[2019, "included"]
    cov = coverage(thin, period, None, ref, 0.5, 0.95)
    assert cov.loc[2019, "rth_frac"] < 0.95 and not cov.loc[2019, "included"]
    assert coverage(thin, period, None, ref, 0.5).loc[2019, "included"]  # without the rule, M1 count alone
