# ATLAS v3: Hermes-Derived Autonomous Trading OS

**Product:** ATLAS (Autonomous Trading Intelligence & Learning System)
**Status:** Architecture / Product Requirements Document
**Version:** 3.0
**Date:** October 2026

This is the governing architecture for ATLAS from 2026-10-05. The earlier,
Forex-specific design, `ATLAS_v3_Hermes_Derived_PRD.md` (September 2026),
still holds the detailed numbers (FTMO limits, sizing, edge filters, the
Kanban research flow) that code cites as "PRD §N". Code written for this
document cites it as "PRD v3 §N". Where the two differ, this one wins. How
each section maps onto the repository is in `docs/v3-architecture.md`.

---

## 1. Executive Summary

ATLAS is an autonomous trading system built by specializing the existing
Hermes agent runtime for financial trading.

ATLAS does not recreate an agent framework.

Hermes already provides the autonomous-agent infrastructure required by ATLAS, including:

- LLM providers
- agent personas
- sub-agents
- delegation
- Kanban collaboration
- MCP
- skills
- memory
- channels
- scheduling
- gateway
- tool permissions
- persistent context
- dashboards and control
- multi-agent workflows

ATLAS adds the trading-specific intelligence and deterministic financial
control required to turn Hermes into an autonomous trading organization.

The architecture has three fundamental layers:

```text
┌─────────────────────────────────────────────┐
│                  HERMES                     │
│          Autonomous Agent Runtime           │
│                                             │
│ Personas · Sub-agents · Kanban · MCP        │
│ Skills · Memory · Channels · Scheduling     │
│ LLM Providers · Delegation · Tools          │
└──────────────────────┬──────────────────────┘
                       │
                 Trade Intent
                       │
                       ▼
┌─────────────────────────────────────────────┐
│          DETERMINISTIC TRADING ENGINE       │
│                                             │
│ Market validation · Jev · EV · Risk         │
│ Sizing · Exposure · Prop rules · Execution  │
│ Reconciliation · Kill switch                │
└──────────────────────┬──────────────────────┘
                       │
                  Approved Order
                       │
                       ▼
┌─────────────────────────────────────────────┐
│             EXECUTION ADAPTER               │
│                                             │
│ MT5 · cTrader · Tradovate · Future adapters │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
                  Broker / Prop
                    Platform
```

The core principle is:

> AI proposes. Models estimate. Hermes orchestrates. The deterministic
> trading engine controls consequences. The execution adapter sends orders.
> The broker executes them.

## 2. Product Goals

ATLAS must:

1. Turn Hermes into a specialized autonomous trading organization.
2. Reuse Hermes infrastructure instead of rebuilding it.
3. Allow Hermes agents to collaborate through existing Hermes mechanisms.
4. Support multiple LLM providers through Hermes' existing provider architecture.
5. Allow Jev to be integrated as a specialized bounded decision model.
6. Allow additional statistical/ML decision models to be added without redesigning Hermes.
7. Keep consequential trading decisions deterministic and auditable.
8. Prevent LLMs from directly placing broker orders.
9. Support multiple execution platforms through a common execution abstraction.
10. Maintain complete separation between reasoning and financial execution.
11. Make the system suitable for research, backtesting, paper trading, and eventually controlled live trading.

## 3. Non-Goals

ATLAS must not:

- create a replacement agent framework
- recreate Hermes' sub-agent system
- recreate Hermes' Kanban system
- recreate Hermes' memory system
- recreate Hermes' channel system
- recreate Hermes' scheduling system
- recreate Hermes' LLM provider system
- give LLMs unrestricted broker access
- allow Jev to directly execute trades
- allow trading agents to bypass the deterministic engine
- embed broker-specific logic throughout the trading core
- hard-code one broker or prop firm into the entire system

If Hermes already provides a capability, ATLAS must integrate with it rather
than implement a duplicate.

## 4. Core Architecture

ATLAS consists conceptually of three systems.

### 4.1 Hermes

