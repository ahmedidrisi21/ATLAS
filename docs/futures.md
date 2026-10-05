# Futures-first execution

Written 2026-10-05. The owner moved ATLAS from MT5 forex to futures first
(ES/MES, NQ/MNQ, YM/MYM, RTY/M2K, CL/MCL, GC/MGC), through a prop firm, with
Tradovate as the first execution platform. This page says what changed, what
did not, what was checked and where, and what is still open.

**This replaces the forex choices in `docs/open-questions-decisions.md`**
(FTMO 2-Step on MT5, EURUSD/GBPUSD, M15 forex bars). Those stay in the repo,
and the MT5 path still works and is still tested, but futures is the
direction now.

## The short version

Nothing was rebuilt. Every trade, from a rule or from Hermes, still goes

    trade intent -> engine pipeline -> ALLOW / REJECT / HALT / KILL -> execution adapter -> broker

through the same stages (system state, setup, market, strategy, model, EV,
risk, sizing, exposure, prop, execution). What was wrong for futures was the
execution layer and a few forex assumptions inside the engine. Those are
replaced:

- **Contracts.** The engine knows futures products (MES, NQ, ...) and the dated
  contract it trades for each (MESZ6). It refuses an expired contract, one
  close to rolling, or one whose expiry the broker didn't report.
- **Sizing in contracts.** `contracts = floor(allowed risk / (stop ticks x tick value + fees))`,
  then capped by the firm's contract limit. The engine decides the number; no
  agent or model can.
- **Exchange hours.** Whether the market is open comes from an exchange
  calendar in Chicago time (Globex hours, the daily maintenance break,
  weekends, holidays, daylight saving), not from the forex week.
- **Prop firm rules as data.** FundedNext Futures' Flex 50K rules are a YAML
  file read by one generic policy: contract limit, close-by-15:10 CT, no
  overnight or weekend holds, no opposite positions in one asset group, a
  minimum stop/target distance, the 40% consistency rule, the trailing loss
  limit that locks at $50,100.
- **Execution.** A futures adapter places every entry as one bracket (entry,
  stop and target together). Tradovate is the first platform behind it.

**No futures strategy has been tested yet.** T0's seven failed rounds were all
on forex majors, where the spread and commission ate small intraday moves.
That says nothing about futures either way: futures strategies are untested,
not proven losers. There is also no futures history to test them on yet (see
gaps below).

## Architecture map (the audit)

| Existing piece | What it does | Reusable for futures? | Change made |
| --- | --- | --- | --- |
| `TradeIntent` (`atlas_engine/intents.py`) | One proposal format for rules and Hermes | Yes | Broker order fields (`orderType`, `orderQty`, `contractId`, `accountId`, `clOrdId`, `isAutomated`, brackets...) are now refused in an intent |
| `DecisionPipeline` (`atlas_engine/pipeline.py`) | The ALLOW/REJECT/HALT/KILL stages | Yes | Market stage takes the symbol's session and refuses expired or rolling contracts; prop reasons are filed under the prop stage |
| `TradingEngine` (`atlas_engine/runtime.py`) | Reads the broker, runs the pipeline, writes only through the execution adapter | Yes | One `_session()` per symbol instead of the global forex week; adapter picked by `execution.platform`; flattens at the firm's flat-by time; no MT5 watchdog file when the broker has no server clock |
| Rule and agent setups, strategies | Produce signals and intents | Yes, unchanged | None. A setup is the same type whether a rule or Hermes made it |
| Decision models: rules, GBM, Jev | Estimate the chance the target is hit first | Yes, unchanged | None. Jev still only returns a probability |
| `RiskEngine` (`atlas_engine/risk/engine.py`) | Risk %, loss lines, open risk, sizing call | Yes | Asks a `PropPolicy` instead of reading firm rules directly; passes the target so the consistency rule can be checked |
| `size_position` (`atlas_engine/sizing/lots.py`) | `floor(budget / per-lot risk / step)` | Yes: a contract is a lot with step 1 | A futures product becomes a `ContractSpec` (value per point as the contract size, whole contracts) |
| Exposure netting (`atlas_engine/exposure/netting.py`) | Risk per currency leg | Mostly | A USD future has one leg, its asset group (all equity index futures are one bet) |
| `PropRules` (`atlas_engine/prop_rules/rules.py`) | Firm limits as data | Yes | No-daily-limit firms, a lock offset on trailing limits, a `futures:` section; `StandardPropPolicy` applies it |
| `ExecutionBroker` (`atlas_engine/execution/broker_api.py`) | The one write interface | Yes | Gains `modify_order`; `tradovate` is now a built platform |
| `MT5ExecutionAdapter`, `Executor`, MT5 adapter | MT5 orders with SL/TP on the position | Forex only | Kept as is, still tested |
| Reconciliation (`atlas_engine/reconciliation/`) | Book vs broker findings | Yes | None: the futures adapter reports positions in the same shape |
| Journal and audit (`atlas_engine/journal/`) | Rebuild a decision from the journal | Yes | Market state now records last price, volume, tick size and value, contract and session; the audit summary includes market state, risk, prop, order and fill |
| Health states (`atlas_engine/ops/health.py`) | NORMAL / DEGRADED / HALT / KILL | Yes | Spreads are measured in ticks for futures |
| Hermes profiles, MCP servers, `atlas-trading` | Agents read, propose, and (demo only) send intents | Yes, unchanged | None. They still cannot import any execution code |

