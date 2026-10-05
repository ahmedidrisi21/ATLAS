"""The common execution interface (PRD v3 §14, §15, §30).

Every platform adapter implements ``ExecutionBroker``. The trading engine is
its only caller, and only after the decision pipeline returned ALLOW (or, for
closes and flattening, after the risk engine, a kill or the operator asked).
Hermes, the MCP servers and the API never import this module or an adapter:
``tests/engine/test_v3_boundaries.py`` fails the build if one does.

    submit_order(order, now)      one market order with its SL and TP attached
    get_orders()                  pending orders at the platform
    get_positions()               open positions at the platform
    cancel_order(ticket)          remove one ATLAS pending order
    modify_position(pos, sl, tp)  move a position's SL (tighten only) or restore SL/TP
    get_account_state()           balance, equity, demo flag, trading allowed
    reconcile(book)               platform vs engine book differences (deterministic)
    flatten(reason)               close every ATLAS position

``MT5ExecutionAdapter`` is the one implemented now: the MetaTrader 5 terminal
on the engine's Windows host, through ``atlas_engine.adapters.mt5`` (the only
code that calls the ``MetaTrader5`` package) and the T4 ``Executor`` (retries,
deviation, unknown-outcome handling, never an unprotected position). cTrader
and futures (Tradovate) adapters are placeholders in ``PLATFORMS`` until a
market that needs them is chosen. Credentials live only in the adapter's host
environment, never in this repo or in anything an agent can read (§30).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from atlas_engine.adapters.broker import AccountInfo, BrokerPosition
from atlas_engine.reconciliation import diff, is_atlas

from .executor import EntryOrder, ExecResult, Executor
from .settings import ExecutionSettings

# Platform name -> adapter class, or None while not built. The engine refuses a platform with no adapter.
PLATFORMS: dict[str, type | None] = {"mt5": None, "ctrader": None, "tradovate": None}


@runtime_checkable
class ExecutionBroker(Protocol):
    platform: str

    def submit_order(self, order: EntryOrder, now) -> ExecResult: ...
    def get_orders(self) -> list[dict]: ...
    def get_positions(self) -> list[BrokerPosition]: ...
    def cancel_order(self, ticket: int) -> ExecResult: ...
    def modify_position(self, pos: BrokerPosition, sl: float, tp: float | None = None) -> ExecResult: ...
    def close_position(self, pos: BrokerPosition, reason: str) -> ExecResult: ...
    def get_account_state(self) -> AccountInfo: ...
    def reconcile(self, book, points: dict[str, float]) -> list: ...
    def flatten(self, positions: list[BrokerPosition], reason: str) -> list[ExecResult]: ...


class MT5ExecutionAdapter:
    """``ExecutionBroker`` over an ``MT5Adapter`` (or the fake terminal in tests)."""

    platform = "mt5"

    def __init__(self, broker, settings: ExecutionSettings):
        self.broker, self.settings = broker, settings
        self.executor = Executor(broker, settings)

    def submit_order(self, order: EntryOrder, now) -> ExecResult:
        return self.executor.open(order, now)

    def get_orders(self) -> list[dict]:
        return self.broker.pending_orders()

    def get_positions(self) -> list[BrokerPosition]:
        return self.broker.positions()

    def cancel_order(self, ticket: int) -> ExecResult:
        """ATLAS sends market orders only, so this exists for orders a kill finds at the platform with an
        ATLAS magic number. Anything else is refused: the operator owns foreign orders."""
        order = next((o for o in self.get_orders() if o["ticket"] == ticket), None)
        if order is None:
            return ExecResult("rejected", "", f"no pending order {ticket}")
        base = self.settings.magic_base
        if not base <= order["magic"] < base + 1000:
            return ExecResult("rejected", order["comment"], "not an ATLAS order; left for the operator")
        res = self.broker.send({"action": self.broker.mt5.TRADE_ACTION_REMOVE, "order": int(ticket)})
        ok = res.status == "done"
        return ExecResult("cancelled" if ok else "failed", order["comment"], "" if ok else f"remove {res.retcode}: {res.comment}",
                          retcode=res.retcode, attempts=1)

    def modify_position(self, pos: BrokerPosition, sl: float, tp: float | None = None) -> ExecResult:
        if tp is None:
            return self.executor.modify_stop(pos, sl)
        ok = self.executor.restore_stops(pos, sl, tp)
        return ExecResult("filled" if ok else "failed", pos.comment, "" if ok else "could not set SL/TP",
                          ticket=pos.ticket, sl=sl, tp=tp)

    def close_position(self, pos: BrokerPosition, reason: str) -> ExecResult:
        return self.executor.close(pos, reason)

    def get_account_state(self) -> AccountInfo:
        return self.broker.account()

    def reconcile(self, book, points: dict[str, float]) -> list:
        return diff(book, self.get_positions(), self.settings.magic_base, points)

    def flatten(self, positions: list[BrokerPosition], reason: str) -> list[ExecResult]:
        return self.executor.flatten(positions, reason)

    def cancel_atlas_orders(self) -> list[ExecResult]:
        base = self.settings.magic_base
        return [self.cancel_order(o["ticket"]) for o in self.get_orders() if base <= o["magic"] < base + 1000]

    def owns(self, pos: BrokerPosition) -> bool:
        return is_atlas(pos, self.settings.magic_base)


PLATFORMS["mt5"] = MT5ExecutionAdapter


def execution_adapter(platform: str, broker, settings: ExecutionSettings) -> ExecutionBroker:
    cls = PLATFORMS.get(platform)
    if cls is None:
        built = sorted(k for k, v in PLATFORMS.items() if v is not None)
        raise ValueError(f"no execution adapter for {platform!r}; built: {', '.join(built)}")
    return cls(broker, settings)
