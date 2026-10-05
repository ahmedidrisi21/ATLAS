# Hermes as a trader on the demo account

**Decision, 2026-10-05 (owner):** Hermes trades itself and uses other tools to
get better at it. This reverses the v3 design, where only the rule-based
engine traded and Hermes only supervised (PRD §9's "no ATLAS Trader bot that
trades"). What does not change: agents never trade a live account in v3
(PRD §11, open question 7), the operator alone enables trading, and the engine
alone talks to MT5 and sizes trades.

## The honest starting point

- ATLAS tested twelve rule-based strategies on 2019 to mid-2025 data in seven
  rounds, and none made money after the broker's costs (docs/t0-edge-discovery.md).
  An AI placing the trades doesn't change that maths. A trade still has to beat
  the spread, commission and overnight charges, which cost roughly 0.05R to 0.25R
  a trade depending on how far away the stop is.
- Hermes can't be tested on history. A language model may already know what
  happened in 2019-2025, so a backtest of its decisions would look better than
  it really is. The only fair test is forward: trades on the demo account, in
  real time, counted after costs.
- So this is an experiment with a pass mark fixed in advance (below), not a
  switch that makes ATLAS profitable.

## What was built

| Piece | Where |
| --- | --- |
| Engine: agent trade intents, its own positions, live bars, the scorecard | `atlas_engine/agent_intents.py`, `atlas_engine/runtime.py` (`agent_*`) |
| Engine API: seven trading routes, scopes `trading:read` and `trading:demo` | `atlas_api/trading.py`, `atlas_api/ops.py` |
| MCP server `atlas-trading` | `atlas_mcp/servers.py`, `atlas_mcp/scopes.py` |
| Hermes profile `trader` (frontier model tier) | `atlas-profiles/atlas-trading/`, `atlas-profiles/roster.yaml` |
| Skill `demo-trading`: one trading session, step by step | `atlas-skills/demo-trading/SKILL.md` |
| Hourly "Trading session" card, weekdays 07:05-19:05 UTC | `deploy/hermes/cron.yaml` (`atlas-trading-session`) |
| `atlas-engine scorecard --state ...` for the operator | `atlas_api/engine_cli.py` |
| Tests, and a 39th T4 failure-injection scenario | `tests/engine/test_agent_intents.py`, `tests/engine/t4_scenarios.py` |

### The trader's tools

| Tool | What it does |
| --- | --- |
| `get_live_market` | Live bid/ask, spread now and its recent median, and closed M15/H1/H4/D1 bars from the broker |
| `submit_trade_intent` | Opens one trade: symbol, buy or sell, stop price, target price, confidence (0-1) and a written reason |
| `get_intent_status` | Whether an intent filled or was refused and why, and its result in R once closed |
| `list_my_positions` | Its open positions with profit so far in R, and its limits left today |
| `close_my_position` | Closes one position it opened |
| `tighten_stop` | Moves the stop of one of its positions closer to the price |
| `my_track_record` | Its closed trades in R after costs, its calibration, every other setup on the account, and the verdict |
| `system_status`, `health_state` | Read-only engine state (from atlas-operations) |
| Hermes web search | News, central banks and today's economic calendar |

### What keeps it safe

Every intent takes the same path as a rule-based signal (`TradingEngine.submit`):
health state, T3 risk engine (risk per trade, daily and drawdown stops,
currency exposure, correlation, trades per day, FTMO rules), sizing from the
broker's contract spec, then the executor's checks (stop and target on the
right side, spread at most 20% of the stop, stop and target sent to the broker
with the order). On top of that, rules in code, not config:

- **Demo only.** The engine refuses every agent intent, close and stop change
  unless the broker reports a demo account, and raises a critical alert if one
  arrives on a live account. `agent_intents.enabled_modes` accepts `paper` and
  `demo`; `live` is refused when the config loads.
- **No lot size.** No argument sets a volume; the engine sizes every trade.
- **Only its own positions.** It can close or tighten only positions tagged
  `hermes`. Stops only move towards the price.
- **Its own limits:** 4 intents a day and 2 open positions by default, a target
  1-5 times as far as the stop, and a written reason of at least 40 characters.
  The PRD §27 `agent_intents:` section in `config/atlas.yaml` can change these;
  it is absent today, so the defaults apply. Changing it is a signed operator commit.
- **Retries are safe.** The same `intent_id` never opens a second trade.
- **The operator still enables trading.** With trading disabled, intents come
  back `skipped` and cost the trader nothing from its daily limit.
- Every intent, close and stop change is journaled in `agent_actions` with the
  reason, and positions carry their own magic number (base + 900).

The trader gets no shell, no file access and no way to disable or enable
trading; atlas-operations keeps `disable_trading`.

## How Hermes's record is measured

Everything is in **R after costs**: profit or loss divided by the money at risk
when the trade was sized, after spread, commission and swap as the broker
booked them. 1R means "won as much as it risked".

**Pass mark, fixed before the first trade** (`atlas_engine/agent_intents.py`):

| Verdict | Rule |
| --- | --- |
| `too_early` | Fewer than 100 closed trades |
| `passing` | Average at least +0.10R a trade, and the low end of its 95% range above 0 |
| `losing` | The high end of the 95% range below 0 |
| `no_edge_shown` | Anything else: can't tell it apart from luck yet |

A rough sense of scale: with trades that win 2R or lose 1R, 100 trades leave
about ±0.25R of uncertainty in the average, so a real but small edge may need
200-300 trades to show. At one or two trades a weekday that is three to six
months of demo trading.

**What it is compared with:**

1. **Doing nothing / coin flips.** Random entries in the T0 tests broke even
   before costs, so the bar is simply "better than 0R after costs". Any average
   below 0 means Hermes's decisions cost money.
2. **The rule-based strategies.** Their T0 results after costs ran from -0.02R
   (daily trend following, round 5) to -0.30R (M15 trend pullback, round 1).
   Any rule-based strategy enabled on the same demo account shows up in the
   same scorecard, scored the same way, so the comparison is like for like.
3. **Its own confidence.** Each intent says how likely Hermes thinks the target
   is. The scorecard compares those numbers with what happened (Brier score
   against always guessing its actual win rate). A trader whose confidence
   means nothing has no business sizing up later.

`atlas-engine scorecard --state <engine state dir>` prints the same numbers
for the operator. Nothing here can promote Hermes to a live account: that
would be a code change plus the PRD §15 promotion gate and a signed decision.

## Extra tools worth adding, in order

| # | Tool | Why | Cost and caveats |
| --- | --- | --- | --- |
| 1 | **Economic calendar feed** (e.g. the free Forex Factory weekly JSON) on the engine host | Most big currency moves come from scheduled news. It also gives the engine its news blackout, which T4 left unbuilt for lack of a feed, so a deterministic rule can block entries around releases instead of trusting the agent. | Free. One egress allowlist entry. Small build. |
| 2 | **Overnight swap per symbol** in `get_live_market` (from MT5 `symbol_info`) | Round 5 lost mainly to overnight charges. The trader should see what holding costs before it holds. | Free; already in the terminal. Tiny build. |
| 3 | **Interest rates and central-bank meeting dates** (BIS policy rates, already used in T0 round 6) | Rate differences drive the big currency trends and the swap. | Free. Small build: a read-only tool over data ATLAS already downloads. |
| 4 | **Web search and news** | Already on: the trader has Hermes's `web` toolset. | Uses the existing egress proxy. |
| 5 | **Futures positioning (CFTC Commitments of Traders)** | Weekly view of how big speculators are positioned in each currency. | Free, weekly, slow-moving; modest value for a few-days trade. |
| 6 | Retail sentiment (Myfxbook, broker order books) | Sometimes used as a contrarian signal. | Needs accounts or scraping; weak evidence. Not recommended yet. |

Not recommended: paid "AI signal" services, copy-trading platforms (HKUDS
AI-Trader was judged on 2026-09-28 to break the no-agent-live-trading rule),
social-media sentiment scrapers, and any backtest of the trader's judgement on
history.

## Turning it on

1. Finish T4's demo run on the Windows VPS (docs/t4-engine-and-mt5.md, "Still
   owed"): MT5 logged in to the **demo** account, watchdog EA compiled, engine
   running with `atlas-engine run`.
2. Re-run `deploy/hermes/bootstrap.py` with `--engine-url` pointing at the
   engine. It installs the `atlas-trading` profile and issues its two engine tokens
   (`atlas-trading/atlas-trading` with `trading:read, trading:demo`;
   `trader/atlas-operations` with `ops:read`).
3. Enable trading with a signed operator command (`atlas-engine operator enable_trading ...`).
4. Install the hourly card: it is gated on T4, so pass `--enable-gated` (which
   also installs the other gated jobs) or remove `gated_on` once T4 is signed off.

The trader runs on the frontier model tier, about 13 sessions a weekday.
Changing its `tier` in `atlas-profiles/roster.yaml` to `mid` makes each
session cheaper.

## Open for the owner

- Should the trader also get a Telegram bot so you can ask it why it made a
  trade? It has none now; trade and fill alerts come from the engine.
- Which of the extra tools above to build first. The calendar feed is the
  recommendation.
