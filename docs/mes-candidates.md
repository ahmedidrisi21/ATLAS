# MES candidate strategies, round 1 (2026-10-06)

**Bottom line: none of the three strategies passed. There is no strategy to
forward-test on the NinjaTrader demo account from this round, and no draft
strategy file was written.** The gates were not loosened and nothing was
re-tuned after the results came in.

The three were declared in `atlas_research/configs/mes.yaml` before any MES data
was loaded. All three are recorded in `research/experiments.jsonl`, the
experiment log. Each run's full report is under `research/runs/<experiment id>/`,
which is not committed.

## What was tested, in plain words

The market is the CME Micro E-mini S&P 500 future (MES). One point of the
S&P 500 is $5 a contract, and the smallest price step (a "tick") is 0.25 points,
or $1.25.

1. **Opening-range breakout (`mes_orb_m15`). This is a new setup.** It watches
   the first 15 or 30 minutes after the New York stock market opens (09:30). On
   the first 15-minute close above that range it buys, and on the first close
   below it sells. It takes at most one trade a day and makes no new entries
   after 12:00. The stop sits at the far edge of the range, or at its middle.
   The trade takes profit at 2× the risk, or closes at 15:55 New York whatever
   happens, so it never holds overnight.
2. **Channel breakout on 4-hour bars (`mes_channel_breakout_h4`). This is an
   existing setup.** It buys when a 4-hour bar closes above the highest high of
   the last 20 or 55 bars, and sells on a close below the lowest low. The stop
   is 1.5 or 2.5 × the average bar range away. It takes profit at 2× the risk
   and closes everything on Friday at 20:00 UTC, so it never holds over the
   weekend.
3. **Trend pullback on 1-hour bars (`mes_trend_pullback_h1`). This is an
   existing setup.** When the hourly trend is up, it buys a dip to the 20-bar
   average once price turns back up, and the reverse when the trend is down.
   Profit is taken at 2× the risk.

Each strategy had 4 parameter combinations, fixed in advance. The test follows
the same method as the forex rounds (`docs/t0-edge-discovery.md`):

- **Dev period, Jan 2019 to Dec 2023, walk-forward.** Each 12-month window picks
  the best combination, and that combination then trades the next 3 months,
  which it has not seen.
- **Validation period, Jan 2024 to Jun 2025.** The combination that did best on
  the whole dev period trades this period once.
- **Holdout, July 2025 onward.** Locked. It was never loaded.

"Out-of-sample" (OOS) below means only those unseen 3-month windows plus
validation.

## Price data and how it differs from the real future

The repo holds no futures history, so the prices come from Dukascopy's **S&P 500
cash-index CFD** (`USA500IDXUSD`), one-minute bid/ask bars from 2019-01-01 to
2025-06-30: 2.16 million bars. The price scale was checked by decoding a sample:
2019-01-15 closes at 2,610.0, which matches the S&P 500 close that day.
`price_scale` is 1,000. The quality report found no duplicate bars, no crossed
quotes and no broken bars. The CFD's own spread is 2.0–2.8 ticks at the median
(0.5–0.7 points).

Things that may not carry over to the real future:

- **No basis and no roll.** The future trades above the cash index by the cost
  of carry, and it rolls every quarter. Intraday the difference is constant, so
  the opening-range test is barely affected. For trades held several days (the
  channel breakout) the carry is a few hundredths of a point a day, which is
  small next to stops of about 36 points. A roll that falls inside a trade isn't
  modelled.
- **Trading hours.** CME Globex trades 23 hours a day (17:00–18:00 New York is
  the daily halt). The CFD has a daily break of about 1¾ hours (about 21:15–23:00
  UTC), and in 2019 some overnight minutes have no quotes. Overnight bars,
  especially in 2019, therefore aren't the same as the future's. The 4-hour and
  1-hour tests see these bars. The opening-range test trades only 09:30–15:55
  New York, when the two markets move almost identically.
- **Volume and order flow** on the CFD are Dukascopy's own, not CME's. None of
  these strategies uses volume.

## Costs (conservative, explicit)

The costs are charged in index points per contract and converted to R, where 1R
is the amount risked on the trade:

| Cost | Charged as | Per contract |
| --- | --- | --- |
| Commission and fees | $1.50 round turn (NinjaTrader is about $1.24) | 0.30 pt |
| Spread | the future's 1 tick, stressed ×1.5 (×2 in the stress gate) | 0.375 pt per round trip |
| Slippage | 1 tick on every market fill: entry, stop, time exit, Friday close | 0.25 pt each |
| Profit target | a resting limit order, so no slippage | 0 |
| Overnight financing | none on futures | 0 |

A trade that loses at its stop costs about 1.2 points, or $5.90 a contract,
against roughly $3.70 in real life. **The CFD's own spread is not charged.** Only
the CFD's mid price is used, and it is re-quoted at the future's 1-tick spread
(`data.proxies` in the config). The CFD spread of 2–3 ticks is the dealer's
markup and doesn't exist on CME. Charging it on top of the futures slippage
would count the spread twice.

Costs turned out to be small on MES: 0.01–0.03 R a trade, against 0.05–0.23 R
on the forex pairs. **Costs are not why these strategies fail. They have little
or no edge even before costs.**

## Results

