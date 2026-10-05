"""Which API scope each MCP tool needs. Importable without the MCP SDK.

deploy/hermes/bootstrap.py issues each (profile, server) token with exactly the
scopes of the tools that profile is given in atlas-profiles/roster.yaml, so a
profile allowed only to read backtests gets a token that cannot start one.

Most servers call the research API. atlas-operations and atlas-trading call
the engine API, which has its own URL, token file and scope set.
"""

SERVER_TOOLS = {
    "atlas-market": {
        "collect_market_state": "market:read",
        "get_bars": "market:read",
        "get_spread_stats": "market:read",
    },
    "atlas-backtest": {
        "run_backtest": "backtest:run",
        "run_walk_forward": "backtest:run",
        "run_exit_research": "backtest:run",
        "monte_carlo": "backtest:read",
        "get_run_summary": "backtest:read",
        "list_runs": "backtest:read",
    },
    "atlas-journal": {
        "query_trades": "journal:read",
        "mfe_mae": "journal:read",
        "loss_clusters": "journal:read",
    },
    "atlas-performance": {
        "performance_summary": "performance:read",
    },
    "atlas-operations": {
        "system_status": "ops:read",
        "health_state": "ops:read",
        "reconciliation_report": "ops:read",
        "disable_trading": "ops:disable_trading",
    },
    "atlas-trading": {
        "get_live_market": "trading:read",
        "submit_trade_intent": "trading:demo",
        "get_intent_status": "trading:read",
        "list_my_positions": "trading:read",
        "close_my_position": "trading:demo",
        "tighten_stop": "trading:demo",
        "my_track_record": "trading:read",
    },
}

# Which API a server's token belongs to. Anything not listed calls the research API.
ENGINE_SERVERS = {"atlas-operations", "atlas-trading"}


def api_of(server: str) -> str:
    return "engine" if server in ENGINE_SERVERS else "research"


def scopes_for(server: str, tools: list[str]) -> list[str]:
    """The least set of scopes that lets ``tools`` of ``server`` work."""
    known = SERVER_TOOLS[server]
    unknown = set(tools) - set(known)
    if unknown:
        raise ValueError(f"{server} has no tool(s): {', '.join(sorted(unknown))}")
    return sorted({known[t] for t in tools})


def token_env_var(server: str) -> str:
    """Name of the profile .env variable holding this server's token, e.g. ATLAS_TOKEN_BACKTEST."""
    return "ATLAS_TOKEN_" + server.removeprefix("atlas-").upper()
