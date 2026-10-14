"""Where a price came from (futures-first, docs/futures.md "Market data").

Every market state the engine journals carries a ``DataProvenance``: the
source, the mode, the contract it is a price of, the session it fell in, how
old it was and whether the series is adjusted. A backtest or replay records the
same fields, so a decision can always be traced to the data it saw. Futures
history is per dated contract; a continuous series must say how it was
adjusted, and an unadjusted roll is never presented as one series.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

MODES = ("live", "paper", "replay", "historical", "backtest")
ADJUSTMENTS = ("none", "back_adjusted", "ratio_adjusted")


@dataclass(frozen=True)
class DataProvenance:
    source: str  # e.g. ninjatrader-md-websocket, fake-ninjatrader, a CSV path's name
    mode: str  # live | paper | replay | historical | backtest
    contract: str | None  # the dated contract (MESZ6); None for a continuous series
    adjustment: str = "none"  # a continuous futures series must say how it was stitched
    session: str | None = None  # the exchange session phase the price fell in (rth, eth, closed)
    as_of: dt.datetime | None = None  # the price's own timestamp
    age_s: float | None = None  # seconds between that timestamp and the decision
    quality: str = "unchecked"  # ok | stale | gap | unchecked

    def problems(self) -> list[str]:
        out = []
        if self.mode not in MODES:
            out.append("unknown_mode")
        if self.adjustment not in ADJUSTMENTS:
            out.append("unknown_adjustment")
        if self.contract is None and self.adjustment == "none":
            out.append("continuous_series_without_adjustment")
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat() if self.as_of else None
        return d