R is the profit or loss of a trade divided by the amount risked. +0.10 R a
trade means 10 cents of profit for each dollar risked. "Gross" is before
commission only: the stressed spread and the slippage are already in the fill
prices. (Corrected in round 2; an earlier version said gross was also before
slippage.)

### Summary (out-of-sample: walk-forward test windows plus validation)

| Strategy | Trades | Avg R gross | Avg R after costs | Win rate | Max drawdown | Gates failed | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Opening-range breakout (M15) | 1,353 | +0.011 | **−0.012** | 41.8% | 45.8 R | 11 of 14 | FAIL |
| Channel breakout (H4) | 372 | +0.095 | **+0.086** | 44.1% | 12.8 R | 6 of 14 | FAIL |
| Trend pullback (H1) | 202 | −0.201 | **−0.227** | 27.7% | 52.3 R | 13 of 14 | FAIL |

Max drawdown is the largest peak-to-trough fall in the actual order of the OOS
trades, counted in R.

### By year (out-of-sample, after costs)

| Year | ORB trades | ORB avg R | Channel trades | Channel avg R | Pullback trades | Pullback avg R |
| --- | --- | --- | --- | --- | --- | --- |
| 2020 | 251 | −0.089 | 61 | +0.086 | 38 | +0.144 |
| 2021 | 246 | −0.017 | 76 | +0.076 | 40 | −0.374 |
| 2022 | 244 | −0.014 | 66 | −0.046 | 45 | −0.371 |
| 2023 | 235 | +0.083 | 50 | +0.089 | 45 | −0.313 |
| 2024 | 254 | −0.020 | 77 | +0.306 | 26 | −0.125 |
| 2025 (H1) | 123 | −0.010 | 42 | −0.091 | 8 | −0.279 |

2019 is the first walk-forward training window, so it has no out-of-sample
trades.

### Dev vs validation, after costs

| Strategy | Dev OOS trades | Dev avg R | Validation trades | Validation avg R | Final parameters |
| --- | --- | --- | --- | --- | --- |
| ORB | 976 | −0.011 | 377 | −0.017 | 15-minute range, stop at the far edge |
| Channel | 253 | +0.049 | 119 | +0.166 | 20-bar channel, 1.5 × ATR stop |
| Pullback | 168 | −0.240 | 34 | −0.162 | ADX ≥ 25, 8-bar pullback |

### Gates (pass / fail)

| Gate (threshold) | ORB | Channel H4 | Pullback H1 |
| --- | --- | --- | --- |
| OOS trades (≥ 300) | pass 1,353 | pass 372 | **fail** 202 |
| Avg R after costs, dev (≥ +0.10) | **fail** −0.011 | **fail** +0.049 | **fail** −0.240 |
| Avg R after costs, validation (≥ +0.10) | **fail** −0.017 | pass +0.166 | **fail** −0.162 |
| Profit factor, dev (≥ 1.25) | **fail** 0.98 | **fail** 1.10 | **fail** 0.69 |
| Profit factor, validation (≥ 1.25) | **fail** 0.97 | pass 1.34 | **fail** 0.78 |
| Monte Carlo drawdown p95 (< 6% at 0.4% risk) | **fail** 32.4% | **fail** 10.5% | **fail** 24.2% |
| Daily-loss breach probability (< 2%) | pass 0% | pass 0% | pass 0% |
| Deflated Sharpe (> 0.95) | **fail** 0.15 | **fail** 0.58 | **fail** 0.00 |
| Walk-forward efficiency (≥ 50%) | **fail** 0% | pass 54% | **fail** 0% |
| Avg R at 2× spread (> 0) | **fail** −0.022 | pass +0.076 | **fail** −0.226 |
| Largest single year's share of profit (≤ 40%) | **fail** 100% | **fail** 73% | **fail** 100% |
| Skip-10% Monte Carlo p05 (> 0) | **fail** −0.030 | pass +0.051 | **fail** −0.281 |
| Beats random entries' p95 (> 0) | pass +0.023 | **fail** −0.054 | **fail** −0.297 |
| Worst ±20% parameter neighbour (> 0) | **fail** −0.058 | pass +0.099 | **fail** −0.755 |

`p_target_first` is the out-of-sample share of trades that reached the 2R
target. The engine would use it as its prior, but no strategy passed, so the
engine has none to use. For reference only: ORB 0.177, channel 0.196, pullback
0.272.

### Every grid point (dev and validation, after costs)

None of the 12 combinations clears +0.10 R on dev:

| Strategy | Parameters | Dev trades | Dev avg R | Validation trades | Validation avg R |
| --- | --- | --- | --- | --- | --- |
| ORB | 15 min, stop at middle | 1,238 | −0.056 | 375 | +0.008 |
| ORB | 15 min, stop at far edge | 1,239 | −0.015 | 377 | −0.017 |
| ORB | 30 min, stop at middle | 1,150 | −0.067 | 363 | −0.058 |
| ORB | 30 min, stop at far edge | 1,150 | −0.029 | 363 | −0.058 |
| Channel | 20 bars, 1.5 ATR | 379 | +0.031 | 119 | +0.166 |
| Channel | 20 bars, 2.5 ATR | 312 | +0.019 | 100 | +0.150 |
| Channel | 55 bars, 1.5 ATR | 252 | −0.009 | 76 | +0.193 |
| Channel | 55 bars, 2.5 ATR | 214 | +0.015 | 63 | +0.227 |
| Pullback | ADX 20, 4 bars | 210 | −0.279 | 71 | −0.326 |
| Pullback | ADX 20, 8 bars | 167 | −0.276 | 55 | −0.282 |
| Pullback | ADX 25, 4 bars | 118 | −0.157 | 45 | −0.110 |
| Pullback | ADX 25, 8 bars | 103 | −0.142 | 34 | −0.162 |

