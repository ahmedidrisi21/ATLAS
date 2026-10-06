import numpy as np
import pandas as pd
import pytest

from atlas_engine.features import sessions
from atlas_engine.features.frame import FeatureConfig, build_features
from atlas_engine.market_data import synthetic
from atlas_engine.setups import SETUPS

ORB = SETUPS["opening_range_breakout"]


def frame(day: str, rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """M15 feature rows from (New York open time, high, low, close)."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{day} {t}", tz=sessions.NEW_YORK) for t, *_ in rows]).tz_convert("UTC")
    f = pd.DataFrame({"high": [r[1] for r in rows], "low": [r[2] for r in rows], "close": [r[3] for r in rows]}, index=idx)
    f["close_time"] = idx + pd.Timedelta(minutes=15)
    f["atr"] = 2.0
    f["spread"] = 0.25
    return f


DAY = [
    ("09:15", 4010.0, 3990.0, 4000.0),  # pre-open: not part of the range
    ("09:30", 4005.0, 3995.0, 4001.0),  # the 15-minute range: 3995..4005
    ("09:45", 4004.0, 3996.0, 4003.0),  # inside
    ("10:00", 4008.0, 4001.0, 4007.0),  # first close above 4005 -> long
    ("10:15", 4010.0, 3990.0, 3992.0),  # later break the other way: ignored (one per day)
]


def test_long_on_first_close_above_the_range_with_stop_and_time_exit():
    sig = ORB.detect(frame("2023-03-14", DAY), {**ORB.defaults, "range_minutes": 15, "sl_range_frac": 1.0})
    assert len(sig) == 1
    s = sig.iloc[0]
    assert s["direction"] == 1
    assert s["decision_time"] == pd.Timestamp("2023-03-14 10:15", tz=sessions.NEW_YORK)
    assert s["stop"] == pytest.approx(3995.0)  # far edge
    assert s["exit_by"] == pd.Timestamp("2023-03-14 15:55", tz=sessions.NEW_YORK)


def test_half_range_stop_and_thirty_minute_range():
    sig = ORB.detect(frame("2023-03-14", DAY), {**ORB.defaults, "range_minutes": 15, "sl_range_frac": 0.5})
    assert sig.iloc[0]["stop"] == pytest.approx(4000.0)
    # A 30-minute range (09:30 and 09:45 bars) is 3995..4005 too, so the same bar triggers.
    sig30 = ORB.detect(frame("2023-03-14", DAY), {**ORB.defaults, "range_minutes": 30})
    assert sig30.iloc[0]["decision_time"] == pd.Timestamp("2023-03-14 10:15", tz=sessions.NEW_YORK)


def test_short_break_and_exit_time_follows_daylight_saving():
    rows = [("09:30", 4005.0, 3995.0, 4000.0), ("09:45", 4000.0, 3990.0, 3991.0)]
    sig = ORB.detect(frame("2023-01-10", rows), {**ORB.defaults, "range_minutes": 15})
    s = sig.iloc[0]
    assert s["direction"] == -1 and s["stop"] == pytest.approx(4005.0)
    assert s["exit_by"] == pd.Timestamp("2023-01-10 20:55", tz="UTC")  # EST: New York = UTC-5


def test_no_trade_after_entry_end_or_without_a_0930_bar():
    late = [("09:30", 4005.0, 3995.0, 4000.0)] + [(f"{h}:{m}", 4004.0, 3996.0, 4000.0) for h in (9, 10, 11) for m in ("00", "15", "30", "45")
                                                  if (h, m) > (9, "30")] + [("12:00", 4010.0, 4001.0, 4009.0)]
    assert ORB.detect(frame("2023-03-14", late), dict(ORB.defaults)).empty  # 12:00 bar closes 12:15 > entry_end
    no_open = [("09:45", 4005.0, 3995.0, 4000.0), ("10:00", 4010.0, 4001.0, 4009.0)]
    assert ORB.detect(frame("2023-03-14", no_open), dict(ORB.defaults)).empty


def test_signals_on_real_feature_frame_are_causal_and_one_per_day():
    m1 = synthetic.random_walk_m1(start="2019-01-01", end="2019-04-01", seed=3)
    f = build_features(m1, FeatureConfig(bar="15min"))
    sig = ORB.signals(f, {"range_minutes": 30})
    assert len(sig) > 20
    t = pd.DatetimeIndex(sig["decision_time"]).tz_convert(sessions.NEW_YORK)
    mins = t.hour * 60 + t.minute
    assert (mins >= 10 * 60 + 15).all() and (mins <= 12 * 60).all()  # first close after the range, by 12:00
    assert not pd.Index(t.date).duplicated().any()
    assert (pd.DatetimeIndex(sig["exit_by"]) > pd.DatetimeIndex(sig["decision_time"])).all()
    assert (np.sign(f.set_index("close_time")["close"].reindex(sig["decision_time"]).to_numpy() - sig["stop"].to_numpy())
            == sig["direction"].to_numpy()).all()
