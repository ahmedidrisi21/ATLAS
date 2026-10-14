"""Research data access with the locked-holdout guard (PRD §13, §22).

Everything research code loads goes through here. Any request that reaches
into the holdout period raises, whoever asks. The human-run holdout step
(Phase T6) will get its own entry point outside the agent runtime; the
server-side refusal in ``atlas-backtest`` (Phase H2) is the real boundary,
and this guard keeps research code from ever needing holdout rows.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from atlas_engine.market_data import store


class HoldoutAccessError(PermissionError):
    pass


def check_not_holdout(end: pd.Timestamp, holdout_start: pd.Timestamp) -> None:
    if _utc(end) > _utc(holdout_start):
        raise HoldoutAccessError(
            f"requested data up to {end:%Y-%m-%d}, but the holdout starts {holdout_start:%Y-%m-%d}; "
            "research code never reads the holdout"
        )


def load_m1(root: Path, symbol: str, start, end, holdout_start) -> pd.DataFrame:
    check_not_holdout(pd.Timestamp(end), pd.Timestamp(holdout_start))
    return store.load_m1(root, symbol, _utc(start), _utc(end))


def load_research_m1(root: Path, cfg: dict, symbol: str, start, end) -> pd.DataFrame:
    """M1 bars for ``symbol`` under ``cfg``, holdout-guarded.

    A symbol listed under ``data.proxies`` is built from another series: e.g.
    ``MES: {source: USA500IDXUSD, spread: 0.25}`` takes the S&P 500 cash-index
    CFD's mid prices and quotes them at the future's own spread (one tick), so
    the CFD dealer's wider spread is not charged on top of the futures costs.
    """
    proxy = (cfg.get("data") or {}).get("proxies", {}).get(symbol)
    holdout = cfg["segments"]["holdout_start"]
    if proxy is None:
        return load_m1(root, symbol, start, end, holdout)
    return quoted_at_spread(load_m1(root, proxy["source"], start, end, holdout), float(proxy["spread"]))


def quoted_at_spread(m1: pd.DataFrame, spread: float) -> pd.DataFrame:
    """Replace each bar's bid/ask with its mid price -/+ half of a fixed ``spread``."""
    out = m1.copy()
    for c in "ohlc":
        mid = (m1[f"bid_{c}"] + m1[f"ask_{c}"]) / 2
        out[f"bid_{c}"] = mid - spread / 2
        out[f"ask_{c}"] = mid + spread / 2
    return out


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
