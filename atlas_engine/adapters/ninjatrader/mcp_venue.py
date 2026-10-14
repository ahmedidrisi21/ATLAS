"""NinjaTrader's MCP server as a ``FuturesVenue``: the free demo route (docs/futures.md, "The MCP route").

Same job as ``NinjaTraderAdapter`` (the REST API), over the MCP tools, because the REST API needs a funded
account and a paid add-on. Built on the server's recorded answers (docs/ninjatrader-mcp-capture/, demo,
2026-10-06). The client it uses (``mcp.McpClient``) reaches the demo server only, so every account here is
a demo account.

Tool mapping:

    user_profile.accounts[]                 -> the account (exactly one active, or the one named)
    my_portfolio.account                    -> balance (cashBalance) and equity (netLiq)
    my_portfolio.positions[]                -> net position per contract (netPos, netPrice)
    my_portfolio.workingOrders[]            -> working VenueOrders, with bracket.parentId / bracket.ocoId
    order_history.items[]                   -> every VenueOrder of the last days (status, prices, time)
    fill_history.items[]                    -> VenueFill
    search_contracts                        -> FuturesContract, tick size, value per point
    market_snapshot                         -> Tick (bid, ask, last, time)
    place_order (+ brackets), modify_order, cancel_order

Where it differs from the REST adapter, and what ATLAS does about it:

- No client order ID: ``place_order`` takes none and no answer carries one. ATLAS keeps the platform's order
  ids in its bracket ledger; after a lost answer it finds the entry by contract, side, quantity and time
  (``FuturesVenue.adopt``), and an ambiguous match leaves a foreign position that HALTs reconciliation.
- Bracket legs are offsets from the entry's working price, and a market entry's working price is its fill.
  ATLAS sends the offsets from the quote it decided on, then (once filled) the protection check sees the legs
  a little off the decided prices and moves them there with ``modify_order``, which takes absolute prices.
- A leg's ``bracket.ocoId`` is its sibling's id, so the OCO group is the smaller of the two ids.
- No standalone linked stop-and-target: ``place_order`` links legs only to an entry. A position whose legs are
  gone can't be re-protected here, so ``_send_oco`` refuses and the execution adapter closes the position and
  HALTs (its rule for an unprotected position).
- ``close_position`` is never used: it flattens the whole contract, the operator's contracts included. Exits
  are ATLAS's own reduce-only market orders.
- Quotes on an account without a CME data subscription are delayed (~10 minutes; ``dataFeedMode: Delayed``).
  The decision pipeline refuses a quote older than ``decision.max_quote_age_s``, so no trade is priced on one.
- Bars come from ``market_history`` (1-minute, trade prices, timestamps at the bar start, UTC). ``m1_rates``
  turns them into the engine's M1 rows quoted at a one-tick spread around the traded price (bid half a tick
  below, ask half a tick above), the same quoting the MES research used (``atlas_research.data.quoted_at_spread``).
  At most ``MAX_HISTORY_BARS`` per call: the server's own limit is not documented, so this is kept small.
  A bar that closed seconds ago may not be served yet, so ``bar_grace_s`` tells the signal sources to wait
  that long for it before deciding on a 15-minute close without it.
"""

from __future__ import annotations

import datetime as dt
import time
import zlib

import numpy as np

from atlas_engine.adapters.broker import AccountInfo, BrokerUnavailable, Tick
from atlas_engine.adapters.futures_venue import FuturesVenue, VenueAck, VenueFill, VenueOrder
from atlas_engine.adapters.orders import OrderRequest
from atlas_engine.futures import FuturesContract, parse_contract, product

from .client import NinjaTraderError
from .mcp import SERVERS, McpClient, McpError, NoAnswer