Hermes is the autonomous organization. It provides agents, personas, LLMs,
collaboration, MCP, skills, memory, scheduling, channels, delegation, tools,
dashboards and workflows. ATLAS configures Hermes for trading.

Example Hermes personas:

```text
atlas-market
atlas-research
atlas-backtest
atlas-journal
atlas-performance
atlas-operations
atlas-trading
```

These are Hermes personas/sub-agents, not a second agent framework.

## 5. Hermes Agent Responsibilities

**atlas-market**: observing market conditions; collecting market
information; identifying potential setups; analyzing regime; providing
normalized market context. It may propose a trade. It may not execute a
broker order.

**atlas-research**: strategy research; hypothesis generation; experiment
design; investigating market behavior; analyzing strategy changes;
coordinating research tasks.

**atlas-backtest**: running backtests; walk-forward analysis; robustness
testing; Monte Carlo analysis; comparing strategy variants; evaluating
historical performance.

**atlas-journal**: trade journals; decision records; experiment records;
failure analysis; research history; operational records.

**atlas-performance**: performance analysis; expectancy; drawdown analysis;
strategy degradation; execution quality; model calibration; statistical
monitoring.

**atlas-operations**: system health; reconciliation monitoring; execution
infrastructure monitoring; alerts; operational incidents; trading status. It
may request a trading halt through the appropriate deterministic control
interface.

**atlas-trading**: preparing trade intents; monitoring trade lifecycle;
querying trade status; communicating with the deterministic trading engine.
It does not receive unrestricted broker access.

## 6. Hermes LLM Provider Architecture

Hermes already supports configurable LLM providers. ATLAS must use that
architecture. The system should allow different models to serve different
Hermes personas, for example Claude, GPT, Gemini, Qwen, DeepSeek, Kimi, local
models and other compatible providers. The exact model selection must remain
configuration-driven:

```yaml
agents:
  atlas-orchestrator:
    provider: anthropic
    model: <configured-model>
  atlas-research:
    provider: <configured-provider>
    model: <configured-model>
  atlas-market:
    provider: <configured-provider>
    model: <configured-model>
  atlas-operations:
    provider: <configured-provider>
    model: <configured-model>
```

ATLAS must not create a separate model-routing framework if Hermes already
provides one.

## 7. Jev Integration

Jev is a specialized decision model. Jev is not an autonomous agent. Jev is
not responsible for execution. Jev is a bounded statistical decision
component used by the deterministic trading engine.

The engine may call `jev.evaluate_setup(setup, state)`. Jev receives
normalized trading features, for example:

```json
{
  "setup": "trend_pullback",
  "regime": "trend",
  "adx": 31.2,
  "atr_percentile": 0.81,
  "ema_slope": 0.64,
  "stop_distance_r": 1.0,
  "target_distance_r": 2.4,
  "spread_r": 0.08
}
```

Jev returns a bounded result:

```json
{
  "p_target_first": 0.67,
  "regime": "trend",
  "reason_codes": ["TREND_ALIGNMENT", "VOLATILITY_SUPPORT", "GOOD_RR"],
  "model_version": "jev-0.8.2"
}
```

Jev must not receive account balance, broker credentials, raw broker
execution controls, prop-firm authorization, unrestricted account
information, or permission to execute orders. Jev must never directly place
trades.

## 8. Model Provider Abstraction

The architecture should allow additional bounded decision models to be added
without changing the trading engine's fundamental architecture.

```text
Decision Model Interface
│
├── Rules
├── GBM
├── Jev
├── Future statistical models
└── Future ML models
```

A model produces a decision estimate. The deterministic engine interprets
that estimate according to explicit rules:

```text
Model → Probability / prediction → EV calculation → Risk validation
→ Prop validation → Exposure validation → Execution validation → ALLOW / REJECT
```

Models do not control the final action.

## 9. Deterministic Trading Engine

The deterministic trading engine is the financial authority of ATLAS. It must
operate independently from LLM reasoning. Its responsibilities include setup
validation, market condition validation, model evaluation, expected-value
calculation, risk calculation, position sizing, exposure management,
strategy limits, prop-firm rules, execution constraints, order validation,
reconciliation and the kill switch.

