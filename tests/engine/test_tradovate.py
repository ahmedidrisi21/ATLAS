"""The futures execution adapter over Tradovate (on the stand-in): brackets, idempotency, protection, closing,
reconciliation, rate limits and credentials."""

from __future__ import annotations

import datetime as dt

import pytest

from atlas_engine.adapters.futures_venue import ContractMismatch, foreign_ticket
from atlas_engine.adapters.tradovate import (
    Credentials, FakeTradovate, QuoteBook, RateLimited, TradovateClient, TradovateError, parse_frame,
)
from atlas_engine.execution import EntryOrder, ExecutionSettings, FuturesExecutionAdapter
from atlas_engine.positions.book import ManagedPosition, PositionBook

from futureshelp import CREDS, F0, MESZ6_LAST_TRADE, venue
from t4help import Clock

SETTINGS = ExecutionSettings(platform="tradovate", contracts={"MES": "MESZ6"})


@pytest.fixture
def t(tmp_path):
    clock = Clock(F0)
    v, fake = venue(clock, ledger=tmp_path / "brackets.json")
    v.connect()

    class T:
        pass

    x = T()
    x.clock, x.venue, x.fake, x.path = clock, v, fake, tmp_path / "brackets.json"
    x.ex = FuturesExecutionAdapter(v, SETTINGS, pause=lambda s: None)

    def quote(bid, ask=None, code="MESZ6"):
        ask = bid + 0.25 if ask is None else ask
        fake.set_quote(code, bid, ask)
        v.quotes.apply(fake.quote_message(code, bid, ask))
        v.refresh()

    x.quote = quote
    quote(5000.0)
    return x


def order(decision="d1", direction=1, volume=2.0, stop=4990.0, target=5020.0, expected=5000.25):
    return EntryOrder(decision, "MES", "trend_pullback", direction, volume, stop, target, 26_090_001, expected)


# ---------------------------------------------------------------- entries

def test_an_entry_goes_out_as_one_automated_bracket(t):
    r = t.ex.submit_order(order(), F0)
    assert r.status == "filled" and (r.volume, r.price, r.sl, r.tp) == (2.0, 5000.25, 4990.0, 5020.0)
    assert [c for c in t.fake.calls if c[0] == "POST" and c[1].startswith("/order")] == [("POST", "/order/placeoso")]
    legs = {o["role"]: o for o in t.ex.get_orders()}
    assert legs["stop"]["type"] == "STOP" and legs["stop"]["side"] == "SELL" and legs["target"]["type"] == "LIMIT"
    (p,) = t.ex.get_positions()
    assert (p.symbol, p.direction, p.volume, p.comment) == ("MES", 1, 2.0, order().client_id)


def test_off_grid_prices_are_snapped_toward_the_entry_so_risk_only_shrinks(t):
    r = t.ex.submit_order(order(stop=4990.1, target=5020.3), F0)
    assert (r.sl, r.tp) == (4990.25, 5020.25)


def test_the_same_decision_never_sends_twice(t):
    first = t.ex.submit_order(order(), F0)
    again = t.ex.submit_order(order(), F0)
    assert again.status == "duplicate" and again.ticket == first.ticket
    assert sum(1 for c in t.fake.calls if c[1] == "/order/placeoso") == 1


def test_one_position_per_contract(t):
    assert t.ex.submit_order(order(), F0).opened
    r = t.ex.submit_order(order("d2"), F0)
    assert r.status == "rejected" and "contract_has_position" in r.reason


def test_a_contract_the_operator_holds_is_not_traded(t):
    t.fake.open_foreign("MESZ6", 1, 4995.0)
    r = t.ex.submit_order(order(), F0)
    assert r.status == "rejected" and "contract_has_position" in r.reason


