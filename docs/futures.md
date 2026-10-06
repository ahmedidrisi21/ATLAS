# Futures-first execution

Written 2026-10-05, revised 2026-10-06. The owner moved ATLAS from MT5 forex
to futures first (ES/MES, NQ/MNQ, YM/MYM, RTY/M2K, CL/MCL, GC/MGC), through a
prop firm (FundedNext Futures as the worked example), and on 2026-10-06 made
**NinjaTrader the primary execution platform**. This page says what changed,
what did not, what was checked and where, and what is still open. Claims about
outside systems are labelled VERIFIED (read in an official document, with the
date), ASSUMED (built on, not yet confirmed), UNVERIFIED (asked, no document
answers it) or NOT IMPLEMENTED.

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
  stop and target together), verifies the protection at the platform, and
  HALTs when it can't. NinjaTrader is the platform behind it, through
  NinjaTrader's official API, which turns out to be the Tradovate API
  ("One API, two names" below).
- **Not yet confirmed:** that a FundedNext account can be traded through that
  API at all. See "FundedNext compatibility".

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
| `ExecutionBroker` (`atlas_engine/execution/broker_api.py`) | The one write interface | Yes | Gains `modify_order`; `ninjatrader` is the built futures platform (`tradovate` is an alias) |
| `MT5ExecutionAdapter`, `Executor`, MT5 adapter | MT5 orders with SL/TP on the position | Forex only | Kept as is, still tested |
| Reconciliation (`atlas_engine/reconciliation/`, `runtime.reconcile`) | Book vs broker findings | Yes | The futures adapter adds findings (unprotected positions, foreign orders); an unreadable platform is a finding, not a stale copy |
| Journal and audit (`atlas_engine/journal/`) | Rebuild a decision from the journal | Yes | Market state records last price, volume, tick size and value, contract, session and data provenance; a new `execution_events` table records each lifecycle step; the prop check records its policy version |
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
            -> NinjaTraderAdapter           (NinjaTrader's official REST and WebSocket API; adapters/ninjatrader/)
               -> NinjaTrader (Tradovate) -> the prop firm's account

The brief's adapter list, and what each name is in the code:

| Brief | In the code | Status |
| --- | --- | --- |
| `ExecutionBroker` | `ExecutionBroker` protocol, `PLATFORMS` registry | Built |
| `NinjaTraderAdapter` (primary) | `atlas_engine.adapters.ninjatrader.NinjaTraderAdapter`, platform `ninjatrader` | Built, tested on the stand-in only |
| `TradovateAdapter` | The same class: `tradovate` is accepted as an alias (config and `--broker`) | Built (same API, see "One API, two names") |
| `RithmicAdapter` | Would be one more `FuturesVenue` subclass and one line in `PLATFORMS` | NOT IMPLEMENTED |
| `SimulatedBroker` | `FakeNinjaTrader`, the API stand-in, behind the real `NinjaTraderAdapter` and `FuturesExecutionAdapter` | Built for tests and drills |

**Conflict with the brief, explained.** The brief lists `NinjaTraderAdapter`
and `TradovateAdapter` as two adapters. NinjaTrader's official developer API
*is* the Tradovate API (next section), so two adapters would be two copies of
the same code. There is one, named NinjaTrader, and `tradovate` is an alias
of it. Likewise a separate `SimulatedBroker` class would be a second
execution path; instead the simulation is the platform stand-in underneath
the same adapter, so a drill exercises exactly the code a live run uses.

`FuturesVenue` (`atlas_engine/adapters/futures_venue.py`) is the base every
futures platform adapter extends. It turns the platform's orders, fills and
net positions into the engine's view and keeps a durable record of each ATLAS
bracket (the ledger), written before anything is sent.

## Order model

Every order ATLAS sends is a broker-neutral `OrderRequest`
(`atlas_engine/adapters/orders.py`), built only inside the execution adapter
after the pipeline sized the trade:

| Field | Meaning |
| --- | --- |
| `symbol` | the dated contract (MESZ6), resolved by the engine from the product |
| `side` | BUY or SELL |
| `quantity` | whole contracts; a fraction is refused, never rounded |
| `order_type` | MARKET, LIMIT, STOP, STOP_LIMIT |
| `limit_price`, `stop_price` | required by the type, positive and finite |
| `time_in_force` | DAY or GTC; none on a market order |
| `reduce_only` | may only shrink ATLAS' own position (stops, targets, exits) |
| `stop_loss`, `take_profit` | together: the entry goes out as a bracket |
| `client_order_id` | deterministic, from the decision ID; legs add `-S`, `-T`, re-placed legs `-S2`/`-T2`, exits `-X1` |
| `strategy_id`, `trade_intent_id` | for the journal |

`OrderRequest.problems()` names everything wrong with an order, and the
venue refuses a malformed one before the platform sees it. NinjaTrader's API
has no reduce-only flag (VERIFIED: not in the PlaceOrder schema), so the venue
checks it against ATLAS' own open quantity on the contract. No trade intent
can carry any of these fields; the intent parser refuses them.

## Adapter methods

| Method | NinjaTrader API source | Status |
| --- | --- | --- |
| connect / disconnect / reconnect / health | `/auth/accesstokenrequest`, `/account/list`, market-data WebSocket | Built |
| get_account / get_balance / get_equity | `/cashBalance/getcashbalancesnapshot` (equity adds open P&L at live quotes) | Built |
| get_margin | the same snapshot's `initialMargin`, `maintenanceMargin`, `autoLiqLevel` | Built |
| get_buying_power | no such field in the API | NOT IMPLEMENTED |
| get_positions | `/position/list` plus the ledger | Built |
| subscribe_market_data / get_quote / get_last_price | `md/subscribequote` over the WebSocket, at connect, for the pinned contracts | Built (subscriptions fixed at connect) |
| unsubscribe_market_data | | NOT IMPLEMENTED (only pinned contracts are subscribed) |
| get_depth | `md/subscribeDOM` exists in the API | NOT IMPLEMENTED |
| submit_order | `/order/placeoso` (bracket), `/order/placeorder`, `/order/placeoco` (protective pair) | Built |
| modify_order / cancel_order / cancel_all_orders | `/order/modifyorder`, `/order/cancelorder`; ATLAS orders only | Built |
| get_order / get_orders | `/order/list`, `/orderVersion/list` | Built |
| get_fills | `/fill/list` | Built (read into deals and positions) |
| subscribe_execution_events | `user/syncrequest` over the WebSocket | NOT IMPLEMENTED: the adapter polls REST every `broker_poll_s` |
| reconcile_orders / reconcile_positions / reconcile_account | `findings()` and the position diff, every `reconcile_interval_s` | Built |

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

## NinjaTrader

### One API, two names (VERIFIED, 2026-10-06)

NinjaTrader's developer page (developer.ninjatrader.com/products/api) says
"The API is exposed as a REST API with Swagger definitions" with "real-time
and historical market data utilizing high performing websockets". Its API
documentation link serves the same specification as api.tradovate.com, titled
"Tradovate API", with hosts `live.tradovateapi.com/v1` and
`demo.tradovateapi.com/v1`, and an embedded "NinjaTrader API License
Agreement". NinjaTrader Group owns Tradovate. So the adapter ATLAS built for
Tradovate on 2026-10-05 already speaks NinjaTrader's official API; this round
renamed it and hardened it rather than writing a second one.

### Routes to NinjaTrader

| Route | What it is | Status |
| --- | --- | --- |
| NinjaTrader API (REST + WebSocket, the Tradovate API) | Official, documented, Python can call it | **Built** (this adapter) |
| NinjaTrader 8 desktop, NinjaScript `Account` class | Official C# API inside the desktop app: `CreateOrder`, `Submit`, `Change`, `Cancel`, `CancelAllOrders`, `Flatten`, `OrderUpdate` / `ExecutionUpdate` / `PositionUpdate` events (VERIFIED, docs.ninjatrader.com). An outside engine would need a custom AddOn as a bridge on a Windows host; NinjaTrader ships none | NOT IMPLEMENTED; the fallback if the API route is closed to FundedNext accounts |
| Partner APIs (NT Connect, NT Prop) | For brokers and prop firms (organization admin credentials, API key, CID); prop firms create evaluation accounts with them | Not for traders; not used |
| Third-party bridges (e.g. CrossTrade) | Not NinjaTrader's own | Not used |

### Access requirements (VERIFIED from the official specification's text)

"In order to access the features of the Tradovate REST API you'll need to sign
up for a Tradovate Trader account": a **LIVE account with more than $1,000 in
equity**, a **subscription to API Access**, and an **API key**, which is then
exchanged for an access token. Access tokens last **80 minutes** and carry an
`expirationTime`; renew with `/auth/renewaccesstoken` before then (partner
auth overview). The adapter renews 15 minutes early and logs in again after a
401.

### What the adapter uses, and how sure we are

| Area | Detail | Label |
| --- | --- | --- |
| Hosts | `demo.tradovateapi.com/v1`, `live.tradovateapi.com/v1`; market data `wss://md.tradovateapi.com/v1/websocket` | VERIFIED (spec servers); the md host from the partner docs read 2026-10-05 |
| Auth | `/auth/accesstokenrequest` with name, password, appId, appVersion, cid, sec, deviceId; `/auth/renewaccesstoken` | VERIFIED |
| Orders | `/order/placeoso` (entry with `bracket1` stop and `bracket2` limit), `/order/placeoco` (two orders, one cancels the other; answer `{orderId, ocoId}`), `/order/placeorder`, `/order/modifyorder`, `/order/cancelorder` | VERIFIED |
| `isAutomated` | "must be true if this isn't an order made directly by a human"; ATLAS sets it on every order | VERIFIED |
| Client order IDs | `clOrdId` (max 64) on PlaceOrder, PlaceOSO, PlaceOCO and on each bracket leg; the Order entity returns it | VERIFIED |
| OCO link | Order entity has `ocoId`, `parentId`, `linkedId` | VERIFIED |
| Bracket legs share an `ocoId` | ATLAS requires it before calling a position protected | ASSUMED until a demo run; if not, ATLAS re-places the legs with `placeoco`, which links them |
| `placeoco` answer's `ocoId` is the other order's id | Used to record the target leg | ASSUMED; the legs are re-checked from `/order/list` either way |
| Order states | Canceled, Completed, Expired, Filled, PendingCancel, PendingNew, PendingReplace, Rejected, Suspended, Unknown, Working; ATLAS treats Unknown as working | VERIFIED |
| Rejections | `failureReason` (e.g. LiquidationOnly, MaxPosLimitReached, SessionClosed, TradingLocked) and `failureText` | VERIFIED |
| Reads | `/order/list`, `/orderVersion/list`, `/fill/list`, `/position/list`, `/cashBalance/getcashbalancesnapshot`, `/account/list`, `/contract/find`, `/contractMaturity/item` (`expirationDate`, `firstIntentDate`, `isFront`), `/product/item` (`tickSize`, `valuePerPoint`) | VERIFIED |
| Rate limits | A throttled call answers with `p-ticket`, `p-time` and maybe `p-captcha`; retry once after `p-time` with the ticket; a captcha means stop | VERIFIED (spec) |
| Request budget | About 5,000 requests an hour; ATLAS reuses reads for `broker_poll_s` (5 s) and the cash snapshot for 30 s | ASSUMED (community figure, not in the spec) |
| WebSocket | `authorize`, `md/subscribequote`, `[]` heartbeat every 2.5 s, reconnect on drop | From the partner docs (2026-10-05); not run against the real socket |
| Contract roll data | `isFront` reported in `health()["not_front"]`; ATLAS never switches contracts by itself | VERIFIED field; its meaning for liquidity is ASSUMED |

**Nothing has run against NinjaTrader itself.** No credentials or account
exist. Every test runs on `FakeNinjaTrader`, a stand-in that answers the same
endpoints with the specification's shapes.

## FundedNext compatibility

Checked 2026-10-06. The nine questions from the brief, each with what a
document says and what it doesn't.

| # | Question | Answer | Label |
| --- | --- | --- | --- |
| 1 | Do FundedNext-issued NinjaTrader credentials support API access? | No document says. FundedNext's NinjaTrader login article (updated 2026-08-19) has the trader enter the username and password FundedNext provides and launch from the *simulation* section, so FundedNext accounts look like simulation accounts on NinjaTrader's side. It does not mention the API. A NinjaTrader forum question asking exactly this (Aug 2025) has no staff answer | UNVERIFIED |
| 2 | Is a separate NinjaTrader account needed? | The API needs a LIVE account with over $1,000 equity. A FundedNext account is not that, so a separate funded NinjaTrader/Tradovate account appears needed just to unlock API access. Whether that unlock then reaches the FundedNext account is not stated | Requirement VERIFIED; effect on FundedNext UNVERIFIED |
| 3 | Is an API subscription needed? | Yes, "a subscription to API Access" | VERIFIED (general requirement) |
| 4 | Is an API key needed? | Yes, generated by the trader, then exchanged for a token | VERIFIED (general requirement) |
| 5 | Can the API send automated orders to a FundedNext account? | Not stated anywhere we could read | UNVERIFIED |
| 6 | Does FundedNext allow this architecture? | The futures challenge terms (2.2.6): "Using automated trading bots, artificial intelligence, or ultra-high-speed execution strategies is allowed", and FundedNext may ask to see the system. An external engine on the API is not mentioned specifically. (FundedNext's CFD platform page says automation is MetaTrader-only; that is the CFD business, not futures) | Bots VERIFIED; this exact route UNVERIFIED |
| 7 | Can the account be used in the normal NinjaTrader UI at the same time? | Not stated. ATLAS assumes the operator may trade it and treats any position or working order it did not place as foreign: it never touches them and HALTs until they are gone | UNVERIFIED |
| 8 | Restrictions on automated order flow? | Prohibited: multi-order spam, latency arbitrage, spoofing, layering, grid trading, wash trading, hedging with correlated instruments, trading in gapped or illiquid markets, within 2% of CME price limits; scalping in and out within 5 ticks | VERIFIED (challenge terms) |
| 9 | Does the API work in simulation/demo first? | The API has a demo host (`demo.tradovateapi.com`). Whether a FundedNext login can use it is the same open question as 1 | Demo host VERIFIED; for FundedNext UNVERIFIED |

**What to ask, in one message to FundedNext support and NinjaTrader:** "Can a
FundedNext Futures account be traded through the NinjaTrader (Tradovate) REST
API by my own automated program, and if so, which account must hold the API
Access subscription and API key?" Until that is answered yes, the fallback is
the NinjaTrader 8 desktop bridge, which works with the normal login but has to
be written in C# and run on Windows.

## MCP servers from NinjaTrader and FundedNext (checked 2026-10-06)

Both firms now run MCP servers. Neither changes the rule that only the engine
places orders; both are useful.

| | NinjaTrader MCP | FundedNext MCP |
| --- | --- | --- |
| URL | `https://mcp-demo.tradovateapi.com/mcp` (Demo), `https://mcp-live.tradovateapi.com/mcp` (Live) | `https://mcp.fundednext.com` |
| Status | Beta, "not yet generally available"; hostnames and tools may change (VERIFIED, docs.ninjatrader.com/mcp) | Live since 2026-08 (VERIFIED, helpfutures.fundednext.com article 17230016, updated 2026-09-30) |
| Sign-in | OAuth 2.1 with the NinjaTrader login in a desktop browser; access token ~80 min, rotating refresh token ~26 h; session ends after ~1 h idle | OAuth 2.0 plus a one-time token from the FundedNext dashboard |
| Can trade? | Yes: `place_order` (with OCO/OSO brackets as price offsets), `modify_order`, `cancel_order`, `close_position`, `update_risk_settings` | No. Strictly read-only |
| Reads | `my_portfolio`, `market_snapshot`, `market_history`, `dom_snapshot`, `search_contracts`, `estimate_order`, `economic_calendar`, order / fill / position / cash history, `performance_summary`, `risk_settings` | Balances, trade history, drawdown room, consistency-rule status, breaches, payout eligibility, across CFD and futures (Tradovate) accounts |
| Extra safety | Per-connection limits set on the consent screen: max exposure, max traded volume per session, and a product/contract allowlist, enforced by NinjaTrader on every opening order | n/a |
| API Access subscription / $1,000 live account | Not mentioned in the MCP docs (the REST API needs them). Whether a FundedNext login can authorize it: UNVERIFIED | n/a |

How ATLAS could use them, within AGENTS.md:

- **FundedNext MCP, read-only, for the reporting personas** (atlas-performance,
  atlas-operations): a second opinion from the firm itself on drawdown room and
  rule breaches, to compare against ATLAS' own risk lines. NOT IMPLEMENTED.
- **NinjaTrader MCP read tools for atlas-market** (`economic_calendar`,
  `market_snapshot`, `market_history`): the economic calendar the Hermes trader
  was waiting for. NOT IMPLEMENTED. Its write tools must never be given to a
  Hermes profile: that would be an order path around the engine.
- **NinjaTrader MCP as the engine's transport** (a second `FuturesVenue` that
  calls the MCP tools instead of REST): chosen 2026-10-06 as the route to a
  **free NinjaTrader demo account**, because the REST API needs a funded live
  account and the paid API Access add-on and the owner has bought nothing yet.
  The connection is built (below); the venue is built once the server's real
  answers are recorded. Against it, still: beta, no client order ID on
  `place_order` (a `TrackingTimeout` leaves the outcome unknown), bracket legs
  as offsets, hourly idle sessions.
- **Its connection risk limits** (exposure cap, MES-only allowlist) are a
  broker-side backstop independent of ATLAS, whichever route places orders,
  if the engine's connection is authorized through the same OAuth.

### The MCP route to a free demo account (connection and venue BUILT 2026-10-06)

`atlas_engine/adapters/ninjatrader/mcp.py`, used only by the engine host.

| Part | State |
| --- | --- |
| Discovery: protected-resource metadata names `https://demo.tradovateapi.com`; its metadata gives `web.ninjatrader.com/oauth?env=demo`, `/auth/oauthtoken`, `/auth/register`, PKCE S256, public clients | VERIFIED (live probe 2026-10-06) |
| Registration with a `http://localhost:<port>/callback` return address. The server answers every registration with the same shared public client ("NinjaTrader MCP") and refuses addresses off its allow list. Two sign-ins with a `127.0.0.1` address ended in `access_denied` (cause not certain); `localhost` worked | VERIFIED (2026-10-06) |
| Code exchange (form body, PKCE verifier, `resource`) | VERIFIED (first sign-in, 2026-10-06, free practice account) |
| Refresh (JSON body with `resource`, rotating refresh token, keep the saved token on failure) | VERIFIED (live refresh, 2026-10-06) |
| Streamable HTTP JSON-RPC, session id, re-initialize after an idle session ends, one retry after a 401 | Initialize, tools/list (27 tools) and a read call VERIFIED; the idle-session restart is not yet seen live |
| A free simulation login is accepted, with no paid API add-on | VERIFIED (2026-10-06) |
| The shape of every tool's answer | VERIFIED for the read tools (recorded in `docs/ninjatrader-mcp-capture/`, demo, 2026-10-06); write answers from `describe` only, until the first practice order |
| The venue's reads (account, contract, quote, orders, fills, positions) against the real server | VERIFIED (2026-10-06); its writes are tested on a stand-in only |
| Quotes without a CME data subscription | `dataFeedMode: Delayed`, about 600 s behind (VERIFIED). The pipeline refuses a quote older than `decision.max_quote_age_s` (default 30 s) with `quote_not_live`, so the practice account cannot trade on them |

What ATLAS lets the connection do, enforced in the client, whatever the
consent screen grants:

- Demo only: there is no live server in `SERVERS`.
- Read tools for anyone holding a client; `place_order`, `modify_order`,
  `cancel_order`, `close_position` only for a client built with `orders=True`,
  which only the futures venue will build, behind the decision pipeline.
- Never `update_risk_settings` or the alert tools.
- Tokens in `<state>/ninjatrader-mcp-token.json`, mode 600; never logged or
  shown to Hermes. The browser's return address carries a one-time code that
  is useless without the verifier ATLAS kept.

Operator steps: `atlas-engine ninjatrader-mcp login --state <dir>` (add
`--paste` when the browser is on another computer), then `check`, then
`capture`. On the consent screen grant Trade only when the engine is going to
trade, never Manage Risk Settings or Alerts, and set the connection's risk
limits: MES only, max total exposure 2.

The venue (`atlas_engine/adapters/ninjatrader/mcp_venue.py`, `--broker ninjatrader-mcp`) and where it
differs from the REST adapter:

- No client order ID on `place_order`: after a lost answer the entry is found by contract, side, quantity
  and time (`FuturesVenue.adopt`); an ambiguous match HALTs reconciliation.
- Bracket legs are offsets from the entry's fill: ATLAS sends them from the quote it decided on, then moves
  the legs onto the decided prices with `modify_order` (absolute prices) once the entry has filled.
- Legs appear only after the entry fills; the protection check picks up late ones instead of replacing them.
- A leg's `bracket.ocoId` is its sibling's id; the OCO group is the smaller id.
- No standalone linked stop and target, so a position whose legs are gone is closed and the engine HALTs.
- `close_position` is never called (it would flatten the operator's contracts too); exits are reduce-only
  market orders. `update_risk_settings` and the alert tools are refused by the client.
- Bars (`market_history`) are NOT wired: the engine's bar path is built for MT5's server clock.

## Order lifecycle and brackets

What `FuturesExecutionAdapter` does with an ALLOWed trade, step by step, each
step journaled (see Observability):

1. **Checks again at execution:** whole contracts; prices on the tick grid,
   stop and target snapped toward the entry so risk only shrinks; no expired
   or rolling contract; one ATLAS position per contract and none while the
   operator holds it (accounts net); price not moved past the deviation limit;
   a well-formed `OrderRequest`.
2. **Records the bracket** in the ledger, keyed by the client order ID, before
   sending. A decision never sends twice.
3. **Sends one bracket:** a market entry with its stop and target attached.
4. **Unknown outcome** (timeout, lost answer, unusable answer): finds the
   order on the platform by its client order ID, never resends. If the
   platform shows nothing, the bracket is marked failed; if it can't tell, the
   engine records the outcome as unknown and reconciliation decides.
5. **Waits for the fill.** Unfilled: the bracket is cancelled. Partly filled:
   the rest is cancelled and the legs are resized to the fill.
6. **Verifies protection** at the platform: a working STOP and a working LIMIT
   on the opposite side, each for exactly the open quantity, at the recorded
   prices, sharing one OCO id.
7. **Repairs** anything short of that at once. A fresh linked pair is placed
   with `placeoco` before the old legs are cancelled, so the position is never
   without a stop.
8. **If it still doesn't verify, closes the position** and returns
   `unprotected_closed`; the engine alerts and reconciles immediately. An
   unprotected position is never silently accepted.

After that: stops only tighten; a protective order can't be cancelled while
its position is open; closing cancels the legs first, then exits at market and
puts the protection back if the exit doesn't fill; orders and positions ATLAS
didn't open are never touched; housekeeping cancels any leg left working after
its position closed.

## Reconciliation and failure behaviour

At start-up and every `reconcile_interval_s`, and at once after an
unprotected fill, the engine compares its book with the platform:

| Finding | What happens |
| --- | --- |
| Position diff (missing, orphan, volume, stop) | As before: adopt from the journal, restore the stop and target, or close |
| `unprotected_position` | An open ATLAS position whose protection doesn't verify after one repair: HALT |
| `foreign_order` | A working order on a traded contract that ATLAS didn't place: HALT, left for the operator |
| `foreign_position` | A position ATLAS didn't open: HALT, left for the operator |
| `broker_state_unreadable` | The platform's answer was missing or malformed: HALT. The engine no longer reconciles against a stale copy as if it were fresh |

HALT lasts while any finding stands and clears when a later reconciliation is
clean. Other failure behaviour:

| Failure | Behaviour |
| --- | --- |
| Request timeout on a command | Found by client order ID; never resent |
| Duplicate or late answer | The ledger and the client order ID make the second send a `duplicate` |
| Malformed rows or answers | `BrokerUnavailable`; the command's outcome is treated as unknown |
| 401 / expired token | Log in again once, then fail |
| Penalty ticket | Wait `p-time` (up to 60 s) and resend once; a captcha, a second ticket or HTTP 429 stops |
| Quote socket drops | It reconnects; stale quotes put the symbol in DEGRADED; a dead stream for over 60 s is HALT |
| Restart | The ledger and the platform's client order IDs rebuild every ATLAS bracket; anything else is foreign |
| Kill switch | Cancels ATLAS orders, flattens ATLAS positions and disables trading, from a signed operator command. No model or LLM is involved, and no agent can lift KILL |

## Observability

Every step is a row in the journal's `execution_events` table, keyed by the
decision ID where there is one:

`trade_intent_created`, `validation_started`, `validation_completed`,
`probability_requested`, `probability_returned`, `ev_calculated`,
`risk_calculated`, `size_calculated`, `exposure_checked`,
`prop_policy_checked`, `execution_requested`, `order_submitted` (the full
`OrderRequest`), `order_acknowledged`, `order_rejected`, `fill_received`,
`stop_attached`, `target_attached`, `position_opened`, `position_closed`,
`protection_unverified`, `protection_replaced`, `protection_failed`,
`reconciliation_started`, `reconciliation_completed`, `halted`, `killed`.

With the existing tables (decisions, market states with data provenance, model
evaluations, risk checks with the prop policy version, orders, fills, trades),
`atlas_engine/journal/audit.py` rebuilds why ATLAS traded or rejected, the
probability, the risk limit, the quantity, the policy version, the order sent
and what the platform did, from the journal alone.

**Prop policy versions.** `StandardPropPolicy.version` is the rule file's
checked date plus a hash of the parsed rules and ATLAS' margins
(`2026-10-05:3f1c...`). Any change to the rules changes it, and it is journaled
with every prop check. An optional `futures.products` list in the rule file
refuses any product the firm doesn't list (`prop_instrument_not_allowed`).

**Market data.** Each journaled market state carries a `DataProvenance`
(`atlas_engine/market_data/provenance.py`): source, mode (live, paper, replay,
historical, backtest), dated contract, session, adjustment, the quote's age and
a quality flag. A continuous futures series without a stated adjustment is
refused as a problem. A single source abstraction for live, historical, replay
and backtest data is NOT IMPLEMENTED: there is no futures history yet to put
behind it.

**Jev and Hermes.** Jev still returns only `p_target_first`, regime, reason
codes and model version; timeouts, malformed output, NaN, out-of-range
probabilities and unknown versions fail closed (existing tests). A Hermes idea
is scored by its own stated probability on demo, unless the operator assigns
its strategy to a model in config (for example `hermes: jev`); then Jev scores
it like any rule signal. Neither chooses the quantity.

## Security

Credentials come only from the engine host's environment
(`ATLAS_NINJATRADER_USER`, `_PASSWORD`, `_APP_ID`, `_APP_VERSION`, `_CID`,
`_SEC`, `_DEVICE_ID`; `ATLAS_NINJATRADER_ENV` demo or live;
`ATLAS_NINJATRADER_ACCOUNT`). `Credentials` never prints a secret, errors
carry the platform's message but never the request body, and the journal
records `OrderRequest`s, which have no credential fields. A test fails if a
Hermes profile or skill mentions the variables.

## Safety boundaries

Tests that fail the build if broken (`tests/engine/test_v3_boundaries.py`,
`test_futures_engine.py`, `test_ninjatrader.py`,
`test_ninjatrader_safety.py`):

- Hermes, the API, the MCP servers and plugins can't import any execution or platform adapter code, or the order model.
- Setups, strategies, features, decisions, intents and research can't reach an order path.
- No file outside the NinjaTrader adapter (scripts, CLI, MCP tools, Hermes profiles and skills, deploy files) contains a platform order command, a platform host or a desktop-bridge client name.
- Only the engine host's entry point builds the platform adapter; the engine never imports one.
- Decision models, Jev included, can't reach orders, accounts, risk or config.
- An intent carrying a broker order field is refused before the pipeline.
- Hermes's trades go through the same pipeline; the engine sizes them, and refuses them on a live account.

## Rollout and simulation

Order: **stand-in -> paper -> NinjaTrader demo -> FundedNext**. A funded
account is never where basic API bugs are found.

    atlas-engine run --config deploy/futures-config --state /tmp/atlas-drill --tokens tokens.yaml \
                     --broker fake-ninjatrader --allow-writable-config

`--broker ninjatrader` uses the real API with the environment variables above
(`tradovate` and `fake-tradovate` are accepted as the same thing). `--broker`
must match `execution.platform` in the config. The example config lives in
`deploy/futures-config/`, outside `config/`, because config changes only by a
signed operator commit.

## Production-readiness checklist

Not production-ready. Each line is what must be true first.

| Item | Status |
| --- | --- |
| FundedNext confirms API access for its accounts (questions 1, 2, 5, 9) | UNVERIFIED |
| API Access subscription and API key on the right account | NOT DONE |
| First run on the NinjaTrader demo host: login, token renewal, account list, contract lookup | NOT DONE |
| Demo: a bracket's legs share an `ocoId` (else ATLAS re-places them every time) | ASSUMED |
| Demo: `clOrdId` comes back on `/order/list` | VERIFIED in the spec, not yet observed |
| Demo: market-data WebSocket quotes, heartbeat, reconnect | NOT DONE |
| Demo: forced flat before 15:10 CT works on a real clock | NOT DONE |
| Commission per contract set (`execution.commission_per_lot`) | NOT DONE |
| Futures history and a futures strategy that passes T0 | NOT DONE |
| Live bars for rule strategies (`md/getChart`) | NOT IMPLEMENTED |
| Real-time user sync (`user/syncrequest`) instead of polling | NOT IMPLEMENTED |
| Independent backstop like the MT5 watchdog | NOT IMPLEMENTED (the firm's own auto-liquidation is the last line) |
| CME price-limit feed (firm rule) | NOT IMPLEMENTED |
| CME holiday calendar instead of conservative rules | NOT IMPLEMENTED |
| Operator signs the futures config into `config/` | NOT DONE |

## Gaps (open, not invented)

1. **API access on a FundedNext account** (above). Ask before paying for anything.
2. **Futures history for research.** Dukascopy is forex only. T0 can't test a
   futures strategy until a source is chosen (a CME data vendor, or
   NinjaTrader's own historical data).
3. **Bars from NinjaTrader.** Live rule strategies need minute bars, served over
   the WebSocket (`md/getChart`); not wired. Until then only intents with their
   own prices (Hermes) can trade.
4. **Session features.** Shared features still label London / New York forex
   sessions; futures strategies will want RTH / overnight labels.
5. **CME price limits, holiday calendar, commission** (checklist above).
6. **No independent backstop like the MT5 watchdog EA.**
7. **Polling instead of real-time user sync.**
8. **NinjaTrader 8 desktop bridge**, if the API route is closed.
