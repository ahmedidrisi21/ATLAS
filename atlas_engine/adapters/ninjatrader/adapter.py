"""NinjaTrader's API (the Tradovate API) as a ``FuturesVenue`` (futures-first, docs/futures.md).

Translates between ATLAS' generic futures vocabulary and Tradovate's REST
entities; no platform name or type leaves this package. Orders, fills and net
positions are read over REST and reused for ``poll_s`` seconds (Tradovate
allows about 5,000 requests an hour per user); every write drops the cache.
Quotes come from the market-data WebSocket (``quotes.py``).

Entity mapping (partner.tradovate.com, read 2026-10-05):

    /order/list + /orderVersion/list   -> VenueOrder (the list has the status, the latest version the prices)
    /fill/list                         -> VenueFill
    /position/list                     -> net position per contract (netPos, netPrice)
    /cashBalance/getcashbalancesnapshot -> balance (equity adds open P&L from live quotes)
    /contract/find, /contractMaturity/item, /product/item -> FuturesContract, tick size, value per point
    /order/placeoso                    -> a bracket: market entry, bracket1 stop, bracket2 limit (OCO)
    /order/placeoco                    -> a protective stop and target re-placed together (one cancels the other)
    /order/placeorder, /order/cancelorder, /order/modifyorder

Every order carries ``isAutomated: true`` (the API requires it for orders a
person did not place) and the client order ID in ``clOrdId`` (stored on the
order and returned by /order/list) and ``text``. ATLAS' ``OrderRequest`` maps
onto these bodies here and nowhere else.

Checked against the official specification (api.tradovate.com, which
developer.ninjatrader.com links as its API docs; read 2026-10-06): the
endpoints above, ``clOrdId`` on PlaceOrder / PlaceOSO / PlaceOCO and on each
bracket leg, ``ocoId`` / ``parentId`` / ``clOrdId`` on the Order entity, and
the ``failureReason`` values. Assumed until a demo run shows it: that the two
legs of a ``placeoso`` bracket come back sharing one ``ocoId`` (ATLAS refuses to
call a position protected otherwise), and that ``placeoco``'s ``ocoId`` answer is
the linked order's id (the legs are re-checked from /order/list either way).
"""

from __future__ import annotations

import datetime as dt
import time

from atlas_engine.adapters.broker import AccountInfo
from atlas_engine.adapters.futures_venue import FuturesVenue, VenueAck, VenueFill, VenueOrder
from atlas_engine.adapters.orders import OrderRequest
from atlas_engine.futures import FuturesContract, parse_contract, product

from .client import MD_SOCKET, OutcomeUnknown, NinjaTraderClient, NinjaTraderError
from .quotes import QuoteBook, QuoteStream

TYPES = {"MARKET": "Market", "LIMIT": "Limit", "STOP": "Stop", "STOP_LIMIT": "StopLimit"}
TYPES_BACK = {v: k for k, v in TYPES.items()}
# Unknown is treated as working: an order whose state the platform can't say is assumed live, never gone.
STATUS = {"Unknown": "working", "Working": "working", "Suspended": "working", "PendingNew": "working", "PendingReplace": "working",
          "PendingCancel": "working", "Filled": "filled", "Completed": "filled", "Canceled": "cancelled",
          "Expired": "cancelled", "Rejected": "rejected"}
ACTIONS = {1: "Buy", -1: "Sell"}
TIF = {"DAY": "Day", "GTC": "GTC"}


