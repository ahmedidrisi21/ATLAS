"""The broker-neutral order (futures-first, docs/futures.md "Order model").

``OrderRequest`` is what the execution adapter hands a platform adapter. It is
built only inside ``atlas_engine.execution`` after the decision pipeline sized
the trade; a trade intent never carries one (``atlas_engine.intents.FORBIDDEN``
refuses the same field names). The platform adapter turns it into its own
command (NinjaTrader: an order-sends-order bracket, or a single order) and
nothing above it sees a platform field.

    symbol           the dated contract the order is for (MESZ6), resolved by the engine from the product
    side             BUY or SELL
    quantity         whole contracts, decided by the engine's sizing only
    order_type       MARKET | LIMIT | STOP | STOP_LIMIT
    limit_price      LIMIT and STOP_LIMIT
    stop_price       STOP and STOP_LIMIT
    time_in_force    DAY | GTC; a MARKET order has none
    reduce_only      may only shrink the position on the contract (protective legs and exits). No futures
                     platform ATLAS uses has this flag, so the venue checks it against the net position
                     before sending.
    stop_loss        with take_profit: send the entry as a bracket whose stop and target cancel each other
    take_profit
    client_order_id  deterministic (sha256 of the decision ID, ``executor.client_order_id``); the platform
                     stores it, so a retry after a lost answer finds the order instead of sending a second
    strategy_id      for the journal
    trade_intent_id  for the journal: which decision the order serves
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

ORDER_TYPES = ("MARKET", "LIMIT", "STOP", "STOP_LIMIT")
SIDE_NAMES = {1: "BUY", -1: "SELL"}
TIME_IN_FORCE = ("DAY", "GTC")


class InvalidOrder(ValueError):
    """The order is malformed; it is never sent."""


def _price(v) -> bool:
    return v is not None and not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v > 0


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    quantity: int
    order_type: str
    client_order_id: str
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str | None = None
    reduce_only: bool = False
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy_id: str = ""
    trade_intent_id: str = ""

    @property
    def direction(self) -> int:
        return 1 if self.side == "BUY" else -1

    @property
    def bracket(self) -> bool:
        return self.stop_loss is not None or self.take_profit is not None

    def problems(self) -> list[str]:
        """Why this order must not be sent; empty when it is well formed."""
        out = []
        if not self.symbol or not isinstance(self.symbol, str):
            out.append("symbol_missing")
        if self.side not in SIDE_NAMES.values():
            out.append("side_invalid")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity < 1:
            out.append("quantity_not_whole_contracts")
        if self.order_type not in ORDER_TYPES:
            out.append("order_type_invalid")
        if not self.client_order_id or len(self.client_order_id) > 64:
            out.append("client_order_id_invalid")
        needs_limit = self.order_type in ("LIMIT", "STOP_LIMIT")
        needs_stop = self.order_type in ("STOP", "STOP_LIMIT")
        if needs_limit != (self.limit_price is not None) or (needs_limit and not _price(self.limit_price)):
            out.append("limit_price_invalid")
        if needs_stop != (self.stop_price is not None) or (needs_stop and not _price(self.stop_price)):
            out.append("stop_price_invalid")
        if self.order_type == "MARKET" and self.time_in_force is not None:
            out.append("market_order_has_no_time_in_force")
        if self.order_type != "MARKET" and self.time_in_force not in TIME_IN_FORCE:
            out.append("time_in_force_invalid")
        if self.bracket:
            if not (_price(self.stop_loss) and _price(self.take_profit)):
                out.append("bracket_needs_stop_and_target")
            elif self.side in SIDE_NAMES.values() and self.direction * (self.take_profit - self.stop_loss) <= 0:
                out.append("bracket_stop_and_target_crossed")
            if self.reduce_only:
                out.append("bracket_cannot_be_reduce_only")
            if self.order_type != "MARKET":
                out.append("bracket_entry_must_be_market")
        return out

    def check(self) -> "OrderRequest":
        bad = self.problems()
        if bad:
            raise InvalidOrder(",".join(bad))
        return self

    def to_dict(self) -> dict:
        return asdict(self)


def entry_bracket(contract: str, direction: int, quantity: float, stop: float, target: float, client_order_id: str,
                  strategy_id: str = "", trade_intent_id: str = "") -> OrderRequest:
    return OrderRequest(contract, SIDE_NAMES[direction], _whole(quantity), "MARKET", client_order_id,
                        stop_loss=stop, take_profit=target, strategy_id=strategy_id, trade_intent_id=trade_intent_id)


def closing_order(contract: str, direction: int, quantity: float, order_type: str, client_order_id: str,
                  price: float | None = None, trade_intent_id: str = "") -> OrderRequest:
    """An order that only reduces a position held in ``direction``: a protective stop or target, or an exit."""
    return OrderRequest(contract, SIDE_NAMES[-direction], _whole(quantity), order_type, client_order_id,
                        limit_price=price if order_type == "LIMIT" else None,
                        stop_price=price if order_type == "STOP" else None,
                        time_in_force=None if order_type == "MARKET" else "GTC", reduce_only=True,
                        trade_intent_id=trade_intent_id)


def _whole(q: float) -> int:
    """Whole contracts. A fractional size never rounds into an order: it is refused as malformed."""
    return int(q) if float(q).is_integer() else q  # type: ignore[return-value]
