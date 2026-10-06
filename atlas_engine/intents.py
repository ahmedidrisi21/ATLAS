"""The trade intent: the only way anything proposes a trade to the engine (PRD v3 §10, §15).

A rule-based strategy and a Hermes agent propose trades in the same shape.
An intent says what the proposer wants (strategy, symbol, side, where the
stop and target are, and why). It never says how much: the engine sizes
every trade, and an intent that tries to set size, risk, exposure, a
prop-rule override, credentials or raw order parameters is rejected whole
rather than having those fields ignored (§10).

The JSON form (``parse_intent``), as in §10:

    {"strategy": "trend_pullback", "strategy_version": "12", "symbol": "EURUSD",
     "side": "BUY", "setup": {"entry": 1.0850, "stop": 1.0830, "target": 1.0898},
     "reason": "Trend continuation after controlled pullback"}

``setup.entry`` is informational: a market order fills at the live quote,
and the engine measures the stop and target from that quote. A target may be
given as a price (``target``) or as a multiple of the stop distance (``rr``),
which is how rule-based strategies set it.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = "atlas-intent-1"

SOURCES = ("rules", "agent")
SIDES = {"BUY": 1, "SELL": -1}
TOP_KEYS = {"intent_id", "strategy", "strategy_version", "symbol", "side", "setup", "reason", "p_estimate"}
SETUP_KEYS = {"entry", "stop", "target", "rr"}
# Fields a proposer may never set (§10). Matched anywhere in the intent, case-insensitively.
FORBIDDEN = {
    "volume", "lots", "lot", "quantity", "qty", "contracts", "size", "position_size",
    "risk", "risk_pct", "risk_amount", "risk_per_trade", "exposure", "max_exposure", "leverage",
    "override", "overrides", "prop_override", "ignore_limits", "skip_checks", "force",
    "credentials", "password", "api_key", "secret", "token", "login", "account",
    "magic", "deviation", "order_type", "type_filling", "filling", "comment", "ticket",
    # Broker order fields (Tradovate and alike): an intent names a product and prices, never an order command.
    "ordertype", "orderqty", "contractid", "contract_id", "accountid", "account_id", "accountspec", "clordid",
    "client_order_id", "isautomated", "timeinforce", "time_in_force", "bracket1", "bracket2", "stopprice",
}
NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
VERSION = re.compile(r"^[A-Za-z0-9_.-]{1,24}$")
INTENT_ID = re.compile(r"^[A-Za-z0-9_.:+-]{4,120}$")
MAX_REASON = 2000


class IntentRejected(ValueError):
    """The intent is malformed or asks for something a proposer may not set. ``code`` is stable."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class TradeIntent:
    strategy: str
    strategy_version: str
    symbol: str
    direction: int  # +1 buy, -1 sell
    stop: float
    decision_time: dt.datetime  # UTC; the bar close for a rule, the receipt time for an agent
    source: str = "rules"
    target: float | None = None  # absolute price, or
    rr: float | None = None  # target distance as a multiple of the stop distance from the fill
    entry: float | None = None  # what the proposer saw; informational
    reason: str = ""
    intent_id: str = ""
    p_estimate: float | None = None  # the proposer's own probability; recorded, used only where policy allows
    magic_offset: int = 0
    features: dict = field(default_factory=dict)  # normalised decision state (atlas_engine.decisions.state)

    @property
    def side(self) -> str:
        return "BUY" if self.direction == 1 else "SELL"

    @property
    def decision_id(self) -> str:
        """Stable per proposal; the same format rule signals have always used, so replays stay idempotent."""
        return f"{self.strategy}:{self.strategy_version}:{self.symbol}:{self.decision_time.isoformat()}:{self.direction:+d}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decision_time"] = self.decision_time.isoformat()
        d["side"] = self.side
        d["schema_version"] = SCHEMA_VERSION
        return d


