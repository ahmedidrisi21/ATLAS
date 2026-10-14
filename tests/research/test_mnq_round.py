"""MNQ round 1: research setups, the re-entry flag, the extra gates, the all-in cost tier and the trend filter."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from atlas_engine.features import sessions
from atlas_engine.market_data import synthetic
from atlas_research import daily_trend
from atlas_research.backtest import CostModel, ExitPolicy, simulate
from atlas_research.check import rth_coverage
from atlas_research.cli import load_config
from atlas_research.data import quoted_at_spread
from atlas_research.registry import Registry
from atlas_research.research_setups import RESEARCH_SETUPS, daily_bars, rsi, trading_day
from atlas_research.t0 import run_t0

MNQ_CONFIG = Path(__file__).resolve().parents[2] / "atlas_research" / "configs" / "mnq.yaml"
LATE = RESEARCH_SETUPS["late_day_momentum"]
NOISE = RESEARCH_SETUPS["noise_area"]
CANDLE = RESEARCH_SETUPS["opening_candle"]
RSI2 = RESEARCH_SETUPS["rsi2_pullback"]


def frame(bars, minutes: int) -> pd.DataFrame:
    """Decision bars from (New York *close* time, close) or (time, open, high, low, close)."""
    close_t = pd.DatetimeIndex([pd.Timestamp(b[0], tz=sessions.NEW_YORK) for b in bars]).tz_convert("UTC")
    rows = [(b[1], b[1] + 1, b[1] - 1, b[1]) if len(b) == 2 else b[1:] for b in bars]
    o, h, low, c = (np.array(x, float) for x in zip(*rows))
    f = pd.DataFrame({"open": o, "high": h, "low": low, "close": c}, index=close_t - pd.Timedelta(minutes=minutes))
    f["close_time"] = close_t
    f["atr"] = 2.0
    f["spread"] = 0.25
    return f


# --- late-day momentum -------------------------------------------------------


def late_day(d: str, at1500: float, at1530: float, at1600: float):
    return [(f"{d} 15:00", at1500), (f"{d} 15:30", at1530), (f"{d} 16:00", at1600)]


def test_late_day_momentum_trades_the_sign_from_the_prior_close_to_1530():
    # Mon closes 100. Tue 15:30 is 103 (up from 100) but 15:00-15:30 fell (105 -> 103).
    f = frame(late_day("2023-03-13", 99, 99, 100) + late_day("2023-03-14", 105, 103, 104), 30)
    sig = LATE.detect(f, LATE.defaults)
    assert len(sig) == 1
    s = sig.iloc[0]
    assert s["direction"] == 1
    assert s["decision_time"] == pd.Timestamp("2023-03-14 15:30", tz=sessions.NEW_YORK)
    assert s["exit_by"] == pd.Timestamp("2023-03-14 16:00", tz=sessions.NEW_YORK)
    assert s["stop"] == pytest.approx(103 - 1.0 * 2.0)  # 1 x ATR
    assert LATE.detect(f, {**LATE.defaults, "confirm": "last_half_hour"}).empty  # the half hour disagrees


def test_late_day_momentum_skips_holidays_and_early_closes():
    # 2023-11-24 is the day after Thanksgiving (early close).
    f = frame(late_day("2023-11-22", 99, 99, 100) + late_day("2023-11-24", 99, 103, 104), 30)
    assert LATE.detect(f, LATE.defaults).empty
    assert list(trading_day([pd.Timestamp("2023-11-24").date(), pd.Timestamp("2023-11-27").date()])) == [False, True]


# --- noise area --------------------------------------------------------------


def noise_day(d: str, path: dict[int, float], open_: float = 100.0, quiet: float = 100.5) -> list:
    """M5 bars 09:30-16:00; ``path`` maps checkpoint minutes after 09:30 to the close there, others close at ``quiet``."""
    out = []
    t0 = pd.Timestamp(f"{d} 09:30")
    for k in range(78):
        close_t = t0 + pd.Timedelta(minutes=5 * (k + 1))
        m = 5 * (k + 1)
        c = path.get(m, quiet)
        o = open_ if k == 0 else c
        out.append((str(close_t), o, max(o, c), min(o, c), c))
    return out


def test_noise_area_enters_beyond_the_band_trails_and_reverses():
    days = pd.bdate_range("2023-05-01", periods=16)
    bars = []
    for d in days[:-1]:
        bars += noise_day(str(d.date()), {})  # every checkpoint 0.5% above the open: sigma = 0.005
    # Day 16: 10:00 at 101.5 (> 101.0 upper band) -> long; 10:30 at 100.2 (< upper band) -> exit;
    # 11:00 at 99 (< 99.5 lower band) -> short; 11:30 back at 100 (> lower band) -> exit.
    bars += noise_day(str(days[-1].date()), {30: 101.5, 60: 100.2, 90: 99.0, 120: 100.0}, quiet=100.0)
    f = frame(bars, 5)
    sig = NOISE.detect(f, NOISE.defaults)
    ny = lambda hhmm: pd.Timestamp(f"{days[-1].date()} {hhmm}", tz=sessions.NEW_YORK)  # noqa: E731
    # The prior close (100.5) is above the open, so the upper band is 100.5 x 1.005 (gap-adjusted).
    assert list(sig["direction"]) == [1, -1]
    assert list(sig["decision_time"]) == [ny("10:00"), ny("11:00")]
    assert list(sig["exit_by"]) == [ny("10:30"), ny("11:30")]
    assert sig.iloc[0]["stop"] == pytest.approx(100.0 * (1 - 0.005))  # the lower band at entry
    assert sig.iloc[1]["stop"] == pytest.approx(100.5 * 1.005)


def test_noise_area_twap_trail_holds_a_long_above_the_band_until_the_twap_is_lost():
    days = pd.bdate_range("2023-05-01", periods=16)
    bars = []
    for d in days[:-1]:
        bars += noise_day(str(d.date()), {})
    bars += noise_day(str(days[-1].date()), {30: 103.0, 60: 101.5}, quiet=103.0)
    f = frame(bars, 5)
    band = NOISE.detect(f, NOISE.defaults)
    twap = NOISE.detect(f, {**NOISE.defaults, "trail": "band_twap"})
    ny = lambda hhmm: pd.Timestamp(f"{days[-1].date()} {hhmm}", tz=sessions.NEW_YORK)  # noqa: E731
    # At 10:30, 101.5 is above the band (~101.0) but below the session TWAP (~102.8).
    assert band.iloc[0]["exit_by"] == ny("16:00")
    assert twap.iloc[0]["exit_by"] == ny("10:30")


# --- opening candle ----------------------------------------------------------


def test_opening_candle_follows_the_first_five_minutes_with_its_far_end_as_stop():
    f = frame([("2023-05-01 09:35", 100, 110, 95, 108), ("2023-05-02 09:35", 100, 101, 99, 99.5),
               ("2023-05-03 09:35", 100, 100.5, 99.5, 100.1), ("2023-05-03 09:40", 100, 101, 99, 100)], 5)
    sig = CANDLE.detect(f, CANDLE.defaults).sort_values("decision_time").reset_index(drop=True)
    assert list(sig["direction"]) == [1, -1]  # 05-03 is a doji (body 0.1 < one tick) and its 09:40 bar is not the open
    assert sig.iloc[0]["stop"] == 95  # far end of the candle
    assert sig.iloc[1]["stop"] == pytest.approx(99.5 + 4.0)  # range too small: the 4-point minimum stop
    assert sig.iloc[0]["exit_by"] == pd.Timestamp("2023-05-01 16:00", tz=sessions.NEW_YORK)


# --- RSI(2) pullback ---------------------------------------------------------


def daily_frame(closes: list[float], start="2023-01-02") -> pd.DataFrame:
    days = [d for d in pd.bdate_range(start, periods=len(closes) + 10) if trading_day([d.date()])[0]][: len(closes)]
    bars = []
    for d, c in zip(days, closes):
        bars += [(f"{d.date()} 15:30", c), (f"{d.date()} 16:00", c)]
    return frame(bars, 30)


def test_daily_bars_use_the_1600_close_and_skip_holidays():
    f = frame([("2023-07-03 16:00", 10), ("2023-07-03 16:30", 11), ("2023-07-04 16:00", 12), ("2023-07-05 16:00", 13)], 30)
    d = daily_bars(f)
    assert [str(x) for x in d.index] == ["2023-07-03", "2023-07-05"]  # July 4th dropped
    assert d.loc[pd.Timestamp("2023-07-05").date(), "high"] == 14  # the 16:30 bar belongs to the next session


RAMP = [100 + i + (0.6 if i % 2 else 0) for i in range(30)]  # a zig-zag uptrend


def test_rsi2_pullback_buys_dips_above_the_trend_and_exits_on_the_rules():
    f = daily_frame(RAMP + [126, 122, 123, 128, 129, 130, 131, 132, 133])
    p = {**RSI2.defaults, "trend_sma": 30}
    sig = RSI2.detect(f, p)
    d = daily_bars(f)
    t = list(d["decision_time"])
    assert len(sig) == 1 and sig.iloc[0]["direction"] == 1
    assert t.index(sig.iloc[0]["decision_time"]) == 31  # 122: RSI(2) 9.4 < 10, above the 30-day average
    assert rsi(d["close"], 2).iloc[31] < 10
    assert t.index(sig.iloc[0]["exit_by"]) == 33  # 123 is under the 5-day SMA; 128 is the first close above it
    assert sig.iloc[0]["stop"] == pytest.approx(122 - 3.0 * ind_atr(d).iloc[31])
    assert RSI2.detect(f, {**p, "trigger": "three_lower_closes"}).empty  # never three lower closes in a row
    assert RSI2.detect(f, {**p, "trend_sma": 5}).empty  # 122 is below the 5-day average: no uptrend


def test_rsi2_pullback_times_out_after_five_sessions():
    f = daily_frame(RAMP + [126, 122, 121.9, 121.8, 121.7, 121.6, 121.5, 121.4, 121.3])
    sig = RSI2.detect(f, {**RSI2.defaults, "trend_sma": 30, "rsi_exit": 101})
    t = list(daily_bars(f)["decision_time"])
    assert t.index(sig.iloc[0]["decision_time"]) == 31
    assert t.index(sig.iloc[0]["exit_by"]) == 36  # never above the 5-day SMA: out at the 5th close


def ind_atr(d):
    from atlas_engine.features import indicators

    return indicators.atr(d["high"], d["low"], d["close"], 14)


# --- backtest: re-entry at the exit bar ----------------------------------------


def test_reenter_at_exit_bar_lets_a_reversal_fill_on_the_exit_minute():
    idx = pd.date_range("2023-05-01 14:00", periods=60, freq="1min", tz="UTC")
    m1 = pd.DataFrame({f"{s}_{c}": 100.0 for s in ("bid", "ask") for c in "ohlc"}, index=idx)
    m1[[f"ask_{c}" for c in "ohlc"]] = 100.25
    t = lambda k: idx[k]  # noqa: E731
    sig = pd.DataFrame({"decision_time": [t(0), t(30)], "direction": [1, -1], "stop": [90.0, 110.0], "atr": 1.0,
                        "spread": 0.25, "setup": "x", "exit_by": [t(30), t(50)]})
    costs = CostModel(spread_mult=1.0, commission_rt=0.0, entry_slippage=0.0, stop_slippage=0.0, swap_per_rollover=0.0)
    plain = simulate(sig, m1, costs, ExitPolicy(rr=None, friday_flatten_utc=None))
    again = simulate(sig, m1, costs, ExitPolicy(rr=None, friday_flatten_utc=None, reenter_at_exit_bar=True))
    assert len(plain) == 1 and len(again) == 2
    assert again.iloc[1]["entry_time"] == again.iloc[0]["exit_time"] == t(30)


# --- the harness: all-in tier, same-sign and random-direction gates ----------


@pytest.fixture()
def mnq_cfg():
    c = load_config(MNQ_CONFIG)
    c["segments"] = {"dev": ["2019-01-01", "2021-01-01"], "validation": ["2021-01-01", "2021-07-01"], "holdout_start": "2021-07-01"}
    c["gates"].update(mc_sims=500, random_control_runs=5, random_direction_runs=5)
    return c


def _synthetic_mnq(start="2018-06-01", end="2021-07-01"):
    m1 = synthetic.random_walk_m1("USATECHIDXUSD", start, end, seed=3, start_price=7000.0, annual_vol=0.25, spread_pips=1.0)
    return quoted_at_spread(m1, 0.25)


def test_mnq_late_day_run_has_the_review_gates_and_the_all_in_tier(mnq_cfg, tmp_path):
    m1 = _synthetic_mnq()
    load = lambda sym, a, b: m1.loc[(m1.index >= a) & (m1.index < b)]  # noqa: E731
    reg = Registry(tmp_path / "exp.jsonl")
    res = run_t0("mnq_late_day_momentum_m30", mnq_cfg, load, reg)
    names = [g["gate"] for g in res["gates"]]
    assert "Expectancy at all-in Stress round trip (R)" in names
    assert "Worst OOS year's average R after costs" in names
    assert "Expectancy minus random-direction p95 (R)" in names
    assert len(res["gates"]) == 17 and not res["passed"]
    ex = res["extra"]
    assert ex["all_in_trades"] > 0 and ex["random_direction"]["runs"] == 5
    # The all-in tier charges 2.0 pt at the mid; base charges 0.75 + 1.5 ticks of spread + 2 ticks of slippage = 1.625.
    assert ex["all_in_expectancy_r"] < ex["gross_expectancy_r"]
    assert reg.entries("mnq_late_day_momentum_m30")[0]["extra"]["by_year"]


def test_warmup_days_load_earlier_data_but_count_no_trade_before_dev(mnq_cfg, tmp_path):
    m1 = _synthetic_mnq()
    seen = {}

    def load(sym, a, b):
        seen["start"] = a
        return m1.loc[(m1.index >= a) & (m1.index < b)]

    mnq_cfg["strategies"]["mnq_rsi2_pullback_d1"]["warmup_days"] = 180
    res = run_t0("mnq_rsi2_pullback_d1", mnq_cfg, load, Registry(tmp_path / "exp.jsonl"))
    assert seen["start"] == pd.Timestamp("2019-01-01", tz="UTC") - pd.Timedelta(days=180)
    assert any(g["gate"].startswith("Expectancy minus exposure-matched buy-and-hold") for g in res["gates"])
    assert all(y >= 2019 for y in res["extra"]["by_year"])


# --- the daily trend filter --------------------------------------------------


def test_trend_filter_always_on_matches_buy_and_hold_and_off_earns_nothing():
    days = [d.date() for d in pd.bdate_range("2023-01-02", periods=60) if trading_day([d.date()])[0]]
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
    d = pd.DataFrame({"open": close * 0.999, "close": close, "high": close * 1.01, "low": close * 0.99}, index=pd.Index(days, name="session"))
    period = (pd.Timestamp(days[5], tz="UTC"), pd.Timestamp(days[-1], tz="UTC") + pd.Timedelta(days=1))
    on = daily_trend.backtest(d, pd.Series(1.0, index=d.index), period, 2.0)
    assert np.allclose(on["strategy"].to_numpy()[1:-1], on["buy_and_hold"].to_numpy()[1:-1])
    assert on["round_trips"] == 1
    off = daily_trend.backtest(d, pd.Series(0.0, index=d.index), period, 2.0)
    assert (off["strategy"] == 0).all() and off["round_trips"] == 0
    tr = daily_trend.trades_r(d, pd.Series(1.0, index=d.index), period, 2.0, 3.0)
    assert len(tr) == 1 and tr.iloc[0]["exit"] == d["close"].iloc[-1]


def test_trend_signal_needs_both_the_average_and_the_momentum():
    c = pd.Series([10, 11, 12, 13, 9, 14], dtype=float)
    s = daily_trend.signal(pd.DataFrame({"close": c}), sma=3, momentum_sessions=2)
    assert s.isna().sum() == 2
    assert list(s.iloc[2:]) == [1.0, 1.0, 0.0, 1.0]


def test_rth_coverage_flags_thin_years():
    m1 = _synthetic_mnq("2019-01-01", "2021-01-01")
    thin = m1.loc[~((m1.index.year == 2020) & (m1.index.month <= 6))]
    cov = rth_coverage(thin, (2019, 2020), (2019, 2020), 0.7, 0.95)
    assert not cov.loc[2019, "flagged"] and cov.loc[2019, "rth_frac"] > 0.99
    assert cov.loc[2020, "flagged"]
