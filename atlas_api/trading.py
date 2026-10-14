"""The engine API's trading routes: Hermes trading the demo account (PRD §6 ``atlas-trading``, §11).

    POST /v1/trading/market          trading:read   live quotes and closed bars from the broker
    POST /v1/trading/submit_intent   trading:demo   one trade the agent decided: symbol, side, stop, target, why
    POST /v1/trading/intent_status   trading:read   what became of an intent, and its result once closed
    POST /v1/trading/positions       trading:read   the agent's open positions, in R
    POST /v1/trading/close_position  trading:demo   close one of the agent's own positions
    POST /v1/trading/tighten_stop    trading:demo   move one of the agent's own stops towards the price
    POST /v1/trading/track_record    trading:read   closed trades in R after costs, by setup, and the verdict

Only the real engine serves these (``atlas-engine run``); the H3 simulator
doesn't. Every write is refused unless the broker reports a demo account,
and an intent is still refused while the operator hasn't enabled trading.
The engine sizes every trade; no argument sets a lot size or touches a limit.
"""

from __future__ import annotations

from atlas_engine.agent_intents import TIMEFRAMES, IntentRefused

from .auth import Principal
from .ops import OPS_ROUTES, OpsService
from .service import BadRequest, NotFound

TRADING_ROUTES = {
    "trading/market": "market",
    "trading/submit_intent": "submit_intent",
    "trading/intent_status": "intent_status",
    "trading/positions": "positions",
    "trading/close_position": "close_position",
    "trading/tighten_stop": "tighten_stop",
    "trading/track_record": "track_record",
}
ENGINE_ROUTES = {**OPS_ROUTES, **TRADING_ROUTES}
MAX_SYMBOLS, MAX_BARS = 4, 300
MIN_REASON, MAX_REASON = 10, 500


class EngineService(OpsService):
    """The operations routes plus the trading routes, over the real engine (atlas_engine.runtime)."""

    def _trade(self, fn, *a, **kw) -> dict:
        try:
            return self._call(fn, *a, **kw)
        except IntentRefused as e:
            raise BadRequest(str(e)) from None

    def market(self, p: Principal, symbols: list[str], timeframe: str = "H1", count: int = 100) -> dict:
        p.require("trading:read")
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= MAX_SYMBOLS or \
                not all(isinstance(s, str) for s in symbols):
            raise BadRequest(f"symbols must be a list of 1-{MAX_SYMBOLS} symbol names")
        syms = [s.upper() for s in symbols]
        unknown = [s for s in syms if s not in self.engine.cfg.symbols]
        if unknown:
            raise BadRequest(f"the engine does not trade {', '.join(unknown)}; symbols: {', '.join(self.engine.cfg.symbols)}")
        if timeframe not in TIMEFRAMES:
            raise BadRequest(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_BARS:
            raise BadRequest(f"count must be an int in 1..{MAX_BARS}")
        return self._trade(self.engine.agent_market, syms, timeframe, count)

    def submit_intent(self, p: Principal, intent_id: str, symbol: str, direction: str, stop: float, target: float,
                      confidence: float, thesis: str) -> dict:
        p.require("trading:demo")
        return self._trade(self.engine.agent_submit, p.name, intent_id, symbol, direction, stop, target, confidence,
                           thesis)

    def intent_status(self, p: Principal, intent_id: str) -> dict:
        p.require("trading:read")
        try:
            return self._trade(self.engine.agent_intent_status, str(intent_id))
        except KeyError as e:
            raise NotFound(str(e.args[0])) from None

    def positions(self, p: Principal) -> dict:
        p.require("trading:read")
        return self._trade(self.engine.agent_positions)

    def close_position(self, p: Principal, ticket: int, reason: str) -> dict:
        p.require("trading:demo")
        return self._trade(self.engine.agent_close, p.name, ticket, _reason(reason))

    def tighten_stop(self, p: Principal, ticket: int, new_stop: float, reason: str) -> dict:
        p.require("trading:demo")
        return self._trade(self.engine.agent_tighten, p.name, ticket, new_stop, _reason(reason))

    def track_record(self, p: Principal) -> dict:
        p.require("trading:read")
        return self._trade(self.engine.agent_record)


def _reason(reason) -> str:
    if not isinstance(reason, str) or not MIN_REASON <= len(reason.strip()) <= MAX_REASON:
        raise BadRequest(f"reason must be {MIN_REASON}-{MAX_REASON} characters")
    return reason.strip()
