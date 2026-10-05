"""Which decision model serves which strategy (PRD v3 §8, §23).

Model choice is configuration: a strategy's YAML names its model (``rules``
by default) and the engine builds this registry at start. A strategy with no
model, or a model that is not loaded, gets no estimate, and its trades are
rejected. Jev is selected for a strategy only after T2's keep/kill run shows
it beats rules-only and the GBM baseline out of sample (§23).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .base import DEFAULT_TIMEOUT_MS, SafeModel
from .rules import RulesModel
from .stated import AgentStatedModel


@dataclass
class ModelRegistry:
    models: dict[str, SafeModel] = field(default_factory=dict)  # model name -> wrapped model
    assignment: dict[str, str] = field(default_factory=dict)  # strategy -> model name
    timeout_ms: float = DEFAULT_TIMEOUT_MS

    @classmethod
    def default(cls, priors: dict[str, float] | None = None, assignment: dict[str, str] | None = None,
                timeout_ms: float = DEFAULT_TIMEOUT_MS, extra: list | None = None) -> "ModelRegistry":
        """Rules (with the given priors) and the agent-stated model, plus any ``extra`` models (GBM, Jev)."""
        reg = cls(assignment=dict(assignment or {}), timeout_ms=timeout_ms)
        for m in [RulesModel(dict(priors or {})), AgentStatedModel(), *(extra or [])]:
            reg.add(m)
        return reg

    def add(self, model) -> None:
        self.models[model.name] = SafeModel(model, self.timeout_ms)

    def model_for(self, strategy: str) -> str:
        return self.assignment.get(strategy, "rules")

    def get(self, name: str) -> SafeModel | None:
        return self.models.get(name)

    def latency_p95_ms(self, name: str) -> float | None:
        m = self.models.get(name)
        return m.p95_ms() if m else None

    def worst_failure_frac(self) -> tuple[str | None, float]:
        worst = (None, 0.0)
        for name, m in self.models.items():
            f = m.failure_frac()
            if f > worst[1]:
                worst = (name, f)
        return worst

    def describe(self) -> dict:
        return {"assignment": dict(self.assignment), "default": "rules", "timeout_ms": self.timeout_ms,
                "models": {n: {"version": getattr(m.model, "version", None)} for n, m in self.models.items()}}

    def close(self) -> None:
        for m in self.models.values():
            m.close()
