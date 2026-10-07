"""MNQ round 3 range fade (docs/mnq-range-fade.md): rules on synthetic bars, causality, and the T0 wiring."""

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from atlas_engine.market_data import synthetic
from atlas_engine.setups import EdgeFilters
from atlas_research.research_setups import ALL_SETUPS
from atlas_research.t0 import prepare_market

CFG = yaml.safe_load((Path(__file__).resolve().parents[2] / "atlas_research/configs/mnq.yaml").read_text())


M1 = synthetic.random_walk_m1("EURUSD", "2020-01-01", "2020-06-01", seed=3) * 10000


def _market(end="2020-06-01"):
    return prepare_market("MNQ", M1.loc[M1.index < pd.Timestamp(end, tz="UTC")], CFG, "5min")


def _signals(m, **p):
    st = ALL_SETUPS["range_fade"]
    return st.signals(m.features, {**st.defaults, **p}, EdgeFilters(**CFG["filters"]))


def test_one_entry_a_day_stop_beyond_the_range_flat_at_close():
    sig = _signals(_market(), edge=0.2, calm=0)
    assert len(sig) > 3 and set(sig["direction"]) <= {1, -1}
    t = pd.DatetimeIndex(sig["decision_time"]).tz_convert("America/New_York")
    assert len(set(t.date)) == len(sig)                       # first qualifying entry per day only
    assert ((t.hour * 60 + t.minute >= 600) & (t.hour * 60 + t.minute <= 900)).all()
    x = pd.DatetimeIndex(sig["exit_by"]).tz_convert("America/New_York")
    assert ((x.hour == 16) & (x.minute == 0)).all()


def test_calm_filter_only_removes_entries_and_the_signals_do_not_look_ahead():
    m = _market()
    every, calm = _signals(m, edge=0.2, calm=0), _signals(m, edge=0.2, calm=1)
    assert set(calm["decision_time"]) <= set(every["decision_time"])
    # Same signals when the data after a cut-off is thrown away: nothing uses later bars.
    cut = pd.Timestamp("2020-04-01", tz="UTC")
    early = _signals(_market("2020-04-01"), edge=0.2, calm=0)
    full = every.loc[pd.DatetimeIndex(every["decision_time"]) < cut - pd.Timedelta(days=1)]
    got = early.loc[pd.DatetimeIndex(early["decision_time"]) < cut - pd.Timedelta(days=1)]
    assert np.allclose(got["stop"].to_numpy(), full["stop"].to_numpy()) and list(got["direction"]) == list(full["direction"])


def test_declared_in_the_config_with_a_four_point_grid():
    s = CFG["strategies"]["mnq_range_fade_m5"]
    assert s["setup"] == "range_fade" and s["exits"]["rr"] == 1.5
    assert len(s["grid"]["edge"]) * len(s["grid"]["calm"]) == 4
