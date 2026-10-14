"""Bounded decision models behind one interface (PRD v3 §7, §8, §23-§25).

    DecisionModel.evaluate_setup(setup, state) -> ModelEstimate | ModelFailure

Implementations: ``RulesModel`` (the validated backtest prior), ``GBMModel``
(T2's gradient-boosted baseline), ``JevModel`` (TypeSafe AI's Jev through its
adapter) ``AgentStatedModel`` (Hermes's own probability, demo only) and ``PracticeModel`` (a machinery test's
break-even stand-in, demo only). The
engine reaches every one of them through ``SafeModel``, which enforces the
timeout and the schema, and ``ModelRegistry``, which says which model serves
which strategy. A model estimates; it never decides, sizes or trades.
"""

from .base import DEFAULT_TIMEOUT_MS, DecisionModel, ModelEstimate, ModelFailure, SafeModel, check_inputs, validate
from .gbm import GBMModel
from .jev import JevModel
from .practice import PracticeModel
from .registry import ModelRegistry
from .rules import RulesModel
from .stated import AgentStatedModel

__all__ = [
    "DEFAULT_TIMEOUT_MS", "AgentStatedModel", "DecisionModel", "GBMModel", "JevModel", "ModelEstimate",
    "ModelFailure", "ModelRegistry", "PracticeModel", "RulesModel", "SafeModel", "check_inputs", "validate",
]
