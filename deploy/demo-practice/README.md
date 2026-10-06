# Demo practice run configuration

The engine config for the demo practice run: NinjaTrader's free **simulated** $50,000 account, through its
MCP server (`atlas-engine run --broker ninjatrader-mcp`), trading 1 MES. It exists to prove the machinery,
not to make money. The runbook is docs/demo-practice.md.

- `atlas.yaml`: MES only (MESZ6 pinned). `execution.demo_only: true` means the engine refuses every trade
  unless NinjaTrader reports a demo account. Everything closes at 14:55 Chicago (15:55 New York). Hermes's
  limits are in `agent_intents`.
- `risk.yaml`: up to 0.5% ($250) at risk a trade. No new trades after a $500 day. Trading stops at $1,800 down.
- `prop_rules/ninjatrader_sim_50k.yaml`: no prop firm. ATLAS's own practice limits, written in the
  prop-rule format: 1 MES per trade (`max_lot`), 2 in total (`contract_limit`), $1,000 a day and $3,000 overall.
- `strategies/mes_orb_practice.yaml`: the MES opening-range breakout. **A machinery test, not a strategy
  expected to profit** (-0.012R after costs in its backtest). It uses the demo-only `practice` decision model.
- `atlas-practice.service`: an optional systemd user service that keeps the engine running.

It lives in `deploy/`, not `config/`: `config/` changes only by a signed operator commit (AGENTS.md).