The engine must produce explicit decisions: `ALLOW`, `REJECT`, `HALT`, `KILL`.

Every consequential trading action must pass through this engine.

## 10. Trade Intent

Hermes communicates with the trading engine through a structured trade intent:

```json
{
  "strategy": "trend_pullback",
  "strategy_version": "tp_v12",
  "symbol": "NQ",
  "side": "BUY",
  "setup": {"entry": 24000, "stop": 23950, "target": 24120},
  "reason": "Trend continuation after controlled pullback"
}
```

This is only a proposal. The engine independently validates the proposal.
Hermes cannot specify final contract quantity, final risk, final exposure,
prop-rule overrides, broker credentials, or unrestricted order parameters,
unless those values are explicitly permitted by deterministic policy.

## 11. Decision Pipeline

Every trade follows:

```text
Hermes
  │ Trade Intent
  ▼
Trading Engine
  ├── Validate setup
  ├── Validate market state
  ├── Evaluate Jev / selected model
  ├── Calculate EV
  ├── Validate strategy
  ├── Validate risk
  ├── Calculate position size
  ├── Validate exposure
  ├── Validate prop rules
  ├── Validate execution conditions
  └── Final decision
       ├── REJECT
       └── ALLOW → Execution Adapter → Broker / Platform
```

## 12. Expected Value

For a setup: `EV_R = p × R_target − (1 − p) × 1 − C_R`, where `p` is the
probability estimated by the selected decision model, `R_target` the planned
reward in R, and `C_R` the normalized trading costs.

Initial minimum: `EV_min = +0.15R`. The threshold must remain
configuration-driven. A model cannot override the EV threshold.

## 13. Risk Engine

The deterministic risk engine controls maximum risk per trade, daily risk,
total drawdown, open exposure, correlated exposure, maximum simultaneous
positions, strategy exposure, currency exposure, contract limits and
prop-firm limits.

Position sizing is deterministic: `allowed_risk ÷ risk_per_contract =
maximum_contracts`. The engine then applies all additional constraints.

## 14. Execution Architecture

Execution must use an abstraction rather than embedding a broker directly
into the trading engine.

```text
ExecutionBroker
├── MT5ExecutionAdapter
├── CTraderExecutionAdapter
└── FuturesExecutionAdapter
      └── TradovateAdapter
```

The exact adapters implemented depend on the selected trading markets. The
common execution interface should provide operations such as
`submit_order()`, `get_orders()`, `get_positions()`, `cancel_order()`,
`modify_position()`, `get_account_state()`, `reconcile()`, `flatten()`.

The deterministic engine calls the adapter. Hermes does not call broker APIs directly.

## 15. Execution Authority

The authority chain is: LLM → Hermes agent → Trade intent → Deterministic
trading engine → Risk / EV / policy validation → Execution adapter → Broker.

There must be no alternative execution path. A Hermes agent must never be
able to bypass the engine by calling broker APIs, calling MT5, Tradovate or
cTrader directly, modifying orders outside the engine, or overriding risk
limits.

## 16. Trading Engine Safety Boundary

The deterministic engine is the constitutional layer of ATLAS. Hermes can
research, reason, investigate, propose, backtest, analyze, collaborate,
create tasks and request actions. The engine decides whether financial
consequences are permitted.

```text
Hermes = autonomous intelligence
Jev = bounded probabilistic intelligence
Trading Engine = deterministic authority
Broker = execution authority
```

## 17. Market Data

Market data must enter the trading system through normalized interfaces. The
system should support real-time and historical data, bid/ask, spread,
candles, ticks and volume where available, session information and
instrument metadata.

The same normalized feature definitions should be used by research,
backtesting, Jev and deterministic trading. This prevents train/live feature drift.

## 18. Strategy Layer

Initial ATLAS strategies: Trend Pullback, Session Breakout, Liquidity Sweep
Reversal. Strategies generate structured setup candidates. They do not
directly execute orders. A strategy produces setup, entry, stop, target,
strategy version, market state and features. The deterministic engine
decides whether the setup is tradable.

