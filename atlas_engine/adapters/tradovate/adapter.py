"""Tradovate as a ``FuturesVenue`` (futures-first, docs/futures.md).

Translates between ATLAS' generic futures vocabulary and Tradovate's REST
entities; no Tradovate name or type leaves this package. Orders, fills and net
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
    /order/placeorder, /order/cancelorder, /order/modifyorder

Every order carries ``isAutomated: true`` (Tradovate requires it for orders a
person did not place) and the client order ID in ``clOrdId`` and ``text``.
"""

from __future__ import annotations

import datetime as dt
import time

from atlas_engine.adapters.broker import AccountInfo
from atlas_engine.adapters.futures_venue import FuturesVenue, VenueAck, VenueFill, VenueOrder
from atlas_engine.futures import FuturesContract, parse_contract, product

from .client import MD_SOCKET, OutcomeUnknown, TradovateClient, TradovateError
from .quotes import QuoteBook, QuoteStream

TYPES = {"MARKET": "Market", "LIMIT": "Limit", "STOP": "Stop", "STOP_LIMIT": "StopLimit"}
TYPES_BACK = {v: k for k, v in TYPES.items()}
STATUS = {"Working": "working", "Suspended": "working", "PendingNew": "working", "PendingReplace": "working",
          "PendingCancel": "working", "Filled": "filled", "Completed": "filled", "Canceled": "cancelled",
          "Expired": "cancelled", "Rejected": "rejected"}
ACTIONS = {1: "Buy", -1: "Sell"}


