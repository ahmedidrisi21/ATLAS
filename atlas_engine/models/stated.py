"""The proposer's own probability, for Hermes on the demo account only (PRD v3 §12, docs/hermes-trader.md).

When Hermes proposes a trade it states how likely it thinks the target is
to fill before the stop. That number is an LLM's opinion, not a model's
estimate, so the engine accepts it only on a demo account, where the point is
to measure whether Hermes's opinions are any good (its Brier score in the
track record). It still has to clear the same EV threshold after costs as
any model. On a live account the engine refuses this model before asking it
(atlas_engine.pipeline), and a missing probability is a failure, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import ModelEstimate, ModelFailure


@dataclass
class AgentStatedModel:
    name: str = "agent_stated"
    version: str = "agent-stated-1"

    def evaluate_setup(self, setup: dict, state: dict) -> ModelEstimate | ModelFailure:
        p = state.get("stated_p")
        if p is None:
            return ModelFailure(self.name, "no_stated_probability")
        return ModelEstimate(float(p), self.name, self.version, reason_codes=("AGENT_STATED",))