MT5 assumptions found and where they now stand:

- The global forex week (`market_open`) decided whether any symbol could trade. Now forex only.
- Spreads in pips. Now ticks for futures.
- SL/TP live on the position. On futures they are separate working orders, so the adapter keeps a record of each bracket.
- The MT5 watchdog EA and its limits file. Skipped when the broker has no server clock; see gaps.
- Lot steps of 0.01. Futures trade whole contracts.
- MT5-dependent tests (`tests/engine/t4*`, `test_agent_intents`, `test_v3_*`, `tests/ops/*`, `tests/hermes/h3_gate.py`) were kept and still pass.

## The layers

    TradingEngine
      -> ExecutionBroker                    (the interface; atlas_engine/execution/broker_api.py)
         -> FuturesExecutionAdapter         (rules shared by every futures platform; execution/futures.py)
            -> TradovateAdapter             (Tradovate's REST and WebSocket; adapters/tradovate/)
               -> Tradovate -> the prop firm's account

`FuturesVenue` (`atlas_engine/adapters/futures_venue.py`) is the base every
futures platform adapter extends. It turns the platform's orders, fills and
net positions into the engine's view and keeps a durable record of each ATLAS
bracket, because Tradovate's order list doesn't return the client order ID. A
second platform, for example Rithmic, would be one more `FuturesVenue`
subclass and one line in `PLATFORMS`. No Rithmic code exists yet.

Generic order vocabulary (the adapter translates it): side BUY/SELL, type
MARKET/LIMIT/STOP/STOP_LIMIT, quantity, stop, target.

What `FuturesExecutionAdapter` enforces:

- **Whole contracts, prices on the tick grid.** Stop and target are snapped toward the entry, so risk can only shrink.
- **One ATLAS position per contract**, and none while the operator holds that contract. Futures accounts net positions, so a second bracket would merge with the first.
- **No entry in an expired or rolling contract**, checked again at execution.
- **One bracket per entry.** A market entry with its stop and target placed together; the stop and target cancel each other.
- **Recorded before it is sent**, keyed by the client order ID, so a decision never sends twice. A lost answer is matched to the platform's orders, never resent.
- **Never unprotected.** If a fill has no working stop, a stop is placed at once; if that fails, the position is closed.
- **Stops only tighten.** A stop can't be moved through the pending-order route, and a protective order can't be cancelled while its position is open.
- **Closing cancels the stop and target first, then exits at market.** If the exit doesn't fill, the stop is put back.
- **Orders and positions ATLAS didn't open are never touched.** A kill or flatten closes ATLAS positions only.
- **Housekeeping.** A re-placed stop or target isn't linked to its partner, so at every reconciliation any leg left working after its position closed is cancelled.

## Contracts and roll

`atlas_engine/futures/products.yaml` lists the products: exchange, currency,
tick size, value per point, asset group, listed months, session template,
regular hours, and which mini a micro shadows. Strategy code never branches
on these names.

- **Research symbol** is the product (`MES`): one continuous series, used by
  setups, research, the journal and Hermes.
- **Execution symbol** is the contract (`MESZ6`), pinned by the operator in
  `execution.contracts`. The adapter reads its last trade date and first
  notice date from the broker.
- **Roll is manual and deterministic.** No new entry within `roll_days`
  (default 5) of the last trade date or the first notice date, and none once
  the contract has expired. The operator pins the next contract in a signed
  config commit. There is no automatic rollover.
- On connect, the adapter compares the broker's tick size and value per point
  with the catalogue and refuses to start on any difference, so a wrong
  catalogue row stops trading rather than mis-sizing it.

Tick values checked against FundedNext's contract article: ES $12.50, MES
$1.25, NQ $5.00, MNQ $0.50, YM $5.00, MYM $0.50, MGC $1.00. RTY, M2K, CL, MCL
and GC come from memory of CME's specs, which couldn't be fetched (cmegroup.com
returned 403). They are marked `verified: false`, and the broker cross-check
covers them.

## Sizing

    risk_per_contract = stop distance / tick size x tick value + round-turn fees
    contracts         = floor(allowed risk / risk_per_contract)

then capped by the strategy, the account's open-risk and group limits, the
firm's contract limit (counted in minis; a micro is a tenth), and the product's
maximum. Example: on $50,000 at 0.25% risk ($125), a 10-point MES stop risks
$50 a contract, so the engine sizes 2.

## Sessions

`atlas_engine/futures/sessions.py`, in Chicago time so daylight saving is
handled by the time zone database:

- Sunday 17:00 to Friday 16:00, with a daily maintenance break from 16:00 to 17:00.
- The session that opens at 17:00 belongs to the next day's trading day.
- RTH is a label (08:30 to 15:00 for equity index); the calendar alone decides whether the engine may trade.
- **Holidays are handled conservatively.** CME's holiday calendar couldn't be read, so every US exchange
  holiday, plus the day after Thanksgiving, Christmas Eve and New Year's Eve (usual early closes), is a
  no-trade day, worked out by rule. The operator can add more days (`execution.no_trade_days`). Being flat
  on an open day costs a missed trade; being in on an early-close day could break a prop rule.

## Prop firm: FundedNext Futures

Checked 2026-10-05 on FundedNext's futures help centre
(helpfutures.fundednext.com) and the futures challenge terms (updated
2026-09-25). The example rule file is
`deploy/futures-config/prop_rules/fundednext_futures_flex_50k.yaml`.

| Rule | Flex 50K | How ATLAS applies it |
| --- | --- | --- |
| Profit target | $2,500 | `phases.challenge.profit_target_pct: 5` |
| Max loss | $1,500, trailing the highest end-of-day balance, locks at $50,100 | `max_loss` trailing_eod, lock offset $100; the risk engine's internal stop is 60% of it |
| Daily loss | None on Flex | `daily_loss.pct: null` |
| Contracts | 3 minis or 30 micros, mixed 1:10 | `prop_contract_limit`, counted in minis |
| Hours | Close by 15:10 CT; reopens 17:00 CT | No new entries 30 min before; ATLAS flattens 10 min before |
| Overnight, weekend | Not allowed | Follows from the daily flat-by |
| Hedging | No opposite positions in one asset group (long ES and short NQ, for example) | `prop_correlated_hedge` |
| Scalping | Trades in and out within 5 ticks, repeatedly, are prohibited | `min_bracket_ticks: 6` on stop and target |
| Consistency | Challenge only: one day's profit at most 40% of the target | `prop_consistency_cap`: skip a trade whose target would take today past it |
| Bots | Allowed (no HFT) | `eas_allowed: true` |
| News | Allowed | No news window |

Rules not enforced, and why: trading within 2% of the CME price limit
(no price-limit feed yet), and gapped or illiquid markets (a judgement call).
Legacy and Rapid programs have different numbers (Rapid Daily has a daily
loss limit); each would be its own YAML file, with no new code.

The Flex 50K max loss is 3% of the account, much tighter than FTMO's 10%, so
the example risk file is smaller: 0.25% per trade and 0.5% open risk at most.
The loader refuses the forex file's 1.5% open risk against this firm.

## Tradovate

**Why Tradovate first.** FundedNext Futures offers Tradovate, NinjaTrader and
TradingView. Tradovate is the only one with an API that a Python engine can
drive directly. NinjaTrader would need a C# NinjaScript inside its desktop
app, and TradingView has no order API for this.

**Prerequisite (unverified).** Tradovate's own community post says retail API
access needs a live funded account over $1,000 and the API Access add-on.
Whether API access works on a FundedNext evaluation account was not stated in
any document we could read. **Confirm with FundedNext and Tradovate before
paying for anything.**

What the adapter uses (Tradovate Partner API docs, partner.tradovate.com,
read 2026-10-05):

- `demo.tradovateapi.com/v1` or `live.tradovateapi.com/v1`. Which host is in
  use is the account's demo flag, so Hermes can trade only through the demo host.
- Access token from `/auth/accesstokenrequest`, renewed 15 min before its
  80 minutes run out (`/auth/renewaccesstoken`).
- `/order/placeoso` for the bracket (a market entry, then the stop and the
  target cancelling each other), plus `/order/placeorder`, `/order/cancelorder`
  and `/order/modifyorder`. Every order sends `isAutomated: true`, as
  Tradovate requires.
- `/order/list`, `/orderVersion/list`, `/fill/list`, `/position/list`,
  `/cashBalance/getcashbalancesnapshot`, and `/contract/find`,
  `/contractMaturity/item`, `/product/item` for the contract.
- Quotes over the market-data WebSocket (`md/subscribequote`); needs `pip install '.[tradovate]'`.
- Rate limits: reads are reused for `broker_poll_s` (default 5 s) and the cash
  snapshot for 30 s, which keeps the engine under 5,000 requests an hour. A
  penalty ticket waits and resends once; a captcha, a second ticket or HTTP 429
  stops it and the engine sees the broker as unavailable.
- Credentials come only from the engine host's environment
  (`ATLAS_TRADOVATE_USER`, `_PASSWORD`, `_APP_ID`, `_APP_VERSION`, `_CID`,
  `_SEC`, `_DEVICE_ID`; `ATLAS_TRADOVATE_ENV`, `ATLAS_TRADOVATE_ACCOUNT`).
  They are never logged, and a test fails if a profile or skill mentions them.

**Not tested against Tradovate itself.** No credentials or account exist yet.
Every test runs on `FakeTradovate`, a stand-in that answers the same endpoints
with Tradovate-shaped data. The first real run should be on the demo host.

## Safety boundaries

Tests that fail the build if broken (`tests/engine/test_v3_boundaries.py`,
`test_futures_engine.py`, `test_tradovate.py`):

- Hermes, the API, the MCP servers and plugins can't import any execution or platform adapter code.
- Setups, strategies, features, decisions, intents and research can't reach an order path. Backtests therefore can't reach live execution.
- Only the engine host's entry point (`atlas_api/engine_cli.py`) builds a Tradovate adapter, and the engine itself never imports one.
- Decision models, Jev included, can't reach orders, accounts, risk or config.
- An intent carrying a broker order field is refused before the pipeline.
- Hermes's trades go through the same pipeline; the engine sizes them, and refuses them on a live account.
- A kill or flatten closes ATLAS positions only, through the execution adapter.

## How to run it

The example config is in `deploy/futures-config/`, outside `config/`, because
config changes only by a signed operator commit. A drill on the stand-in:

    atlas-engine run --config deploy/futures-config --state /tmp/atlas-drill --tokens tokens.yaml \
                     --broker fake-tradovate --allow-writable-config

`--broker tradovate` uses the real API with the environment variables above.
`--broker` must match `execution.platform` in the config.

## Gaps (open, not invented)

1. **Futures history for research.** Dukascopy, the current data source, is
   forex only. T0 can't test a futures strategy until a source is chosen
   (for example a paid CME data vendor, or Tradovate's own bars).
2. **Bars from Tradovate.** Live rule strategies need minute bars; Tradovate
   serves them over the WebSocket (`md/getChart`), which isn't wired yet. Until
   then rule sources report the broker as unavailable and only intents with
   their own prices (Hermes) can trade.
3. **Session features.** The shared features still label London / New York
   forex sessions. Futures strategies will want RTH / overnight labels.
4. **CME price limits.** The firm's "not within 2% of the price limit" rule
   needs a price-limit feed.
5. **Holiday calendar.** Conservative rule-based no-trade days, because CME's
   calendar page was blocked. Replace with the exchange's published calendar
   when it can be read.
6. **Commission per contract.** Not known for a FundedNext Tradovate account;
   set `execution.commission_per_lot` once it is.
7. **No independent backstop like the MT5 watchdog EA.** On Tradovate the
   firm's own auto-liquidation at the loss limit is the last line.
8. **Polling instead of Tradovate's real-time user sync.** The WebSocket
   `user/syncrequest` would replace the REST polling and cut requests further.
9. **Tradovate API access on a FundedNext account** (see the prerequisite above).
