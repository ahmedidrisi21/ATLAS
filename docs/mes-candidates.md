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
commission, slippage and fees but already includes the stressed spread.

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