@pytest.mark.parametrize("now,reason", [
    (MESZ6_LAST_TRADE - dt.timedelta(days=3), "contract_roll_due"),
    (MESZ6_LAST_TRADE + dt.timedelta(minutes=1), "contract_expired"),
])
def test_expired_or_rolling_contracts_are_refused_at_execution_too(t, now, reason):
    r = t.ex.submit_order(order(), now)
    assert r.status == "rejected" and reason in r.reason
    assert not any(c[1] == "/order/placeoso" for c in t.fake.calls)


def test_price_moved_past_the_deviation_is_refused(t):
    t.quote(5003.0)
    r = t.ex.submit_order(order(), F0)
    assert r.status == "rejected" and "price_moved_past_deviation" in r.reason


def test_a_refused_bracket_is_reported_and_recorded_as_failed(t):
    t.fake.reject_next = "Insufficient margin"
    r = t.ex.submit_order(order(), F0)
    assert r.status == "rejected" and "Insufficient margin" in r.reason
    assert t.venue.ledger.get(order().client_id).status == "failed"


def test_a_lost_answer_is_found_on_the_platform_not_resent(t):
    t.fake.lose_answer_next = True
    r = t.ex.submit_order(order(), F0)
    assert r.status == "filled" and r.sl == 4990.0
    assert sum(1 for c in t.fake.calls if c[1] == "/order/placeoso") == 1


def test_a_command_that_never_arrived_is_marked_failed_and_raises_unknown(t):
    from atlas_engine.adapters.broker import BrokerUnavailable

    t.fake.drop_next_before_send = True
    with pytest.raises(BrokerUnavailable):
        t.ex.submit_order(order(), F0)
    assert t.venue.ledger.get(order().client_id).status == "failed" and t.ex.get_positions() == []


def test_an_unfilled_market_entry_is_cancelled_with_its_legs(t):
    t.fake.fill_market = False
    r = t.ex.submit_order(order(), F0)
    assert r.status == "rejected" and "did not fill" in r.reason
    assert t.ex.get_orders() == [] and t.ex.get_positions() == []


def test_a_fill_whose_stop_vanished_gets_a_new_stop(t):
    t.fake.drop_stop_after_fill = True
    r = t.ex.submit_order(order(), F0)
    assert r.status == "filled" and "stop re-placed" in r.reason and r.sl == 4990.0
    assert any(o["role"] == "stop" for o in t.ex.get_orders())


def test_a_fill_left_unprotected_is_closed(t):
    t.fake.drop_stop_after_fill = True
    real = t.venue.place_order

    def refuse_stops(contract, side, qty, type, client_id, price=None, stop_price=None):
        if type == "STOP":
            from atlas_engine.adapters.futures_venue import VenueAck
            return VenueAck(False, reason="refused")
        return real(contract, side, qty, type, client_id, price, stop_price)

    t.venue.place_order = refuse_stops
    r = t.ex.submit_order(order(), F0)
    assert r.status == "unprotected_closed" and t.ex.get_positions() == []


# ---------------------------------------------------------------- managing and closing

def test_stops_only_tighten_and_move_the_working_stop_order(t):
    t.ex.submit_order(order(), F0)
    (p,) = t.ex.get_positions()
    assert t.ex.modify_position(p, 4985.0).reason == "stops only tighten"
    assert t.ex.modify_position(p, 4995.0).status == "filled"
    assert t.ex.get_positions()[0].sl == 4995.0
    stop = next(o for o in t.ex.get_orders() if o["role"] == "stop")
    assert stop["stop_price"] == 4995.0


def test_a_stop_cannot_move_through_a_pending_order_route(t):
    t.ex.submit_order(order(), F0)
    stop = next(o for o in t.ex.get_orders() if o["role"] == "stop")
    assert t.ex.modify_order(stop["ticket"], stop_price=4980.0).status == "rejected"
    assert t.ex.cancel_order(stop["ticket"]).status == "rejected"  # it protects an open position


