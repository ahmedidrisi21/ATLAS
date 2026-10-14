"""Jev filter test harness (docs/jev-mes-filter-test.md) on synthetic data with a stand-in Jev."""

from pathlib import Path

import numpy as np
import pandas as pd

from atlas_engine.adapters.jev import JevAdapter, ReplayTransport
from atlas_engine.market_data import synthetic
from atlas_research.cli import DEFAULT_CONFIG, load_config
from atlas_research.jev_filter import RecordingTransport, run, take_mask


def test_take_mask_skips_low_scores_against_earlier_answers_only():
    t = pd.Series(pd.date_range("2020-01-01", periods=6, freq="D", tz="UTC"))
    p = np.array([0.1, 0.5, 0.2, np.nan, 0.05, 0.6])
    take = take_mask(p, t, skip_frac=0.5, min_prior=2)
    # Warm-up keeps the first two; a failed answer is never taken; later ones compare with earlier answers.
    assert take.tolist() == [True, True, False, False, False, True]


def _cfg():
    c = load_config(DEFAULT_CONFIG)
    c["segments"] = {"dev": ["2019-01-01", "2021-01-01"], "validation": ["2021-01-01", "2021-07-01"], "holdout_start": "2021-07-01"}
    c["strategies"]["long_h4"] = {"setup": "channel_breakout", "bar": "4h", "symbols": ["EURUSD"],
                                  "grid": {"channel": [20, 55], "sl_atr": [1.5, 2.5], "sides": ["long"]}}
    return c


def _load(seen):
    m1 = synthetic.random_walk_m1("EURUSD", "2019-01-01", "2021-07-01", seed=5)

    def load(sym, a, b):
        seen.append(b)
        return m1.loc[(m1.index >= a) & (m1.index < b)]
    return load


def test_constant_jev_keeps_every_trade_and_never_sees_the_holdout(tmp_path: Path):
    requests, seen = [], []

    def flat_jev(req):
        requests.append(req)
        return {"setup_id": req["setup_id"], "p_target_first": 0.3, "regime": "trend_normal_vol",
                "reason_codes": [], "model_version": "jev-test"}

    rec = RecordingTransport(flat_jev)
    res = run("long_h4", _cfg(), _load(seen), JevAdapter(rec, "jev-test"), controls=5)
    assert max(seen) <= pd.Timestamp("2021-07-01", tz="UTC")
    assert res["plain"]["trades"] > 0
    # Every answer equals the earlier ones' quantile, so nothing is skipped: the Jev arm is the plain strategy.
    assert res["jev"]["trades"] == res["plain"]["trades"]
    assert abs(res["jev"]["expectancy_r"] - res["plain"]["expectancy_r"]) < 1e-12
    assert not res["passed"] and not res["checks"]["jev_trades_at_least_300"]
    # Only the whitelisted state reaches Jev: no prices, times or symbols.
    for req in requests:
        assert "decision_time" not in req["state"] and "entry" not in req["state"] and "symbol" not in req["state"]
    # The recorded answers replay to the same result.
    replay = JevAdapter(ReplayTransport({r["request_hash"]: r["response"] for r in rec.records}), "jev-test")
    again = run("long_h4", _cfg(), _load([]), replay, controls=5)
    assert again["jev"]["trades"] == res["jev"]["trades"] and not again["jev_skips"]


def test_jev_errors_skip_trades_rather_than_take_them():
    def broken(req):
        raise RuntimeError("down")

    res = run("long_h4", _cfg(), _load([]), JevAdapter(broken, "jev-test"), controls=2)
    assert res["jev"]["trades"] == 0 and sum(res["jev_skips"].values()) > 0