## 19. Trading Regime

The deterministic engine should support normalized regime information such
as ADX, EMA slope, ATR percentile, session, volatility, market structure and
high-impact event state. Regime information can be generated by
deterministic calculations and/or bounded models. The final trading rules
remain deterministic.

## 20. Research Workflow

Hermes handles the research organization: market observation → hypothesis →
Kanban card → atlas-research → atlas-backtest → performance analysis → risk
review → human review → paper trading → promotion decision. This uses
Hermes' existing Kanban and collaboration infrastructure. ATLAS does not
implement a new collaboration system.

## 21. Backtesting

The backtest engine must use the same strategy and feature logic used by
live trading. Backtests should model bid/ask, spread, commission, swap where
applicable, slippage, execution rules, stop/target behavior, position
sizing, risk limits and prop rules where applicable. A backtest must not use
information unavailable at the time of the simulated decision.

## 22. Validation Requirements

A strategy/model should not be promoted based only on total return. Initial
validation requirements include:

- ≥300 OOS trades
- Expectancy ≥ +0.10R
- Profit Factor ≥ 1.25
- Monte Carlo 95% DD < 60% of allowed prop DD
- Daily-loss breach probability < 2%
- DSR > 0.95
- WFE ≥ 50%
- Positive under 2× normal spread
- Brier score better than base rate
- Paper/backtest drift within ±30%

Additional requirements may be added by the deterministic policy engine.

## 23. Model Evaluation

Jev must be evaluated against alternative decision methods: rules-only vs GBM
baseline vs Jev. All models must use identical data and evaluation periods.
Jev remains the selected model only when it demonstrates meaningful
out-of-sample improvement. Minimum evaluation should include out-of-sample
performance, calibration, Brier score, reliability curves, robustness,
regime stability and execution-cost sensitivity.

## 24. Jev Calibration

Jev probabilities must be calibrated. Initial approach: isotonic regression.
Calibration window: rolling 500 closed trades. The calibration process must
be versioned and recorded. Every prediction should be traceable to model
version, calibration version, feature schema version and strategy version.

## 25. Failure Handling

Model failures must fail safely. If Jev times out, returns invalid schema,
produces an out-of-range probability, becomes unavailable or produces invalid
data, the system must not guess. Default behavior: REJECT / SKIP TRADE.
Initial model timeout: 500 ms. This value must be configurable.

## 26. Operational States

ATLAS must have explicit system states: `NORMAL`, `DEGRADED`, `HALT`, `KILL`.

```text
Jev latency too high        → DEGRADED
Broker disconnected         → HALT
Reconciliation mismatch     → HALT
Hard drawdown breach        → KILL
```

A degraded system must not silently continue operating outside defined
safety conditions.

## 27. Reconciliation

The trading engine must continuously reconcile its internal state with the
execution platform. It must detect missing orders, unexpected orders,
unexpected positions, partial fills, rejected orders, stale orders, orphan
positions, inconsistent balances and execution-state divergence.
Reconciliation must be deterministic.

## 28. Kill Switch

ATLAS must have an independent deterministic kill mechanism, capable of:
disable new trades; cancel eligible orders; flatten positions when required;
prevent execution; record the event; notify operators. The kill switch must
not depend on an LLM deciding to behave correctly.

## 29. Memory Boundary

Hermes memory may contain research findings, hypotheses, market
observations, strategy discoveries, failure patterns, operational knowledge
and broker observations. Hermes memory must not be authoritative for risk
limits, prop rules, live strategy parameters, account limits, kill switches
or execution permissions. Authoritative financial configuration belongs to
deterministic configuration/policy.

## 30. Security Boundary

Broker credentials must never be exposed to Hermes LLMs. Credentials belong
only to the execution infrastructure. LLMs receive structured information.
They do not receive broker passwords, API secrets, account credentials or
private signing keys. The execution adapter is the only component authorized
to authenticate with the trading platform.

## 31. Auditability

