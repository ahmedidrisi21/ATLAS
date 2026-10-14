"""The practice model: a machinery test on a demo account, never an estimate (docs/demo-practice.md).

The demo practice run trades a strategy that failed its backtest (the MES opening-range breakout,
-0.012R after costs over 1,353 trades) to prove that orders, brackets, stops, targets and the flat-by
exit work end to end on NinjaTrader's simulated account. No strategy has a validated probability, so the
rules model would (rightly) reject every trade. This model states the smallest probability the EV gate
accepts for the trade in front of it, so the trade reaches risk, sizing and execution. It says nothing
about whether the trade is good, and its journal entry says so (``MACHINERY_TEST_NOT_AN_ESTIMATE``).

The pipeline refuses it unless the broker reports a demo account (``practice_model_needs_demo_account``),
before the model is asked, so it can never price a trade on a live account.
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas_engine.decisions.ev import breakeven_p

from .base import ModelEstimate, ModelFailure

NAME = "practice"
ROUNDING = 1e-4  # the pipeline hands models target_r and cost_r rounded to 4 decimals


@dataclass
class PracticeModel:
    ev_min_r: float = 0.15
    name: str = NAME
    version: str = "practice-1"

    def evaluate_setup(self, setup: dict, state: dict) -> ModelEstimate | ModelFailure:
        try:
            # The setup arrives rounded to 4 decimals; step past the rounding so the gate's exact sums clear too.
            p = breakeven_p(float(setup["target_r"]) - ROUNDING, float(setup["cost_r"]) + ROUNDING, self.ev_min_r)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return ModelFailure(self.name, "schema:setup")
        if not 0.0 < p < 1.0:
            return ModelFailure(self.name, "no_break_even_probability")
        return ModelEstimate(p, self.name, self.version, reason_codes=("MACHINERY_TEST_NOT_AN_ESTIMATE",))
