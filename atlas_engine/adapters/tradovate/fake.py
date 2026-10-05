"""A stand-in for Tradovate's REST API, for tests and dry runs (``atlas-engine --broker fake-tradovate``).

It answers the endpoints ``TradovateClient`` calls with Tradovate-shaped JSON
and keeps a small, honest exchange: market orders fill at the quote, a
bracket's stop and limit work once the entry fills and cancel each other, and
``set_quote`` fills stops and limits the price reaches. Fault switches mimic
what the real platform can do: refuse an order, lose the answer to a command,
hand out a penalty ticket, leave a market order unfilled, drop a stop.
"""

from __future__ import annotations

import datetime as dt
import itertools
import random

from .quotes import QuoteBook

PRODUCTS = {"MES": (0.25, 5.0), "ES": (0.25, 50.0), "MNQ": (0.25, 2.0), "NQ": (0.25, 20.0), "MGC": (0.1, 10.0),
            "GC": (0.1, 100.0), "MYM": (1.0, 0.5), "YM": (1.0, 5.0), "MCL": (0.01, 100.0), "CL": (0.01, 1000.0)}


class FakeTradovate:
    def __init__(self, contracts: dict[str, dt.datetime] | None = None, now=None, balance: float = 50_000.0,
                 account_name: str = "DEMO123", other_accounts: int = 0):
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.ids = itertools.count(1000)
        self.token = None
        self.calls: list[tuple[str, str]] = []
        self.accounts = [{"id": 1, "name": account_name, "closed": False, "restricted": False, "readonly": False}]
        for i in range(other_accounts):
            self.accounts.append({"id": 2 + i, "name": f"OTHER{i}", "closed": False})
        self.cash = balance
        self.contracts: dict[int, dict] = {}
        self.maturities: dict[int, dict] = {}
        self.products: dict[int, dict] = {}
        self.quotes: dict[str, tuple[float, float]] = {}
        for code, expiry in (contracts or {}).items():
            self.list_contract(code, expiry)
        self.orders: dict[int, dict] = {}
        self.versions: dict[int, dict] = {}
        self.fills: list[dict] = []
        self.foreign: dict[str, tuple[int, float]] = {}  # positions opened outside ATLAS (another app, a person)
        # fault switches
        self.reject_next = None  # failureText for the next order command
        self.lose_answer_next = False  # apply the next order command, then drop the answer
        self.drop_next_before_send = False  # drop the next order command without applying it
        self.penalty_next = 0.0  # p-time for the next call
        self.fill_market = True
        self.drop_stop_after_fill = False
        self.expire_token = False

    # -- set up -----------------------------------------------------------------------------------------

    def list_contract(self, code: str, last_trade: dt.datetime, first_intent: dt.datetime | None = None) -> int:
        root = code[:-2]
        tick, vpp = PRODUCTS[root]
        pid = next(self.ids)
        self.products[pid] = {"id": pid, "name": root, "tickSize": tick, "valuePerPoint": vpp, "currencyId": 1}
        mid = next(self.ids)
        self.maturities[mid] = {"id": mid, "productId": pid, "expirationDate": last_trade.isoformat().replace("+00:00", "Z"),
                                "firstIntentDate": first_intent.isoformat().replace("+00:00", "Z") if first_intent else None}
        cid = next(self.ids)
        self.contracts[cid] = {"id": cid, "name": code, "contractMaturityId": mid}
        return cid

    def cid(self, code: str) -> int:
        return next(c["id"] for c in self.contracts.values() if c["name"] == code)

    def quote_message(self, code: str, bid: float, ask: float, last: float | None = None) -> dict:
        entries = {"Bid": {"price": bid, "size": 5}, "Offer": {"price": ask, "size": 5}}
        if last is not None:
            entries["Trade"] = {"price": last, "size": 1}
        return {"e": "md", "d": {"quotes": [{"timestamp": self._ts(), "contractId": self.cid(code), "entries": entries}]}}

    def set_quote(self, code: str, bid: float, ask: float) -> None:
        self.quotes[code] = (bid, ask)
        for o in list(self.orders.values()):
            if o["ordStatus"] != "Working" or self.contracts[o["contractId"]]["name"] != code:
                continue
            v = self.versions[o["id"]]
            buy = o["action"] == "Buy"
            if v["orderType"] == "Stop" and ((buy and ask >= v["stopPrice"]) or (not buy and bid <= v["stopPrice"])):
                self._fill(o, ask if buy else bid)
            elif v["orderType"] == "Limit" and ((buy and ask <= v["price"]) or (not buy and bid >= v["price"])):
                self._fill(o, v["price"])

    def open_foreign(self, code: str, net: int, price: float) -> None:
        self.foreign[code] = (net, price)

    # -- exchange ---------------------------------------------------------------------------------------

    def _ts(self) -> str:
        return self.now().isoformat().replace("+00:00", "Z")

    def _new_order(self, body: dict, status: str = "Working", parent: int | None = None, account=1) -> dict:
        oid = next(self.ids)
        contract = next(c for c in self.contracts.values() if c["name"] == body["symbol"])
        o = {"id": oid, "accountId": account, "contractId": contract["id"], "timestamp": self._ts(),
             "action": body["action"], "ordStatus": status, "parentId": parent, "ocoId": None}
        self.orders[oid] = o
        self.versions[oid] = {"id": next(self.ids), "orderId": oid, "orderQty": body.get("orderQty", 0),
                              "orderType": body["orderType"], "price": body.get("price"), "stopPrice": body.get("stopPrice"),
                              "text": body.get("text")}
        return o

    def _fill(self, o: dict, price: float, qty: int | None = None) -> None:
        v = self.versions[o["id"]]
        qty = qty or v["orderQty"]
        self.fills.append({"id": next(self.ids), "orderId": o["id"], "contractId": o["contractId"], "timestamp": self._ts(),
                           "action": o["action"], "qty": qty, "price": price, "active": True})
        o["ordStatus"] = "Filled"
        for child in self.orders.values():  # bracket legs go live when the entry fills
            if child["parentId"] == o["id"] and child["ordStatus"] == "Suspended":
                child["ordStatus"] = "Working"
                if self.drop_stop_after_fill and self.versions[child["id"]]["orderType"] == "Stop":
                    child["ordStatus"] = "Canceled"
        if o["ocoId"]:  # one cancels the other
            for other in self.orders.values():
                if other["ocoId"] == o["ocoId"] and other["id"] != o["id"] and other["ordStatus"] == "Working":
                    other["ordStatus"] = "Canceled"
        self._realize(o, price, qty)

    def _realize(self, o: dict, price: float, qty: int) -> None:
        """Cash moves on closing fills: match against the account's earlier opposite fills (FIFO, net)."""
        code = self.contracts[o["contractId"]]["name"]
        net, avg = self._net(code, exclude=self.fills[-1]["id"])
        side = 1 if o["action"] == "Buy" else -1
        if net and (net > 0) != (side > 0):
            closing = min(abs(net), qty)
            self.cash += (price - avg) * closing * (1 if net > 0 else -1) * PRODUCTS[code[:-2]][1]

    def _net(self, code: str, exclude: int | None = None) -> tuple[int, float]:
        net, cost = 0, 0.0
        for f in self.fills:
            if f["id"] == exclude or self.contracts[f["contractId"]]["name"] != code:
                continue
            s = f["qty"] if f["action"] == "Buy" else -f["qty"]
            if net == 0 or (net > 0) == (s > 0):
                cost, net = cost + f["price"] * s, net + s
            else:
                new = net + s
                cost = 0.0 if new == 0 else (cost / net * new if (new > 0) == (net > 0) else f["price"] * new)
                net = new
        return net, (cost / net if net else 0.0)

    # -- endpoints --------------------------------------------------------------------------------------

    def __call__(self, method: str, url: str, headers: dict, body: dict | None, timeout: float):
        path, _, query = url.partition("/v1")[2].partition("?")
        params = dict(kv.split("=", 1) for kv in query.split("&") if kv)
        self.calls.append((method, path))
        if self.penalty_next and not (body or {}).get("p-ticket"):
            wait, self.penalty_next = self.penalty_next, 0.0
            return 200, {"p-ticket": "TICKET", "p-time": wait}
        if path == "/auth/accesstokenrequest":
            if body.get("password") != "pw":
                return 200, {"errorText": "Incorrect username or password"}
            return 200, self._token()
        if headers.get("Authorization") != f"Bearer {self.token}" or self.expire_token:
            self.expire_token = False
            return 401, None
        if path == "/auth/renewaccesstoken":
            return 200, self._token()
        if method == "GET":
            return 200, self._get(path, params)
        return 200, self._post(path, body)

    def _token(self) -> dict:
        self.token = f"T{next(self.ids)}"
        exp = (self.now() + dt.timedelta(minutes=80)).isoformat().replace("+00:00", "Z")
        return {"accessToken": self.token, "mdAccessToken": "MD" + self.token, "expirationTime": exp, "userId": 7}

    def _get(self, path: str, p: dict):
        if path == "/account/list":
            return self.accounts
        if path == "/contract/find":
            return next((c for c in self.contracts.values() if c["name"] == p["name"]), None)
        if path == "/contract/item":
            return self.contracts[int(p["id"])]
        if path == "/contractMaturity/item":
            return self.maturities[int(p["id"])]
        if path == "/product/item":
            return self.products[int(p["id"])]
        if path == "/order/list":
            return list(self.orders.values())
        if path == "/orderVersion/list":
            return list(self.versions.values())
        if path == "/fill/list":
            return self.fills
        if path == "/position/list":
            out = []
            for c in self.contracts.values():
                net, avg = self._net(c["name"])
                fnet, fpx = self.foreign.get(c["name"], (0, 0.0))
                if net or fnet:
                    tot = net + fnet
                    out.append({"accountId": 1, "contractId": c["id"], "netPos": tot,
                                "netPrice": ((avg * net + fpx * fnet) / tot) if tot else 0.0})
            return out
        raise AssertionError(f"fake Tradovate: no GET {path}")

    def _post(self, path: str, b: dict):
        if path == "/cashBalance/getcashbalancesnapshot":
            return {"accountId": b["accountId"], "totalCashValue": self.cash, "netLiq": self.cash, "openPnL": 0.0}
        if self.drop_next_before_send:
            self.drop_next_before_send = False
            raise TimeoutError("fake: dropped before it reached the platform")
        if self.reject_next is not None:
            text, self.reject_next = self.reject_next, None
            return {"failureReason": "UnknownReason", "failureText": text}
        out = self._command(path, b)
        if self.lose_answer_next:
            self.lose_answer_next = False
            raise TimeoutError("fake: answer lost")
        return out

    def _command(self, path: str, b: dict):
        if path in ("/order/placeorder", "/order/placeoso"):
            assert b.get("isAutomated") is True, "automated orders must say so"
            entry = self._new_order(b)
            out = {"failureReason": "Success", "orderId": entry["id"]}
            if path == "/order/placeoso":
                oco = next(self.ids)
                for key, n in (("bracket1", "oso1Id"), ("bracket2", "oso2Id")):
                    if key in b:
                        leg = self._new_order({**b[key], "symbol": b["symbol"], "orderQty": b["orderQty"]},
                                              status="Suspended", parent=entry["id"])
                        leg["ocoId"] = oco if "bracket2" in b else None
                        out[n] = leg["id"]
            if b["orderType"] == "Market" and self.fill_market:
                bid, ask = self.quotes[b["symbol"]]
                self._fill(entry, ask if b["action"] == "Buy" else bid)
            return out
        if path == "/order/cancelorder":
            o = self.orders.get(b["orderId"])
            if o is None or o["ordStatus"] not in ("Working", "Suspended"):
                return {"failureReason": "UnknownReason", "failureText": "order is not working"}
            o["ordStatus"] = "Canceled"
            for child in self.orders.values():
                if child["parentId"] == o["id"] and child["ordStatus"] == "Suspended":
                    child["ordStatus"] = "Canceled"
            return {"failureReason": "Success", "commandId": next(self.ids)}
        if path == "/order/modifyorder":
            o = self.orders.get(b["orderId"])
            if o is None or o["ordStatus"] not in ("Working", "Suspended"):
                return {"failureReason": "UnknownReason", "failureText": "order is not working"}
            v = dict(self.versions[o["id"]], id=next(self.ids), orderQty=b["orderQty"], orderType=b["orderType"])
            for k in ("price", "stopPrice"):
                if k in b:
                    v[k] = b[k]
            self.versions[o["id"]] = v
            return {"failureReason": "Success", "commandId": next(self.ids)}
        raise AssertionError(f"fake Tradovate: no POST {path}")


START_PRICES = {"MES": 5000.0, "ES": 5000.0, "MNQ": 18000.0, "NQ": 18000.0, "MYM": 40000.0, "YM": 40000.0,
                "MGC": 2400.0, "GC": 2400.0, "MCL": 75.0, "CL": 75.0}


class FakeQuoteBook(QuoteBook):
    """Live-looking quotes for drills: each read moves the price by up to a tick and stamps the current time."""

    def __init__(self, fake: FakeTradovate, seed: int = 7):
        super().__init__()
        self.fake, self.rng = fake, random.Random(seed)
        self.mid: dict[str, float] = {}

    def tick(self, contract: str):
        root = contract[:-2]
        tick = PRODUCTS[root][0]
        mid = self.mid.get(contract, START_PRICES[root]) + self.rng.choice((-tick, 0.0, tick))
        self.mid[contract] = mid
        self.names[self.fake.cid(contract)] = contract
        self.fake.set_quote(contract, mid, mid + tick)
        self.apply(self.fake.quote_message(contract, mid, mid + tick))
        return super().tick(contract)
