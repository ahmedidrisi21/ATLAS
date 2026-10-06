"""A stand-in for NinjaTrader's MCP server, answering in the shapes recorded from the demo server
(docs/ninjatrader-mcp-capture/, 2026-10-06). Duck-types ``mcp.McpClient`` for the venue."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from atlas_engine.adapters.ninjatrader.mcp import NEVER_TOOLS, ORDER_TOOLS, SERVERS, McpError, NoAnswer

ACCOUNT = "DEMO_ACCOUNT_1"
EXPIRY = {"MESZ6": "2026-12-18", "MESH7": "2027-03-19"}


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class FakeMcpNinjaTrader:
    def __init__(self, now, orders: bool = True, slip_ticks: int = 0):
        self.now, self.orders, self.slip = now, orders, slip_ticks
        self.oauth = SimpleNamespace(resource=SERVERS["demo"])
        self.session_id = "s1"
        self.quotes: dict[str, tuple[float, float, dt.datetime]] = {}
        self.book: dict[int, dict] = {}
        self.fills: list[dict] = []
        self.next_id = 1000
        self.calls: list[tuple[str, dict]] = []
        self.cash = 50_000.0
        self.lose_answer: set[str] = set()  # tools that act but whose answer is lost (once)
        self.legs_hidden = 0  # portfolio reads that don't show spawned legs yet
        self.status_override: dict[str, dict] = {}  # tool -> answer to return instead of acting

    # -- driving the market ---------------------------------------------------------------------------

    def set_quote(self, code: str, bid: float, ask: float | None = None, at: dt.datetime | None = None) -> None:
        self.quotes[code] = (bid, bid + 0.25 if ask is None else ask, at or self.now())
        bid, ask, _ = self.quotes[code]
        for o in [o for o in self.book.values() if o["ordStatus"] == "Working" and o["symbol"] == code]:
            buy = o["action"] == "Buy"
            if o["orderType"] == "Stop" and ((buy and ask >= o["stopPrice"]) or (not buy and bid <= o["stopPrice"])):
                self._fill(o, o["stopPrice"])
            elif o["orderType"] == "Limit" and ((buy and ask <= o["price"]) or (not buy and bid >= o["price"])):
                self._fill(o, o["price"])

    # -- the client interface the venue uses ----------------------------------------------------------

    def call(self, tool: str, args: dict | None = None) -> dict:
        args = dict(args or {})
        self.calls.append((tool, args))
        if tool in NEVER_TOOLS or tool == "close_position":
            raise McpError(f"the venue must never call {tool}")
        if tool in ORDER_TOOLS and not self.orders:
            raise McpError("not an order client")
        if tool in self.status_override:
            return self.status_override.pop(tool)
        out = getattr(self, "_" + tool)(args)
        if tool in self.lose_answer:
            self.lose_answer.discard(tool)
            raise NoAnswer("NinjaTrader MCP server unreachable (TimeoutError)")
        return out

    def _user_profile(self, a):
        return {"accounts": [{"name": ACCOUNT, "active": True}], "identity": {}, "tradovateSubscription": {}}

    def _search_contracts(self, a):
        return {"contracts": [{"symbol": c, "productName": "MES", "expirationDate": e, "expired": False,
                               "tickSize": 0.25, "tickValue": 1.25, "valuePerPoint": 5, "isMicro": True}
                              for c, e in EXPIRY.items() if c.startswith(a.get("text", ""))]}

    def _market_snapshot(self, a):
        out = []
        for c in a["symbols"]:
            bid, ask, at = self.quotes[c]
            out.append({"symbol": c, "bidPrice": bid, "askPrice": ask, "lastPrice": bid, "timestamp": _iso(at),
                        "dataFeedMode": "RealTime", "tickSize": 0.25, "valuePerPoint": 5, "totalVolume": 100})
        return {"snapshots": out}

    def _positions(self) -> dict[str, tuple[int, float]]:
        net: dict[str, list] = {}
        for f in self.fills:
            q = f["quantity"] * (1 if f["action"] == "Buy" else -1)
            n = net.setdefault(f["symbol"], [0, 0.0])
            if n[0] == 0 or (n[0] > 0) == (q > 0):
                n[1] = (n[1] * abs(n[0]) + f["price"] * abs(q)) / (abs(n[0]) + abs(q))
            n[0] += q
        return {s: (n[0], n[1]) for s, n in net.items() if n[0]}

    def _my_portfolio(self, a):
        out = {"account": {"name": ACCOUNT, "currency": "USD", "cashBalance": self.cash, "netLiq": self.cash,
                           "totalUsedMargin": 0, "autoLiqLevel": 0}}
        pos = [{"symbol": s, "netPos": n, "netPrice": p, "valuePerPoint": 5} for s, (n, p) in self._positions().items()]
        if pos:
            out["positions"] = pos
        working = []
        for o in self.book.values():
            if o["ordStatus"] != "Working":
                continue
            if o.get("parent") and self.legs_hidden > 0:
                continue
            w = {k: o[k] for k in ("id", "symbol", "action", "orderType", "quantity", "timeInForce", "ordStatus")}
            for k in ("price", "stopPrice"):
                if o.get(k) is not None:
                    w[k] = o[k]
            if o.get("parent"):
                w["bracket"] = {"parentId": o["parent"], "ocoId": o["oco"]}
            working.append(w)
        if self.legs_hidden > 0:
            self.legs_hidden -= 1
        if working:
            out["workingOrders"] = working
        return out

    def _order_history(self, a):
        assert a["account"] == ACCOUNT and a.get("startDate")
        return {"items": [{"orderId": o["id"], "timestamp": o["timestamp"], "account": ACCOUNT, "symbol": o["symbol"],
                           "action": o["action"], "orderType": o["orderType"], "ordStatus": o["ordStatus"],
                           "quantity": o["quantity"], "filledQty": o.get("filledQty", 0),
                           **({"price": o["price"]} if o.get("price") is not None else {}),
                           **({"stopPrice": o["stopPrice"]} if o.get("stopPrice") is not None else {})}
                          for o in self.book.values()]}

    def _fill_history(self, a):
        return {"items": list(self.fills)}

    def _new(self, a: dict, **extra) -> dict:
        self.next_id += 1
        o = {"id": self.next_id, "symbol": a["symbol"], "action": a["action"], "orderType": a["orderType"],
             "quantity": a["quantity"], "timeInForce": a.get("timeInForce"), "price": a.get("price"),
             "stopPrice": a.get("stopPrice"), "ordStatus": "Working", "timestamp": _iso(self.now()), **extra}
        self.book[o["id"]] = o
        return o

    def _fill(self, o: dict, price: float) -> None:
        o["ordStatus"], o["filledQty"] = "Filled", o["quantity"]
        self.fills.append({"fillId": len(self.fills) + 1, "orderId": o["id"], "timestamp": _iso(self.now()),
                           "account": ACCOUNT, "symbol": o["symbol"], "action": o["action"],
                           "quantity": o["quantity"], "price": price})
        if o.get("oco") and self.book[o["oco"]]["ordStatus"] == "Working":
            self.book[o["oco"]]["ordStatus"] = "Canceled"
        for leg in o.get("pending_legs", []):  # bracket legs spawn on the entry's fill, offsets from the fill
            back = "Sell" if o["action"] == "Buy" else "Buy"
            stop = self._new({"symbol": o["symbol"], "action": back, "orderType": "Stop", "quantity": leg["qty"],
                              "timeInForce": "GTC", "stopPrice": price + leg["stopLoss"]}, parent=o["id"])
            target = self._new({"symbol": o["symbol"], "action": back, "orderType": "Limit", "quantity": leg["qty"],
                                "timeInForce": "GTC", "price": price + leg["profitTarget"]}, parent=o["id"])
            stop["oco"], target["oco"] = target["id"], stop["id"]

    def _place_order(self, a):
        assert a["account"] == ACCOUNT and a.get("timeInForce") in ("Day", "GTC")
        o = self._new(a, pending_legs=a.get("brackets") or [])
        if a["orderType"] == "Market":
            bid, ask, _ = self.quotes[a["symbol"]]
            slip = self.slip * 0.25
            self._fill(o, ask + slip if a["action"] == "Buy" else bid - slip)
        out = {"status": "ok", "id": o["id"], "timestamp": o["timestamp"], **{k: a[k] for k in a if k != "account"}}
        if a.get("brackets"):
            out["strategyId"] = 9000 + o["id"]
        return out

    def _modify_order(self, a):
        o = self.book.get(a["orderId"])
        if o is None or o["ordStatus"] != "Working":
            return {"status": "rejected", "error": {"reason": "OrderNotWorking", "message": "not working"}}
        for k, field in (("price", "price"), ("stopPrice", "stopPrice"), ("quantity", "quantity")):
            if a.get(k) is not None:
                o[field] = a[k]
        return {"status": "ok", "id": o["id"], "symbol": o["symbol"], "orderType": o["orderType"]}

    def _cancel_order(self, a):
        o = self.book.get(a["orderId"])
        ok = o is not None and o["ordStatus"] == "Working"
        if ok:
            o["ordStatus"] = "Canceled"
        return {"canceled": [{"id": a["orderId"], "symbol": o["symbol"] if o else None,
                              "status": "ok" if ok else "rejected", **({} if ok else {"reason": "not working"})}],
                "remainingWorkingOrders": sum(1 for x in self.book.values() if x["ordStatus"] == "Working")}
