"""Tradovate, the first futures platform (futures-first, docs/futures.md).

Imported only by the engine host (``atlas_api.engine_cli``) and the execution
adapter registry; Hermes, the MCP servers, setups, strategies and research
never import it (tests/engine/test_v3_boundaries.py).
"""

from .adapter import TradovateAdapter
from .client import Credentials, OutcomeUnknown, RateLimited, TradovateClient, TradovateError
from .fake import FakeTradovate
from .quotes import QuoteBook, QuoteStream, parse_frame

__all__ = ["Credentials", "FakeTradovate", "OutcomeUnknown", "QuoteBook", "QuoteStream", "RateLimited",
           "TradovateAdapter", "TradovateClient", "TradovateError", "parse_frame"]
