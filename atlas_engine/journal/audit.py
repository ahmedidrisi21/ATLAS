"""Reconstruct one decision, and the trade it led to, from the journal alone (PRD v3 §31).

Everything the engine decided is in the journal keyed by ``decision_id``:
the intent, the market state, the model's estimate with its versions, the EV
calculation, the risk checks and sizing, the final ALLOW/REJECT/HALT/KILL
with every stage, the order, the fill, and the closed trade. Nothing here
reads Hermes memory or asks an LLM.
"""

from __future__ import annotations

from .store import Journal

SECTIONS = ("trade_intents", "market_states", "model_evaluations", "risk_checks", "decisions", "orders", "fills",
            "agent_actions")


def reconstruct(journal: Journal, decision_id: str) -> dict:
    out: dict = {"decision_id": decision_id}
    for table in SECTIONS:
        out[table] = journal.rows(table, 1000, decision_id=decision_id)
    tickets = {f["trade_id"] for f in out["fills"] if f.get("trade_id")}
    out["trades"] = [t for tid in sorted(tickets) for t in journal.rows("trades", 100, trade_id=tid)]
    decision = out["decisions"][-1] if out["decisions"] else {}
    pipeline = decision.get("pipeline") or {}
    out["summary"] = {
        "decision": decision.get("decision"),
        "outcome": decision.get("outcome"),
        "reasons": decision.get("reasons"),
        "failed_stage": pipeline.get("failed_stage"),
        "stages": [(s["stage"], s["ok"]) for s in pipeline.get("stages", [])],
        "model": {k: (pipeline.get("model") or {}).get(k) for k in
                  ("model", "model_version", "calibration_version", "feature_schema_version", "p_target_first")},
        "strategy_version": (pipeline.get("intent") or {}).get("strategy_version"),
        "ev": pipeline.get("ev"),
        "volume": pipeline.get("volume"),
        "closed": [{k: t.get(k) for k in ("exit_reason", "pnl", "r")} for t in out["trades"]],
        "complete": bool(decision) and bool(out["market_states"]),
    }
    return out