def test_close_cancels_the_bracket_then_exits_at_market(t):
    t.ex.submit_order(order(), F0)
    (p,) = t.ex.get_positions()
    t.quote(5010.0)
    r = t.ex.close_position(p, "operator")
    assert r.status == "closed" and r.price == 5010.0
    assert t.ex.get_positions() == [] and t.ex.get_orders() == []
    deals = [(d.entry, d.reason, d.profit) for d in t.venue.recent_deals(F0)]
    assert deals == [("in", "expert", 0.0), ("out", "expert", pytest.approx(97.5))]  # 2 x 9.75 pts x $5


def test_a_stop_fill_is_a_closing_deal_with_the_right_pnl(t):
    t.ex.submit_order(order(), F0)
    t.quote(4989.75)  # through the stop
    assert t.ex.get_positions() == [] and t.ex.get_orders() == []  # the target was cancelled (OCO)
    out = [d for d in t.venue.recent_deals(F0) if d.entry == "out"]
    assert out[0].reason == "sl" and out[0].profit == pytest.approx(2 * (4989.75 - 5000.25) * 5)


def test_flatten_closes_atlas_positions_and_leaves_the_operators(t):
    t.ex.submit_order(order(), F0)
    t.fake.open_foreign("MESZ6", 1, 4995.0)
    positions = t.ex.get_positions()
    foreign = [p for p in positions if not t.ex.owns(p)]
    assert len(foreign) == 1 and foreign[0].ticket == foreign_ticket("MESZ6") and foreign[0].volume == 1
    assert t.ex.close_position(foreign[0], "kill").status == "rejected"
    results = t.ex.flatten([p for p in positions if t.ex.owns(p)], "kill")
    assert [r.status for r in results] == ["closed"]
    left = t.ex.get_positions()
    assert len(left) == 1 and left[0].ticket == foreign_ticket("MESZ6")


def test_reconcile_restores_a_missing_stop_and_housekeeping_clears_stale_legs(t):
    t.ex.submit_order(order(), F0)
    (p,) = t.ex.get_positions()
    stop = next(o for o in t.ex.get_orders() if o["role"] == "stop")
    t.fake.orders[stop["ticket"]]["ordStatus"] = "Canceled"  # someone cancelled it at the platform
    t.venue.refresh()
    book = PositionBook()
    book.add(ManagedPosition(p.ticket, p.comment, "d1", "MES", "trend_pullback", 1, 2.0, p.price_open, 4990.0, 5020.0,
                             p.magic, F0.isoformat()))
    kinds = [f.kind for f in t.ex.reconcile(book, {"MES": 0.25})]
    assert kinds == ["stop_missing"]
    assert t.ex.modify_position(t.ex.get_positions()[0], 4990.0, 5020.0).status == "filled"
    assert t.ex.reconcile(book, {"MES": 0.25}) == []
    # The re-placed stop is not linked to the target: once the target fills, the stop must not stay working.
    t.quote(5020.0)
    assert t.ex.get_positions() == []
    assert any(o["role"] == "stop" for o in t.ex.get_orders())
    assert [r.status for r in t.ex.housekeeping(F0)] == ["cancelled"] and t.ex.get_orders() == []


def test_the_ledger_survives_a_restart(t):
    t.ex.submit_order(order(), F0)
    v2, _ = venue(t.clock, fake=t.fake, ledger=t.path)
    v2.connect()
    v2.quotes = t.venue.quotes
    (p,) = v2.positions()
    assert p.comment == order().client_id and p.sl == 4990.0


# ---------------------------------------------------------------- the platform

def test_connect_refuses_a_contract_whose_numbers_differ_from_the_catalogue(tmp_path):
    clock = Clock(F0)
    fake = FakeTradovate({"MESZ6": MESZ6_LAST_TRADE}, now=clock)
    fake.products[next(iter(fake.products))]["valuePerPoint"] = 50.0
    v, _ = venue(clock, fake=fake)
    with pytest.raises(ContractMismatch):
        v.connect()