## What the results say

- **Opening-range breakout:** there is essentially no edge. It averages +0.01 R
  before costs and −0.01 R after, on 1,353 trades. Only 2023 was positive. It
  does beat random entries at the same times of day by a little, but not by
  enough to matter.
- **Channel breakout on H4:** this came closest, and it is still a fail. Its
  profit is mostly the S&P 500's upward drift over 2020–2024. Longs averaged
  +0.18 R and shorts −0.04 R. Random entries with the same long/short mix scored
  as high as +0.14 R at the 95th percentile, which is above the strategy's
  +0.086 R. 2024 alone made 73% of the profit, and the dev period was only
  +0.05 R. The good validation numbers (+0.17 R) come from one strong year.
- **Trend pullback on H1:** it loses clearly in every year except 2020.

**A "long-only channel breakout" was not run.** It looks tempting after seeing
the long/short split, and that is exactly why it would be fitting to these
results. If the operator wants it tested, it should be declared as a new round
before any run, and judged against a buy-and-hold benchmark, not only against
random entries.

## Caveats on the gates themselves

- The account numbers (0.4% risk a trade, 10% firm max drawdown, 5% daily loss)
  are the forex T0 placeholders, kept so results compare across rounds. Futures
  prop accounts usually have tighter, trailing drawdowns, so the drawdown gate
  would be stricter on a real futures evaluation, not looser.
- The deflated Sharpe counts the channel breakout and trend pullback together
  with their earlier forex trials (12 trials each), as the harness requires.
  This doesn't change any verdict, because both fail other gates.
