"""MES round 2 setups: intraday momentum, overnight drift, opening gap, long-only channel breakout."""

import numpy as np
import pandas as pd
import pytest

from atlas_engine.features import sessions
from atlas_engine.features.frame import FeatureConfig, build_features
from atlas_engine.market_data import synthetic
from atlas_engine.setups import SETUPS, EdgeFilters

MOM = SETUPS["intraday_momentum"]
OVN = SETUPS["overnight_drift"]
GAP = SETUPS["opening_gap"]


def frame(bars: list[tuple[str, float]], minutes: int = 30) -> pd.DataFrame:
    """Decision-bar rows from (New York *close* time 'YYYY-MM-DD HH:MM', close)."""
    close_t = pd.DatetimeIndex([pd.Timestamp(t, tz=sessions.NEW_YORK) for t, _ in bars]).tz_convert("UTC")
    idx = close_t - pd.Timedelta(minutes=minutes)
    c = np.array([x for _, x in bars])
    f = pd.DataFrame({"high": c + 1, "low": c - 1, "close": c}, index=idx)
    f["close_time"] = close_t
    f["atr"] = 2.0
    f["spread"] = 0.25
    return f


def day(d: str, at0930: float, at1000: float, at1530: float, at1600: float) -> list[tuple[str, float]]:
    return [(f"{d} 09:30", at0930), (f"{d} 10:00", at1000), (f"{d} 15:30", at1530), (f"{d} 16:00", at1600)]


def test_intraday_momentum_follows_the_first_half_hour_from_the_prior_close():
    # Day 1 closes 4000. Day 2: 3990 at 09:30, 3995 at 10:00 -> down from the prior close, up from the open.
    f = frame(day("2023-03-13", 4001, 4002, 4003, 4000) + day("2023-03-14", 3990, 3995, 3996, 3997))
    sig = MOM.detect(f, {**MOM.defaults, "signal_from": "prev_close"})
    assert len(sig) == 1  # day 1 has no prior close
    s = sig.iloc[0]
    assert s["direction"] == -1
    assert s["decision_time"] == pd.Timestamp("2023-03-14 15:30", tz=sessions.NEW_YORK)
    assert s["stop"] == pytest.approx(3996 + 2.0 * 2.0)
    assert s["exit_by"] == pd.Timestamp("2023-03-14 16:00", tz=sessions.NEW_YORK)
    sig = MOM.detect(f, {**MOM.defaults, "signal_from": "open", "exit_at": "15:55"})
    assert list(sig["direction"]) == [1, 1]  # 4001 -> 4002 and 3990 -> 3995
    assert sig.iloc[1]["exit_by"] == pd.Timestamp("2023-03-14 15:55", tz=sessions.NEW_YORK)


def test_intraday_momentum_skips_flat_first_half_hours_and_follows_daylight_saving():
    f = frame(day("2023-01-09", 4000, 4000, 4001, 4002) + day("2023-01-10", 4002, 4001, 4003, 4004))
    sig = MOM.detect(f, {**MOM.defaults, "signal_from": "open"})
    assert len(sig) == 1 and sig.iloc[0]["direction"] == -1
    assert sig.iloc[0]["exit_by"] == pd.Timestamp("2023-01-10 21:00", tz="UTC")  # EST


def test_intraday_momentum_trades_friday_afternoons_only_without_the_friday_cutoff():
    f = frame(day("2023-03-16", 4000, 4001, 4002, 4003) + day("2023-03-17", 4010, 4012, 4013, 4014))  # Thu, Fri
    assert len(MOM.signals(f, {"signal_from": "open"})) == 1  # 15:30 New York Friday is after 18:00 UTC
    assert len(MOM.signals(f, {"signal_from": "open"}, EdgeFilters(friday_no_entry_after_utc=None))) == 2


