---
name: demo-trading
description: "Run one trading session on the ATLAS demo account: check the engine, read the market and news, decide whether to trade, manage open positions, and record why."
version: 0.1.0
author: ATLAS
license: UNLICENSED
platforms: [linux]
metadata:
  hermes:
    tags: [ATLAS, Forex, Trading, Demo]
    related_skills: []
---

# Demo Trading

## Objective

Make one well-reasoned decision per session on the MT5 demo account: open a trade, manage an open one, or stand aside. Every trade carries a stop, a target, a confidence and a written thesis, so the record can show whether your judgement beats costs.

## When to use

- The hourly "Trading session" card on the atlas-ops board.
- The operator asks you to look at the market and trade.

Don't use for: backtests or research on history (you may already know what happened), or anything on a live account.

## Required inputs

- The card body (session time).
- Nothing else. Read the live state yourself.

## Procedure

1. `system_status()`. If new trades are not enabled, or the state is HALT or KILL, skip to step 7: manage nothing, open nothing, and say so.
2. `my_track_record()` and `list_my_positions()`. Note your expectancy, your calibration (Brier against the base rate) and how many intents you have left today. If your last five trades share a mistake, name it before going on.
3. For each open position: `get_live_market` on its symbol. If the thesis that opened it is broken, `close_my_position(ticket, reason)`. If price has moved well in your favour and a nearby level now protects it, you may `tighten_stop(ticket, new_stop, reason)`. Otherwise leave it: the broker holds its stop and target.
4. Read the market: `get_live_market(symbols, "D1", 60)`, then `"H4"` and `"H1"` for the symbols that look interesting. Note the spread now against its median.
5. Read the news with web search: today's economic calendar (high-impact releases in the next 2 hours on the currencies you're looking at), central-bank news, and anything moving the dollar, euro or pound. Write down the sources you used.
6. Decide. Trade only when all of these hold:
   - You can say in two sentences why price should reach the target before the stop, and what would prove you wrong.
   - No high-impact release on either currency in the next 30 minutes.
   - The spread is near its median.
   - The target is 1-5 times as far as the stop, and the stop sits beyond a level the market has respected, not at a round distance.
   - Your honest confidence clears the engine's EV gate: confidence x reward-to-risk - (1 - confidence) - costs >= +0.15R. At 2R that means about 0.40 or more. The engine rejects anything below with `ev_below_min`; never inflate a confidence to get past it.
   Then `submit_trade_intent(intent_id, symbol, direction, stop, target, confidence, thesis)`. Use an `intent_id` like `YYYYMMDD-HHMM-SYMBOL`; reuse it if you retry after a timeout. If the engine refuses, record the reasons and don't resubmit with changed numbers.
7. Write one memory note only if you learned something new, with the `intent_id` or `trade_id`.

## Tools

- atlas-trading: `get_live_market`, `submit_trade_intent`, `get_intent_status`, `list_my_positions`, `close_my_position`, `tighten_stop`, `my_track_record`
- atlas-operations: `system_status`, `health_state`
- web search, for news and the economic calendar

## Output format

`kanban_complete(summary, metadata)`. The summary is three short parts: what you saw (engine state, market, news with sources), what you did (each tool call that changed something, with its outcome), and why. "No trade" is a full answer when you say why.

Metadata: `{"session": "<UTC hour>", "decision": "trade" | "manage" | "no_trade" | "engine_not_trading", "intents": [{"intent_id", "symbol", "direction", "confidence", "outcome", "reasons"}], "closed": [<tickets>], "tightened": [<tickets>], "expectancy_r": <from my_track_record>, "trades": <closed trades so far>, "artifacts": []}`.

## Safety constraints

- Never request, read or infer holdout data; never backtest your own judgement on history.
- Demo account only. If `system_status()` shows a live account, do nothing and block the card for the operator.
- Never ask for a lot size, a limit change, or for trading to be enabled. Those are the operator's, signed, outside Hermes.
- Never resubmit a refused trade with changed numbers to get it through.
- Report every intent you sent, refusals and losses included, in R after costs.

## Validation requirements

- Every intent in the metadata matches a `get_intent_status(intent_id)` result.
- Every close and tighten names its reason in the call.
- The expectancy you report is the one `my_track_record()` returned, not your own arithmetic.
