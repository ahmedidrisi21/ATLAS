"""What every futures platform adapter shares (futures-first, docs/futures.md).

The engine reads a broker through one small interface (connect, account,
positions, symbol_rules, tick, recent_deals, pending_orders); MT5 answers it
natively. A futures account is different: it nets each contract into one
position, and a protective stop or target is a separate working order. So a
futures adapter keeps ATLAS' own record of each *bracket* (entry, stop and
target orders placed together for one decision) and rebuilds the engine's view
from the platform's orders, fills and net positions:

    one ATLAS bracket with filled contracts   -> one BrokerPosition
                                                 (ticket = entry order id,
                                                 sl/tp = the working stop/target)
    anything else net on a contract           -> one foreign BrokerPosition
                                                 (the operator's; reconciliation HALTs)
    fills                                     -> BrokerDeals (in / out, sl / tp)

``FuturesVenue`` does that rebuild and the contract checks. A platform
subclass (``atlas_engine.adapters.tradovate.TradovateAdapter``) supplies the
primitives: its orders, fills and net positions in the generic vocabulary below,
and the order commands. Platform types never leave the subclass. Only the
execution adapter (``atlas_engine.execution.futures``) sends orders.

Generic order vocabulary: side +1 BUY / -1 SELL; type MARKET, LIMIT, STOP,
STOP_LIMIT; status working, filled, cancelled, rejected.
"""

from __future__ import annotations

import datetime as dt
import json
import zlib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from atlas_engine.adapters.broker import AccountInfo, BrokerDeal, BrokerPosition, BrokerUnavailable, SymbolRules, Tick
from atlas_engine.futures import FuturesContract, check_against_broker, product

ORDER_TYPES = ("MARKET", "LIMIT", "STOP", "STOP_LIMIT")
SIDES = {1: "BUY", -1: "SELL"}
FINAL = ("filled", "cancelled", "rejected")


class ContractMismatch(RuntimeError):
    """The broker's contract differs from what ATLAS sizes with; the symbol must not trade."""


@dataclass(frozen=True)
class VenueOrder:
    id: int
    contract: str  # execution symbol, e.g. MESZ6
    side: int
    qty: float
    type: str
    status: str  # working | filled | cancelled | rejected
    price: float | None = None
    stop_price: float | None = None
    time: dt.datetime | None = None


@dataclass(frozen=True)
class VenueFill:
    id: int
    order_id: int
    contract: str
    side: int
    qty: float
    price: float
    time: dt.datetime


@dataclass(frozen=True)
class VenueAck:
    ok: bool
    ids: dict = field(default_factory=dict)  # role -> order id: entry / stop / target, or order
    reason: str = ""
    unknown: bool = False  # no answer: the command may or may not have reached the platform


@dataclass
class Bracket:
    """ATLAS' record of one decision's orders, written before anything is sent."""

    client_id: str
    root: str
    contract: str
    direction: int
    qty: float
    magic: int
    stop: float
    target: float
    sent_at: str
    status: str = "sending"  # sending | live | closed | failed
    entry_id: int | None = None
    stop_id: int | None = None
    target_id: int | None = None
    exit_ids: list = field(default_factory=list)

    def order_ids(self) -> list[int]:
        return [i for i in (self.entry_id, self.stop_id, self.target_id, *self.exit_ids) if i is not None]