def test_overnight_drift_buys_the_close_and_exits_next_weekday():
    f = frame(day("2023-03-15", 4000, 4001, 4002, 4003) + day("2023-03-16", 4004, 4005, 4006, 4007))  # Wed, Thu
    sig = OVN.detect(f, dict(OVN.defaults))
    assert list(sig["direction"]) == [1, 1]
    assert sig.iloc[0]["decision_time"] == pd.Timestamp("2023-03-15 16:00", tz=sessions.NEW_YORK)
    assert sig.iloc[0]["stop"] == pytest.approx(4003 - 3.0 * 2.0)
    assert sig.iloc[0]["exit_by"] == pd.Timestamp("2023-03-16 09:30", tz=sessions.NEW_YORK)
    assert sig.iloc[1]["exit_by"] == pd.Timestamp("2023-03-17 09:30", tz=sessions.NEW_YORK)
    short = OVN.detect(f, {**OVN.defaults, "exit_at": "04:00"})
    assert short.iloc[0]["exit_by"] == pd.Timestamp("2023-03-16 04:00", tz=sessions.NEW_YORK)


def test_overnight_drift_never_enters_on_friday_with_the_default_filters():
    f = frame(day("2023-03-16", 4000, 4001, 4002, 4003) + day("2023-03-17", 4004, 4005, 4006, 4007))  # Thu, Fri
    sig = OVN.signals(f, {})
    assert len(sig) == 1 and sig.iloc[0]["exit_by"] == pd.Timestamp("2023-03-17 09:30", tz=sessions.NEW_YORK)
    fri = OVN.detect(f, dict(OVN.defaults))  # unfiltered, a Friday entry would exit Monday
    assert fri.iloc[1]["exit_by"] == pd.Timestamp("2023-03-20 09:30", tz=sessions.NEW_YORK)


def test_opening_gap_fades_or_follows_the_overnight_move():
    f = frame(day("2023-03-13", 4000, 4001, 4002, 4000) + day("2023-03-14", 4010, 4005, 4004, 4003))  # gap up 10
    fade = GAP.detect(f, {**GAP.defaults, "mode": "fade"})
    assert len(fade) == 1
    s = fade.iloc[0]
    assert s["direction"] == -1 and s["stop"] == pytest.approx(4010 + 4.0 * 2.0)
    assert s["decision_time"] == pd.Timestamp("2023-03-14 09:30", tz=sessions.NEW_YORK)
    assert s["exit_by"] == pd.Timestamp("2023-03-14 10:30", tz=sessions.NEW_YORK)
    follow = GAP.detect(f, {**GAP.defaults, "mode": "follow", "exit_at": "15:55"})
    assert follow.iloc[0]["direction"] == 1
    assert follow.iloc[0]["exit_by"] == pd.Timestamp("2023-03-14 15:55", tz=sessions.NEW_YORK)
    with pytest.raises(ValueError):
        GAP.detect(f, {**GAP.defaults, "mode": "both"})


def test_long_only_channel_breakout_drops_the_shorts():
    m1 = synthetic.random_walk_m1(start="2019-01-01", end="2019-07-01", seed=5)
    f = build_features(m1, FeatureConfig(bar="4h"))
    cb = SETUPS["channel_breakout"]
    both = cb.signals(f, {"channel": 20})
    long_ = cb.signals(f, {"channel": 20, "sides": "long"})
    assert (both["direction"] == -1).any() and len(long_) and (long_["direction"] == 1).all()
    pd.testing.assert_frame_equal(long_.reset_index(drop=True), both[both["direction"] == 1].reset_index(drop=True))


@pytest.mark.parametrize("setup,params", [(MOM, {"signal_from": "prev_close"}), (MOM, {"signal_from": "open"}),
                                          (OVN, {"exit_at": "04:00"}), (GAP, {"mode": "fade"})])
def test_time_of_day_signals_on_a_real_feature_frame_are_causal(setup, params):
    m1 = synthetic.random_walk_m1(start="2019-01-01", end="2019-04-01", seed=3)
    f = build_features(m1, FeatureConfig(bar="30min" if setup is not OVN else "1h"))
    sig = setup.signals(f, params, EdgeFilters(friday_no_entry_after_utc=None))
    assert len(sig) > 40
    assert not pd.Index(pd.DatetimeIndex(sig["decision_time"]).tz_convert(sessions.NEW_YORK).date).duplicated().any()
    assert (pd.DatetimeIndex(sig["exit_by"]) > pd.DatetimeIndex(sig["decision_time"])).all()
    close = f.set_index("close_time")["close"].reindex(sig["decision_time"]).to_numpy()
    assert (np.sign(close - sig["stop"].to_numpy()) == sig["direction"].to_numpy()).all()
