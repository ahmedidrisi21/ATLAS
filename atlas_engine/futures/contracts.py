"""Futures products and contracts (futures-first, docs/futures.md).

A *product* is what research and strategies talk about: ``MES``, a continuous
series. A *contract* is what the broker trades: ``MESZ6``, which expires. The
engine keys everything by the product (its ``symbol`` in config, signals and
the book); the execution adapter trades the pinned contract for it and reports
that contract's dates in ``SymbolRules.contract``, so the pipeline can refuse a
contract that has expired or is due to roll.

Sizing needs nothing new: a product becomes a ``ContractSpec`` with
``contract_size`` = value per point and whole-contract steps, so

    risk_per_contract = stop_distance / tick_size x tick_value (+ round-turn commission)
    contracts         = floor(allowed_risk / risk_per_contract)

is the same arithmetic the engine already runs (``atlas_engine.sizing.lots``).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from atlas_engine.sizing.lots import ContractSpec

MONTH_CODES = "FGHJKMNQUVXZ"  # Jan..Dec
_CATALOGUE = Path(__file__).with_name("products.yaml")
_KEYS = {"exchange", "currency", "tick_size", "point_value", "group", "underlying", "months", "session", "rth",
         "mini", "verified"}


@dataclass(frozen=True)
class FuturesProduct:
    root: str  # MES; also the research symbol (continuous series) and the engine's symbol
    exchange: str
    currency: str
    tick_size: float
    point_value: float  # money per 1.0 price move per contract (the multiplier)
    group: str  # asset group: positions in one group are one exposure, and opposite sides are a hedge
    underlying: str
    months: str  # listed month codes
    session: str  # session template in atlas_engine.futures.sessions
    rth: tuple[str, str]  # regular trading hours, exchange zone (a label; the calendar decides trading)
    mini: str | None = None  # the mini this micro shadows
    verified: bool = False  # catalogue row checked against an official source

    @property
    def tick_value(self) -> float:
        return round(self.tick_size * self.point_value, 10)

    @property
    def mini_equivalent(self) -> float:
        """Prop firms count contracts in minis; a micro is a tenth."""
        return 0.1 if self.mini else 1.0

    @property
    def research_symbol(self) -> str:
        return self.root

    def spec(self, volume_max: float = 100.0, commission_per_contract: float = 0.0) -> ContractSpec:
        """The engine's sizing view: whole contracts, value per point as the contract size."""
        return ContractSpec(self.root, self.group, self.currency, self.tick_size, self.point_value,
                            volume_min=1.0, volume_step=1.0, volume_max=volume_max,
                            commission_per_lot=commission_per_contract, asset_class="future")


@dataclass(frozen=True)
class FuturesContract:
    """One tradable expiry, as the broker reports it."""

    symbol: str  # execution symbol, e.g. MESZ6
    root: str
    month: int
    year: int
    last_trade: dt.datetime | None  # UTC; None when the broker did not say (then nothing trades)
    first_notice: dt.date | None = None  # physically delivered products only

    @property
    def research_symbol(self) -> str:
        return self.root

    def entry_block(self, now: dt.datetime, roll_days: int) -> str | None:
        """Why no new position may open in this contract now, or None.

        Deterministic: past the last trade time it has expired; within ``roll_days`` calendar days of the
        last trade day or the first notice day the operator must pin the next contract. An unknown expiry
        is refused rather than guessed."""
        if self.last_trade is None:
            return "contract_expiry_unknown"
        if now >= self.last_trade:
            return "contract_expired"
        cutoff = self.last_trade.date() - dt.timedelta(days=roll_days)
        if self.first_notice is not None:
            cutoff = min(cutoff, self.first_notice - dt.timedelta(days=roll_days))
        if now.date() >= cutoff:
            return "contract_roll_due"
        return None

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "root": self.root, "month": self.month, "year": self.year,
                "last_trade": self.last_trade.isoformat() if self.last_trade else None,
                "first_notice": self.first_notice.isoformat() if self.first_notice else None}


@lru_cache(maxsize=1)
def products() -> dict[str, FuturesProduct]:
    raw = yaml.safe_load(_CATALOGUE.read_text())["products"]
    out = {}
    for root, p in raw.items():
        extra = set(p) - _KEYS
        if extra:
            raise ValueError(f"{_CATALOGUE}: {root}: unknown key(s) {sorted(extra)}")
        months = str(p["months"])
        if not months or any(m not in MONTH_CODES for m in months):
            raise ValueError(f"{_CATALOGUE}: {root}: bad months {months!r}")
        out[root] = FuturesProduct(root, str(p["exchange"]), str(p["currency"]), float(p["tick_size"]),
                                   float(p["point_value"]), str(p["group"]), str(p["underlying"]), months,
                                   str(p["session"]), tuple(p["rth"]), p.get("mini"), bool(p.get("verified")))
    for root, p in out.items():
        if p.mini is not None and (p.mini not in out or out[p.mini].group != p.group):
            raise ValueError(f"{_CATALOGUE}: {root}: mini {p.mini!r} must be a product in the same group")
    return out


def product(symbol: str) -> FuturesProduct:
    """The product for a root (``MES``) or a contract code (``MESZ6``)."""
    s = symbol.upper()
    cat = products()
    if s in cat:
        return cat[s]
    root = s[:-2]
    if len(s) > 2 and root in cat and s[-2] in MONTH_CODES and s[-1].isdigit():
        return cat[root]
    raise KeyError(f"unknown futures product {symbol!r}; add it to {_CATALOGUE.name}")


def is_futures(symbol: str) -> bool:
    try:
        product(symbol)
    except KeyError:
        return False
    return True


def parse_contract(code: str, today: dt.date) -> tuple[str, int, int]:
    """``MESZ6`` -> (``MES``, 12, 2026). The one-digit year is the first year >= today's year - 1 ending in it."""
    s = code.upper()
    p = product(s)
    if s == p.root:
        raise ValueError(f"{code!r} is a product, not a contract; pin a contract such as {p.root}Z6")
    letter, digit = s[-2], int(s[-1])
    if letter not in p.months:
        raise ValueError(f"{code!r}: {p.root} does not list month {letter} (lists {p.months})")
    year = today.year - 1
    while year % 10 != digit:
        year += 1
    return p.root, MONTH_CODES.index(letter) + 1, year


def check_against_broker(p: FuturesProduct, tick_size: float, point_value: float) -> str | None:
    """A difference between the catalogue and the broker's own numbers, or None."""
    if abs(tick_size - p.tick_size) > 1e-9 or abs(point_value - p.point_value) > 1e-9:
        return (f"{p.root}: broker reports tick {tick_size} / {point_value} per point, catalogue says "
                f"{p.tick_size} / {p.point_value}")
    return None