TYPES = {"MARKET": "Market", "LIMIT": "Limit", "STOP": "Stop", "STOP_LIMIT": "StopLimit"}
TYPES_BACK = {v: k for k, v in TYPES.items()}
# Unknown is treated as working: an order whose state the platform can't say is assumed live, never gone.
STATUS = {"Unknown": "working", "Working": "working", "Suspended": "working", "PendingNew": "working",
          "PendingReplace": "working", "PendingCancel": "working", "Filled": "filled", "Completed": "filled",
          "Canceled": "cancelled", "Expired": "cancelled", "Rejected": "rejected"}
ACTIONS = {1: "Buy", -1: "Sell"}
TIF = {"DAY": "Day", "GTC": "GTC"}
# CME equity-index futures stop trading at 09:30 New York on the expiration date; the platform gives the date
# only, so ATLAS takes 13:30 UTC (the earlier of the EDT/EST times) as the last trade.
LAST_TRADE_UTC = dt.time(13, 30)
MAX_HISTORY_BARS = 5000  # about 3.5 days of 1-minute bars; enough for the 15-minute setups' features
RATE_FIELDS = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
               ("tick_volume", "f8"), ("spread", "f8")]


def _time(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


class NinjaTraderMcpAdapter(FuturesVenue):
    platform = "ninjatrader"
    data_source = "ninjatrader-mcp"
    bar_grace_s = 90.0  # wait this long after a 15-minute close for its last 1-minute bar to be served

    def __init__(self, client: McpClient, contracts: dict[str, str], account_name: str | None = None,
                 ledger_path=None, commission_per_contract: dict | None = None, poll_s: float = 5.0,
                 quote_s: float = 1.0, history: str = "last 7 days", leg_wait_s: float = 5.0,
                 sleep=time.sleep, today=None):
        super().__init__(contracts, ledger_path, commission_per_contract)
        if not client.orders:
            raise ValueError("the venue needs an order-capable MCP client")
        if client.oauth.resource != SERVERS["demo"]:
            raise ValueError("the MCP venue trades the demo server only")
        self.client, self.account_name = client, account_name
        self.poll_s, self.quote_s, self.history, self.leg_wait_s = poll_s, quote_s, history, leg_wait_s
        self.sleep = sleep
        self.account_spec: str | None = None
        self.today = today or (lambda: dt.datetime.now(dt.timezone.utc).date())
        self._cache: dict[str, tuple[float, object]] = {}
        self._ok = False

    # -- reads ------------------------------------------------------------------------------------------

    def _cached(self, key: str, ttl: float, fn):
        hit = self._cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (time.monotonic(), value)
        return value

    def refresh(self) -> None:
        self._cache.clear()

    def _call(self, tool: str, args: dict | None = None) -> dict:
        try:
            out = self.client.call(tool, args)
        except McpError as e:
            raise NinjaTraderError(str(e)) from None
        if not isinstance(out, dict):
            raise NinjaTraderError(f"malformed {tool} answer from the platform")
        return out

    def _connect(self) -> None:
        self._ok = False
        self.refresh()
        accounts = [a for a in self._call("user_profile").get("accounts") or [] if a.get("active")]
        if self.account_name:
            accounts = [a for a in accounts if a.get("name") == self.account_name]
        if len(accounts) != 1 or not accounts[0].get("name"):
            raise NinjaTraderError(f"expected exactly one active NinjaTrader account"
                                   f"{' named ' + self.account_name if self.account_name else ''}, found "
                                   f"{len(accounts)}; set ATLAS_NINJATRADER_ACCOUNT")
        self.account_spec = str(accounts[0]["name"])
        self._ok = True

    def connected(self) -> bool:
        return self._ok

    def disconnect(self) -> None:
        self._ok = False
        self.client.session_id = None
        self.refresh()

    def reconnect(self) -> None:
        self.disconnect()
        self.connect()

    def health(self) -> dict:
        return {"platform": self.platform, "route": "mcp", "env": "demo", "connected": self.connected(),
                "session": self.client.session_id is not None,
                "not_front": sorted(c.symbol for c in self.contracts.values() if c.front is False)}

    def _portfolio(self) -> dict:
        return self._cached("portfolio", self.poll_s, lambda: self._call("my_portfolio", {"account": self.account_spec}))

    def _items(self, tool: str) -> list[dict]:
        rows = self._cached(tool, self.poll_s, lambda: self._call(
            tool, {"account": self.account_spec, "startDate": self.history}).get("items") or [])
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            self._cache.pop(tool, None)
            raise NinjaTraderError(f"malformed {tool} rows from the platform")
        return rows

    def _resolve_contract(self, code: str) -> tuple[FuturesContract, float, float]:
        root, month, year = parse_contract(code, self.today())
        rows = self._call("search_contracts", {"text": root}).get("contracts") or []
        c = next((r for r in rows if r.get("symbol") == code and not r.get("expired")), None)
        if c is None:
            raise NinjaTraderError(f"NinjaTrader does not list contract {code}")
        expiry = c.get("expirationDate")
        last = dt.datetime.combine(dt.date.fromisoformat(expiry), LAST_TRADE_UTC, dt.timezone.utc) if expiry else None
        return FuturesContract(code, root, month, year, last), float(c["tickSize"]), float(c["valuePerPoint"])

    def _quote(self, contract: str) -> Tick:
        def snap():
            rows = self._call("market_snapshot", {"symbols": [contract]}).get("snapshots") or []
            q = next((r for r in rows if r.get("symbol") == contract), None)
            if q is None or q.get("error") or q.get("bidPrice") is None or q.get("askPrice") is None:
                raise NinjaTraderError(f"no quote for {contract}")
            return Tick(contract, _time(q["timestamp"]), float(q["bidPrice"]), float(q["askPrice"]),
                        q.get("lastPrice"), q.get("totalVolume"))
        try:
            return self._cached(f"quote:{contract}", self.quote_s, snap)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise NinjaTraderError(f"malformed quote from the platform ({type(e).__name__})") from None

    def m1_rates(self, symbol: str, count: int):
        """The last ``count`` (at most ``MAX_HISTORY_BARS``) 1-minute bars of the pinned contract, oldest first,
        as M1 rows in UTC epoch seconds: bid OHLC half a tick under the traded price, ``spread`` one tick."""
        contract = self.contract_of(symbol)
        n = int(min(max(count, 1), MAX_HISTORY_BARS))
        r = self._call("market_history", {"symbol": contract, "barType": "Minute", "barSize": 1, "count": n})
        bars = r.get("bars")
        if not isinstance(bars, list) or not bars:
            raise BrokerUnavailable(f"no 1-minute bars for {contract}")
        tick = product(self.root_of(contract)).tick_size
        try:
            rows = sorted((int(_time(b["timestamp"]).timestamp()), float(b["open"]) - tick / 2,
                           float(b["high"]) - tick / 2, float(b["low"]) - tick / 2, float(b["close"]) - tick / 2,
                           float((b.get("upVolume") or 0) + (b.get("downVolume") or 0)), 1.0) for b in bars)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise BrokerUnavailable(f"malformed bars from the platform ({type(e).__name__})") from None
        self.bars_feed_mode = r.get("dataFeedMode")
        return np.array(rows, dtype=RATE_FIELDS)

    def _orders(self) -> list[VenueOrder]:
        try:
            return self._map_orders()
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise NinjaTraderError(f"malformed order data from the platform ({type(e).__name__})") from None

    @staticmethod
    def _side(action: str) -> int:
        if action not in ("Buy", "Sell"):
            raise ValueError("action")
        return 1 if action == "Buy" else -1

    def _map_orders(self) -> list[VenueOrder]:
        out: dict[int, VenueOrder] = {}
        for o in self._items("order_history"):
            out[int(o["orderId"])] = VenueOrder(
                int(o["orderId"]), str(o["symbol"]), self._side(o["action"]), float(o["quantity"]),
                TYPES_BACK.get(o.get("orderType"), str(o.get("orderType"))), STATUS.get(o.get("ordStatus"), "working"),
                o.get("price"), o.get("stopPrice"), _time(o.get("timestamp")))
        for w in self._portfolio().get("workingOrders") or []:
            oid, leg = int(w["id"]), w.get("bracket") or {}
            sibling = leg.get("ocoId")
            seen = out.get(oid)
            out[oid] = VenueOrder(
                oid, str(w["symbol"]), self._side(w["action"]), float(w["quantity"]),
                TYPES_BACK.get(w.get("orderType"), str(w.get("orderType"))), STATUS.get(w.get("ordStatus"), "working"),
                w.get("price"), w.get("stopPrice"), seen.time if seen else None,
                oco_id=min(oid, int(sibling)) if sibling else None,
                parent_id=int(leg["parentId"]) if leg.get("parentId") else None)
        return list(out.values())

    def _fills(self) -> list[VenueFill]:
        try:
            return [VenueFill(int(f["fillId"]), int(f["orderId"]), str(f["symbol"]), self._side(f["action"]),
                              float(f["quantity"]), float(f["price"]), _time(f["timestamp"]))
                    for f in self._items("fill_history")]
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise NinjaTraderError(f"malformed fill data from the platform ({type(e).__name__})") from None

    def _net_positions(self) -> dict[str, tuple[float, float]]:
        try:
            return {str(p["symbol"]): (float(p["netPos"]), float(p.get("netPrice") or 0.0))
                    for p in self._portfolio().get("positions") or [] if p.get("netPos")}
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise NinjaTraderError(f"malformed position data from the platform ({type(e).__name__})") from None

    def account(self) -> AccountInfo:
        a = self._portfolio().get("account") or {}
        if a.get("cashBalance") is None or a.get("netLiq") is None:
            raise NinjaTraderError("the account summary has no cashBalance or netLiq")
        return AccountInfo(zlib.crc32(self.account_spec.encode()), "ninjatrader-mcp-demo", str(a.get("currency") or "USD"),
                           float(a["cashBalance"]), float(a["netLiq"]), True, True)

    def get_balance(self) -> float:
        return self.account().balance

    def get_equity(self) -> float:
        return self.account().equity

    def get_margin(self) -> dict:
        a = self._portfolio().get("account") or {}
        return {"initial": a.get("fullInitialMargin"), "maintenance": a.get("maintenanceMargin"),
                "auto_liquidation_level": a.get("autoLiqLevel")}

    # -- commands ---------------------------------------------------------------------------------------

    def _write(self, tool: str, args: dict) -> tuple[VenueAck | None, dict]:
        """Send one command. Returns (a failed ack, {}) or (None, the answer) when the platform accepted it."""
        try:
            r = self.client.call(tool, args)
        except NoAnswer as e:
            return VenueAck(False, reason=str(e), unknown=True), {}
        except McpError as e:
            return VenueAck(False, reason=str(e)), {}
        finally:
            self.refresh()
        status = r.get("status")
        if status == "ok":
            return None, r
        err = r.get("error") or {}
        reason = f"{status or 'no status'}: {err.get('reason') or ''} {err.get('message') or ''}".strip()
        # "failed" is a system error: the order may or may not be working (a tracking timeout is one).
        return VenueAck(False, reason=reason, unknown=status in (None, "failed")), r

    def _order_args(self, req: OrderRequest) -> dict:
        args = {"account": self.account_spec, "symbol": req.symbol, "action": ACTIONS[req.direction],
                "quantity": int(req.quantity), "orderType": TYPES[req.order_type],
                # The tool requires a time in force; a market order lasts the day.
                "timeInForce": TIF[req.time_in_force] if req.time_in_force else "Day"}
        if req.limit_price is not None:
            args["price"] = req.limit_price
        if req.stop_price is not None:
            args["stopPrice"] = req.stop_price
        return args

    def _send(self, req: OrderRequest) -> VenueAck:
        args = self._order_args(req)
        if not req.bracket:
            failed, r = self._write("place_order", args)
            return failed or self._id_ack(r, "order")
        # Offsets from the quote the decision used; the protection check moves the legs onto the decided prices
        # once the entry has filled (modify_order takes absolute prices).
        q = self._quote(req.symbol)
        ref = q.ask if req.direction > 0 else q.bid
        tick = self._tick_size(req.symbol)
        args["brackets"] = [{"qty": int(req.quantity), "stopLoss": self._round(req.stop_loss - ref, tick),
                             "profitTarget": self._round(req.take_profit - ref, tick)}]
        failed, r = self._write("place_order", args)
        if failed:
            return failed
        ack = self._id_ack(r, "entry")
        if ack.ok:
            ack.ids.update(self._legs(ack.ids["entry"]))
        return ack

    def _tick_size(self, contract: str) -> float | None:
        c = self.contracts.get(self.root_of(contract))
        return product(c.root).tick_size if c is not None else None

    @staticmethod
    def _round(x: float, tick: float | None) -> float:
        return round(round(x / tick) * tick, 10) if tick else x

    @staticmethod
    def _id_ack(r: dict, role: str) -> VenueAck:
        try:
            return VenueAck(True, {role: int(r["id"])})
        except (KeyError, TypeError, ValueError):
            return VenueAck(False, reason="accepted without an order id", unknown=True)

    def _legs(self, entry_id: int) -> dict:
        """The stop and target the platform spawned for an entry (they appear once it fills)."""
        end = time.monotonic() + self.leg_wait_s
        while True:
            self.refresh()
            legs = [o for o in self._orders() if o.parent_id == entry_id and o.status == "working"]
            stop = [o.id for o in legs if o.type == "STOP"]
            target = [o.id for o in legs if o.type == "LIMIT"]
            if (stop and target) or time.monotonic() >= end:
                return {"stop": stop[0] if len(stop) == 1 else None, "target": target[0] if len(target) == 1 else None}
            self.sleep(0.5)

    def protection_problems(self, b) -> list[str]:
        """As the base, after picking up legs the platform spawned late (they appear only once the entry fills)."""
        if b.entry_id is not None and (b.stop_id is None or b.target_id is None):
            legs = [o for o in self._orders() if o.parent_id == b.entry_id and o.status == "working"]
            for role, kind in (("stop", "STOP"), ("target", "LIMIT")):
                found = [o.id for o in legs if o.type == kind]
                if getattr(b, f"{role}_id") is None and len(found) == 1:
                    setattr(b, f"{role}_id", found[0])
            self.ledger.save()
        return super().protection_problems(b)

    def _send_oco(self, stop: OrderRequest, target: OrderRequest) -> VenueAck:
        return VenueAck(False, reason="ninjatrader_mcp_cannot_place_a_linked_stop_and_target_without_an_entry")

    def cancel(self, order_id: int) -> VenueAck:
        failed, r = self._write("cancel_order", {"account": self.account_spec, "orderId": int(order_id)})
        if failed:
            return failed
        rows = [c for c in r.get("canceled") or [] if c.get("id") == order_id] if "canceled" in r else []
        if rows and rows[0].get("status") != "ok":
            return VenueAck(False, reason=f"{rows[0].get('status')}: {rows[0].get('reason') or ''}".strip())
        return VenueAck(True)

    def modify(self, order_id: int, qty: float, type: str, price=None, stop_price=None) -> VenueAck:
        args = {"account": self.account_spec, "orderId": int(order_id), "quantity": int(qty)}
        if price is not None:
            args["price"] = price
        if stop_price is not None:
            args["stopPrice"] = stop_price
        failed, _ = self._write("modify_order", args)
        return failed or VenueAck(True)