def test_connect_needs_exactly_one_account(tmp_path):
    clock = Clock(F0)
    v, _ = venue(clock, fake=FakeTradovate({"MESZ6": MESZ6_LAST_TRADE}, now=clock, other_accounts=1))
    with pytest.raises(TradovateError, match="exactly one"):
        v.connect()


def test_account_equity_marks_open_positions_at_the_live_quote(t):
    t.ex.submit_order(order(), F0)
    t.quote(4995.0)
    a = t.venue.account()
    assert a.demo and a.trade_allowed and a.balance == 50_000
    assert a.equity == pytest.approx(50_000 + 2 * (4995.0 - 5000.25) * 5)


def test_contract_dates_come_from_the_platform(t):
    c = t.venue.symbol_rules("MES").contract
    assert c.symbol == "MESZ6" and c.last_trade == MESZ6_LAST_TRADE
    assert t.venue.symbol_rules("MES").spec.volume_step == 1.0


def test_penalty_tickets_wait_then_resend_once(t):
    waits = []
    t.venue.client.sleep = waits.append
    t.fake.penalty_next = 3.0
    t.venue.refresh()
    t.venue.positions()
    assert waits == [3.0]
    t.fake.penalty_next = 600.0
    t.venue.refresh()
    with pytest.raises(RateLimited):
        t.venue.positions()


def test_tokens_are_renewed_before_they_expire_and_reacquired_after_a_401(t):
    before = t.venue.client.token
    t.clock.advance(minutes=70)
    t.venue.refresh()
    t.venue.positions()
    assert ("GET", "/auth/renewaccesstoken") in t.fake.calls and t.venue.client.token != before
    t.fake.expire_token = True
    t.venue.refresh()
    t.venue.positions()
    assert t.fake.calls.count(("POST", "/auth/accesstokenrequest")) == 2


def test_a_wrong_password_is_reported_without_echoing_secrets():
    fake = FakeTradovate({})
    c = TradovateClient("demo", Credentials("atlas", "hunter2", "a", "1", "0", "s3cr3t", "d"), transport=fake)
    with pytest.raises(TradovateError) as e:
        c.authenticate()
    assert "hunter2" not in str(e.value) and "s3cr3t" not in str(e.value)
    assert "hunter2" not in repr(c._creds) and "s3cr3t" not in repr(c._creds)


def test_credentials_come_from_the_environment_only():
    with pytest.raises(ValueError, match="ATLAS_TRADOVATE_PASSWORD"):
        Credentials.from_env({"ATLAS_TRADOVATE_USER": "x"})
    env = {v: "x" for v in ("ATLAS_TRADOVATE_USER", "ATLAS_TRADOVATE_PASSWORD", "ATLAS_TRADOVATE_APP_ID",
                            "ATLAS_TRADOVATE_APP_VERSION", "ATLAS_TRADOVATE_CID", "ATLAS_TRADOVATE_SEC",
                            "ATLAS_TRADOVATE_DEVICE_ID")}
    assert Credentials.from_env(env).name == "x"
    assert TradovateClient("live", CREDS).demo is False and TradovateClient("demo", CREDS).demo is True


def test_quote_frames_become_ticks():
    book = QuoteBook({42: "MESZ6"})
    frame = ('a[{"e":"md","d":{"quotes":[{"timestamp":"2026-10-06T14:00:00.5Z","contractId":42,'
             '"entries":{"Bid":{"price":5000.0,"size":3},"Offer":{"price":5000.25,"size":4},'
             '"Trade":{"price":5000.25,"size":1},"TotalTradeVolume":{"size":123456}}}]}}]')
    for m in parse_frame(frame):
        book.apply(m)
    tk = book.tick("MESZ6")
    assert (tk.bid, tk.ask, tk.last, tk.volume, tk.spread) == (5000.0, 5000.25, 5000.25, 123456, 0.25)
    assert parse_frame("h") == [] and parse_frame("o") == []
    with pytest.raises(Exception):
        book.tick("MNQZ6")
