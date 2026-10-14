"""Jev as a bounded decision model (PRD v3 §7, §24, §25).

Jev is TypeSafe AI's external System One model (docs/open-questions-decisions.md),
reached through ``atlas_engine.adapters.jev.JevAdapter``. The adapter keeps
the §7 contract: a leakage guard on every request, pinned question wording and
model version, and a skip on any timeout, error or bad answer. This wrapper
turns the adapter's raw answer into a calibrated estimate with every version
attached. Jev gets the normalised state and the trade's geometry in R; it
never sees the account, the broker or an order.
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas_engine.adapters.jev import JevAdapter, JevResult, LeakageError
from atlas_engine.calibration import Calibrator, calibration_version

from .base import ModelEstimate, ModelFailure


@dataclass
class JevModel:
    adapter: JevAdapter
    calibrator: Calibrator | None = None  # isotonic over the last 500 closed trades (§24)
    name: str = "jev"

    @property
    def version(self) -> str:
        return self.adapter.model_version

    def evaluate_setup(self, setup: dict, state: dict) -> ModelEstimate | ModelFailure:
        setup_id = f"{setup.get('strategy')}:{setup.get('strategy_version')}"
        try:
            res = self.adapter.evaluate_setup(setup_id, dict(state), float(setup["target_r"]), float(setup["cost_r"]))
        except LeakageError as e:
            return ModelFailure(self.name, f"leakage:{str(e)[:60]}")
        if not isinstance(res, JevResult):
            return ModelFailure(self.name, res.reason, res.latency_ms)
        p, cal = res.p_target_first, "none"
        if self.calibrator is not None:
            p, cal = float(self.calibrator([p])[0]), calibration_version(self.calibrator)
        return ModelEstimate(p, self.name, res.model_version, cal, regime=res.regime,
                             reason_codes=tuple(c.upper() for c in res.reason_codes), latency_ms=res.latency_ms)