def _time(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


class NinjaTraderAdapter(FuturesVenue):
    platform = "ninjatrader"
    data_source = "ninjatrader-md-websocket"

    def __init__(self, client: NinjaTraderClient, contracts: dict[str, str], account_name: str | None = None,
                 ledger_path=None, commission_per_contract: dict | None = None, quotes: QuoteBook | None = None,
                 stream: bool = True, poll_s: float = 5.0, account_poll_s: float = 30.0, today=None):
        super().__init__(contracts, ledger_path, commission_per_contract)
        self.client, self.account_name = client, account_name
        self.quotes = quotes or QuoteBook()
        self.stream = None
        self._want_stream = stream
        self.poll_s, self.account_poll_s = poll_s, account_poll_s
        self.today = today or (lambda: dt.datetime.now(dt.timezone.utc).date())
        self.account_id: int | None = None
        self.account_spec: str | None = None
        self._acct: dict = {}
        self.names: dict[int, str] = {}
        self._cache: dict[str, tuple[float, object]] = {}
        self._ok = False

    @property
    def requests_sent(self) -> int:
        return self.client.requests_sent

    # -- connection -------------------------------------------------------------------------------------

    def disconnect(self) -> None:
        if self.stream is not None:
            self.stream.stop()
        self.stream = None
        self.client.token = None
        self._ok = False
        self.refresh()

    def reconnect(self) -> None:
        """Log in again and re-read everything: nothing cached survives a reconnect."""
        self.disconnect()
        self.client.authenticate()
        self.connect()

    def health(self) -> dict:
        now = self.client.now()
        quotes = {}
        for code in self.pins.values():
            try:
                quotes[code] = round((now - self.quotes.tick(code).time).total_seconds(), 1)
            except Exception:  # noqa: BLE001 - no quote yet
                quotes[code] = None
        return {"platform": self.platform, "env": self.client.env, "connected": self.connected(),
                "token_expires_in_s": None if self.client.expires is None
                else round((self.client.expires - now).total_seconds()),
                "quote_stream": None if self.stream is None else
                {"alive": self.stream.alive(), "error": self.stream.error},
                "quote_age_s": quotes, "requests_sent": self.client.requests_sent,
                # A pinned contract the platform no longer flags as the front month: liquidity is moving to the
                # next one. Reported for the operator, who re-pins; ATLAS never switches contracts by itself.
                "not_front": sorted(c.symbol for c in self.contracts.values() if c.front is False)}

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

    def _connect(self) -> None:
        self._ok = False
        accounts = [a for a in (self.client.get("/account/list") or []) if not a.get("closed")]
        if self.account_name:
            accounts = [a for a in accounts if a.get("name") == self.account_name]
        if len(accounts) != 1:
            raise NinjaTraderError(f"expected exactly one open Tradovate account{' named ' + self.account_name if self.account_name else ''}, "
                                 f"found {len(accounts)}; set ATLAS_NINJATRADER_ACCOUNT")
        self._acct = accounts[0]
        self.account_id, self.account_spec = int(self._acct["id"]), str(self._acct["name"])
        self.refresh()
        if self._want_stream and (self.stream is None or not self.stream.alive()):
            self.stream = QuoteStream(self.quotes, lambda: self.client.md_token, list(self.pins.values()), MD_SOCKET)
            self.stream.start()
        self._ok = True

    def connected(self) -> bool:
        return self._ok and self.client.token is not None and (self.stream is None or self.stream.alive())

    def _resolve_contract(self, code: str) -> tuple[FuturesContract, float, float]:
        c = self.client.get("/contract/find", name=code)
        if not c or not c.get("id"):
            raise NinjaTraderError(f"Tradovate does not list contract {code}")
        m = self.client.get("/contractMaturity/item", id=c["contractMaturityId"])
        p = self.client.get("/product/item", id=m["productId"])
        self.names[int(c["id"])] = code
        self.quotes.names[int(c["id"])] = code
        root, month, year = parse_contract(code, self.today())
        first = _time(m.get("firstIntentDate"))
        front = m.get("isFront")
        return (FuturesContract(code, root, month, year, _time(m.get("expirationDate")), first.date() if first else None,
                                None if front is None else bool(front)),
                float(p["tickSize"]), float(p["valuePerPoint"]))

    def _name(self, contract_id: int) -> str:
        if contract_id not in self.names:
            self.names[contract_id] = str(self.client.get("/contract/item", id=contract_id)["name"])
        return self.names[contract_id]

    def _raw_orders(self) -> list[dict]:
        rows = self._cached("orders", self.poll_s, lambda: self.client.get("/order/list") or [])
        if not isinstance(rows, list) or not all(isinstance(o, dict) and "accountId" in o and "id" in o for o in rows):
            self._cache.pop("orders", None)
            raise NinjaTraderError("malformed order data from the platform (rows without id or accountId)")
        return [o for o in rows if o.get("accountId") == self.account_id]

    def _orders(self) -> list[VenueOrder]:
        try:
            return self._map_orders()
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise NinjaTraderError(f"malformed order data from the platform ({type(e).__name__})") from None

    def _map_orders(self) -> list[VenueOrder]:
        versions: dict[int, dict] = {}
        for v in self._cached("versions", self.poll_s, lambda: self.client.get("/orderVersion/list") or []):
            if v["orderId"] not in versions or v["id"] > versions[v["orderId"]]["id"]:
                versions[v["orderId"]] = v
        out = []
        for o in self._raw_orders():
            v = versions.get(o["id"], {})
            if o["action"] not in ("Buy", "Sell"):
                raise ValueError("action")
            out.append(VenueOrder(int(o["id"]), self._name(o["contractId"]), 1 if o["action"] == "Buy" else -1,
                                  float(v.get("orderQty") or 0), TYPES_BACK.get(v.get("orderType"), str(v.get("orderType"))),
                                  STATUS.get(o.get("ordStatus"), "working"), v.get("price"), v.get("stopPrice"),
                                  _time(o.get("timestamp")), str(o.get("clOrdId") or ""), o.get("ocoId"),
                                  o.get("parentId")))
        return out

    def _fills(self) -> list[VenueFill]:
        mine = {o["id"] for o in self._raw_orders()}
        try:
            return [VenueFill(int(f["id"]), int(f["orderId"]), self._name(f["contractId"]),
                              1 if f["action"] == "Buy" else -1, float(f["qty"]), float(f["price"]), _time(f["timestamp"]))
                    for f in self._cached("fills", self.poll_s, lambda: self.client.get("/fill/list") or [])
                    if f["orderId"] in mine]
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise NinjaTraderError(f"malformed fill data from the platform ({type(e).__name__})") from None

    def _net_positions(self) -> dict[str, tuple[float, float]]:
        rows = self._cached("positions", self.poll_s, lambda: self.client.get("/position/list") or [])
        try:
            return {self._name(p["contractId"]): (float(p["netPos"]), float(p.get("netPrice") or 0.0))
                    for p in rows if p.get("accountId") == self.account_id and p.get("netPos")}
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise NinjaTraderError(f"malformed position data from the platform ({type(e).__name__})") from None

    def _quote(self, contract: str):
        return self.quotes.tick(contract)

    def account(self) -> AccountInfo:
        snap = self._cash()
        if snap.get("totalCashValue") is None:
            raise NinjaTraderError("the cash balance snapshot has no totalCashValue")
        balance = float(snap["totalCashValue"])
        # Open P&L from live quotes, so a floating loss shows at once (the firm's trailing limit counts it).
        open_pnl = 0.0
        for code, (net, avg) in self._net_positions().items():
            try:
                t = self.quotes.tick(code)
            except Exception:  # noqa: BLE001 - no quote yet: fall back to Tradovate's own figure
                return AccountInfo(self.account_id, f"ninjatrader-{self.client.env}", "USD", balance,
                                   float(snap.get("netLiq") or balance + float(snap.get("openPnL") or 0.0)),
                                   self._trade_allowed(), self.client.demo)
            mark = t.bid if net > 0 else t.ask
            open_pnl += (mark - avg) * net * product(self.root_of(code)).point_value
        return AccountInfo(self.account_id, f"ninjatrader-{self.client.env}", "USD", balance, round(balance + open_pnl, 2),
                           self._trade_allowed(), self.client.demo)

    def _cash(self) -> dict:
        snap = self._cached("cash", self.account_poll_s, lambda: self.client.post(
            "/cashBalance/getcashbalancesnapshot", {"accountId": self.account_id}) or {})
        if not isinstance(snap, dict) or snap.get("errorText"):
            raise NinjaTraderError("the platform gave no cash balance snapshot")
        return snap

    def get_balance(self) -> float:
        return self.account().balance

    def get_equity(self) -> float:
        return self.account().equity

    def get_margin(self) -> dict:
        """Margin from the cash balance snapshot (its documented fields); None where the platform gave none."""
        snap = self._cash()
        return {"initial": snap.get("initialMargin"), "maintenance": snap.get("maintenanceMargin"),
                "auto_liquidation_level": snap.get("autoLiqLevel")}

    def _trade_allowed(self) -> bool:
        a = self._acct
        return not (a.get("closed") or a.get("restricted") or a.get("readonly") or a.get("futuresDisabled"))

    # -- commands ---------------------------------------------------------------------------------------

    def _body(self, req: OrderRequest) -> dict:
        body = {"accountSpec": self.account_spec, "accountId": self.account_id, "action": ACTIONS[req.direction],
                "symbol": req.symbol, "orderQty": int(req.quantity), "orderType": TYPES[req.order_type],
                "clOrdId": req.client_order_id, "text": req.client_order_id, "isAutomated": True}
        if req.limit_price is not None:
            body["price"] = req.limit_price
        if req.stop_price is not None:
            body["stopPrice"] = req.stop_price
        if req.time_in_force is not None:
            body["timeInForce"] = TIF[req.time_in_force]
        return body

    def _write(self, path: str, body: dict, ids) -> VenueAck:
        try:
            r = self.client.post(path, body)
        except OutcomeUnknown as e:
            return VenueAck(False, reason=str(e), unknown=True)
        except NinjaTraderError as e:
            return VenueAck(False, reason=str(e))
        finally:
            self.refresh()
        if not isinstance(r, dict):
            return VenueAck(False, reason="empty answer", unknown=True)
        if r.get("failureReason") not in (None, "", "Success") or r.get("failureText"):
            return VenueAck(False, reason=f"{r.get('failureReason')}: {r.get('failureText') or ''}".strip())
        try:
            return VenueAck(True, {k: (int(r[v]) if r.get(v) else None) for k, v in ids.items()})
        except (TypeError, ValueError):
            return VenueAck(False, reason="malformed answer", unknown=True)

    def _send(self, req: OrderRequest) -> VenueAck:
        body = self._body(req)
        if not req.bracket:
            return self._write("/order/placeorder", body, {"order": "orderId"})
        back = ACTIONS[-req.direction]
        cid = req.client_order_id
        body["bracket1"] = {"action": back, "orderType": "Stop", "stopPrice": req.stop_loss, "timeInForce": "GTC",
                            "clOrdId": f"{cid}-S", "text": f"{cid}-S"}
        body["bracket2"] = {"action": back, "orderType": "Limit", "price": req.take_profit, "timeInForce": "GTC",
                            "clOrdId": f"{cid}-T", "text": f"{cid}-T"}
        return self._write("/order/placeoso", body, {"entry": "orderId", "stop": "oso1Id", "target": "oso2Id"})

    def _send_oco(self, stop: OrderRequest, target: OrderRequest) -> VenueAck:
        body = self._body(stop)
        other = self._body(target)
        body["other"] = {k: other[k] for k in ("action", "orderType", "price", "timeInForce", "clOrdId", "text")
                         if k in other}
        return self._write("/order/placeoco", body, {"stop": "orderId", "target": "ocoId"})

    def cancel(self, order_id: int) -> VenueAck:
        return self._write("/order/cancelorder", {"orderId": order_id, "isAutomated": True}, {})

    def modify(self, order_id: int, qty: float, type: str, price=None, stop_price=None) -> VenueAck:
        body = {"orderId": order_id, "orderQty": int(qty), "orderType": TYPES[type], "isAutomated": True}
        if price is not None:
            body["price"] = price
        if stop_price is not None:
            body["stopPrice"] = stop_price
        return self._write("/order/modifyorder", body, {})
