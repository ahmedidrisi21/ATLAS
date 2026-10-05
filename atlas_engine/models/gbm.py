"""The gradient-boosted baseline as a live decision model (PRD v3 §8, §23).

Wraps a fitted classifier and its isotonic calibrator, as T2's ``GBMArm``
trains them (atlas_research/selection/arms.py). The classifier sees the same
normalised state the research run saw, built by
``atlas_engine.decisions.state`` and one-hot encoded with the same fixed
columns, so live and backtest decide from identical inputs (§17).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from atlas_engine.calibration import Calibrator, calibration_version

from .base import ModelEstimate, ModelFailure


@dataclass
class GBMModel:
    classifier: object  # anything with predict_proba(DataFrame) -> [[p0, p1]]
    calibrator: Calibrator
    version: str  # e.g. "gbm-<training run id>"; pinned, never "latest"
    name: str = "gbm"

    @classmethod
    def from_arm(cls, arm, version: str) -> "GBMModel":
        """A fitted ``GBMArm`` from a T2 run."""
        return cls(arm.model, arm.calibrator, version)

    def evaluate_setup(self, setup: dict, state: dict) -> ModelEstimate | ModelFailure:
        from atlas_research.selection.arms import feature_matrix

        if self.classifier is None:
            return ModelFailure(self.name, "unavailable")
        row = pd.DataFrame([{**state, "setup": setup.get("strategy"), "recent_signal_r20": state.get("recent_signal_r20")}])
        raw = self.classifier.predict_proba(feature_matrix(row))[:, 1]
        p = float(self.calibrator(raw)[0])
        return ModelEstimate(p, self.name, self.version, calibration_version(self.calibrator),
                             regime=state.get("regime"), reason_codes=("GBM",))