- **The engine can't run two of these as tested yet.** It builds features on
  15-minute bars and supports a fixed-R target only, with no per-trade time exit
  (it flattens at the prop firm's flat-by time and on Fridays). The H4 and H1 strategies, and
  the ORB's 15:55 exit, would need engine work before a demo test. That doesn't
  matter this round, since nothing passed.
- There is no news blackout, same as the forex rounds.

## Files

- `atlas_research/configs/mes.yaml`: the declared MES round (strategies, costs, proxy).
- `atlas_engine/setups/opening_range_breakout.py`: the new setup.
- `atlas_research/data.py`: `load_research_m1` / `quoted_at_spread`, which build MES from the CFD proxy at a 1-tick spread.
- `atlas_research/backtest.py`: `CostModel.exit_slippage`, slippage on time exits and Friday closes. It defaults to 0, so earlier forex results don't change.
- `atlas_engine/market_data/symbols.py`: `USA500IDXUSD` (price scale 1,000).

Reproduce:

```bash
atlas-research data download --symbols USA500IDXUSD     # ~4,750 day-files; the feed answers 503/429 often, rerun to resume
atlas-research data build    --symbols USA500IDXUSD
atlas-research --config atlas_research/configs/mes.yaml t0 run --all
```

# MES round 2: time-of-day index effects (declared 2026-10-06)

## Declaration (written before any round-2 run)

Round 2 tests four ideas. Three are well-known published effects in the S&P 500.
The fourth, the long-only channel breakout, was suggested by round 1's
results, and that is why it gets an extra benchmark. The strategies, grids and
stops are fixed in `atlas_research/configs/mes.yaml`, written there before anything ran
(uncommitted until the operator reviews this round). **The gates are the unchanged T0 gates**
(same thresholds, same dev / walk-forward / validation split, same costs, same
random-entry control). The holdout (July 2025 on) stays locked. Nothing is
retuned after the results.

| Strategy | Setup | Bar | Hypothesis | Grid (points) | Fixed |
| --- | --- | --- | --- | --- | --- |
| `mes_intraday_momentum_m30` | `intraday_momentum` (new) | 30 min | Gao, Han, Li & Zhou (2018): the return from the prior 16:00 close (or 09:30) to 10:00 New York predicts the last half hour. Enter at 15:30 New York in that direction. | signal from prior close / 09:30 open × exit 16:00 / 15:55 (4) | stop 2 × M30 ATR(14) |
| `mes_overnight_drift_h1` | `overnight_drift` (new) | 1 h | Overnight drift (Cooper, Cliff & Gulen 2008; Boyarchenko, Larsen & Whelan 2023): long from the 16:00 close to the next 09:30 open, or a short hold to 04:00 (after the European open). | exit 09:30 / 04:00 (2) | long only, stop 3 × H1 ATR(14); Fridays excluded (no weekend holds) |
| `mes_channel_breakout_long_h4` | `channel_breakout`, `sides: long` | 4 h | Long-only H4 channel breakout. New hypothesis, from round 1's long/short split. | channel 20 / 55 × stop 1.5 / 2.5 ATR (4) | 2R target, Friday 20:00 UTC flatten, as in round 1 |
| `mes_opening_gap_m30` | `opening_gap` (new) | 30 min | Fade, or follow, the overnight gap (prior 16:00 close to 09:30 New York). | fade / follow × exit 10:30 / 15:55 (4) | any non-zero gap, stop 4 × M30 ATR(14) |

**Extra benchmark for the long-only strategies** (overnight drift, long-only
channel breakout). Both must beat buy-and-hold with the same exposure, as one
more gate: the average R after costs minus the R that a long S&P position
would have earned over the same hours on the same risk must be above 0. The
buy-and-hold rate is the mean log return per calendar hour over the same
out-of-sample windows, charged no costs. The random-entry gate also applies
unchanged. For an all-long strategy it draws random long entries with the same
stops and holding times.

**How R is defined for a time exit.** Three of these trades close at a clock
time, not at a target or a stop. Each still has a protective stop, fixed in
advance as an ATR multiple. That stop is the distance the engine would size
the position on, so 1R is the stop distance, as for every other strategy:
R = direction × (exit − entry) / (entry − stop), minus costs over the same
distance. A trade that reaches its stop loses about 1R. Most trades end at the
clock, somewhere between −1R and a few R. Because a wider stop means fewer
contracts, the R numbers depend on the stop chosen. That is why the stop is
fixed and not part of the grid. There is no target, so the engine's
`p_target_first` (the chance the target is hit before the stop) doesn't apply
as defined. These strategies report it as the share of trades that close in
profit, and say so.

**Engine readiness.** The live engine builds features on 15-minute bars only,
supports a fixed-R target only, and has no per-trade time exit. It flattens only
at the prop firm's flat-by time and on Fridays. Any round-2 strategy that passes
would need engine work before it could run on the demo account: 30-minute or
hourly features, and a per-trade time exit. The overnight hold would also
conflict with a prop firm's flat-by rule.

## Round 2 results

**Bottom line: none of the four strategies passed. There is again nothing to
forward-test on the demo account, and no draft strategy file was written.**
Each strategy ran once, as declared, with no reruns and no retuning. All four
runs are in `research/experiments.jsonl`:

- `mes_intraday_momentum_m30-20261006-041925-9b3b8b`
- `mes_overnight_drift_h1-20261006-041954-60bb93`
- `mes_channel_breakout_long_h4-20261006-042013-7cd8a4`
- `mes_opening_gap_m30-20261006-042023-8e7053`

Before the runs, a signal-count check was done on the dev data, with no
simulation and no R. It confirmed that each setup fires at the declared
New York times on the expected weekdays.

"Gross" below means before commission. The spread (stressed ×1.5) and the
1-tick slippage on market fills are already inside the fill prices.

### Summary (out-of-sample: walk-forward test windows plus validation)

| Strategy | Trades | Avg R gross | Avg R after costs | Win rate | Max drawdown | Gates failed | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Intraday momentum (M30, 15:30→close) | 1,371 | −0.049 | **−0.065** | 44.2% | 93.4 R | 11 of 14 | FAIL |
| Overnight drift (16:00→next morning) | 1,099 | +0.014 | **+0.006** | 53.1% | 17.5 R | 11 of 15 | FAIL |
| Long-only channel breakout (H4) | 231 | +0.154 | **+0.145** | 49.4% | 11.7 R | 7 of 15 | FAIL |
| Opening gap (M30, fade/follow) | 1,411 | +0.003 | **−0.009** | 46.4% | 38.0 R | 11 of 14 | FAIL |

The two long-only strategies have 15 gates, because the exposure-matched
buy-and-hold benchmark was added to them.

### By year (out-of-sample, after costs)

| Year | Momentum trades | Momentum avg R | Overnight trades | Overnight avg R | Long channel trades | Long channel avg R | Gap trades | Gap avg R |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2020 | 251 | −0.087 | 203 | +0.028 | 42 | +0.088 | 254 | +0.001 |
| 2021 | 251 | −0.082 | 202 | +0.010 | 47 | +0.304 | 258 | −0.086 |
| 2022 | 250 | −0.047 | 200 | −0.035 | 35 | −0.216 | 258 | +0.020 |
| 2023 | 248 | −0.051 | 198 | −0.035 | 37 | +0.120 | 256 | +0.045 |
| 2024 | 249 | −0.075 | 199 | +0.082 | 46 | +0.371 | 259 | −0.041 |
| 2025 (H1) | 122 | −0.034 | 97 | −0.037 | 24 | +0.067 | 126 | +0.023 |

### Dev vs validation, after costs

| Strategy | Dev OOS trades | Dev avg R | Dev PF | Validation trades | Validation avg R | Validation PF | Final parameters |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Intraday momentum | 1,000 | −0.067 | 0.68 | 371 | −0.062 | 0.70 | return from 09:30, exit 15:55 |
| Overnight drift | 803 | −0.008 | 0.96 | 296 | +0.043 | 1.19 | exit at the next 09:30 |
| Long-only channel | 161 | +0.092 | 1.19 | 70 | +0.267 | 1.62 | 20-bar channel, 1.5 × ATR stop |
| Opening gap | 1,026 | −0.005 | 0.99 | 385 | −0.020 | 0.96 | fade, exit 15:55 |

### Gates (pass / fail)

| Gate (threshold) | Momentum | Overnight | Long channel | Gap |
| --- | --- | --- | --- | --- |
| OOS trades (≥ 300) | pass 1,371 | pass 1,099 | **fail** 231 | pass 1,411 |
| Avg R after costs, dev (≥ +0.10) | **fail** −0.067 | **fail** −0.008 | **fail** +0.092 | **fail** −0.005 |
| Avg R after costs, validation (≥ +0.10) | **fail** −0.062 | **fail** +0.043 | pass +0.267 | **fail** −0.020 |
| Profit factor, dev (≥ 1.25) | **fail** 0.68 | **fail** 0.96 | **fail** 1.19 | **fail** 0.99 |
| Profit factor, validation (≥ 1.25) | **fail** 0.70 | **fail** 1.19 | pass 1.62 | **fail** 0.96 |
| Monte Carlo drawdown p95 (< 6%) | **fail** 38.4% | **fail** 10.6% | **fail** 7.2% | **fail** 26.0% |
| Daily-loss breach probability (< 2%) | pass 0% | pass 0% | pass 0% | pass 0% |
| Deflated Sharpe (> 0.95) | **fail** 0.00 (4 trials) | **fail** 0.50 (2) | **fail** 0.71 (16) | **fail** 0.02 (4) |
| Walk-forward efficiency (≥ 50%) | **fail** 0% | **fail** < 0 | pass 77% | **fail** < 0 |
| Avg R at 2× spread (> 0) | **fail** −0.072 | pass +0.003 | pass +0.142 | **fail** −0.014 |
| Largest year's share of profit (≤ 40%) | **fail** 100% | **fail** (total ≈ 0) | **fail** 51% | **fail** 100% |
| Skip-10% Monte Carlo p05 (> 0) | **fail** −0.072 | **fail** −0.003 | pass +0.100 | **fail** −0.024 |
| Beats random entries' p95 (> 0) | pass +0.003 | **fail** −0.036 | **fail** −0.028 | pass +0.006 |
| Worst ±20% neighbour (> 0) | **fail** −0.073 | pass +0.034 | pass +0.183 | **fail** −0.020 |
| Beats exposure-matched buy-and-hold (> 0) | n/a | **fail** −0.020 | pass +0.072 | n/a |

`p_target_first`, for reference only, since nothing passed: the long-only
channel breakout reached its 2R target first on 0.182 of trades. The three
time-exit strategies have no target, so this is the share of trades that
closed in profit, as declared: momentum 0.442, overnight 0.531, gap 0.464.
Exits for the time-exit strategies:

- Momentum: 1,305 at the clock and 66 at the stop.
- Overnight: 994 at the clock and 104 at the stop.
- Gap: 986 at the clock and 425 at the stop. The 4 × ATR stop is tight next
  to the session's range, because the ATR at 09:30 is measured on the quiet
  overnight bars.

One overnight trade and one channel trade were still open when the validation
data ends on 2025-06-30. Both were closed at the last bar before the holdout.

### Every grid point (dev and validation, after costs)

These are the same deterministic backtests the T0 runs made, read per point:

| Strategy | Parameters | Dev trades | Dev avg R (gross) | Validation trades | Validation avg R |
| --- | --- | --- | --- | --- | --- |
| Momentum | from prior close, exit 16:00 | 1,247 | −0.088 (−0.068) | 371 | −0.056 |
| Momentum | from prior close, exit 15:55 | 1,247 | −0.078 (−0.058) | 371 | −0.043 |
| Momentum | from 09:30, exit 16:00 | 1,242 | −0.097 (−0.077) | 371 | −0.063 |
| Momentum | from 09:30, exit 15:55 | 1,242 | −0.077 (−0.056) | 371 | −0.062 |
| Overnight | exit 09:30 | 1,002 | +0.004 (+0.014) | 296 | +0.043 |
| Overnight | exit 04:00 | 1,002 | −0.010 (−0.000) | 296 | +0.008 |
| Long channel | 20 bars, 1.5 ATR | 222 | +0.099 (+0.112) | 70 | +0.267 |
| Long channel | 20 bars, 2.5 ATR | 187 | +0.051 (+0.058) | 61 | +0.212 |
| Long channel | 55 bars, 1.5 ATR | 168 | +0.005 (+0.019) | 51 | +0.279 |
| Long channel | 55 bars, 2.5 ATR | 144 | −0.003 (+0.005) | 44 | +0.194 |
| Gap | fade, exit 10:30 | 1,282 | −0.048 (−0.034) | 385 | −0.063 |
| Gap | fade, exit 15:55 | 1,282 | −0.011 (+0.003) | 385 | −0.020 |
| Gap | follow, exit 10:30 | 1,282 | −0.063 (−0.048) | 385 | −0.019 |
| Gap | follow, exit 15:55 | 1,282 | −0.060 (−0.046) | 385 | −0.055 |

### What round 2 says

- **Intraday momentum doesn't exist here.** At the mid price, before spread,
  slippage and commission, it averages about 0.00 R a trade. That figure is
  approximate: it adds back 0.875 points of spread and slippage, or about
  0.05 R on a median stop of 19.5 points. The spread and slippage alone make
  it lose at every grid point and in every year from 2020 to 2025. That fits
  later reports that the published effect weakened after the sample it was
  found in. It "beats" the random-entry control only because random
  30-minute holds at any hour do even worse after costs.
- **The overnight drift is too small to trade on one contract's risk.** It
  makes +0.014 R a trade before costs and +0.006 R after. That is less than
  exposure-matched buy-and-hold (+0.026 R for the same hours) and inside the
  range of random long holds. The dev period is negative. Excluding Fridays
  (no weekend holds) removes part of the documented effect, but the rule was
  declared before the run. A firm's flat-by rule would forbid the trade anyway.
- **The long-only channel breakout is the best result so far and still a
  fail.** It makes +0.145 R after costs, beats exposure-matched buy-and-hold
  by +0.072 R, and its walk-forward efficiency (77%) and ±20% neighbours are
  fine. But:
  - It has 231 trades, short of the 300-trade minimum.
  - Dev OOS is +0.092 R, under +0.10 R, with a profit factor of 1.19.
  - Random long entries with the same stops and exits reach +0.173 R at the
    95th percentile, above the strategy. On this sample, any long entry with
    a 2R target did about as well, so the breakout trigger adds nothing
    measurable over the S&P's drift.
  - 2022 lost −0.22 R a trade, and 2021 plus 2024 made half the profit.
- **The opening gap is noise.** Fade and follow both lose a little at every
  exit. The best point, fade to 15:55, is −0.011 R on dev.

## Round 2 caveats

- The holdout was never loaded. Every run ends at 2025-07-01.
- The deflated Sharpe for the long-only channel pools its 4 trials with the
  12 earlier `channel_breakout` trials, from forex and MES round 1, as the
  harness requires.
- The random-entry control for the time-exit strategies gives random entries
  the strategy's own holding times, at any time of day. For the long-only
  strategies every random entry is long, so the control is also a
  buy-and-hold test.
- The engine readiness note in the declaration stands. These strategies would
  need 30-minute or hourly features and a per-trade time exit in the engine.
  The overnight hold also conflicts with a prop firm's flat-by rule.

## Round 2 files

- `atlas_engine/setups/intraday_momentum.py`, `overnight_drift.py` and
  `opening_gap.py`: the new time-of-day setups. The New York clock helpers
  are in `base.py`.
- `atlas_engine/setups/channel_breakout.py`: a `sides` parameter, `both` by
  default, so earlier results don't change.
- `atlas_engine/features/sessions.py` and `setups/base.py`: the Friday entry
  cut-off can be `None` for intraday strategies.
- `atlas_research/backtest.py`: `rr: null` is allowed when every signal
  carries a time exit.
- `atlas_research/t0.py`:
  - a strategy may override the edge filters (`filters:`);
  - `benchmark: buy_and_hold` adds the exposure-matched buy-and-hold gate
    (`buy_and_hold_r`).

Reproduce:

```bash
for s in mes_intraday_momentum_m30 mes_overnight_drift_h1 mes_channel_breakout_long_h4 mes_opening_gap_m30; do
  atlas-research --config atlas_research/configs/mes.yaml t0 run $s
done
```

# Round 3: independent check on 2013-2018

## Declaration (written 2026-10-06, before any 2013-2018 data was downloaded or loaded)

Round 2's closest candidate, the long-only H4 channel breakout
(`mes_channel_breakout_long_h4`), made +0.145 R after costs on 231
out-of-sample trades from 2019 to mid-2025 and failed 7 of 15 gates. One of
its problems is that all of its evidence comes from one stretch of market
history. Round 3 tests it once on six earlier years that no test in this repo
has used: **2013-01-01 to 2018-12-31.** It is a confirmation test, not a
search. Nothing about the strategy may change.

**Frozen parameters.** These are the final parameters round 2 selected on its
dev period (run `mes_channel_breakout_long_h4-20261006-042013-7cd8a4` in
`research/experiments.jsonl`): `channel: 20`, `sl_atr: 1.5`, `sides: long`,
with the `channel_breakout` setup's other defaults, on 4-hour bars. Exits are
round 2's: a 2R profit target, the stop, and a flatten on Friday at 20:00 UTC.
The edge filters are unchanged: spread at most 20% of the stop, no entry in the
first 15 minutes after the open, no Friday entry after 18:00 UTC. There is no
grid, no walk-forward and no re-selection. The strategy runs once over the
whole period.

**Same data recipe and costs.** MES is again proxied by Dukascopy's
`USA500IDXUSD` (S&P 500 cash-index CFD). Its mid price is re-quoted at the
future's 1-tick spread. The costs are the same as rounds 1 and 2: spread ×1.5,
$1.50 round turn (0.30 pt), and 1 tick of slippage on every market fill. The
indicators warm up on the first bars of January 2013. No data before 2013 is
loaded.

**Data-quality rule, declared before the download.** Dukascopy's index CFD
history before about 2016 may be thin, have gaps or keep different trading
hours. The reference is the median full year of this series over 2019-2024,
which round 1 and 2 already used: 1,600 non-empty H4 bars and 334,700 one-minute
bars. **A year with fewer than 70% of either is excluded and reported.** That
is fewer than 1,120 H4 bars or fewer than 234,290 one-minute bars. Its trades
are dropped, and it is left out of the benchmark windows. Coverage is reported
for every year. The price scale is checked against the published S&P 500
closes on 2013-01-02 (1,462.42), 2015-12-31 (2,043.94) and 2018-12-31
(2,506.85). A CFD close more than 1% away would mean the scale is wrong. That
would be fixed in the loader and reported. It would not count as a tuning
change.

**Pass rule, declared up front.** The check passes only if all five hold over
the included years:

| Rule | Threshold |
| --- | --- |
| (a) Average R after costs | > +0.10 |
| (b) Beats exposure-matched buy-and-hold | average R minus the round-2 `buy_and_hold_r` benchmark > 0. The buy-and-hold rate is the mean log return per calendar hour over the included years, charged no costs. |
| (c) Beats random long entries | average R above the 95th percentile of 50 random long-entry runs, which use the same count, stops and exits (round 2's `random_control`, seed 0) |
| (d) Trade count | at least 150 trades |
| (e) No single year carries it | no year makes more than 50% of the total profit. A total of zero or less fails. |

The T0 gate table is also reported, for information only. Gates that need a
dev and validation split or a walk-forward don't apply to a single frozen run
and are marked n/a. The check does not add deflated-Sharpe trials, since
nothing is selected. Its deflated Sharpe uses the 16 `channel_breakout`
trials already in the registry. The run is logged in
`research/experiments.jsonl` whether it passes or fails. The holdout (July 2025
on) stays locked; this period ends 2018-12-31.

The same declaration is in `atlas_research/configs/mes.yaml` under `checks:`.

### Addendum to the declaration (written after the download and data-quality report, before any 2013-2018 backtest)

The data-quality report (bar counts and trading hours only, no trades or R)
shows that the declared coverage rule removes four of the six years:

| Year | Days with data | M1 bars (% of 334,700) | H4 bars (% of 1,600) | UTC hours quoted | Rule |
| --- | --- | --- | --- | --- | --- |
| 2013 | 203 | 192,401 (57%) | 979 (61%) | about 23 h, but ~40 of 60 minutes quoted, ~60 days missing | **excluded** |
| 2014 | 288 | 286,092 (85%) | 1,527 (95%) | about 23 h | included |
| 2015 | 267 | 228,274 (68%) | 1,340 (84%) | mostly 07:00-20:00 only | **excluded** (M1) |
| 2016 | 254 | 210,787 (63%) | 1,260 (79%) | 07:00-20:00 only | **excluded** (M1) |
| 2017 | 255 | 190,503 (57%) | 1,222 (76%) | 07:00-20:00 only, ~52 of 60 minutes quoted | **excluded** (M1) |
| 2018 | 297 | 277,562 (83%) | 1,488 (93%) | about 23 h | included |

The declared check therefore runs on 2014 and 2018 only. That stands, and its
verdict is the round-3 verdict. Nothing about it changes.

One more run is declared here, before any backtest, **for information only. It
cannot change the verdict:** the same frozen strategy over all six years with no
exclusions (`mes_channel_breakout_long_h4_2013_2018_all_years`, `min_frac: 0`).
In 2015-2017 the CFD has no overnight quotes, so its 4-hour bars cover only
European and US hours, and its ATR and channel are measured on different bars
from the future's. The informational run is reported with that caveat and is
logged like any other run.

## Round 3 results

**Bottom line: the long-only H4 channel breakout fails its independent check.
Four of the five declared rules fail. Nothing was retuned and nothing is
proposed for the demo account.** Over 2014 and 2018, the only two years the
declared coverage rule kept, it made **+0.071 R a trade after costs on 79
trades**. It beat exposure-matched buy-and-hold, but it did not reach
+0.10 R. It did not beat random long entries, it is short of 150 trades, and
2018 made 76% of the profit. The informational all-years run is worse:
**−0.038 R after costs on 212 trades**, with losses in 2013, 2015 and 2016.
Read together with round 2, the strategy's record is mostly the S&P 500's
2019-2024 drift. It doesn't hold up on earlier data.

Each check ran once, as declared. Both are in `research/experiments.jsonl` as
`kind: frozen_check`:

- `mes_channel_breakout_long_h4_2013_2018-20261006-075152-19eb60` (the check)
- `mes_channel_breakout_long_h4_2013_2018_all_years-20261006-075207-e40f8d` (information only)

**Price scale:** confirmed. The CFD mid at 16:00 New York was 1,456.44 on
2013-01-02 against a published close of 1,462.42 (−0.41%). It was 2,057.86 on
2015-12-31 against 2,043.94 (+0.68%), and 2,507.10 on 2018-12-31 against
2,506.85 (+0.01%). All three are within 1%, so `price_scale` 1,000 holds for
the older files. The CFD's own median spread was 1.7-2.6 ticks, as in later
years. It isn't charged, because the mid is re-quoted at 1 tick.

### The declared check (2014 and 2018; 2013, 2015, 2016 and 2017 excluded for coverage)

| Rule | Value | Threshold | Result |
| --- | --- | --- | --- |
| (a) Average R after costs | +0.071 | > +0.10 | **fail** |
| (b) Minus exposure-matched buy-and-hold | +0.050 (buy-and-hold +0.021) | > 0 | pass |
| (c) Minus random long-entry p95 | −0.083 (p95 +0.154) | > 0 | **fail** |
| (d) Trades | 79 | ≥ 150 | **fail** |
| (e) Largest year's share of profit | 76% (2018) | ≤ 50% | **fail** |

| Year | Trades | Avg R after costs | Win rate | Total R | Max drawdown |
| --- | --- | --- | --- | --- | --- |
| 2014 | 40 | +0.034 | 42.5% | +1.4 R | 4.8 R |
| 2018 | 39 | +0.110 | 48.7% | +4.3 R | 6.1 R |
| **Both** | **79** | **+0.071** | **45.6%** | **+5.6 R** | **6.1 R** |

The figures are after costs. Costs averaged 0.026 R a trade, so before
commission the trade made about +0.097 R, with the stressed spread and the
slippage already in the fill prices. The profit factor was 1.14. Max drawdown
is the largest peak-to-trough fall in R, in trade order.

T0 gate table, for information (a single frozen run has no dev/validation
split or walk-forward, so those rows are n/a):

| Gate (threshold) | Value | Result |
| --- | --- | --- |
| Trades (≥ 300) | 79 | **fail** |
| Avg R after costs (≥ +0.10) | +0.071 | **fail** |
| Avg R, validation | n/a | n/a |
| Profit factor (≥ 1.25) | 1.14 | **fail** |
| Profit factor, validation | n/a | n/a |
| Monte Carlo drawdown p95 (< 6%) | 5.7% | pass |
| Daily-loss breach probability (< 2%) | 0% | pass |
| Deflated Sharpe (> 0.95, 16 prior trials) | 0.41 | **fail** |
| Walk-forward efficiency | n/a | n/a |
| Avg R at 2× spread (> 0) | +0.062 | pass |
| Largest year's share of profit (≤ 40%) | 76% | **fail** |
| Skip-10% Monte Carlo p05 (> 0) | −0.005 | **fail** |
| Beats random entries' p95 (> 0) | −0.083 | **fail** |
| Worst ±20% neighbour (> 0) | +0.003 | pass |
| Beats exposure-matched buy-and-hold (> 0) | +0.050 | pass |

### Information only: all six years, no exclusions

This run can't change the verdict. In 2015-2017 the CFD has no overnight
quotes, so the 4-hour bars there aren't the future's bars, and 2013 is missing
about 60 trading days.

| Year | Trades | Avg R after costs | Win rate | Total R | Max drawdown |
| --- | --- | --- | --- | --- | --- |
| 2013 | 36 | −0.085 | 41.7% | −3.1 R | 11.0 R |
| 2014 | 40 | +0.034 | 42.5% | +1.4 R | 4.8 R |
| 2015 | 31 | −0.312 | 38.7% | −9.7 R | 11.0 R |
| 2016 | 33 | −0.394 | 42.4% | −13.0 R | 18.0 R |
| 2017 | 33 | +0.366 | 54.5% | +12.1 R | 3.4 R |
| 2018 | 39 | +0.110 | 48.7% | +4.3 R | 6.1 R |
| **All** | **212** | **−0.038** | **44.8%** | **−8.0 R** | **30.5 R** |

Against the same five rules:

- (a) fails at −0.038.
- (b) fails at −0.131, against exposure-matched buy-and-hold of +0.093.
- (c) fails at −0.033, against a random p95 of −0.005.
- (d) passes, with 212 trades.
- (e) fails, because the total is negative.

The profit factor was 0.93. The ±20% neighbours were all negative, from
−0.054 to −0.001.

### What round 3 says

- The breakout trigger adds nothing over being long the S&P 500. On the
  declared years it beats buy-and-hold for the same hours, but not random long
  entries with the same stops and target. That was also the case in round 2.
- The edge isn't stable. Over all six years, two are clearly positive (2017
  and 2018), one is flat (2014), and three lose. 2015-2016, a choppy range for
  the S&P, cost 22.7 R. Round 2's 2022, a falling year, also lost −0.22 R a
  trade.
- The clean data is short. Only 2014 and 2018 have futures-like 23-hour
  coverage on Dukascopy. A fuller pre-2019 test would need real ES or MES
  futures history, which the repo doesn't have. But even the all-years view
  gives no reason to expect a pass on cleaner data.

## Round 3 files

- `atlas_research/check.py`: `run_check`, one frozen-parameter run over a
  declared period, read from `checks:`. It does these things:
  - checks that the params are on the strategy's declared grid, and refuses a
    period that reaches the holdout;
  - reports coverage per year and applies the exclusion rule;
  - stops the run if a price check is more than 1% off;
  - applies the five pass rules and reports the T0 gate table for information;
  - logs a `kind: frozen_check` entry with no new deflated-Sharpe trials.
- `atlas_research/cli.py`: `atlas-research t0 check <name>`.
- `atlas_research/configs/mes.yaml`: the `checks:` declarations.
- `tests/research/test_check.py`: tests on synthetic data.

Reproduce:

```bash
atlas-research --config atlas_research/configs/mes.yaml data download --symbols USA500IDXUSD --start 2013-01-01 --end 2018-12-31 --workers 2
atlas-research --config atlas_research/configs/mes.yaml data build    --symbols USA500IDXUSD --start 2013-01-01 --end 2018-12-31
atlas-research --config atlas_research/configs/mes.yaml t0 check mes_channel_breakout_long_h4_2013_2018
atlas-research --config atlas_research/configs/mes.yaml t0 check mes_channel_breakout_long_h4_2013_2018_all_years
```
