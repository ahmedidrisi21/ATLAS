"""Rules-only: the baseline every other model has to beat (PRD v3 §8, §23).

A rule strategy has no per-trade opinion. Its estimate is the fixed
probability its validated backtest measured (target hit before stop, out of
sample), set per strategy and version in that strategy's config. A strategy
with no measured probability gets no estimate, so the engine rejects its
trades instead of assuming one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .base import ModelEstimate, ModelFailure


@dataclass
class RulesModel:
    priors: dict[str, float] = field(default_factory=dict)  # "strategy:version" (or "strategy") -> p_target_first
    name: str = "rules"
    version: str = "rules-1"

    @property
    def calibration_version(self) -> str:
        """The priors are this model's calibration: a hash of the table, so a changed prior is a new version."""
        digest = hashlib.sha256(json.dumps(self.priors, sort_keys=True).encode()).hexdigest()[:12]
        return f"priors-{digest}"

    def evaluate_setup(self, setup: dict, state: dict) -> ModelEstimate | ModelFailure:
        key = f"{setup.get('strategy')}:{setup.get('strategy_version')}"
        p = self.priors.get(key, self.priors.get(str(setup.get("strategy"))))
        if p is None:
            return ModelFailure(self.name, "no_validated_prior")
        return ModelEstimate(float(p), self.name, self.version, self.calibration_version,
                             regime=state.get("regime"), reason_codes=("RULES_PRIOR",))