def _time(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


class TradovateAdapter(FuturesVenue):
    platform = "tradovate"

    def __init__(self, client: TradovateClient, contracts: dict[str, str], account_name: str | None = None,
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
            raise TradovateError(f"expected exactly one open Tradovate account{' named ' + self.account_name if self.account_name else ''}, "
                                 f"found {len(accounts)}; set ATLAS_TRADOVATE_ACCOUNT")
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
            raise TradovateError(f"Tradovate does not list contract {code}")
        m = self.client.get("/contractMaturity/item", id=c["contractMaturityId"])
        p = self.client.get("/product/item", id=m["productId"])
        self.names[int(c["id"])] = code
        self.quotes.names[int(c["id"])] = code
        root, month, year = parse_contract(code, self.today())
        first = _time(m.get("firstIntentDate"))
        return (FuturesContract(code, root, month, year, _time(m.get("expirationDate")), first.date() if first else None),
                float(p["tickSize"]), float(p["valuePerPoint"]))

    def _name(self, contract_id: int) -> str:
        if contract_id not in self.names:
            self.names[contract_id] = str(self.client.get("/contract/item", id=contract_id)["name"])
        return self.names[contract_id]

    def _raw_orders(self) -> list[dict]:
        return [o for o in self._cached("orders", self.poll_s, lambda: self.client.get("/order/list") or [])
                if o.get("accountId") == self.account_id]

    def _orders(self) -> list[VenueOrder]:
        versions: dict[int, dict] = {}
        for v in self._cached("versions", self.poll_s, lambda: self.client.get("/orderVersion/list") or []):
            if v["orderId"] not in versions or v["id"] > versions[v["orderId"]]["id"]:
                versions[v["orderId"]] = v
        out = []
        for o in self._raw_orders():
            v = versions.get(o["id"], {})
            out.append(VenueOrder(int(o["id"]), self._name(o["contractId"]), 1 if o["action"] == "Buy" else -1,
                                  float(v.get("orderQty") or 0), TYPES_BACK.get(v.get("orderType"), str(v.get("orderType"))),
                                  STATUS.get(o.get("ordStatus"), "working"), v.get("price"), v.get("stopPrice"),
                                  _time(o.get("timestamp"))))
        return out

    def _fills(self) -> list[VenueFill]:
        mine = {o["id"] for o in self._raw_orders()}
        return [VenueFill(int(f["id"]), int(f["orderId"]), self._name(f["contractId"]), 1 if f["action"] == "Buy" else -1,
                          float(f["qty"]), float(f["price"]), _time(f["timestamp"]))
                for f in self._cached("fills", self.poll_s, lambda: self.client.get("/fill/list") or [])
                if f["orderId"] in mine]

    def _net_positions(self) -> dict[str, tuple[float, float]]:
        rows = self._cached("positions", self.poll_s, lambda: self.client.get("/position/list") or [])
        return {self._name(p["contractId"]): (float(p["netPos"]), float(p.get("netPrice") or 0.0))
                for p in rows if p.get("accountId") == self.account_id and p.get("netPos")}

    def _quote(self, contract: str):
        return self.quotes.tick(contract)

    def account(self) -> AccountInfo:
        snap = self._cached("cash", self.account_poll_s, lambda: self.client.post(
            "/cashBalance/getcashbalancesnapshot", {"accountId": self.account_id}) or {})
        balance = float(snap.get("totalCashValue") or 0.0)
        # Open P&L from live quotes, so a floating loss shows at once (the firm's trailing limit counts it).
        open_pnl = 0.0
        for code, (net, avg) in self._net_positions().items():
            try:
                t = self.quotes.tick(code)
            except Exception:  # noqa: BLE001 - no quote yet: fall back to Tradovate's own figure
                return AccountInfo(self.account_id, f"tradovate-{self.client.env}", "USD", balance,
                                   float(snap.get("netLiq") or balance + float(snap.get("openPnL") or 0.0)),
                                   self._trade_allowed(), self.client.demo)
            mark = t.bid if net > 0 else t.ask
            open_pnl += (mark - avg) * net * product(self.root_of(code)).point_value
        return AccountInfo(self.account_id, f"tradovate-{self.client.env}", "USD", balance, round(balance + open_pnl, 2),
                           self._trade_allowed(), self.client.demo)

    def _trade_allowed(self) -> bool:
        a = self._acct
        return not (a.get("closed") or a.get("restricted") or a.get("readonly") or a.get("futuresDisabled"))

    # -- commands ---------------------------------------------------------------------------------------

    def _order(self, contract: str, side: int, qty: float, type: str, client_id: str, price=None, stop_price=None) -> dict:
        body = {"accountSpec": self.account_spec, "accountId": self.account_id, "action": ACTIONS[side],
                "symbol": contract, "orderQty": int(qty), "orderType": TYPES[type], "clOrdId": client_id,
                "text": client_id, "isAutomated": True}
        if price is not None:
            body["price"] = price
        if stop_price is not None:
            body["stopPrice"] = stop_price
        return body

    def _write(self, path: str, body: dict, ids) -> VenueAck:
        try:
            r = self.client.post(path, body)
        except OutcomeUnknown as e:
            return VenueAck(False, reason=str(e), unknown=True)
        except TradovateError as e:
            return VenueAck(False, reason=str(e))
        finally:
            self.refresh()
        if not isinstance(r, dict):
            return VenueAck(False, reason="empty answer", unknown=True)
        if r.get("failureReason") not in (None, "", "Success") or r.get("failureText"):
            return VenueAck(False, reason=f"{r.get('failureReason')}: {r.get('failureText') or ''}".strip())
        return VenueAck(True, {k: (int(r[v]) if r.get(v) else None) for k, v in ids.items()})

    def place_bracket(self, contract, side, qty, stop, target, client_id) -> VenueAck:
        body = self._order(contract, side, qty, "MARKET", client_id)
        body["bracket1"] = {"action": ACTIONS[-side], "orderType": "Stop", "stopPrice": stop, "timeInForce": "GTC"}
        body["bracket2"] = {"action": ACTIONS[-side], "orderType": "Limit", "price": target, "timeInForce": "GTC"}
        return self._write("/order/placeoso", body, {"entry": "orderId", "stop": "oso1Id", "target": "oso2Id"})

    def place_order(self, contract, side, qty, type, client_id, price=None, stop_price=None) -> VenueAck:
        body = self._order(contract, side, qty, type, client_id, price, stop_price)
        if type != "MARKET":
            body["timeInForce"] = "GTC"
        return self._write("/order/placeorder", body, {"order": "orderId"})

    def cancel(self, order_id: int) -> VenueAck:
        return self._write("/order/cancelorder", {"orderId": order_id, "isAutomated": True}, {})

    def modify(self, order_id: int, qty: float, type: str, price=None, stop_price=None) -> VenueAck:
        body = {"orderId": order_id, "orderQty": int(qty), "orderType": TYPES[type], "isAutomated": True}
        if price is not None:
            body["price"] = price
        if stop_price is not None:
            body["stopPrice"] = stop_price
        return self._write("/order/modifyorder", body, {})
