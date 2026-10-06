"""The bounded decision-model interface (PRD v3 §7, §8, §24, §25).

A decision model estimates one thing: the probability that a setup reaches
its target before its stop. Rules, a gradient-boosted baseline, Jev and any
later model plug in behind the same call:

    model.evaluate_setup(setup, state) -> ModelEstimate | ModelFailure

``setup`` holds the trade's geometry in R (``strategy``, ``target_r``,
``cost_r``) and ``state`` the normalised features from
``atlas_engine.decisions.state``. Neither carries account balance, prices,
dates, symbols, credentials or anything that could place an order (§7).

The engine never calls a model directly. ``SafeModel`` wraps every model and
enforces the failure rules of §25: a timeout (500 ms by default), an
exception, an invalid schema or an out-of-range probability all become a
``ModelFailure``, and the engine turns a failure into REJECT. It never guesses.
Every estimate carries the versions §24 asks for, so a prediction can be traced
to its model, calibration, feature schema and strategy.
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable

from atlas_engine.decisions.state import FEATURE_SCHEMA_VERSION, REGIMES

DEFAULT_TIMEOUT_MS = 500.0
OPERATIONAL_FAILURES = ("timeout", "exception:", "schema:", "out_of_range", "transport_error", "unavailable")
REASON_CODE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
SETUP_KEYS = frozenset({"strategy", "strategy_version", "target_r", "cost_r"})
# What a model may never be handed (§7). Checked by key name on both arguments.
FORBIDDEN_INPUTS = frozenset({"balance", "equity", "account", "login", "password", "server", "credentials", "api_key",
                              "volume", "lots", "risk_amount", "symbol", "price", "entry", "stop", "target", "time",
                              "decision_time", "date"})


@dataclass(frozen=True)
class ModelEstimate:
    p_target_first: float
    model: str  # rules | gbm | jev | agent_stated | ...
    model_version: str
    calibration_version: str = "none"
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    regime: str | None = None
    reason_codes: tuple[str, ...] = ()
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["reason_codes"] = list(self.reason_codes)
        return d


@dataclass(frozen=True)
class ModelFailure:
    model: str
    reason: str  # timeout | exception:<type> | schema:<field> | out_of_range | unavailable | forbidden_input:<key> | ...
    latency_ms: float | None = None

    @property
    def ok(self) -> bool:
        return False

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class DecisionModel(Protocol):
    name: str
    version: str

    def evaluate_setup(self, setup: dict, state: dict) -> ModelEstimate | ModelFailure: ...


def check_inputs(setup: dict, state: dict) -> str | None:
    """The first input a model may not see, or None."""
    for k in list(setup) + list(state):
        if str(k).lower() in FORBIDDEN_INPUTS:
            return str(k)
    extra = set(setup) - SETUP_KEYS
    if extra:
        return sorted(extra)[0]
    return None


def validate(model_name: str, out, latency_ms: float) -> ModelEstimate | ModelFailure:
    """Schema and range checks on whatever a model returned (§25)."""
    if isinstance(out, ModelFailure):
        return ModelFailure(model_name, out.reason, latency_ms)
    if not isinstance(out, ModelEstimate):
        return ModelFailure(model_name, "schema:not_an_estimate", latency_ms)
    p = out.p_target_first
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p):
        return ModelFailure(model_name, "schema:p_target_first", latency_ms)
    if not 0.0 <= p <= 1.0:
        return ModelFailure(model_name, "out_of_range:p_target_first", latency_ms)
    if not out.model_version or out.model_version.endswith("latest"):
        return ModelFailure(model_name, "schema:model_version", latency_ms)
    if out.regime is not None and out.regime not in REGIMES:
        return ModelFailure(model_name, "schema:regime", latency_ms)
    if not all(isinstance(c, str) and REASON_CODE.match(c) for c in out.reason_codes):
        return ModelFailure(model_name, "schema:reason_codes", latency_ms)
    return ModelEstimate(float(p), out.model, out.model_version, out.calibration_version, out.feature_schema_version,
                         out.regime, tuple(out.reason_codes), latency_ms)


@dataclass
class SafeModel:
    """Runs a model with a hard timeout and validation. The engine only ever calls this."""

    model: DecisionModel
    timeout_ms: float = DEFAULT_TIMEOUT_MS
    history: deque = field(default_factory=lambda: deque(maxlen=100))  # (latency_ms, ok)

    def __post_init__(self):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"model-{self.name}")

    @property
    def name(self) -> str:
        return self.model.name

    def evaluate_setup(self, setup: dict, state: dict) -> ModelEstimate | ModelFailure:
        bad = check_inputs(setup, state)
        if bad is not None:
            return self._record(ModelFailure(self.name, f"forbidden_input:{bad}", 0.0))
        t0 = time.perf_counter()
        try:
            out = self._pool.submit(self.model.evaluate_setup, dict(setup), dict(state)).result(
                timeout=self.timeout_ms / 1000)
        except FutureTimeout:
            # The worker may still be running; a fresh pool keeps the next call from queueing behind it.
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"model-{self.name}")
            return self._record(ModelFailure(self.name, "timeout", (time.perf_counter() - t0) * 1000))
        except Exception as e:  # noqa: BLE001 - any model failure is a reject, never a guess
            return self._record(ModelFailure(self.name, f"exception:{type(e).__name__}",
                                             (time.perf_counter() - t0) * 1000))
        ms = (time.perf_counter() - t0) * 1000
        if ms > self.timeout_ms:
            return self._record(ModelFailure(self.name, "timeout", ms))
        return self._record(validate(self.name, out, ms))

    def _record(self, res):
        # Health counts only failures of the model itself (timeouts, crashes, bad answers), not a model
        # declining a strategy it has no estimate for.
        healthy = res.ok or not res.reason.startswith(OPERATIONAL_FAILURES)
        self.history.append((res.latency_ms or 0.0, healthy))
        return res

    def p95_ms(self) -> float | None:
        lat = sorted(ms for ms, _ in self.history)
        return lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None

    def failure_frac(self, last: int = 20) -> float:
        recent = list(self.history)[-last:]
        return sum(not ok for _, ok in recent) / len(recent) if recent else 0.0

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
