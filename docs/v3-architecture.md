# ATLAS v3: how the PRD maps onto this repository

Written 2026-10-05, when ATLAS was reworked around the new PRD (`ATLAS_PRD.md`).
The PRD asks to evolve the existing repo, not rewrite it (§34), so the rework
began with an audit. Each PRD section below is marked:

- **Kept**: it already existed and meets the PRD.
- **Extended**: it existed and was changed to meet the PRD.
- **Built**: it is new in this change.
- **Not yet**: it is still open, with the reason.

## The short version

ATLAS already had most of the PRD before this change: the Hermes profiles,
Kanban boards, MCP servers and skills (H0 to H3), the risk engine and FTMO
rules (T3), and the engine with its MT5 adapter, reconciliation, journal and
kill switch (T4). The PRD's new ideas were missing. These were the single
trade-intent format, the explicit ALLOW/REJECT/HALT/KILL decision, the
pluggable decision models with a fail-safe timeout, the common execution
interface, and a journal you can rebuild a trade from. This change adds them
and puts every trade, from a rule or from Hermes, through one pipeline.

**Futures first (2026-10-05).** After this rework the owner moved ATLAS from
MT5 forex to futures through a prop firm. The pipeline,
models, risk, journal and Hermes side were kept; contracts, sessions, sizing
in contracts, prop rules as data and the execution adapter were added. On
2026-10-06 NinjaTrader became the primary platform; its official API is the
Tradovate API the adapter already spoke, so the adapter was renamed and
hardened (broker-neutral orders, verified brackets, lifecycle journal), not
duplicated. See `docs/futures.md`.

**No strategy has an edge yet.** T0 tested twelve strategies over seven
rounds on real 2019 to mid-2025 data, and every one lost money after costs
(`docs/t0-edge-discovery.md`). The PRD's three starter strategies (§18) are
among them. Everything here is built strategy-agnostic. The engine trades a
strategy only if its config file exists and names a validated hit rate, and
no such file exists, so a live engine today watches and trades nothing.
Hermes's demo-account trading (`docs/hermes-trader.md`) is the one exception.

## Phase 1: Hermes integration (§1 to §6, §20, §29, §30)

| PRD | Status | Where | Notes |
| --- | --- | --- | --- |
| §1-§3 Reuse Hermes, no second framework | Kept | `vendor/hermes-agent` (pinned v2026.9.21), `docs/hermes-h0-checklist.md` | ATLAS loads from outside Hermes and patches nothing. The H0 checklist audited all 21 Hermes capabilities ATLAS uses. |
| §4-§5 Personas | Extended | `atlas-profiles/`, `atlas-profiles/roster.yaml` | Profiles renamed to the PRD's personas, and `atlas-journal` added. Four supporting roles stay, also renamed: `atlas-risk`, `atlas-execution`, `atlas-data` and `atlas-models` (was `jev-analyst`). Each persona is a Hermes profile with its own SOUL, toolsets, MCP tools and skills. |
| §5 Sub-agents, delegation | Kept | Hermes `delegate_task`, Kanban | Not reimplemented. |
| §6 Per-persona LLM providers | Extended | `deploy/hermes/models.yaml`, `deploy/hermes/bootstrap.py` | Models were set per tier. Now any persona can name its own provider and model under `profiles:`, which wins over its tier. Hermes's own per-profile provider config does the routing. |
| §20 Research workflow on Kanban | Kept | `deploy/hermes/boards.yaml`, `atlas-skills/`, `atlas_plugins/` | Boards `atlas-research`, `atlas-engineering` and `atlas-ops`. Results go into card metadata. |
| §29 Memory boundary | Kept, now tested | `atlas_engine/config.py`, `tests/engine/test_v3_boundaries.py` | Limits come only from `config/` (operator-signed). A test fails if engine code reads a Hermes home or memory file. |
| §30 Credentials | Kept, now tested | `atlas_engine/adapters/mt5/adapter.py` | MT5 credentials are read only from the engine host's environment. A test fails if a profile or skill mentions them. |

The profile gate (`tests/hermes/h1_gate.py`, offline) passes 18 of 18 with
the new names, against Hermes v2026.9.21.

### Old to new profile names