class BracketLedger:
    """Durable ``client_id -> Bracket``. The platform's order list does not carry the client order ID back,
    so without this file a restart could not tell ATLAS' orders from the operator's."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.items: dict[str, Bracket] = {}
        if self.path and self.path.exists():
            for cid, b in json.loads(self.path.read_text()).items():
                self.items[cid] = Bracket(**b)

    def save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self.items.items()}, indent=1))
        tmp.replace(self.path)

    def put(self, b: Bracket) -> None:
        self.items[b.client_id] = b
        self.save()

    def get(self, client_id: str) -> Bracket | None:
        return self.items.get(client_id)

    def by_entry(self, entry_id: int) -> Bracket | None:
        return next((b for b in self.items.values() if b.entry_id == entry_id), None)

    def owner(self, order_id: int) -> Bracket | None:
        return next((b for b in self.items.values() if order_id in b.order_ids()), None)

    def active(self) -> list[Bracket]:
        return [b for b in self.items.values() if b.status in ("sending", "live")]


def foreign_ticket(contract: str) -> int:
    """A stable negative ticket for the non-ATLAS remainder on a contract (never collides with order ids)."""
    return -(zlib.crc32(contract.encode()) or 1)


class FuturesVenue:
    """Base for futures platform adapters: the engine's read interface, rebuilt from orders and fills."""

    platform = "futures"
    clock = None  # every time here is UTC already; there is no broker server clock to convert

    def __init__(self, contracts: dict[str, str], ledger_path: str | Path | None = None,
                 commission_per_contract: dict | None = None):
        self.pins = dict(contracts)  # root -> contract code
        self.ledger = BracketLedger(ledger_path)
        self.commission = dict(commission_per_contract or {})  # root -> round turn per contract
        self.contracts: dict[str, FuturesContract] = {}
        self.last_ticks: dict[str, Tick] = {}

    # -- primitives a platform supplies ---------------------------------------------------------------

    def _connect(self) -> None: raise NotImplementedError
    def connected(self) -> bool: raise NotImplementedError
    def account(self) -> AccountInfo: raise NotImplementedError
    def _orders(self) -> list[VenueOrder]: raise NotImplementedError
    def _fills(self) -> list[VenueFill]: raise NotImplementedError
    def _net_positions(self) -> dict[str, tuple[float, float]]: raise NotImplementedError  # code -> (signed qty, avg)
    def _resolve_contract(self, code: str) -> tuple[FuturesContract, float, float]: raise NotImplementedError
    def _quote(self, contract: str) -> Tick: raise NotImplementedError
    def refresh(self) -> None: """Drop cached reads, after a write."""

    # Order commands (called by atlas_engine.execution.futures only).
    def place_bracket(self, contract: str, side: int, qty: float, stop: float, target: float,
                      client_id: str) -> VenueAck: raise NotImplementedError
    def place_order(self, contract: str, side: int, qty: float, type: str, client_id: str, price: float | None = None,
                    stop_price: float | None = None) -> VenueAck: raise NotImplementedError
    def cancel(self, order_id: int) -> VenueAck: raise NotImplementedError
    def modify(self, order_id: int, qty: float, type: str, price: float | None = None,
               stop_price: float | None = None) -> VenueAck: raise NotImplementedError

    def adopt(self, b: Bracket) -> bool:
        """After a send with no answer: find the bracket's orders on the platform and record them.

        Matches by contract, side, quantity, type and price among orders no bracket owns, placed since the
        send. True when exactly one entry matched (its stop and target are taken when found too). False when
        none did: the bracket is marked failed, nothing reached the platform. An ambiguous match stays
        ``sending``; whatever it filled shows as a foreign position, and reconciliation HALTs."""
        self.refresh()
        owned = {i for x in self.ledger.items.values() for i in x.order_ids()}
        since = dt.datetime.fromisoformat(b.sent_at)
        free = [o for o in self._orders() if o.id not in owned and o.contract == b.contract
                and (o.time is None or o.time >= since)]
        entries = [o for o in free if o.side == b.direction and o.type == "MARKET" and abs(o.qty - b.qty) < 1e-9]
        if len(entries) != 1:
            if not entries:
                b.status = "failed"
                self.ledger.save()
            return False
        stops = [o for o in free if o.side == -b.direction and o.type == "STOP" and o.stop_price == b.stop]
        targets = [o for o in free if o.side == -b.direction and o.type == "LIMIT" and o.price == b.target]
        b.entry_id = entries[0].id
        b.stop_id = stops[0].id if len(stops) == 1 else None
        b.target_id = targets[0].id if len(targets) == 1 else None
        self.ledger.save()
        return True

    # -- the engine's read interface ------------------------------------------------------------------

    def connect(self) -> None:
        self._connect()
        for root, code in self.pins.items():
            c, tick_size, point_value = self._resolve_contract(code)
            bad = check_against_broker(product(root), tick_size, point_value)
            if bad:
                raise ContractMismatch(bad)
            self.contracts[root] = c

    def root_of(self, contract: str) -> str:
        return next((r for r, c in self.pins.items() if c == contract), contract)

    def contract_of(self, root: str) -> str:
        if root not in self.pins:
            raise BrokerUnavailable(f"{root}: no contract pinned for this product")
        return self.pins[root]

    def symbol_rules(self, symbol: str) -> SymbolRules:
        p = product(symbol)
        digits = max(0, -int(f"{p.tick_size:e}".split("e")[1]))
        return SymbolRules(p.root, p.tick_size, digits, 0, 0, 0, "full",
                           p.spec(commission_per_contract=float(self.commission.get(p.root, 0.0))),
                           self.contracts.get(p.root))

    def tick(self, symbol: str) -> Tick:
        t = self._quote(self.contract_of(symbol))
        t = replace(t, symbol=symbol)
        self.last_ticks[symbol] = t
        return t

    def m1_rates(self, symbol: str, count: int):
        raise BrokerUnavailable(f"{self.platform}: bars are not wired yet (docs/futures.md, gaps)")

    def _book(self):
        orders = {o.id: o for o in self._orders()}
        fills: dict[int, list[VenueFill]] = {}
        for f in self._fills():
            fills.setdefault(f.order_id, []).append(f)
        return orders, fills

    @staticmethod
    def _qty(fills: dict, ids) -> tuple[float, float, list[VenueFill]]:
        got = [f for i in ids if i is not None for f in fills.get(i, [])]
        q = sum(f.qty for f in got)
        avg = sum(f.qty * f.price for f in got) / q if q else 0.0
        return q, avg, sorted(got, key=lambda f: f.time)

    def _bracket_state(self, b: Bracket, orders: dict, fills: dict) -> tuple[float, float, list, list]:
        """(open qty, avg entry, entry fills, exit fills)."""
        q_in, avg, f_in = self._qty(fills, [b.entry_id])
        q_out, _, f_out = self._qty(fills, [b.stop_id, b.target_id, *b.exit_ids])
        return q_in - q_out, avg, f_in, f_out

    def _sync(self, orders: dict, fills: dict) -> None:
        """Mark brackets live, closed or failed from what the platform reports."""
        changed = False
        for b in self.ledger.active():
            if b.entry_id is None:
                continue
            open_qty, _, f_in, _ = self._bracket_state(b, orders, fills)
            entry = orders.get(b.entry_id)
            legs_working = any(orders.get(i) and orders[i].status == "working" for i in b.order_ids() if i != b.entry_id)
            if f_in and open_qty <= 1e-9 and not legs_working:
                b.status, changed = "closed", True
            elif not f_in and entry is not None and entry.status in ("cancelled", "rejected"):
                b.status, changed = "failed", True
            elif f_in and b.status == "sending":
                b.status, changed = "live", True
        if changed:
            self.ledger.save()

    def positions(self) -> list[BrokerPosition]:
        orders, fills = self._book()
        self._sync(orders, fills)
        out, ours = [], {}
        for b in self.ledger.active():
            open_qty, avg, f_in, _ = self._bracket_state(b, orders, fills)
            if open_qty <= 1e-9:
                continue
            ours[b.contract] = ours.get(b.contract, 0.0) + b.direction * open_qty
            stop = orders.get(b.stop_id) if b.stop_id else None
            target = orders.get(b.target_id) if b.target_id else None
            sl = b.stop if stop is not None and stop.status == "working" else 0.0
            tp = b.target if target is not None and target.status == "working" else 0.0
            p = product(b.root)
            t = self.last_ticks.get(b.root)
            profit = b.direction * ((t.bid if b.direction == 1 else t.ask) - avg) * open_qty * p.point_value if t else 0.0
            out.append(BrokerPosition(b.entry_id, b.root, b.direction, open_qty, avg, sl, tp, b.magic, b.client_id,
                                      f_in[0].time, round(profit, 2), 0.0))
        for code, (net, avg) in self._net_positions().items():
            rest = net - ours.pop(code, 0.0)
            if abs(rest) > 1e-9:
                out.append(BrokerPosition(foreign_ticket(code), self.root_of(code), 1 if rest > 0 else -1, abs(rest), avg,
                                          0.0, 0.0, 0, "", dt.datetime.now(dt.timezone.utc), 0.0, 0.0))
        for code, mine in ours.items():  # ATLAS thinks it holds contracts the platform shows flat: a mismatch
            out.append(BrokerPosition(foreign_ticket(code), self.root_of(code), -1 if mine > 0 else 1, abs(mine), 0.0,
                                      0.0, 0.0, 0, "", dt.datetime.now(dt.timezone.utc), 0.0, 0.0))
        return out

    def position(self, ticket: int) -> BrokerPosition | None:
        return next((p for p in self.positions() if p.ticket == ticket), None)

    def net_position(self, root: str) -> float:
        return self._net_positions().get(self.contract_of(root), (0.0, 0.0))[0]

    def pending_orders(self) -> list[dict]:
        out = []
        for o in self._orders():
            if o.status != "working":
                continue
            b = self.ledger.owner(o.id)
            role = None if b is None else {b.entry_id: "entry", b.stop_id: "stop", b.target_id: "target"}.get(o.id, "exit")
            out.append({"ticket": o.id, "symbol": self.root_of(o.contract), "magic": b.magic if b else 0,
                        "comment": b.client_id if b else "", "role": role, "type": o.type, "side": SIDES[o.side],
                        "volume": o.qty, "price": o.price, "stop_price": o.stop_price})
        return out

    def recent_deals(self, now: dt.datetime) -> list[BrokerDeal]:
        orders, fills = self._book()
        out = []
        for b in self.ledger.items.values():
            if b.entry_id is None:
                continue
            _, avg, f_in, f_out = self._bracket_state(b, orders, fills)
            p = product(b.root)
            half = float(self.commission.get(b.root, 0.0)) / 2
            for f in f_in:
                out.append(BrokerDeal(f.id, f.order_id, b.entry_id, b.root, b.direction, "in", "expert", f.qty, f.price,
                                      -half * f.qty, 0.0, 0.0, f.time, b.magic, b.client_id))
            for f in f_out:
                reason = "sl" if f.order_id == b.stop_id else "tp" if f.order_id == b.target_id else "expert"
                pnl = b.direction * (f.price - avg) * f.qty * p.point_value
                out.append(BrokerDeal(f.id, f.order_id, b.entry_id, b.root, -b.direction, "out", reason, f.qty, f.price,
                                      -half * f.qty, 0.0, round(pnl, 2), f.time, b.magic, b.client_id))
        return sorted(out, key=lambda d: d.time)
