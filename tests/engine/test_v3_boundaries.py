"""PRD v3 §15, §29, §30: no path from an agent to the broker but the engine.

Static checks over the source tree. They fail the build if agent-facing code
(the API routes, MCP servers, Hermes plugins and profiles) imports anything
that can place, change or close an order, or if anything outside the MT5
adapter talks to the MetaTrader5 package.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT_FACING = ["atlas_api", "atlas_mcp", "atlas_plugins"]
# The engine host's own process entry point builds the engine and its adapter; it is not reachable by an agent.
ENGINE_HOST = {"atlas_api/engine_cli.py"}
ORDER_PATHS = ("atlas_engine.execution", "atlas_engine.adapters.mt5", "atlas_engine.runtime")


def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def _py(*dirs: str):
    for d in dirs:
        yield from (p for p in (ROOT / d).rglob("*.py") if "__pycache__" not in p.parts)


def test_agent_facing_code_cannot_import_an_order_path():
    bad = []
    for p in _py(*AGENT_FACING):
        rel = p.relative_to(ROOT).as_posix()
        if rel in ENGINE_HOST:
            continue
        bad += [f"{rel}: {m}" for m in _imports(p) if m.startswith(ORDER_PATHS)]
    assert not bad, "agent-facing code reaches an order path:\n" + "\n".join(bad)


def test_only_the_mt5_adapter_and_the_engine_host_touch_metatrader5():
    users = {p.relative_to(ROOT).as_posix() for p in _py("atlas_engine", "atlas_api", "atlas_mcp", "atlas_plugins",
                                                          "atlas_research") if "MetaTrader5" in _imports(p)}
    assert users <= ENGINE_HOST, users


def test_decision_models_cannot_reach_orders_or_accounts():
    bad = []
    for p in _py("atlas_engine/models", "atlas_engine/adapters/jev", "atlas_engine/calibration"):
        bad += [f"{p.name}: {m}" for m in _imports(p)
                if m.startswith(ORDER_PATHS + ("atlas_engine.risk", "atlas_engine.journal", "atlas_engine.config"))]
    assert not bad, bad


def test_no_profile_or_skill_carries_a_broker_credential():
    words = ("ATLAS_MT5_PASSWORD", "ATLAS_MT5_LOGIN", "mt5_password")
    hits = [p for d in ("atlas-profiles", "atlas-skills") for p in (ROOT / d).rglob("*")
            if p.is_file() and any(w in p.read_text(errors="ignore") for w in words)]
    assert not hits, hits


def test_risk_limits_and_prop_rules_never_come_from_memory():
    """§29: the engine's authoritative config loads only from config/, never from a Hermes home or memory."""
    for p in _py("atlas_engine"):
        src = p.read_text()
        assert "MEMORY.md" not in src and ".hermes" not in src, p
