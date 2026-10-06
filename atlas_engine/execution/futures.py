"""The futures execution adapter (futures-first, docs/futures.md; PRD v3 §14, §15).

``FuturesExecutionAdapter`` is the ``ExecutionBroker`` for futures platforms. It
holds the execution rules that are the same on every futures platform and talks
to the platform only through a ``FuturesVenue`` (generic orders: side, MARKET /
LIMIT / STOP / STOP_LIMIT, quantity, prices). The layering is

    TradingEngine -> ExecutionBroker -> FuturesExecutionAdapter -> NinjaTraderAdapter (FuturesVenue) -> platform

Every order it sends is a broker-neutral ``OrderRequest`` (``atlas_engine.adapters.orders``).

and nothing above the venue sees a platform type. The engine calls it only
after the decision pipeline returned ALLOW, or to close or flatten.

Rules, on top of the shared entry checks in ``Executor.validate_entry``:

- Whole contracts, prices on the tick grid. Stop and target are snapped toward
  the entry, so snapping can only shrink the risk the engine sized for.
- One ATLAS position per contract, and none while the operator holds the
  contract: a futures account nets, so a second bracket would merge with the first.
- No entry in a contract that has expired, is due to roll, or whose expiry the
  platform did not report.
- Every entry goes out as one bracket: a market entry with its stop and target
  attached (one cancels the other). The bracket is recorded before it is sent,
  keyed by the client order ID, so the same decision never sends twice.
- After the fill the bracket is verified at the platform: a working stop and
  target, each for exactly the filled quantity, at the recorded prices, linked
  one-cancels-other. Anything short of that is repaired at once (a fresh linked
  pair is placed before the old legs are cancelled, so protection never
  lapses); if the repair does not verify, the position is closed and the result
  is ``unprotected_closed``, which the engine follows with an immediate
  reconciliation. ATLAS never silently holds an unprotected position, and
  ``findings`` reports any that reconciliation sees, so the engine HALTs.
- Every step is recorded as a lifecycle event (``drain_events``) that the
  engine journals with the decision it serves.
- Stops only tighten. Closing cancels the bracket's stop and target first, then
  exits at market. Orders and positions ATLAS did not open are never touched.
"""

from __future__ import annotations

import math
import time

from atlas_engine.adapters.broker import AccountInfo, BrokerPosition, BrokerUnavailable
from atlas_engine.adapters.futures_venue import Bracket
from atlas_engine.adapters.orders import closing_order, entry_bracket
from atlas_engine.reconciliation import diff

from .executor import EntryOrder, ExecResult, Executor
from .settings import ExecutionSettings

FILL_CHECKS = 8  # a market order normally fills in well under a second; look this many times
FILL_PAUSE_S = 0.25


def snap_toward(price: float, anchor: float, tick: float) -> float:
    """``price`` on the tick grid, rounded toward ``anchor`` (never further from it)."""
    steps = (price - anchor) / tick
    steps = math.floor(steps + 1e-9) if steps > 0 else math.ceil(steps - 1e-9)
    return round(anchor + steps * tick, 10)


def on_grid(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)


