"""Futures-first: the contract model, tick sizing, the session calendar, expiry and roll, and the prop policy."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from atlas_engine.config import load_engine_config
from atlas_engine.exposure.netting import Exposure, currency_risk, legs
from atlas_engine.futures import (
    FuturesContract, SessionCalendar, check_against_broker, is_futures, no_trade_days, parse_contract, product, products,
)
from atlas_engine.prop_rules import StandardPropPolicy
from atlas_engine.prop_rules.policy import PolicyContext, PolicyTrade
from atlas_engine.risk.engine import OpenPosition
from atlas_engine.sizing.lots import contract, size_position

from futureshelp import FUTURES_CONFIG

UTC = dt.timezone.utc
CAL = SessionCalendar()


def chicago(y, m, d, hh, mm=0):
    from zoneinfo import ZoneInfo

    return dt.datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/Chicago")).astimezone(UTC)


# ---------------------------------------------------------------- the contract model

@pytest.mark.parametrize("root,tick,tick_value,mini", [
    ("ES", 0.25, 12.50, None), ("MES", 0.25, 1.25, "ES"), ("NQ", 0.25, 5.00, None), ("MNQ", 0.25, 0.50, "NQ"),
    ("YM", 1.0, 5.00, None), ("MYM", 1.0, 0.50, "YM"), ("MGC", 0.10, 1.00, "GC"),
])
def test_catalogue_matches_the_firms_published_tick_values(root, tick, tick_value, mini):
    p = product(root)
    assert (p.tick_size, p.tick_value, p.mini) == (tick, tick_value, mini) and p.verified


def test_every_brief_market_is_catalogued_with_its_mini_and_micro():
    for root in ("ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K", "CL", "MCL", "GC", "MGC"):
        assert is_futures(root)
    for p in products().values():
        if p.mini:  # a micro is a tenth of its mini, in the same asset group
            m = products()[p.mini]
            assert m.group == p.group and math.isclose(m.point_value, 10 * p.point_value)
            assert p.mini_equivalent == 0.1 and m.mini_equivalent == 1.0


def test_research_symbol_is_the_product_and_execution_symbol_the_contract():
    assert product("MESZ6").root == "MES" and product("MES").research_symbol == "MES"
    c = FuturesContract("MESZ6", "MES", 12, 2026, dt.datetime(2026, 12, 18, 14, 30, tzinfo=UTC))
    assert (c.symbol, c.research_symbol) == ("MESZ6", "MES")
    assert not is_futures("EURUSD") and not is_futures("MESA6")  # A is not a month code


def test_contract_codes_parse_and_unlisted_months_are_refused():
    assert parse_contract("MESZ6", dt.date(2026, 10, 5)) == ("MES", 12, 2026)
    assert parse_contract("MESH7", dt.date(2026, 10, 5)) == ("MES", 3, 2027)
    assert parse_contract("CLF7", dt.date(2026, 10, 5)) == ("CL", 1, 2027)
    with pytest.raises(ValueError, match="does not list month"):
        parse_contract("MESF7", dt.date(2026, 10, 5))  # equity index trades H M U Z only
    with pytest.raises(ValueError, match="product, not a contract"):
        parse_contract("MES", dt.date(2026, 10, 5))


def test_a_broker_that_disagrees_with_the_catalogue_is_reported():
    assert check_against_broker(product("MES"), 0.25, 5.0) is None
    assert "catalogue says" in check_against_broker(product("MES"), 0.25, 50.0)


# ---------------------------------------------------------------- expiry and roll

LAST = dt.datetime(2026, 12, 18, 14, 30, tzinfo=UTC)


@pytest.mark.parametrize("now,first_notice,expected", [
    (dt.datetime(2026, 10, 6, tzinfo=UTC), None, None),
    (dt.datetime(2026, 12, 12, tzinfo=UTC), None, None),
    (dt.datetime(2026, 12, 13, tzinfo=UTC), None, "contract_roll_due"),  # 5 days before the last trade
    (LAST, None, "contract_expired"),
    (dt.datetime(2026, 12, 1, tzinfo=UTC), dt.date(2026, 12, 4), "contract_roll_due"),  # first notice comes first
])
def test_expired_or_rolling_contracts_take_no_new_entries(now, first_notice, expected):
    c = FuturesContract("MESZ6", "MES", 12, 2026, LAST, first_notice)
    assert c.entry_block(now, roll_days=5) == expected


def test_an_unknown_expiry_is_refused_not_guessed():
    assert FuturesContract("MESZ6", "MES", 12, 2026, None).entry_block(LAST, 5) == "contract_expiry_unknown"


# ---------------------------------------------------------------- sizing in contracts

def test_contracts_are_floor_of_allowed_risk_over_risk_per_contract():
    spec = contract("MES")
    assert (spec.volume_min, spec.volume_step, spec.asset_class) == (1.0, 1.0, "future")
    # 0.25% of 50,000 = $125. A 10-point stop is 40 ticks x $1.25 = $50 per contract -> 2 contracts.
    r = size_position(50_000, 0.25, 1.0, 10.0, spec)
    assert r.ok and r.volume == 2 and r.risk_amount == pytest.approx(100.0)
    # With $2 round-turn commission the per-contract risk is $52: still 2. At $13 it is $63: 1.
    assert size_position(50_000, 0.25, 1.0, 10.0, product("MES").spec(commission_per_contract=13.0)).volume == 1


def test_a_stop_too_wide_for_one_contract_is_refused():
    # ES: 10 points = $500 per contract, far over a $125 budget even with the 10% overshoot allowance.
    r = size_position(50_000, 0.25, 1.0, 10.0, contract("ES"))
    assert not r.ok and r.volume == 0


def test_futures_exposure_is_one_leg_per_asset_group():
    assert currency_risk([Exposure("MES", 1, 0.25), Exposure("NQ", 1, 0.25)]) == {"us_equity_index": 0.5}
    assert currency_risk([Exposure("MES", 1, 0.25), Exposure("MGC", -1, 0.25)]) == {"us_equity_index": 0.25,
                                                                                   "metals": -0.25}
    assert set(legs(Exposure("EURUSD", 1, 0.25))) == {"EUR", "USD"}


# ---------------------------------------------------------------- the session calendar

@pytest.mark.parametrize("when,phase,open_", [
    (chicago(2026, 10, 6, 9, 0), "rth", True),
    (chicago(2026, 10, 6, 15, 30), "eth", True),
    (chicago(2026, 10, 6, 16, 30), "maintenance", False),
    (chicago(2026, 10, 6, 17, 0), "eth", True),  # next trading day's session
    (chicago(2026, 10, 9, 16, 0), "weekend", False),  # Friday close
    (chicago(2026, 10, 10, 12, 0), "weekend", False),
    (chicago(2026, 10, 11, 16, 59), "weekend", False),
    (chicago(2026, 10, 11, 17, 0), "eth", True),  # Sunday reopen
])
def test_session_phases_on_the_exchange_clock(when, phase, open_):
    st = CAL.status("cme_globex", ("08:30", "15:00"), when)
    assert (st.phase, st.open) == (phase, open_)


def test_the_17_00_session_belongs_to_the_next_trading_day():
    st = CAL.status("cme_globex", ("08:30", "15:00"), chicago(2026, 10, 6, 18, 0))
    assert st.trading_day == dt.date(2026, 10, 7) and st.closes_at == chicago(2026, 10, 7, 16, 0)


def test_daylight_saving_moves_the_utc_hours_not_the_exchange_hours():
    summer = CAL.status("cme_globex", ("08:30", "15:00"), dt.datetime(2026, 10, 6, 21, 30, tzinfo=UTC))  # 16:30 CDT
    winter = CAL.status("cme_globex", ("08:30", "15:00"), dt.datetime(2026, 11, 10, 21, 30, tzinfo=UTC))  # 15:30 CST
    assert summer.phase == "maintenance" and winter.phase == "eth" and winter.open


def test_holidays_and_early_close_days_are_no_trade_days():
    days = no_trade_days(2026)
    for d in (dt.date(2026, 1, 1), dt.date(2026, 1, 19), dt.date(2026, 2, 16), dt.date(2026, 4, 3),
              dt.date(2026, 5, 25), dt.date(2026, 6, 19), dt.date(2026, 7, 3), dt.date(2026, 9, 7),
              dt.date(2026, 11, 26), dt.date(2026, 11, 27), dt.date(2026, 12, 24), dt.date(2026, 12, 25),
              dt.date(2026, 12, 31)):
        assert d in days, d
    st = CAL.status("cme_globex", ("08:30", "15:00"), chicago(2026, 11, 26, 10, 0))
    assert not st.open and st.reason == "exchange_holiday"
    extra = SessionCalendar(frozenset({dt.date(2026, 10, 6)}))
    assert not extra.status("cme_globex", ("08:30", "15:00"), chicago(2026, 10, 6, 9, 0)).open


def test_naive_timestamps_are_refused():
    with pytest.raises(ValueError):
        CAL.status("cme_globex", ("08:30", "15:00"), dt.datetime(2026, 10, 6, 9, 0))


# ---------------------------------------------------------------- the prop policy (FundedNext Flex as data)

@pytest.fixture(scope="module")
def flex():
    cfg = load_engine_config(FUTURES_CONFIG)
    return cfg.prop, StandardPropPolicy(cfg.prop)


def ctx(now, positions=(), day_profit=0.0, mode="evaluation", phase="challenge"):
    return PolicyContext(now, mode, phase, 50_000.0, day_profit, tuple(positions))


def pos(symbol, direction, volume):
    return OpenPosition("1", symbol, "s", direction, volume, 5000.0, 4990.0, 50.0)


def trade(symbol="MES", direction=1, stop=4990.0, target=5020.0):
    return PolicyTrade(symbol, direction, 5000.0, stop, target, 0.25)


def test_flex_50k_numbers_load_as_data(flex):
    rules, _ = flex
    assert rules.daily_loss_pct is None and rules.max_loss_amount(50_000) == 1_500
    assert rules.max_loss_reference(50_000, 60_000) - 1_500 == 50_100  # the MLL locks at initial + $100
    assert rules.futures.max_minis == 3 and rules.futures.micros_per_mini == 10


def test_flat_by_time_blocks_entries_and_asks_for_a_flatten(flex):
    _, p = flex
    assert p.entry_reasons(trade(), ctx(chicago(2026, 10, 6, 14, 30))) == []
    assert "prop_outside_trading_hours" in p.entry_reasons(trade(), ctx(chicago(2026, 10, 6, 14, 45)))
    assert p.flatten_due(chicago(2026, 10, 6, 14, 55)) is None
    assert p.flatten_due(chicago(2026, 10, 6, 15, 0)) == "prop_flat_by"
    assert p.flatten_due(chicago(2026, 10, 6, 17, 0)) is None  # reopened


def test_contract_limit_counts_micros_as_a_tenth_of_a_mini(flex):
    _, p = flex
    now = chicago(2026, 10, 6, 9, 0)
    assert p.max_contracts("MES", ctx(now)) == 30 and p.max_contracts("ES", ctx(now)) == 3
    assert p.max_contracts("ES", ctx(now, [pos("MNQ", 1, 15)])) == 1  # 1.5 minis used
    assert p.max_contracts("MES", ctx(now, [pos("ES", 1, 3)])) == 0


def test_opposite_positions_in_one_asset_group_are_a_prohibited_hedge(flex):
    _, p = flex
    now = chicago(2026, 10, 6, 9, 0)
    assert "prop_correlated_hedge" in p.entry_reasons(trade("MES", -1, 5010.0, 4980.0), ctx(now, [pos("NQ", 1, 1)]))
    assert "prop_correlated_hedge" not in p.entry_reasons(trade("MES", 1), ctx(now, [pos("NQ", 1, 1)]))
    assert "prop_correlated_hedge" not in p.entry_reasons(trade("MGC", -1, 5010.0, 4980.0), ctx(now, [pos("NQ", 1, 1)]))


def test_brackets_inside_the_scalping_band_are_refused(flex):
    _, p = flex
    now = chicago(2026, 10, 6, 9, 0)
    assert "prop_bracket_too_tight" in p.entry_reasons(trade(stop=4999.0), ctx(now))  # 4 ticks
    assert "prop_bracket_too_tight" not in p.entry_reasons(trade(stop=4998.5), ctx(now))  # 6 ticks


def test_consistency_rule_caps_one_days_profit_in_the_challenge_only(flex):
    _, p = flex
    now = chicago(2026, 10, 6, 9, 0)
    # 40% of the $2,500 target is $1,000 a day.
    assert p.sized_reasons(trade(), 2, 300.0, ctx(now, day_profit=600.0)) == []
    assert p.sized_reasons(trade(), 2, 500.0, ctx(now, day_profit=600.0)) == ["prop_consistency_cap"]
    assert p.sized_reasons(trade(), 2, 500.0, ctx(now, day_profit=600.0, mode="funded", phase="funded")) == []


def test_a_forex_firm_without_futures_rules_keeps_its_old_behaviour():
    from atlas_engine.config import load_engine_config as load
    from t4help import REPO_CONFIG

    p = StandardPropPolicy(load(REPO_CONFIG).prop)
    now = dt.datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    t = PolicyTrade("EURUSD", 1, 1.1, 1.099, 1.102, 0.00001)
    assert p.entry_reasons(t, ctx(now)) == [] and p.max_contracts("EURUSD", ctx(now)) is None
    assert p.flatten_due(now) is None