| Before | After |
| --- | --- |
| market-researcher | atlas-market |
| strategy-researcher | atlas-research |
| backtest-engineer | atlas-backtest |
| performance-analyst | atlas-performance |
| operations-monitor | atlas-operations |
| trader (PR #13) | atlas-trading |
| (new) | atlas-journal |
| risk-analyst | atlas-risk |
| execution-engineer | atlas-execution |
| data-engineer | atlas-data |
| jev-analyst | atlas-models |

The phase docs written before this change (`h1`, `h2`, `h3`, `t0` to `t4`)
keep the old names, because they record what ran at the time.

Note: the persona `atlas-research` and the Kanban board `atlas-research`
share a name. Hermes keeps profiles and boards apart, so this is only
something to keep in mind when reading logs.

## Phase 2: Trading domain (§9 to §13, §16 to §19)

| PRD | Status | Where | Notes |
| --- | --- | --- | --- |
| §10 Trade intent | Built | `atlas_engine/intents.py` | One `TradeIntent` for rules and agents. `parse_intent` accepts the §10 JSON. Any field that sets size, risk, exposure, an override, credentials or raw order parameters rejects the whole intent. Missing fields are never filled in. |
| §9, §11 Decision pipeline, ALLOW/REJECT/HALT/KILL | Built | `atlas_engine/pipeline.py`, `atlas_engine/runtime.py` (`submit`) | Stages in order: system state, setup, market, strategy, model, EV, then the T3 risk engine's risk, sizing, exposure and prop checks. The first failure decides. Every stage is journaled. |
| §12 EV gate | Extended | `atlas_engine/decisions/ev.py` (from T2), used by the pipeline | `EV_R = p × R_target − (1 − p) − C_R ≥ 0.15`. C_R is round-turn commission over the money at risk, plus an optional slippage allowance. Spread is already counted, because the stop and target are measured from the side of the quote that fills. The threshold is config (`decision.ev_min_r`). No model or intent can change it, and config can't go below 0. |
| §13 Risk engine, sizing | Kept | `atlas_engine/risk/`, `atlas_engine/sizing/`, `atlas_engine/exposure/`, `atlas_engine/prop_rules/` (T3) | Unchanged. The pipeline files T3's reasons under the PRD's stage names. |
| §16 Safety boundary | Kept, now tested | `tests/engine/test_v3_boundaries.py` | |
| §17 Normalised market data and features | Kept | `atlas_engine/market_data/`, `atlas_engine/features/`, `atlas_engine/decisions/state.py` | Research, backtest and the live engine build features with the same code. Live signals now carry the normalised decision state, so GBM and Jev see what research saw. |
| §18 Strategy layer | Extended | `atlas_engine/setups/`, `atlas_engine/strategies.py` | The three PRD setups and two more exist, but none has passed T0. A strategy's config can now name its decision model and its validated hit rate (`decision:`). |
| §19 Regime | Kept | `atlas_engine/decisions/state.py` (`regime_label`) | Deterministic: H1 ADX for trend or range, and the ATR percentile for the volatility bucket. High-impact-event state is not built yet: it needs an economic-calendar feed (see `docs/hermes-trader.md`). |

## Phase 3: Jev and other decision models (§7, §8, §23 to §25)

| PRD | Status | Where | Notes |
| --- | --- | --- | --- |
| §8 One interface for Rules, GBM, Jev, later models | Built | `atlas_engine/models/` | `evaluate_setup(setup, state) -> ModelEstimate or ModelFailure`. `RulesModel` uses a strategy's validated hit rate. `GBMModel` wraps T2's gradient-boosted baseline. `JevModel` wraps the Jev adapter. `AgentStatedModel` is Hermes's own probability, demo account only. `ModelRegistry` maps each strategy to its model (default: rules). |
| §7 What Jev may and may not see | Kept, plus a second guard | `atlas_engine/adapters/jev/` (T2), `atlas_engine/models/base.py` | The adapter's leakage guard checks every request. `SafeModel` also refuses any input named like a balance, price, symbol, time, volume or credential, whichever model it wraps. |
| §25 Fail safe, 500 ms timeout | Built | `atlas_engine/models/base.py` (`SafeModel`) | Every model call has a hard timeout (`decision.model_timeout_ms`, default 500). Timeouts, crashes, a bad schema, an out-of-range or NaN probability, and a `latest` model version all become REJECT. |
| §24 Calibration, versioning | Extended | `atlas_engine/calibration/` (T2) | Isotonic fit over the last 500 trades (T2). Now every estimate carries model version, calibration version (a hash of the fit), feature-schema version (`atlas-state-1`) and strategy version. All four are journaled. |
| §23 Rules vs GBM vs Jev | Kept | `atlas_research/selection/`, `atlas-research t2 run` (T2) | Same data, same folds, same costs for every arm. Jev is selected for a strategy only after it wins there. |
| Jev itself | Built (client only) | `atlas_engine/adapters/jev/typesafe.py`, `atlas_api/engine_cli.py` (`load_jev`) | `TypeSafeTransport` calls TypeSafe's System One API (`POST /v1/systemone`). One request asks a Noul question ("will price reach the target before the stop?"), whose yes-probability is `p_target_first`, and a Choice over the six regime labels. The state is the leakage-checked numbers only: no dates, prices, symbols or news. The model is pinned (`decision.jev_model`, default `jev-1.13.0`); an answer from any other model version is refused, so an alias moving can't change answers silently. The key comes only from `TYPESAFE_API_KEY` on the engine host. A 429, 529, error or slow answer is a REJECT, with no retry. The engine loads Jev only when a strategy names `model: jev` and the key is set. Without a fitted calibrator (`<state>/jev_calibration.json`) Jev may trade a demo account only. Nothing has been asked of the live API yet: no strategy has an edge to evaluate. |

## Phase 4: Execution (§14, §15, §27, §28)

| PRD | Status | Where | Notes |
| --- | --- | --- | --- |
| §14 Common execution interface | Built | `atlas_engine/execution/broker_api.py` | `ExecutionBroker`: submit_order, get_orders, get_positions, cancel_order, modify_order, modify_position, close_position, get_account_state, reconcile, flatten. Every order write in the engine now goes through it. The engine picks the adapter from `execution.platform`. |
| Futures and NinjaTrader adapter | Built (futures-first) | `atlas_engine/execution/futures.py`, `atlas_engine/adapters/futures_venue.py`, `atlas_engine/adapters/orders.py`, `atlas_engine/adapters/ninjatrader/` | Futures first since 2026-10-05; NinjaTrader primary since 2026-10-06 (`tradovate` is an alias of the same adapter). `FuturesExecutionAdapter` sends each entry as one broker-neutral bracket, verifies its protection at the platform and HALTs when it can't. Tested on a stand-in only; FundedNext API access unconfirmed. See `docs/futures.md`. |
| MT5 adapter | Kept | `atlas_engine/adapters/mt5/`, `atlas_engine/execution/executor.py` (T4) | `MT5ExecutionAdapter` wraps them unchanged. Forex only now. |
| cTrader, Rithmic | Not built | | No placeholders. A futures platform is one more `FuturesVenue` subclass and one line in `PLATFORMS`. |
| §15 No other path to the broker | Kept, now tested | `tests/engine/test_v3_boundaries.py` | Fails the build if the API, MCP servers or plugins import an order path; if setups, strategies, features, intents or research reach one; if anything but the engine host builds the NinjaTrader adapter; if a platform order command appears outside that adapter; or if anything outside the engine host touches `MetaTrader5`. |
| §27 Reconciliation | Kept | `atlas_engine/reconciliation/` (T4) | |
| §28 Kill switch | Extended | `atlas_engine/runtime.py` | A KILL already disabled trading, flattened, journaled and alerted. It now also cancels any ATLAS pending order. ATLAS sends only market orders, so normally there are none. The MT5 watchdog EA is the independent backstop (`watchdog/`). |

## Phase 5 and on: backtest, paper, live (§21, §22, §26, §31, §36)

| PRD | Status | Where | Notes |
| --- | --- | --- | --- |
| §21 Backtest uses live logic | Kept | `atlas_research/backtest.py`, `atlas_research/exits.py` | Same setups and features as live. This change also fixes 20 T1 exit-research tests that were failing on `main` after PRs #6 and #10 merged (a column T0 added that T1 didn't fill). |
| §22 Validation gates | Kept, mostly | `atlas_research/t0.py`, `atlas_research/configs/t0.yaml`, T2 | The OOS trades, expectancy, PF, Monte Carlo DD, breach probability, DSR, WFE and 2× spread gates are in T0. The Brier gate is in T2. Paper-vs-backtest drift within ±30% is **not yet** built: it needs paper trades to compare. |
| §26 NORMAL, DEGRADED, HALT, KILL | Extended | `atlas_engine/ops/health.py` | Existed since H3/T4. Now a decision model failing 25% or more of its last 20 calls degrades the system (`model_failures`), so no new trades. A model declining a strategy it has no estimate for doesn't count. |
| §31 Audit | Built | `atlas_engine/journal/`, `atlas-engine audit <decision_id>` | New tables `market_states`, `model_evaluations` and `calibration_models`. The `decisions` row holds the whole pipeline record. `reconstruct()` rebuilds a decision and its trade from the journal alone. |
| §36 Phase 6 paper trading | Not yet | | The engine runs end to end on the fake MT5 terminal. A demo-account run from the Windows VPS is still owed from T4. |
| §36 Phase 7 live | Not yet | | Needs a strategy that passes every gate. Hermes stays demo-only by code (`agent_intents.py`, and now the pipeline's model stage too). |

## What changed for Hermes on the demo account

Before, a Hermes trade went through the risk engine only. Now it goes
through the whole pipeline. One rule is new: the probability Hermes states
for a trade must clear the same EV gate as any model, so a trade Hermes
itself rates below +0.15R after costs is rejected. For example, a 2R target
needs a stated chance of about 40% or better. Hermes's stated probability is
accepted only on a demo account. Its accuracy is still scored by the Brier
score in its track record.

## Configuration added

`decision:` in `config/atlas.yaml` (optional, operator-signed like the rest).
Defaults are the PRD's:

```yaml
decision:
  ev_min_r: 0.15          # §12
  model_timeout_ms: 500   # §25
  min_target_r: 0.5
  max_target_r: 10.0
  max_spread_to_stop: 0.20
  extra_cost_r: 0.0       # slippage allowance added to C_R
  jev_model: jev-1.13.0   # §7: a pinned TypeSafe model ID; aliases such as jev-latest are refused
```

In a strategy file (`config/strategies/<name>.yaml`, operator-signed):

```yaml
decision:
  model: rules            # rules | gbm | jev
  p_target_first: 0.42    # the validated out-of-sample hit rate; without it, no trades
```
