# ATLAS: rules for agents working in this repo

ATLAS is a Forex research and trading system on Hermes. The architecture is
`ATLAS_PRD.md` (v3, cited as "PRD v3 §N"); the detailed numbers it builds on are in
`ATLAS_v3_Hermes_Derived_PRD.md` (cited as "PRD §N"). How each section maps to code
is `docs/v3-architecture.md`; decisions are in `docs/`.

- Stay inside your Kanban card and its git worktree. Don't push to `main`.
- Never read, request or load holdout data (PRD §22). Research code refuses it; don't work around that.
- Never edit `config/risk*`, `config/prop_rules/` or `config/strategies/`. Those change only by a signed operator commit.
- No code path may let an agent place live trades, change risk or enable trading (PRD §6, §11).
- Every trade is a trade intent that goes through the engine's decision pipeline (PRD v3 §10, §11):
  `atlas_engine/intents.py`, `atlas_engine/pipeline.py`. Don't add another path to `atlas_engine.execution`
  or an adapter; `tests/engine/test_v3_boundaries.py` fails if agent-facing code imports one.
- Agents may trade the demo account only, and only the `atlas-trading` profile through the `atlas-trading`
  MCP server (docs/hermes-trader.md). Its trades go through the full pipeline; the engine refuses them
  unless the broker reports a demo account. Don't widen it: no lot sizes, no live, no closing or
  moving positions the agent didn't open, no stop moved away from the price.
- Report every experiment you ran, failures included, in R after costs.
- Run the tests for what you changed before you hand off.
- Hermes is the pinned submodule `vendor/hermes-agent`. Don't patch it; ATLAS code loads from outside it.
