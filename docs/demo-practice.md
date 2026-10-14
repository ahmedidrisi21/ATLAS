# Demo practice run: runbook

**Decision, 2026-10-06 (owner):** run ATLAS on NinjaTrader's free **demo** account (simulated money) to prove
the machinery works end to end. Nothing here can touch real money.

## What it is

ATLAS trades 1 Micro E-mini S&P 500 contract (MES) on the simulated $50,000 account. Two things propose trades:

1. **The opening-range rule.** It watches the first 15 minutes after the New York open (09:30). If a 15-minute
   bar then closes above that range it buys, and if one closes below it sells. It makes at most one trade a
   day, and every trade ends the same day. **This is a machinery test, not a strategy expected to make money.**
   It failed its backtest: on average it lost 0.012R a trade after costs, over 1,353 trades
   (docs/mes-candidates.md). It is used because it trades often and simply. Each trade opens with a stop and
   a target, and closes by 14:55 Chicago time at the latest.
2. **Hermes (optional).** The `atlas-trading` agent proposes its own trades about once an hour. It goes through
   the same checks, can't choose a size, can close only its own trades, and can only move its stops closer.
   It needs Hermes running (see "Hermes on and off").

What keeps it safe:

- **The demo account only.** The engine refuses every trade unless NinjaTrader reports a demo account. The
  connection reaches NinjaTrader's demo server and nothing else.
- **Size limits.** 1 contract per trade for each source, and never more than 2 contracts in total. Because
  NinjaTrader nets each contract, in practice only one MES position can be open at a time.
- **Every trade has protection.** Each trade carries a stop and a target at NinjaTrader. Everything is closed
  by 14:55 Chicago (15:55 New York).
- **Daily and overall loss limits.** No new trades after a $500 loss in a day. Trading stops at $1,800 down.

**Pass marks, fixed before the first trade.** These test the machinery, not profit:

| Mark | Pass |
| --- | --- |
| Positions left without a stop | zero |
| Orders the journal can't match with NinjaTrader | zero |
| Average slippage against the price ATLAS planned | within 2 ticks (the backtest assumed 1) |
| Completed trades | at least 20 (about 5 weeks) |

The report also shows orders sent, filled and refused, refusals by reason, the time from signal to fill, and
results in R after fees next to the backtest's -0.012R. The rule and Hermes are scored separately. Losing
money does not fail the run. A position without protection, or an order ATLAS can't account for, does.

## What you need to do

1. **Buy NinjaTrader's real-time CME market data for the simulated account.** The free feed is 10 minutes
   late, and ATLAS refuses any quote older than 30 seconds (`quote_not_live`). Without real-time data it will
   never trade. Check the current price in NinjaTrader; it's the CME (or CME Micro) real-time add-on.
2. **Use a computer that stays on all trading day.** Use your own PC left on, or a Windows or Linux VPS.
   Cloud sessions like this one stop, so they can't run it. A small Linux VPS is the simplest; on Windows, use
   WSL (Ubuntu). The commands below are for Linux.
3. **Sign in to NinjaTrader once, on that computer** (below).
4. **Check your commission.** `commission_per_lot` in `atlas.yaml` is $1.50 a round turn, the figure the
   backtest used. If NinjaTrader charges you a different amount, change it before you start.

## Set up (once)

```bash
git clone --recurse-submodules --branch claude/atlas-v3-prd-qo32ne https://github.com/yahyeameer/ATLAS.git ~/ATLAS
cd ~/ATLAS && python3 -m venv .venv && . .venv/bin/activate && pip install -e .
mkdir -p ~/atlas-practice && cp -r deploy/demo-practice ~/atlas-practice/config
chmod -R a-w ~/atlas-practice/config                       # the engine refuses a config it could change
atlas-engine operator keygen --key ~/atlas-practice/operator.key
atlas-engine token --tokens ~/atlas-practice/engine-tokens.yaml \
    --name operations-monitor/atlas-operations --scopes ops:read,ops:disable_trading
```

Sign in to NinjaTrader:

```bash
atlas-engine ninjatrader-mcp login --state ~/atlas-practice/state
```

Open the link it prints, sign in with your NinjaTrader login and allow ATLAS. On the consent screen allow
Trade, Market Data and the View permissions only. Under risk limits, list MES only, with a maximum total
exposure of 2. The browser then returns to `localhost`, and the command says "Signed in".

If the browser is on a different computer from the VPS, add `--paste`. The browser then ends on a
`localhost` page that won't load. Copy that page's full address and paste it into the terminal.

The sign-in is saved in `~/atlas-practice/state` and only the engine reads it. Never copy that folder into
git, a chat or an email.

Then check:

```bash
atlas-engine ninjatrader-mcp check --state ~/atlas-practice/state      # your demo account answers
atlas-engine ninjatrader-mcp preflight --state ~/atlas-practice/state  # during market hours: "ready": true
```

`preflight` must show `"quote_feed": "RealTime"` and at least 1,000 bars. Otherwise it says what is missing.

## Start

```bash
cd ~/ATLAS && . .venv/bin/activate
atlas-engine run --broker ninjatrader-mcp --config ~/atlas-practice/config --state ~/atlas-practice/state \
    --tokens ~/atlas-practice/engine-tokens.yaml --operator-key ~/atlas-practice/operator.key
```

To keep it running after you log out, and after a reboot, use the service file instead:

```bash
mkdir -p ~/.config/systemd/user && cp deploy/demo-practice/atlas-practice.service ~/.config/systemd/user/
loginctl enable-linger "$USER" && systemctl --user daemon-reload && systemctl --user enable --now atlas-practice
```

The engine starts with trading switched off. Switch it on with a signed command:

```bash
atlas-engine operator enable_trading --key ~/atlas-practice/operator.key \
    --inbox ~/atlas-practice/state/operator-inbox --operator yahye --reason "start the demo practice run"
```

## Stop

Stop when no trade is open: after 14:55 Chicago, or after a `flatten`. A trade left open while the engine is
stopped keeps its stop and target at NinjaTrader, but nobody closes it at 14:55.

```bash
atlas-engine operator flatten --key ~/atlas-practice/operator.key --inbox ~/atlas-practice/state/operator-inbox \
    --operator yahye --reason "stopping the practice run"
systemctl --user stop atlas-practice        # or Ctrl+C in the terminal running it
```

## Kill switch

```bash
atlas-engine operator kill --key ~/atlas-practice/operator.key --inbox ~/atlas-practice/state/operator-inbox \
    --operator yahye --reason "kill: something looks wrong"
```

This closes every ATLAS position and switches trading off within a second or two. Trading stays off until you
send `clear_kill` and then `enable_trading`. If the engine itself is not answering, close the position in
NinjaTrader's own app, stop the engine, and remove ATLAS's access in your NinjaTrader account settings.

## Hermes on and off

Hermes must run on the same computer as the engine (docs/h1-profiles-and-boards.md). Put a model API key
(for example `ANTHROPIC_API_KEY`) in its service environment (`deploy/hermes/systemd/hermes.env.example`).
Install it once, pointed at this engine:

```bash
pip install -e '.[mcp]'
python deploy/hermes/bootstrap.py --engine-url http://127.0.0.1:8742 \
    --engine-tokens ~/atlas-practice/engine-tokens.yaml --enable-gated
```

Then start the `atlas-orchestrator` and `atlas-operations` gateways (`deploy/hermes/systemd`).
`--enable-gated` also installs the other jobs that wait on later phases. They only open cards.

The hourly `atlas-trading-session` job (`deploy/hermes/cron.yaml`, weekdays 07:05-19:05 UTC) is the on/off
switch:

```bash
hermes -p atlas-operations cron pause atlas-trading-session    # Hermes off
hermes -p atlas-operations cron resume atlas-trading-session   # Hermes on
hermes -p atlas-operations cron list                            # which it is now
```

With Hermes off, the opening-range rule carries on alone. Each Hermes session uses the model, which costs
money.

## Read the report

```bash
atlas-engine practice-report --state ~/atlas-practice/state          # plain words
atlas-engine practice-report --state ~/atlas-practice/state --json   # everything, for Claude
atlas-engine show --state ~/atlas-practice/state                     # is it trading, is anything wrong
```

## What to send back

Once a week, and at the end, send:

- the output of `practice-report --json`;
- the output of `atlas-engine show`;
- `~/atlas-practice/state/alerts.jsonl` if anything looked wrong.

Never send `ninjatrader-mcp-token.json`, `operator.key`, `engine-tokens.yaml` or the state folder.

## Before you start

- The connection to NinjaTrader is checked against the real demo server for sign-in, reading the account,
  quotes and history. Placing, changing and cancelling orders has only been rehearsed against a stand-in
  that answers the same way (tests/engine/test_demo_practice.py). The first real orders are part of what
  this run tests.
- It is not known how many 1-minute bars NinjaTrader serves in one request. ATLAS asks for 5,000 and needs
  at least 1,000. `preflight` shows the number.
- The pinned contract, MESZ6, stops taking new entries on 13 Dec 2026. Before then, change
  `contracts: {MES: MESH7}` in `atlas.yaml`.
- Hermes's `get_live_market` gets at most 5,000 one-minute bars here, so its daily bars cover only the last
  few days.
