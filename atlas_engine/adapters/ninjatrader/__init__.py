"""NinjaTrader, the primary futures platform (futures-first, docs/futures.md).

NinjaTrader's official developer API (developer.ninjatrader.com/products/api,
checked 2026-10-06) is the Tradovate REST and WebSocket API: its docs link
serves the same "Tradovate API" specification, with hosts on tradovateapi.com,
and NinjaTrader Group owns Tradovate. So this one adapter is both the
``NinjaTraderAdapter`` and the ``TradovateAdapter`` of the PRD's adapter list;
there is no second copy. The NinjaTrader 8 desktop route (NinjaScript's
``Account`` class) is a different, C#-only API inside the desktop app and is
not built (docs/futures.md, "Routes to NinjaTrader").

Imported only by the engine host (``atlas_api.engine_cli``) and the execution
adapter registry; Hermes, the MCP servers, setups, strategies and research
never import it (tests/engine/test_v3_boundaries.py).
"""

from .adapter import NinjaTraderAdapter
from .client import Credentials, OutcomeUnknown, RateLimited, NinjaTraderClient, NinjaTraderError
from .fake import FakeNinjaTrader
from .quotes import QuoteBook, QuoteStream, parse_frame

__all__ = ["Credentials", "FakeNinjaTrader", "OutcomeUnknown", "QuoteBook", "QuoteStream", "RateLimited",
           "NinjaTraderAdapter", "NinjaTraderClient", "NinjaTraderError", "parse_frame"]