def from_signal(sig) -> TradeIntent:
    """A rule strategy's ``atlas_engine.strategies.Signal`` as an intent."""
    return TradeIntent(strategy=sig.setup, strategy_version=sig.version, symbol=sig.symbol, direction=sig.direction,
                       stop=float(sig.stop), decision_time=sig.decision_time, source="rules", rr=float(sig.rr),
                       magic_offset=sig.magic_offset, features=dict(getattr(sig, "features", None) or {}),
                       reason=f"{sig.setup} rule fired at the bar close")


def _forbidden(obj, path: str = "") -> list[str]:
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()
            if key in FORBIDDEN:
                hits.append(f"{path}{k}")
            hits.extend(_forbidden(v, f"{path}{k}."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(_forbidden(v, f"{path}{i}."))
    return hits


def _price(name: str, v, required: bool) -> float | None:
    if v is None and not required:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise IntentRejected("invalid_price", f"setup.{name} must be a positive number")
    return float(v)


def parse_intent(raw: dict, *, now: dt.datetime, source: str = "agent", symbols=None) -> TradeIntent:
    """Validate the JSON form of §10. Raises ``IntentRejected``; never fills in a missing field by guessing."""
    if not isinstance(raw, dict):
        raise IntentRejected("not_an_object")
    bad = _forbidden(raw)
    if bad:
        raise IntentRejected("forbidden_field", ", ".join(sorted(bad)))
    extra = set(raw) - TOP_KEYS
    if extra:
        raise IntentRejected("unknown_field", ", ".join(sorted(extra)))
    if source not in SOURCES:
        raise IntentRejected("unknown_source", source)
    strategy, version = raw.get("strategy"), str(raw.get("strategy_version", ""))
    if not isinstance(strategy, str) or not NAME.match(strategy):
        raise IntentRejected("invalid_strategy", "lowercase letters, digits and '_'")
    if not VERSION.match(version):
        raise IntentRejected("invalid_strategy_version")
    symbol = str(raw.get("symbol", "")).upper()
    if symbols is not None and symbol not in symbols:
        raise IntentRejected("symbol_not_traded", symbol)
    side = str(raw.get("side", "")).upper()
    if side not in SIDES:
        raise IntentRejected("invalid_side", "BUY or SELL")
    setup = raw.get("setup")
    if not isinstance(setup, dict):
        raise IntentRejected("missing_setup")
    extra = set(setup) - SETUP_KEYS
    if extra:
        raise IntentRejected("unknown_field", ", ".join(f"setup.{k}" for k in sorted(extra)))
    stop = _price("stop", setup.get("stop"), True)
    target = _price("target", setup.get("target"), False)
    rr = setup.get("rr")
    if (target is None) == (rr is None):
        raise IntentRejected("target_or_rr", "give exactly one of setup.target or setup.rr")
    if rr is not None and (isinstance(rr, bool) or not isinstance(rr, (int, float)) or not 0 < rr <= 20):
        raise IntentRejected("invalid_rr", "setup.rr must be in (0, 20]")
    entry = _price("entry", setup.get("entry"), False)
    reason = raw.get("reason", "")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
        raise IntentRejected("invalid_reason", f"a reason of 1-{MAX_REASON} characters is required")
    p = raw.get("p_estimate")
    if p is not None and (isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 < p < 1):
        raise IntentRejected("invalid_p_estimate", "a probability strictly between 0 and 1")
    intent_id = str(raw.get("intent_id") or "")
    if intent_id and not INTENT_ID.match(intent_id):
        raise IntentRejected("invalid_intent_id")
    return TradeIntent(strategy=strategy, strategy_version=version, symbol=symbol, direction=SIDES[side], stop=stop,
                       decision_time=now.replace(microsecond=0), source=source, target=target,
                       rr=None if rr is None else float(rr), entry=entry, reason=reason.strip(), intent_id=intent_id,
                       p_estimate=None if p is None else float(p))
