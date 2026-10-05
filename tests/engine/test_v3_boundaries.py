"""PRD v3 §15, §29, §30: no path from an agent to the broker but the engine.

Static checks over the source tree. They fail the build if agent-facing code
(the API routes, MCP servers, Hermes plugins and profiles) imports anything
that can place, change or close an order, if strategies, setups, features or
research reach an order path, if anything but the engine host builds a broker
adapter, or if anything outside the MT5 adapter talks to the MetaTrader5 package.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT_FACING = ["atlas_api", "atlas_mcp", "atlas_plugins"]
# The engine host's own process entry point builds the engine and its adapter; it is not reachable by an agent.
ENGINE_HOST = {"atlas_api/engine_cli.py"}
ORDER_PATHS = ("atlas_engine.execution", "atlas_engine.adapters.mt5", "atlas_engine.adapters.tradovate",
               "atlas_engine.adapters.futures_venue", "atlas_engine.runtime")
# Code that proposes or researches trades: it may compute setups, never reach a broker.
PROPOSERS = ["atlas_engine/setups", "atlas_engine/strategies.py", "atlas_engine/features", "atlas_engine/decisions",
             "atlas_engine/futures", "atlas_engine/intents.py", "atlas_engine/agent_intents.py", "atlas_research"]


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
        if (ROOT / d).is_file():
            yield ROOT / d
            continue
        yield from (p for p in (ROOT / d).rglob("*.py") if "__pycache__" not in p.parts)


def test_agent_facing_code_cannot_import_an_order_path():
    bad = []
    for p in _py(*AGENT_FACING):
        rel = p.relative_to(ROOT).as_posix()
        if rel in ENGINE_HOST:
            continue
        bad += [f"{rel}: {m}" for m in _imports(p) if m.startswith(ORDER_PATHS)]
    assert not bad, "agent-facing code reaches an order path:\n" + "\n".join(bad)


def test_strategies_setups_features_and_research_cannot_reach_an_order_path():
    """Strategies and backtests produce signals and results; only the engine turns an allowed intent into an order."""
    bad = [f"{p.relative_to(ROOT).as_posix()}: {m}" for p in _py(*PROPOSERS) for m in _imports(p)
           if m.startswith(ORDER_PATHS)]
    assert not bad, "a proposer or backtest reaches an order path:\n" + "\n".join(bad)


def test_only_the_engine_host_builds_a_futures_platform_adapter():
    users = {p.relative_to(ROOT).as_posix() for p in _py("atlas_engine", "atlas_api", "atlas_mcp", "atlas_plugins",
                                                          "atlas_research")
             if any(m.startswith("atlas_engine.adapters.tradovate") for m in _imports(p))}
    users = {u for u in users if not u.startswith("atlas_engine/adapters/tradovate/")}
    assert users <= ENGINE_HOST, users


def test_the_engine_never_calls_a_platform_directly():
    """The runtime writes only through its ExecutionBroker; it never imports a platform adapter."""
    imported = _imports(ROOT / "atlas_engine/runtime.py")
    assert not [m for m in imported if m.startswith(("atlas_engine.adapters.mt5", "atlas_engine.adapters.tradovate",
                                                     "atlas_engine.adapters.futures_venue"))], imported


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
    words = ("ATLAS_MT5_PASSWORD", "ATLAS_MT5_LOGIN", "mt5_password", "ATLAS_TRADOVATE_PASSWORD",
             "ATLAS_TRADOVATE_SEC", "accesstokenrequest")
    hits = [p for d in ("atlas-profiles", "atlas-skills") for p in (ROOT / d).rglob("*")
            if p.is_file() and any(w in p.read_text(errors="ignore") for w in words)]
    assert not hits, hits


def test_risk_limits_and_prop_rules_never_come_from_memory():
    """§29: the engine's authoritative config loads only from config/, never from a Hermes home or memory."""
    for p in _py("atlas_engine"):
        src = p.read_text()
        assert "MEMORY.md" not in src and ".hermes" not in src, p