Every consequential decision must be traceable. The system should record
market state, strategy version, setup, model used, model version, model
output, EV calculation, risk calculation, position sizing, prop validation,
execution decision, order, fill, position, reconciliation and system state.
A completed trade should be reconstructable from the journal without relying
on LLM memory.

## 32. Core Data Flow

```text
Market Data → Market State → Hermes (market analysis, research, strategy
reasoning, trade proposal) → Trade Intent → Deterministic Engine (Jev | Rules)
→ EV / Risk → Policy Checks → Position Sizing → Execution Checks
→ REJECT | ALLOW → Execution Adapter → Broker → Fill → Reconciliation
→ Journal → Hermes
```

## 33. Technology Principle

ATLAS should favor existing Hermes abstractions, deterministic Python
services where appropriate, typed interfaces, explicit schemas,
configuration-driven behavior, versioned models, versioned strategies,
immutable trade records, reproducible experiments, strict API boundaries and
minimal duplication. Do not introduce a new framework merely because it is
technically interesting.

## 34. Repository Principle

The existing ATLAS/Hermes repository should evolve rather than be rewritten.
Before creating any new abstraction:

1. inspect Hermes
2. identify the existing abstraction
3. determine whether it already satisfies the requirement
4. extend it if necessary
5. create a new abstraction only when the existing architecture genuinely cannot support the requirement

The implementation should preserve upstream Hermes compatibility wherever practical.

## 35. Conceptual Repository Structure

The exact repository structure must follow the existing Hermes architecture.
Conceptually:

```text
ATLAS
├── Hermes: agents/personas, providers, MCP, skills, memory, channels, Kanban, existing runtime
├── Trading Domain: deterministic engine, strategies, risk, models (jev, other bounded models),
│                   policies, market data, journal
└── Execution: MT5, cTrader, Futures adapters
```

This is an architectural model, not a requirement to create duplicate
directories when equivalent Hermes/ATLAS abstractions already exist.

## 36. Development Sequence

Implementation should proceed by auditing and extending the existing system.

1. **Hermes integration.** Verify the existing provider architecture,
   personas, sub-agent system, Kanban, MCP, skills, memory, channels,
   scheduling and permissions. No duplicate implementations.
2. **Trading domain.** Verify and implement market state, strategy
   interface, trade intent, deterministic engine, risk, EV, sizing, policies.
3. **Jev.** Integrate Jev → bounded decision interface → deterministic
   engine. Add schema validation, timeout, calibration, model versioning,
   evaluation.
4. **Execution.** Implement the common execution interface and selected
   platform adapters.
5. **Backtesting.** Ensure live and historical systems use compatible feature
   and strategy logic.
6. **Paper trading.** Run the entire architecture without real financial exposure.
7. **Controlled live trading.** Only after all deterministic safety and
   validation gates pass.

## 37. Final Architecture

```text
                         ATLAS
          ┌────────────────┴────────────────┐
          ▼                                 ▼
       HERMES                         TRADING ENGINE
   Autonomous agents                  Deterministic financial control
   Personas, LLM providers,                 ┌────┴────┐
   Kanban, MCP, Skills, Memory,            Jev      Rules
   Channels, Delegation, Scheduling         └────┬────┘
          └──────── Trade Intent ────────────────┘
                                             │
                                      ALLOW / REJECT
                                             ▼
                                      EXECUTION ADAPTER
                                             ▼
                                       BROKER / PROP
```

The most important architectural boundary is:

```text
                    THINKING
                       ▼
                    HERMES
                       ▼
                 TRADE INTENT
═══════════════════════╪═══════════════════════
              SAFETY / AUTHORITY
                       ▼
             DETERMINISTIC ENGINE
                  ALLOW / REJECT
                       ▼
                  EXECUTION
                       ▼
                    BROKER
```

Hermes is the autonomous organization. LLMs are its reasoning engines. Jev is
a bounded decision model. Other ML models can be plugged into the same
decision architecture. The deterministic trading engine is the authority
over financial consequences. The execution adapter is the only path to the
broker. That separation is the foundation of ATLAS.