class FuturesExecutionAdapter:
    platform = "futures"

    def __init__(self, venue, settings: ExecutionSettings, pause=time.sleep):
        self.venue, self.settings, self.pause = venue, settings, pause
        self.platform = venue.platform
        self.checks = Executor(venue, settings)  # its validate_entry only; it sends nothing here
        self.events: list[dict] = []

    def _emit(self, event: str, client_id: str, **detail) -> None:
        self.events.append({"event": event, "client_id": client_id, **detail})

    def drain_events(self) -> list[dict]:
        """Lifecycle events since the last drain, oldest first (the engine journals them)."""
        out, self.events = self.events, []
        return out

    # -- helpers ----------------------------------------------------------------------------------------

    def _bracket(self, pos: BrokerPosition) -> Bracket | None:
        return self.venue.ledger.by_entry(pos.ticket) if pos.ticket > 0 else None

    def _await(self, ticket: int, want_open: bool) -> BrokerPosition | None:
        pos = None
        for i in range(FILL_CHECKS):
            self.venue.refresh()
            pos = self.venue.position(ticket)
            if (pos is not None) == want_open:
                return pos
            if i + 1 < FILL_CHECKS:
                self.pause(FILL_PAUSE_S)
        return pos

    def _working(self, order_id: int | None) -> bool:
        return order_id is not None and any(o["ticket"] == order_id for o in self.venue.pending_orders())

    def owns(self, pos: BrokerPosition) -> bool:
        return self._bracket(pos) is not None

    # -- entries ----------------------------------------------------------------------------------------

    def submit_order(self, order: EntryOrder, now) -> ExecResult:
        cid, v = order.client_id, self.venue
        seen = v.ledger.get(cid)
        if seen is not None:
            pos = v.position(seen.entry_id) if seen.entry_id else None
            if pos is not None:
                return ExecResult("duplicate", cid, "client order ID already open", pos.ticket, pos.volume,
                                  pos.price_open, sl=pos.sl, tp=pos.tp)
            return ExecResult("duplicate", cid, f"this decision was already sent ({seen.status})")
        rules = v.symbol_rules(order.symbol)
        tick = v.tick(order.symbol)
        t = rules.point
        problems = self.checks.validate_entry(order, rules, tick)
        if rules.contract is None:
            problems.append("contract_unknown")
        else:
            block = rules.contract.entry_block(now, self.settings.roll_days)
            if block:
                problems.append(block)
        code = v.contract_of(order.symbol)
        if any(b.contract == code for b in v.ledger.active()) or abs(v.net_position(order.symbol)) > 1e-9:
            problems.append("contract_has_position")
        price = tick.price(order.direction)
        if abs(price - order.expected_price) > self.settings.max_deviation_points * t + 1e-9:
            problems.append("price_moved_past_deviation")
        stop, target = snap_toward(order.stop, price, t), snap_toward(order.target, price, t)
        if not problems and (order.direction * (price - stop) <= 0 or order.direction * (target - price) <= 0):
            problems.append("bracket_collapses_on_tick_grid")
        req = entry_bracket(code, order.direction, order.volume, stop, target, cid, strategy_id=order.setup,
                            trade_intent_id=order.decision_id)
        problems += [f"invalid_order:{p}" for p in req.problems()]
        if problems:
            return ExecResult("rejected", cid, ",".join(problems))

        b = Bracket(cid, order.symbol, code, order.direction, order.volume, order.magic, stop, target, now.isoformat())
        v.ledger.put(b)  # before sending: a crash after this still knows the order is ATLAS'
        self._emit("order_submitted", cid, request=req.to_dict(), platform=self.platform)
        ack = v.submit(req)
        v.refresh()
        if ack.unknown:
            found = v.adopt(b)
            if not found:
                self._emit("order_outcome_unknown", cid, detail=ack.reason)
                raise BrokerUnavailable(f"bracket outcome unknown: {ack.reason}")
            self._emit("order_acknowledged", cid, adopted=True, entry_id=b.entry_id, stop_id=b.stop_id,
                       target_id=b.target_id)
        elif not ack.ok:
            b.status = "failed"
            v.ledger.save()
            self._emit("order_rejected", cid, reason=ack.reason)
            return ExecResult("rejected", cid, f"platform refused the bracket: {ack.reason}", attempts=1)
        else:
            b.entry_id, b.stop_id, b.target_id = ack.ids.get("entry"), ack.ids.get("stop"), ack.ids.get("target")
            v.ledger.save()
            self._emit("order_acknowledged", cid, entry_id=b.entry_id, stop_id=b.stop_id, target_id=b.target_id)

        pos = self._await(b.entry_id, want_open=True)
        if pos is None:
            for oid in b.order_ids():
                if self._working(oid):
                    v.cancel(oid)
            v.refresh()
            pos = v.position(b.entry_id)  # it may have filled while cancelling
            if pos is None:
                self._emit("order_cancelled", cid, reason="market entry did not fill")
                return ExecResult("rejected", cid, "market entry did not fill; bracket cancelled", attempts=1)
        self._emit("fill_received", cid, ticket=pos.ticket, volume=pos.volume, price=pos.price_open)
        if pos.volume + 1e-9 < order.volume:  # partial: cancel the rest, size the stop and target to the fill
            if self._working(b.entry_id):
                v.cancel(b.entry_id)
            for oid, kind, px in ((b.stop_id, "STOP", b.stop), (b.target_id, "LIMIT", b.target)):
                if self._working(oid):
                    v.modify(oid, pos.volume, kind, price=px if kind == "LIMIT" else None,
                             stop_price=px if kind == "STOP" else None)
            v.refresh()
            pos = v.position(b.entry_id) or pos
        reason = ""
        missing = v.protection_problems(b)
        if missing:
            self._emit("protection_unverified", cid, problems=missing)
            self._protect(pos, b)
            v.refresh()
            still = v.protection_problems(b)
            if still:
                self._emit("protection_failed", cid, problems=still)
                closed = self.close_position(pos, "protection_unverified")
                return ExecResult("unprotected_closed", cid,
                                  f"protection could not be verified ({','.join(still)}); close: {closed.status}",
                                  ticket=pos.ticket, volume=pos.volume, price=pos.price_open, attempts=1)
            pos = v.position(pos.ticket) or pos
            reason = "protection re-placed after fill: " + ",".join(missing)
        self._emit("stop_attached", cid, order_id=b.stop_id, price=b.stop, volume=pos.volume)
        self._emit("target_attached", cid, order_id=b.target_id, price=b.target, volume=pos.volume)
        self._emit("position_opened", cid, ticket=pos.ticket, symbol=order.symbol, contract=code,
                   direction=order.direction, volume=pos.volume, price=pos.price_open)
        return ExecResult("partial" if pos.volume + 1e-9 < order.volume else "filled", cid, reason, pos.ticket,
                          pos.volume, pos.price_open, price, round(order.direction * (pos.price_open - price) / t, 1),
                          tick.spread, pos.sl, pos.tp, attempts=1)

    # -- positions --------------------------------------------------------------------------------------

    def modify_position(self, pos: BrokerPosition, sl: float, tp: float | None = None) -> ExecResult:
        """``tp`` None: move the stop, tighter only. With ``tp``: restore the bracket's stop and target."""
        b, v = self._bracket(pos), self.venue
        if b is None:
            return ExecResult("rejected", pos.comment, "not an ATLAS position; left for the operator", ticket=pos.ticket)
        t = v.symbol_rules(pos.symbol).point
        price = v.tick(pos.symbol).price(pos.direction, closing=True)
        sl = on_grid(sl, t)
        if pos.direction * (price - sl) <= 0:
            return ExecResult("rejected", b.client_id, "stop is on the wrong side of the price", ticket=pos.ticket)
        if tp is None:
            if pos.sl and pos.direction * (sl - pos.sl) <= 0:
                return ExecResult("rejected", b.client_id, "stops only tighten", ticket=pos.ticket)
            ack = v.modify(b.stop_id, pos.volume, "STOP", stop_price=sl)
            if not ack.ok:
                return ExecResult("rejected", b.client_id, f"modify: {ack.reason}", ticket=pos.ticket, attempts=1)
            b.stop = sl
            v.ledger.save()
            v.refresh()
            return ExecResult("filled", b.client_id, "stop moved", ticket=pos.ticket, sl=sl, tp=pos.tp, attempts=1)
        tp = on_grid(tp, t)
        b.stop, b.target = sl, tp
        v.ledger.save()
        ok = self._protect(pos, b)
        return ExecResult("filled" if ok else "failed", b.client_id, "" if ok else "could not set the stop and target",
                          ticket=pos.ticket, sl=sl, tp=tp)

    def _protect(self, pos: BrokerPosition, b: Bracket) -> bool:
        """Make the bracket's stop and target what the ledger says, for the open quantity, linked OCO.

        Legs that are both working and linked are amended in place. Otherwise a fresh linked pair is placed
        first and the old legs cancelled after, so the position is never without a stop in between."""
        v = self.venue
        v.refresh()
        if not v.protection_problems(b):
            return True
        stop_ok, target_ok = self._working(b.stop_id), self._working(b.target_id)
        if stop_ok and target_ok and not any("oco" in p for p in v.protection_problems(b)):
            ok = v.modify(b.stop_id, pos.volume, "STOP", stop_price=b.stop).ok
            ok = v.modify(b.target_id, pos.volume, "LIMIT", price=b.target).ok and ok
            v.refresh()
            return ok and not v.protection_problems(b)
        b.replaced += 1
        n = b.replaced + 1
        ack = v.submit_oco(closing_order(b.contract, b.direction, pos.volume, "STOP", f"{b.client_id}-S{n}", b.stop),
                           closing_order(b.contract, b.direction, pos.volume, "LIMIT", f"{b.client_id}-T{n}", b.target))
        if not ack.ok:
            v.ledger.save()
            self._emit("protection_order_refused", b.client_id, reason=ack.reason)
            return False
        old = [i for i in (b.stop_id, b.target_id) if i is not None]
        b.exit_ids.extend(old)  # their fills (if any) still count against the position
        b.stop_id, b.target_id = ack.ids.get("stop"), ack.ids.get("target")
        v.ledger.save()
        for oid in old:
            if self._working(oid):
                v.cancel(oid)
        v.refresh()
        self._emit("protection_replaced", b.client_id, stop_id=b.stop_id, target_id=b.target_id, stop=b.stop,
                   target=b.target, volume=pos.volume)
        return not v.protection_problems(b)

    def close_position(self, pos: BrokerPosition, reason: str) -> ExecResult:
        b, v = self._bracket(pos), self.venue
        if b is None:
            return ExecResult("rejected", pos.comment, "not an ATLAS position; left for the operator", ticket=pos.ticket)
        for oid in (b.stop_id, b.target_id, b.entry_id):
            if self._working(oid):
                v.cancel(oid)
        v.refresh()
        now_pos = v.position(pos.ticket)
        if now_pos is None:
            return ExecResult("closed", b.client_id, reason + " (already closed)", pos.ticket, attempts=0)
        req = closing_order(b.contract, b.direction, now_pos.volume, "MARKET",
                            f"{b.client_id}-X{len(b.exit_ids) + 1}")
        try:  # the price the exit is planned at (the closing side of the quote now), for the slippage record
            t = v.symbol_rules(pos.symbol).point
            planned = v.tick(pos.symbol).price(b.direction, closing=True)
        except BrokerUnavailable:
            t = planned = None
        self._emit("order_submitted", b.client_id, request=req.to_dict(), purpose=reason)
        ack = v.submit(req)
        if ack.ok:
            b.exit_ids.append(ack.ids.get("order"))
            v.ledger.save()
            self._emit("order_acknowledged", b.client_id, order_id=ack.ids.get("order"))
        else:
            self._emit("order_rejected", b.client_id, reason=ack.reason)
        left = self._await(pos.ticket, want_open=False)
        if left is None:
            deals = [d for d in v.recent_deals(None) if d.position_id == pos.ticket and d.entry == "out"]
            price = deals[-1].price if deals else None
            self._emit("position_closed", b.client_id, ticket=pos.ticket, volume=now_pos.volume, price=price,
                       reason=reason)
            slip = round(b.direction * (planned - price) / t, 1) if planned is not None and price is not None else None
            return ExecResult("closed", b.client_id, reason, pos.ticket, now_pos.volume, price, planned, slip,
                              attempts=1)
        # Still open and its stop was cancelled above: put the protection back before reporting.
        self.modify_position(left, b.stop, b.target)
        return ExecResult("failed", b.client_id, f"exit did not fill: {ack.reason or 'no fill'}", pos.ticket, attempts=1)

    def flatten(self, positions: list[BrokerPosition], reason: str) -> list[ExecResult]:
        return [self.close_position(p, reason) for p in positions]

    # -- orders -----------------------------------------------------------------------------------------

    def get_orders(self) -> list[dict]:
        return self.venue.pending_orders()

    def _protecting(self, order: dict) -> bool:
        """A stop or target whose position is open, or whose entry is still working."""
        b = self.venue.ledger.get(order["comment"]) if order["comment"] else None
        return b is not None and order["role"] in ("stop", "target") and (
            self.venue.position(b.entry_id) is not None or self._working(b.entry_id))

    def cancel_order(self, ticket: int) -> ExecResult:
        order = next((o for o in self.get_orders() if o["ticket"] == ticket), None)
        if order is None:
            return ExecResult("rejected", "", f"no working order {ticket}")
        if not order["comment"]:
            return ExecResult("rejected", "", "not an ATLAS order; left for the operator")
        if self._protecting(order):
            return ExecResult("rejected", order["comment"], "protects an open position; close the position instead")
        ack = self.venue.cancel(ticket)
        self.venue.refresh()
        return ExecResult("cancelled" if ack.ok else "failed", order["comment"], "" if ack.ok else ack.reason, attempts=1)

    def modify_order(self, ticket: int, price: float | None = None, stop_price: float | None = None) -> ExecResult:
        """Reprice a working ATLAS order. A protective stop moves only through ``modify_position`` (tighter only)."""
        order = next((o for o in self.get_orders() if o["ticket"] == ticket), None)
        if order is None or not order["comment"]:
            return ExecResult("rejected", "", f"no working ATLAS order {ticket}")
        if order["role"] == "stop":
            return ExecResult("rejected", order["comment"], "a stop moves only through modify_position (tighter only)")
        ack = self.venue.modify(ticket, order["volume"], order["type"], price=price, stop_price=stop_price)
        self.venue.refresh()
        return ExecResult("filled" if ack.ok else "rejected", order["comment"], "" if ack.ok else ack.reason, attempts=1)

    def cancel_atlas_orders(self) -> list[ExecResult]:
        return [self.cancel_order(o["ticket"]) for o in self.get_orders() if o["comment"] and not self._protecting(o)]

    def housekeeping(self, now) -> list[ExecResult]:
        """Cancel ATLAS stop or target orders left working after their position closed (a re-placed leg is
        not linked to its partner), so a stale leg can never open a new position."""
        return [self.cancel_order(o["ticket"]) for o in self.get_orders()
                if o["comment"] and o["role"] in ("stop", "target", "exit") and not self._protecting(o)]

    # -- state ------------------------------------------------------------------------------------------

    def get_positions(self) -> list[BrokerPosition]:
        return self.venue.positions()

    def get_account_state(self) -> AccountInfo:
        return self.venue.account()

    def findings(self) -> list[dict]:
        """What reconciliation must not accept, beyond the position diff: an open ATLAS position whose
        protection does not verify (after one repair attempt), or a working order on a traded contract that
        ATLAS did not place. Each one keeps the engine in HALT until it is gone."""
        v, out = self.venue, []
        v.refresh()
        for b in v.ledger.active():
            if b.entry_id is None or not v.protection_problems(b):
                continue
            pos = v.position(b.entry_id)
            if pos is not None:
                self._protect(pos, b)
            problems = v.protection_problems(b)
            if problems:
                out.append({"kind": "unprotected_position", "symbol": b.root, "ticket": b.entry_id,
                            "detail": ",".join(problems)})
        for o in v.foreign_working_orders():
            out.append({"kind": "foreign_order", "symbol": v.root_of(o.contract), "ticket": o.id,
                        "detail": f"{o.type} {o.qty:g} on {o.contract} not placed by ATLAS"})
        return out

    def cancel_all_orders(self) -> list[ExecResult]:
        """Cancel every working ATLAS order that does not protect an open position. Never the operator's."""
        return self.cancel_atlas_orders()

    def reconcile(self, book, points: dict[str, float]) -> list:
        return diff(book, self.get_positions(), self.settings.magic_base, points)
