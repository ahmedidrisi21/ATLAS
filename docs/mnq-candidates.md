# MNQ candidate strategies, round 1 (2026-10-06)

Five strategies from the owner's NQ/MNQ evidence review (Oct 6, 2026), tested on
Nasdaq-100 history the same way the MES rounds were tested
(`docs/mes-candidates.md`). The declaration below was written, and committed,
before any Nasdaq-100 data was downloaded, loaded or run.

## Declaration (written before any Nasdaq-100 data was downloaded or run)

### Market, data and costs

The market is the CME Micro E-mini Nasdaq-100 future (MNQ): $2 per index point,
tick 0.25 points ($0.50). That is what a $50k NinjaTrader demo account would
trade.

**Proxy data.** The repo has no futures history, so MNQ is proxied by
Dukascopy's Nasdaq-100 cash-index CFD, `USATECHIDXUSD`, one-minute bid/ask bars,
downloaded with the existing `atlas-research data download/build` path. As for
MES, the CFD's mid price is kept and re-quoted at the future's 1-tick spread
(`data.proxies` in `atlas_research/configs/mnq.yaml`). The CFD dealer's own
spread is not charged, because the futures slippage below already covers it.
2018 is downloaded for indicator warm-up only (200-day averages); no 2018 trade
is ever counted.

**Data-quality rule, declared before the download.** MES round 3 found big
coverage gaps in Dukascopy's index CFDs before 2019. Before any backtest, for
every year 2018 to mid-2025, the report gives the one-minute bar count against
the median full year 2019-2024, and the share of regular-session minutes
(09:30-16:00 New York, exchange trading days) that have a quote. A year under
70% of the median, or under 95% of regular-session minutes, is flagged and
reported. The runs themselves are not changed (as in MES rounds 1-2), but a
flagged year is named in the results. The price scale is checked against
published Nasdaq-100 closes on 2019-12-31 (8,733.07), 2021-12-31 (16,320.08),
2023-12-29 (16,825.93) and 2024-12-31 (21,012.17). A CFD mid more than 1% away
at 16:00 New York means a scale error, which would be fixed in the loader and
reported, not counted as tuning.

**Costs, in index points per contract, converted to R:**

| Cost | Charged as | Per contract |
| --- | --- | --- |
| Commission and fees | NinjaTrader MNQ, about $1.50 round turn | 0.75 pt |
| Spread | the future's 1 tick, stressed ×1.5 (×2 in the 2×-spread gate) | 0.375 pt per round trip |
| Slippage | 1 tick on every market fill: entry, stop, time exit | 0.25 pt each |
| Profit target | resting limit order | 0 |
| Overnight financing | none on futures | 0 |

A market-in, market-out trade therefore costs 1.625 points all in (the
review's "Base" tier is 1.25 pt for NQ; ours is higher because commission is
about ten times larger in points on MNQ). **Every result is also reported at
the review's Stress tier: fills at the mid price and a flat 2.0 points per
round trip, all in.** The review promotes only what stays positive there, so
this is also a gate.

### Segments and method (unchanged from MES)

- **Dev, Jan 2019 to Dec 2023, walk-forward:** each 12-month window picks the
  best grid point (at least 30 trades), which then trades the next 3 months.
- **Validation, Jan 2024 to Jun 2025:** the grid point that did best on the
  whole dev period trades once.
- **Holdout, July 2025 onward: locked, never loaded.** The review suggests a
  January 2025 holdout. That is ignored: ATLAS keeps its own holdout start
  (2025-07-01), so these results compare with every other round.

"Out-of-sample" (OOS) means the walk-forward test windows plus validation.

### Gates

The unchanged T0 gates from `mes.yaml` (OOS trades ≥ 300; average R after
costs ≥ +0.10 on dev OOS and on validation; profit factor ≥ 1.25 on both;
Monte Carlo drawdown p95 < 6%; daily-loss breach < 2%; deflated Sharpe > 0.95;
walk-forward efficiency ≥ 50%; positive at 2× spread; no year > 40% of the
profit; skip-10% Monte Carlo p05 > 0; beats random entries' p95; every ±20%
neighbour > 0), plus three of the review's own checks, which are cheap here:

1. **Positive at the Stress tier** (2.0 pt all in): average OOS R > 0.
2. **Same sign in every test year:** every OOS calendar year's average R
   after costs > 0.
3. **Beats a random-direction version:** the same entry times, stop distances
   and exits with a coin-flip direction, 50 runs; average R must be above
   their 95th percentile.

The long-only RSI(2) strategy also gets MES round 2's exposure-matched
buy-and-hold gate. For the long/short strategies the exposure-matched
buy-and-hold R is reported as information.

R for a time exit is defined as in MES round 2: 1R is the distance to the
declared protective stop, which is what would size the position.

### The candidates (the review's rules, no tuning beyond the declared grid)

| Strategy | Review | Bar | Rule | Grid (points) | Fixed |
| --- | --- | --- | --- | --- | --- |
| `mnq_late_day_momentum_m30` | #2 | 30 min | At 15:30 New York, r = return from the prior day's 16:00 close to 15:30. Long if r > 0, short if r < 0, exit 16:00. | confirm: none / the 15:00-15:30 half hour must agree (2) | stop 1 × M30 ATR(14) ("1 × average 30-minute range"); holidays and early closes skipped |
| `mnq_noise_area_m5` | #3 | 5 min | Bands = 09:30 open × (1 ± average absolute move from the open to that time over the last 14 sessions); upper band uses max(open, prior close), lower uses min(open, prior close) (gap adjustment). Only at the half-hour checkpoints 10:00 … 15:30: long above the upper band, short below the lower. Exit at a checkpoint when a long closes below its trail (short: above); flat at 16:00. An exit and an opposite entry at the same checkpoint both fill. | trail: band only / band and a TWAP stand-in for VWAP (2) | protective stop at the opposite band at entry (1R); holidays and early closes skipped |
| `mnq_opening_candle_m5` | #4 | 5 min | Direction of the 09:30-09:35 candle; enter at 09:35; stop at the candle's far end; target 10R, else exit 16:00. | none (1) | dojis (body < 1 tick) skipped; minimum stop 4.0 pt (2 × the 2.0 pt Stress round trip) |
| `mnq_rsi2_pullback_d1` | #5 | daily (decided on the 16:00 M30 close) | Long only, only when the 16:00 close > its 200-day SMA. Enter at the close on the trigger. Exit at the first close above the 5-day SMA or with RSI(2) > 70, else at the 5th close. | trigger: RSI(2) < 10 / three lower closes (2) | disaster stop 3 × daily ATR(14) (1R); exposure-matched buy-and-hold gate |
| `mnq_trend_filter_d1` | #1 | daily | Long when the 16:00 close > 200-day SMA and the 252-session return > 0, decided at the close, executed at the next 09:30 open; flat otherwise. | none | information only (see below) |

Notes on the rules:

- **VWAP (noise area).** Dukascopy's volume is the dealer's own tick activity,
  not CME volume, so a real session VWAP can't be built honestly. The declared
  primary trail is therefore **band only**. The second grid point uses a
  **time-weighted** average of each 5-minute bar's typical price, (H + L + C)/3,
  from 09:30 to the checkpoint, as a stand-in for VWAP. Futures volume is
  U-shaped through the day, so the real VWAP weights the open and the close
  more than this does. Both points were declared here, before any run. The
  review's vol-scaled sizing is not modelled; results are per unit of risk.
  Trail and entry checks are made only at the half-hour checkpoints, as in the
  paper; the protective stop is live every minute.
- **Opening candle.** This is not the MES round 1 opening-range breakout, which
  waited for a close beyond a 15- or 30-minute range with a 2R target. This is
  the review's 5-minute candle direction, entered immediately, with a 10R
  target.
- **RSI(2)** uses Wilder smoothing over 2 sessions. A "session" here is the
  16:00-to-16:00 New York day of the CFD; the daily bars are built from the
  30-minute bars. The trade holds over nights and weekends; a futures roll that
  falls inside a trade is not modelled.
- **Daily trend filter (review #1).** It makes a handful of switches a year, so
  it can't meet the 300-trade gate and isn't put through it. It is reported
  against buy-and-hold over the same period (2019 to mid-2025, dev and
  validation also separately) on total return, CAGR, maximum drawdown, Sharpe
  (daily, annualized) and exposure, one contract's notional and unlevered, at
  1.625 and 2.0 points per round trip. Its trades are also given in R, with
  1R = 3 × daily ATR(14) at the decision, as AGENTS.md asks. The review's
  volatility-targeted sizing is not tested this round. It is logged as
  `kind: information`.
- **Skipped**, because the review rejects or deprioritizes them: overnight
  drift, the unfiltered ORB as published, raw VWAP crosses, the gap
  continuation short, the MNQ study's "positive controls", dealer-gamma regimes
  and vendor equity curves. A Jev filter is not tested this round.
- **Earlier S&P tests of the same ideas.** MES round 2 tested late-day
  momentum (signal to 10:00, not 15:30) and overnight drift; MES round 1 tested
  an opening-range breakout. None passed. The deflated Sharpe of the two NQ
  versions pools their trials with those earlier ones (`dsr_pool`).

**Engine readiness.** As for the MES round-2 strategies, anything that passed
would need engine work before a demo test: 5- or 30-minute features, a
per-trade time exit, and (for RSI(2)) overnight holds that a prop firm's
flat-by rule may forbid.

## Results

**Bottom line: none of the four gated strategies passed, and nothing is
proposed for the demo account. No draft strategy file was written.** Each
strategy ran once, as declared, with no reruns and no retuning. All five runs
are in `research/experiments.jsonl`:

- `mnq_late_day_momentum_m30-20261006-103618-f94fc5`
- `mnq_noise_area_m5-20261006-103655-90c902`
- `mnq_opening_candle_m5-20261006-103747-24920c`
- `mnq_rsi2_pullback_d1-20261006-103832-7e5a52`
- `mnq_trend_filter_d1-20261006-103622-d152db` (`kind: information`)

The closest call is the noise-area strategy. It passes 13 of its 17 gates,
including every one of the review's extra checks. It fails only because its
edge per unit of risk is small: +0.023 R a trade against the +0.10 R gate.

### Data quality (reported before any backtest)

`atlas-research --config atlas_research/configs/mnq.yaml data coverage`. The
symbol is `USATECHIDXUSD`, with `price_scale` 1,000. That gives 2,526,610
one-minute bars, 2018-01-02 to 2025-06-30. **No year is flagged.** The
pre-2019 gaps that MES round 3 found in the S&P CFD don't affect this round:
the earliest year loaded is 2018, used for warm-up only, and it is clean.

| Year | M1 bars | % of median 2019-2024 | Trading days | Days with RTH quotes | RTH minutes quoted | Flag |
| --- | --- | --- | --- | --- | --- | --- |
| 2018 (warm-up) | 305,647 | starts Jan 2 (partial) | 248 | 247 | 99.3% | no |
| 2019 | 342,115 | 100.0% | 248 | 248 | 99.8% | no |
| 2020 | 341,059 | 99.7% | 249 | 249 | 99.9% | no |
| 2021 | 343,906 | 100.5% | 249 | 249 | 100.0% | no |
| 2022 | 342,776 | 100.2% | 250 | 250 | 100.0% | no |
| 2023 | 340,978 | 99.6% | 249 | 249 | 99.8% | no |
| 2024 | 342,268 | 100.0% | 249 | 249 | 99.8% | no |
| 2025 (to Jun 30) | 167,861 | partial | 123 | 122 | 99.2% | no |

**Price scale: confirmed.** The CFD mid at 16:00 New York against the
published Nasdaq-100 close: 8,726.87 vs 8,733.07 (−0.07%), 16,330.65 vs
16,320.08 (+0.06%), 16,831.65 vs 16,825.93 (+0.03%), and 21,014.81 vs
21,012.17 (+0.01%). The CFD's own median spread is 3.1-3.4 points (12-14
MNQ ticks). It isn't charged, because the mid is re-quoted at 1 tick.

### Summary (out-of-sample: walk-forward test windows plus validation)

"Gross" is before commission. The stressed spread and the slippage are already
in the fill prices. "Stress 2.0 pt" fills at the mid and charges 2.0 points all
in. "B&H" is the R that holding the index for the same hours, in the same
direction and on the same risk, would have earned. "Random dir. p95" is the
95th percentile of 50 runs with the same trades and a coin-flip direction.

| Strategy | Trades | Avg R gross | **Avg R after costs** | Avg R, Stress 2.0 pt | Win rate | B&H R | Random entry p95 | Random dir. p95 | Gates failed | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Late-day momentum (M30) | 1,058 | −0.017 | **−0.036** | −0.048 | 46.1% | +0.001 | −0.024 | +0.007 | 15 of 17 | FAIL |
| Noise area (M5) | 1,171 | +0.027 | **+0.023** | +0.021 | 42.3% | +0.000 | +0.005 | +0.003 | 4 of 17 | FAIL |
| Opening candle (M5) | 1,359 | +0.110 | **+0.084** | +0.071 | 23.3% | +0.002 | +0.061 | +0.095 | 8 of 17 | FAIL |
| RSI(2) pullback (daily) | 16 | +0.206 | **+0.205** | +0.205 | 81.3% | +0.048 | +0.395 | +0.164 | 8 of 18 | FAIL |

The same results in index points per MNQ contract ($2 a point), after base
costs:

| Strategy | Median 1R (pt) | Net pt / trade | Net $ / trade (1 MNQ) | Total OOS (pt) | Max drawdown (pt, trade order) |
| --- | --- | --- | --- | --- | --- |
| Late-day momentum | 42 | −1.1 | −$2.23 | −1,180 | 3,220 |
| Noise area | 241 | +7.7 | +$15.34 | +8,981 | 1,076 |
| Opening candle | 36 | +3.1 | +$6.10 | +4,146 | 1,625 |
| RSI(2) pullback | 979 | +204 | +$409 | +3,268 | n/a (16 trades) |

### By year (out-of-sample, average R after costs; Stress 2.0 pt in brackets)

| Year | Late-day trades | Late-day R | Noise trades | Noise R | Candle trades | Candle R | RSI(2) trades | RSI(2) R |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2020 | 249 | +0.034 (+0.022) | 211 | +0.010 (+0.008) | 246 | +0.071 (+0.041) | 0 | — |
| 2021 | 216 | −0.028 (−0.040) | 211 | +0.028 (+0.026) | 247 | +0.135 (+0.160) | 0 | — |
| 2022 | 167 | +0.074 (+0.067) | 248 | +0.019 (+0.018) | 248 | +0.184 (+0.167) | 0 | — |
| 2023 | 210 | −0.108 (−0.121) | 206 | +0.036 (+0.034) | 248 | −0.021 (−0.041) | 0 | — |
| 2024 | 138 | −0.130 (−0.141) | 198 | +0.017 (+0.016) | 248 | +0.047 (+0.026) | 12 | +0.283 (+0.283) |
| 2025 (H1) | 78 | −0.164 (−0.172) | 97 | +0.037 (+0.036) | 122 | +0.099 (+0.081) | 4 | −0.030 (−0.030) |
| **Same sign every year?** | | **no** | | **yes** | | **no (2023)** | | **no (2025)** |

2019 is the first walk-forward training window, so it has no OOS trades. For
the opening candle, years weighted in points differ from years in R. In points,
2020 (−42), 2023 (−227) and 2025 H1 (−116) lost, and 2022 made 56% of the
total: wide-stop days carry more points per R.

### Dev vs validation, after costs

| Strategy | Dev OOS trades | Dev avg R | Dev PF | Validation trades | Validation avg R | Validation PF | Final parameters |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Late-day momentum | 842 | −0.009 | 0.97 | 216 | −0.142 | 0.65 | 15:00-15:30 must agree |
| Noise area | 876 | +0.023 | 1.21 | 295 | +0.024 | 1.21 | band-only trail |
| Opening candle | 989 | +0.092 | 1.12 | 370 | +0.064 | 1.08 | the published rule (one point) |
| RSI(2) pullback | 0 | — | — | 16 | +0.205 | 4.03 | RSI(2) < 10 |

**RSI(2) got no dev walk-forward trades.** It makes 4-20 qualifying signals a
year (38 trades over all of 2019-2023 once overlapping signals are skipped),
so no 12-month training window reaches the harness's 30-trade minimum, and
walk-forward never selects. That is how the declared method works; it was not
changed. Its only OOS trades are validation's 16.

### Gates (pass / fail)

| Gate (threshold) | Late-day | Noise area | Opening candle | RSI(2) |
| --- | --- | --- | --- | --- |
| OOS trades (≥ 300) | pass 1,058 | pass 1,171 | pass 1,359 | **fail** 16 |
| Avg R after costs, dev (≥ +0.10) | **fail** −0.009 | **fail** +0.023 | **fail** +0.092 | **fail** (no trades) |
| Avg R after costs, validation (≥ +0.10) | **fail** −0.142 | **fail** +0.024 | **fail** +0.064 | pass +0.205 |
| Profit factor, dev (≥ 1.25) | **fail** 0.97 | **fail** 1.21 | **fail** 1.12 | **fail** (no trades) |
| Profit factor, validation (≥ 1.25) | **fail** 0.65 | **fail** 1.21 | **fail** 1.08 | pass 4.03 |
| Monte Carlo drawdown p95 (< 6%) | **fail** 26.6% | pass 4.6% | **fail** 38.5% | pass 0.4% |
| Daily-loss breach probability (< 2%) | pass 0% | pass 0% | pass 0% | pass 0% |
| Deflated Sharpe (> 0.95) | **fail** 0.00 (6 trials) | pass 0.98 (2) | **fail** 0.40 (5) | **fail** 0.88 (2) |
| Walk-forward efficiency (≥ 50%) | **fail** < 0 | pass 156% | pass 88% | **fail** 0% |
| Avg R at 2× spread (> 0) | **fail** −0.040 | pass +0.023 | pass +0.082 | pass +0.205 |
| Largest year's share of profit (≤ 40%) | **fail** 100% | pass 27% | pass 40% (39.7%) | **fail** > 100% |
| Skip-10% Monte Carlo p05 (> 0) | **fail** −0.051 | pass +0.017 | pass +0.048 | pass +0.163 |
| Beats random entries' p95 (> 0) | **fail** −0.013 | pass +0.018 | pass +0.024 | **fail** −0.190 |
| Worst ±20% neighbour (> 0) | **fail** −0.176 | pass +0.026 | pass +0.064 | pass +0.023 |
| *Review:* Stress 2.0 pt all in (> 0) | **fail** −0.048 | pass +0.021 | pass +0.071 | pass +0.205 |
| *Review:* every OOS year > 0 | **fail** −0.164 | pass +0.010 | **fail** −0.021 | **fail** −0.030 |
| *Review:* beats random direction p95 (> 0) | **fail** −0.044 | pass +0.020 | **fail** −0.010 | pass +0.041 |
| Beats exposure-matched buy-and-hold (> 0) | n/a | n/a | n/a | pass +0.157 |

### Every grid point (whole dev period and validation, after costs)

| Strategy | Parameters | Dev trades | Dev avg R (gross) | Validation trades | Validation avg R |
| --- | --- | --- | --- | --- | --- |
| Late-day | no confirmation | 1,242 | −0.018 (+0.007) | 370 | −0.081 |
| Late-day | 15:00-15:30 agrees | 756 | −0.017 (+0.007) | 216 | −0.142 |
| Noise area | band-only trail | 1,056 | +0.016 (+0.021) | 295 | +0.024 |
| Noise area | band + TWAP trail | 1,221 | +0.012 (+0.017) | 371 | +0.022 |
| Opening candle | the published rule | 1,236 | +0.090 (+0.125) | 370 | +0.064 |
| RSI(2) | RSI(2) < 10 | 38 | +0.066 (+0.067) | 16 | +0.205 |
| RSI(2) | three lower closes | 36 | +0.010 (+0.011) | 15 | +0.219 |

### Daily trend filter (review #1, information only)

Long when the 16:00 close is above its 200-day SMA and the 252-session return
is positive, executed at the next 09:30 open. One contract's notional,
unlevered, 2019-01-02 to 2025-06-30. The first valid signal is 2019-01-14,
because the 252-session lookback needs data from early January 2018, so the
first 8 sessions of 2019 are flat. The index was below its 200-day average
then, so they would have been flat anyway.

| Period | | Total return | CAGR | Max drawdown | Sharpe (daily, ann.) | Exposure | Round trips |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2019 - Jun 2025 | **filter** | +159.4% | +15.8% | **23.2%** | **0.97** | 74% | 16 |
| | buy-and-hold | **+265.8%** | **+22.1%** | 35.5% | 0.96 | 100% | 1 |
| Dev 2019-2023 | filter | +105.6% | +15.5% | 23.2% | 0.96 | 70% | 14 |
| | buy-and-hold | +171.6% | +22.2% | 35.5% | 0.94 | 100% | 1 |
| Validation 2024 - Jun 2025 | filter | +27.3% | +17.6% | 13.5% | 1.06 | 88% | 3 |
| | buy-and-hold | +35.9% | +22.8% | 22.9% | 1.03 | 100% | 1 |

These are at base costs (1.625 pt a round trip). At the Stress tier (2.0 pt),
every figure is the same to the first decimal: costs are about 0.01% of
notional per switch.

| Year | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 H1 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Filter | +16.0% | +33.6% | +27.9% | **−17.7%** | +25.9% | +25.9% | +0.2% |
| Buy-and-hold | +40.6% | +47.5% | +27.9% | **−33.4%** | +53.9% | +25.9% | +7.0% |
| Exposure | 85% | 92% | 100% | 7% | 66% | 100% | 63% |

In R (1R = 3 × daily ATR(14) at the decision, as declared), the filter made
16 trades averaging **+0.963 R after costs** (+0.966 gross; +0.963 at
2.0 pt). Two long holds made almost all of it: Apr 2020 - Jan 2022 (+4.95 R)
and May 2023 - Mar 2025 (+10.96 R). The other 14 trades total −0.5 R.

## What this says

- **Late-day momentum doesn't work on the Nasdaq-100 either.** It averages
  about zero before costs and −0.036 R after. 2023 to mid-2025 lose every
  year, and the "15:00-15:30 must agree" variant is worse in validation. It
  does no better than random direction. This matches MES round 2 (S&P,
  signal to 10:00: −0.065 R) and the decay the review reports. Two indices
  and two signal windows now agree.
- **The noise-area strategy is the most consistent result ATLAS has found so
  far, and it still fails.** It is positive in every OOS year, at every cost
  tier and at both grid points. It beats random entries, random directions and
  its ±20% neighbours, and its deflated Sharpe is 0.98. But it earns only
  +0.023 R a trade, with a profit factor of 1.21, against gates of +0.10 R and
  1.25. In points that is +7.7 net per trade (about $15 per MNQ contract),
  1.4 trades a day. The small R comes from the declared 1R: the opposite band,
  a median 241 points away, a catastrophic stop that is almost never hit (4
  of 1,171 trades). A tighter risk unit would raise R per trade but would
  also change which trades stop out. Choosing one now, after seeing this,
  would be fitting to the result. If the operator wants it pursued, it should
  be declared as a new round with its own risk unit and stop, before any run,
  and ideally checked on real NQ futures prints. Longs made almost all of it
  (+0.040 R vs +0.004 R for shorts) in a rising market. Exposure-matched
  buy-and-hold was about 0 R, though, so it isn't simply the drift.
- **The opening candle reproduces the published gross edge, and it isn't
  enough.** Gross is +0.110 R, close to the independent NQ replication's
  +0.131 R. After MNQ costs it is +0.084 R, and +0.071 R at 2.0 pt. Costs
  take 0.027 R a trade here, because the median stop is only 36 points. It
  fails the +0.10 R and profit-factor gates, the drawdown gate (losing streaks
  at a 23% hit rate) and 2023. It also loses to its own random-direction
  benchmark: the same bracket with a coin-flip direction reached +0.095 R at
  the 95th percentile and averaged +0.021 R. Most of the result is the shape
  of the bracket (a 10R target, a close exit), not the candle's direction.
  That is exactly the review's warning.
- **RSI(2) can't be judged on this sample.** It trades too rarely for the
  walk-forward to pick it on dev (38 dev trades in 5 years), so its OOS is 16
  validation trades. Those were good (+0.205 R, PF 4.0, beat buy-and-hold and
  random direction) but are far below 300 trades, below random long entries'
  p95, and 2025 was negative. The whole-dev numbers per grid point (+0.066 R
  and +0.010 R) are weak. It needs a much longer history, which this proxy
  doesn't have before 2018.
- **The daily trend filter did what the review says it does: drawdown
  control, not extra return.** It cut the worst drawdown from 35.5% to 23.2%,
  with about the same Sharpe (0.97 vs 0.96). It gave up a third of the total
  return (+159% vs +266%), mostly by being out for the rebounds of 2019, 2020
  and 2023. In 2022 it was flat 93% of the time and lost −17.7% against
  −33.4%. That is one bear market in 6½ years, so this is anecdote, not
  evidence. Its value to ATLAS is as a regime input, as the review suggests,
  not as a strategy to trade alone.

## Caveats

- **The proxy.** These are cash-index CFD prices re-quoted at the future's
  1-tick spread, not MNQ prints. There is no basis or roll, which matters
  little intraday. RSI(2) and the trend filter hold across days, and a roll
  inside a trade isn't modelled. Dukascopy's volume isn't CME volume, which is
  why the noise area uses a band-only or TWAP trail instead of VWAP.
  Regular-session coverage is 99%+ in every year, so there were no data gaps
  to work around this round.
- **R depends on the declared stop.** It is defined as in MES round 2. For
  the noise area and RSI(2), the stop is a wide disaster stop, so R per trade
  is small and costs in R are tiny. The point and dollar table above is the
  better guide to size.
- The account numbers behind the drawdown and daily-loss gates are the T0
  placeholders (0.4% risk a trade, 10% / 5% limits), kept so rounds compare.
- The deflated Sharpe for late-day momentum and the opening candle pools their
  trials with MES round 2's `intraday_momentum` and round 1's
  `opening_range_breakout`. The noise area and RSI(2) have only their own 2
  trials each, so their deflated Sharpe is generous.
- The review's suggested January 2025 holdout was not used. The ATLAS holdout
  (July 2025 on) stays locked and was never loaded.
- **Engine readiness.** Nothing passed. Anything taken further would need
  5- or 30-minute features and a per-trade time exit in the engine, and for
  RSI(2) overnight holds that a prop firm's flat-by rule may forbid.

## Files

- `atlas_research/configs/mnq.yaml`: the declared round (proxy, costs and the
  Stress tier, gates and the review's extra checks, strategies, the data rule,
  the information run).
- `atlas_research/research_setups.py`: the four research-only setups
  (`late_day_momentum`, `noise_area`, `opening_candle`, `rsi2_pullback`) and
  the daily-bar helpers. They are not wired into the engine.
- `atlas_research/t0.py`:
  - research setups are looked up next to the engine's;
  - `warmup_days` and `dsr_pool`;
  - the all-in cost tier (`costs.all_in_round_trip`);
  - the same-sign-every-year and random-direction gates (`random_direction`);
  - per-grid-point and by-year results.
- `atlas_research/backtest.py`: `ExitPolicy.reenter_at_exit_bar` (default
  off, so earlier results don't change).
- `atlas_research/daily_trend.py`: the trend filter against buy-and-hold.
- `atlas_research/check.py`: `rth_coverage`, the data-quality report.
- `atlas_research/cli.py`:
  - `atlas-research data coverage`;
  - `atlas-research t0 info <name>`.
- `atlas_engine/market_data/symbols.py`: `USATECHIDXUSD` (price scale 1,000).
- `tests/research/test_mnq_round.py`: tests on synthetic data.

Reproduce:

```bash
atlas-research --config atlas_research/configs/mnq.yaml data download --symbols USATECHIDXUSD --start 2018-01-01 --workers 3   # rerun to resume
atlas-research --config atlas_research/configs/mnq.yaml data build    --symbols USATECHIDXUSD --start 2018-01-01
atlas-research --config atlas_research/configs/mnq.yaml data coverage
atlas-research --config atlas_research/configs/mnq.yaml t0 run --all
atlas-research --config atlas_research/configs/mnq.yaml t0 info mnq_trend_filter_d1
```

# MNQ round 2: the noise area with a closer stop (2026-10-06)

## Round 2 declaration (written and committed before any round-2 or 2018 run)

**This is a second look, not a fresh test.** Round 1's results above were seen
before this was written: the noise-area strategy made +0.023 R a trade (+7.7
points) over 1,171 out-of-sample trades and failed only the size gates, with a
1R (the opposite band, a median 241 points away) that was hit on 4 trades.
Round 1's own write-up said a tighter risk unit could only be tested as a new,
declared round. This is that round. Anything it finds is weaker evidence than
a first test would be, which is why it also has an independent 2018 check,
declared here.

### What changes: one rule

A fixed protective stop that also defines 1R. It is live every minute, from
entry to exit:

> stop distance = k × the band's half-width at the entry checkpoint =
> k × σ(t) × the band's base price, measured from the checkpoint close.

σ(t) is the strategy's own noise measure: the average absolute move from the
09:30 open to that checkpoint over the previous 14 sessions. The base price is
the one the band is built on: max(open, prior close) for a long, min(open,
prior close) for a short. So k counts "noise units" against the trade.

**Grid: k = 0.5, 1.0, 1.5, 2.0** (4 points).

**Why this stop, on principle:**

- It uses the strategy's own unit of volatility, so it scales with the market
  and with the time of day exactly as the entry bands do. Nothing new is
  estimated.
- The points have a plain meaning. A long enters on a close above the upper
  band, one noise unit above the base. k = 1 puts the stop about back at the
  base (the open or prior close): the breakout has fully failed. k = 0.5 is
  halfway back into the noise area. k = 2 is near the opposite band. It is
  never wider than round 1's stop, because the opposite band is at least two
  half-widths from the entry close.
- It was not chosen from round-1 trades. No round-1 excursion (MAE/MFE),
  stop-distance or per-k analysis was run or looked at. Only round 1's
  published summary numbers were seen, and they are quoted above. The grid is
  a simple ladder from half to double the natural unit.

### What stays exactly as in round 1

- **Entries:** round 1's signals, unchanged. The setup's checkpoint state
  machine is not told about the stop. A trade stopped out is not re-entered
  until the setup would have gone flat and a new breakout fires, so the
  entry list is round 1's.
- **Exits:** the band-only trail at the half-hour checkpoints (10:00 … 15:30),
  flat at 16:00, an exit and an opposite entry at the same checkpoint both
  fill. The band + TWAP trail is not retested.
- **Data, costs, segments, gates:** the same proxy, base costs (1.625 pt all
  in), the 2.0 pt all-in Stress tier, 2× spread, the walk-forward on 2019-2023
  (OOS from 2020), validation Jan 2024 to Jun 2025, the holdout locked. Every
  T0 gate, plus the review's three checks (Stress > 0, every OOS year > 0,
  beats random direction's p95).
- **Deflated Sharpe:** these 4 trials pool with round 1's 2 noise-area trials
  (same setup, `noise_area`), 6 in all.
- The run is made once. The verdict is the standard T0 run (walk-forward
  picks; the final point is picked on the whole dev period and trades
  validation once).

R is not comparable across rounds: 1R is a different distance. **Every
result is also given in points per trade**, which are comparable.

**Information only, also declared now:** each of the 4 grid points is
also reported held fixed over the same out-of-sample span (2020-01 to
2025-06). That covers trades, average R at base and Stress costs, points per
trade, profit factor, the gates a fixed point can be judged on, sign by year,
and the random-entry and random-direction checks. These per-point numbers
are not the verdict and cannot change it.

### Independent check on 2018, declared before any 2018 run

- **What runs:** the frozen grid point that round 2 picks on the whole dev
  period (its `final_params`), over 2018-01-01 to 2018-12-31. It goes through
  `atlas-research t0 check` (`atlas_research/check.py`): nothing is selected,
  no new deflated-Sharpe trial is added, and the costs and simulator are the
  same. The chosen k is written into `checks:` in `mnq.yaml` after the round-2
  run and before the 2018 run. Nothing else in the check may change then.
- **Data rule:** 2018 is included only if it has at least 70% of the median
  full year's one-minute bars (342,192, from round 1's report) and at least
  95% of regular-session minutes quoted. Round 1's report already showed
  99.3%. The price is checked against the published Nasdaq-100 close on
  2018-12-31 (6,329.96); more than 1% off stops the run. Data begins
  2018-01-02, so the 14-session σ first exists in late January. There are no
  trades before then.
- **Pass rules, all of which must hold:** (a) average R after base costs > 0;
  (b) above exposure-matched buy-and-hold R; (c) above the random-entry p95
  (50 runs); (d) at least 150 trades; (e) average R at the 2.0 pt Stress tier
  > 0; (f) above the random-direction p95 (50 runs). These rules ask for the
  sign, not the +0.10 R size. One year is about 200 trades, too few to measure
  a size reliably. Beating the random-direction p95 on that sample already
  needs a real edge. The year-share rule doesn't apply to a single year. The
  T0 gate table is reported as information.
- **Also reported, as information only:** round 1's original band-stop version
  (`mnq_noise_area_m5`, band trail) on the same 2018, under the same rules.
  It cannot change any verdict.
- **2018 in round 1:** 2018 was loaded only as indicator warm-up for the
  RSI(2) run (`warmup_days: 365`) and the trend filter. The noise-area run
  loaded from 2019-01-01, so it never saw 2018. Every T0 window starts in 2019
  or later. After this declaration is committed, the round-1 trade files are
  checked for any 2018 entry, and the result is reported below.

**After the declaration commit (`1a86b30`):** the round-1 trade files were
checked. No round-1 trade, in any strategy, entered before 2020-01-02. The
noise-area run's first OOS trade is 2020-01-02, RSI(2)'s is 2024-01-02, and
the trend filter's first signal is 2019-01-14. 2018 was never traded.

## Round 2 results

**Bottom line: round 2 fails. It fails 5 of its 17 gates, so nothing is
proposed for the demo account and no strategy file was written.** The
walk-forward picked the tightest stop, k = 0.5. It was strong on dev (+0.198 R,
PF 1.43) and close to zero on validation (+0.009 R, PF 1.02), and 2024 lost.
The frozen k = 0.5 then **passed all six declared 2018 rules**, by a wide
margin. That is a real, independent point in its favour, but it doesn't undo
the validation failure. Each run was made once, with no reruns. All three are
in `research/experiments.jsonl`:

- `mnq_noise_area_r2_m5-20261006-113937-69c5b8`, the round-2 T0 run (the verdict)
- `mnq_noise_area_r2_2018-20261006-114355-ef9f2d`, the 2018 check (`kind: frozen_check`)
- `mnq_noise_area_r1_2018_info-20261006-114406-b74a66`, round 1's band stop
  on 2018, information only. Its registry line says `passed: true`
  against the same rules, but it can't change any verdict.

The chosen k was written into `checks:` in commit `4caf21f`, after the
round-2 run and before the 2018 runs.

### The verdict run (walk-forward picks, out-of-sample 2020 to Jun 2025)

| | Round 2 (k picked per fold; final k = 0.5) | Round 1 (band stop), for comparison |
| --- | --- | --- |
| OOS trades | 1,164 | 1,171 |
| Avg R gross | +0.169 | +0.027 |
| **Avg R after base costs** | **+0.150** | +0.023 |
| Avg R at 2.0 pt Stress | +0.140 | +0.021 |
| Avg R at 2× spread | +0.142 | +0.023 |
| Profit factor | 1.30 | 1.21 / 1.21 |
| Dev OOS: avg R, PF (869 trades) | +0.198, 1.43 | +0.023, 1.21 |
| Validation: avg R, PF (295 trades) | **+0.009, 1.02** | +0.024, 1.21 |
| Median 1R | 44 pt | 241 pt |
| **Net points per trade** | **+7.3** (+$14.55 per MNQ) | +7.7 |
| Total OOS points | +8,470 | +8,981 |
| Exits | 462 stops (1 gap), 702 trail/close | 4 stops |
| Long / short avg R | +0.237 (604) / +0.056 (560) | +0.040 / +0.004 |
| Random entry p95 | −0.009 | +0.005 |
| Random direction mean / p95 | +0.019 / +0.075 | p95 +0.003 |
| Exposure-matched buy-and-hold R | +0.002 | +0.000 |
| Deflated Sharpe (6 trials pooled) | 0.99 | 0.98 (2) |

The 7 fewer trades than round 1 are not a change in entries. In Q1 2020
round 1's walk-forward picked its band + TWAP trail. Every round-2 grid
point has exactly round 1's band-trail entries: 1,056 on dev and 295 on
validation.

Walk-forward picks by test quarter: k = 2.0 for Q1 2020, k = 1.0 from Q2 2020
to Q3 2021, then k = 0.5 from Q4 2021 on. The whole-dev pick (validation's
point and the 2018 check's point) is k = 0.5.

| Year (OOS) | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 H1 |
| --- | --- | --- | --- | --- | --- | --- |
| Trades | 204 | 211 | 248 | 206 | 198 | 97 |
| Avg R after costs | +0.089 | +0.146 | +0.266 | +0.276 | **−0.073** | +0.176 |
| Avg R, Stress 2.0 pt | +0.083 | +0.138 | +0.257 | +0.263 | **−0.089** | +0.168 |

| Gate (threshold) | Value | |
| --- | --- | --- |
| OOS trades (≥ 300) | 1,164 | pass |
| Avg R after costs, dev (≥ +0.10) | +0.198 | pass |
| Avg R after costs, validation (≥ +0.10) | +0.009 | **fail** |
| Profit factor, dev (≥ 1.25) | 1.43 | pass |
| Profit factor, validation (≥ 1.25) | 1.02 | **fail** |
| Monte Carlo drawdown p95 (< 6%) | 18.5% | **fail** |
| Daily-loss breach (< 2%) | 0% | pass |
| Deflated Sharpe (> 0.95), 6 trials | 0.99 | pass |
| Walk-forward efficiency (≥ 50%) | 110% | pass |
| Avg R at 2× spread (> 0) | +0.142 | pass |
| Largest year's share of profit (≤ 40%) | 37.8% | pass |
| Skip-10% Monte Carlo p05 (> 0) | +0.119 | pass |
| Beats random entries' p95 | +0.159 | pass |
| Worst ±20% neighbour, validation (> 0) | −0.025 (k = 0.4) | **fail** |
| *Review:* Stress 2.0 pt (> 0) | +0.140 | pass |
| *Review:* every OOS year > 0 | −0.073 (2024) | **fail** |
| *Review:* beats random direction p95 | +0.075 | pass |

### Every grid point held fixed (information only, declared; not the verdict)

Each point traded over the whole OOS span (2020-01 to 2025-06) with no
selection. Same 1,164 entries for all four.

| k | Avg R base | Avg R 2.0 pt | Net pt / trade | Median 1R (pt) | PF | Dev span R / PF | Validation R / PF | Stops hit | Random entry p95 | Random dir. p95 | Gates failed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.5 | +0.148 | +0.135 | +6.4 | 35 | 1.26 | +0.195 / 1.35 | **+0.009 / 1.02** | 561 | −0.010 | +0.070 | 4: val R, val PF, MC DD 21.7%, 2024 < 0 |
| 1.0 | +0.114 | +0.108 | +8.4 | 69 | 1.31 | +0.117 / 1.33 | +0.106 / 1.27 | 277 | +0.018 | +0.050 | 1: MC DD 12.8% |
| 1.5 | +0.072 | +0.068 | +7.9 | 103 | 1.27 | +0.070 / 1.27 | +0.077 / 1.28 | 137 | +0.009 | +0.023 | 3: dev R, val R, MC DD 9.8% |
| 2.0 | +0.056 | +0.053 | +7.9 | 136 | 1.28 | +0.055 / 1.27 | +0.059 / 1.28 | 68 | +0.011 | +0.021 | 3: dev R, val R, MC DD 7.7% |

Sign by year, average R after base costs:

| k | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 H1 |
| --- | --- | --- | --- | --- | --- | --- |
| 0.5 | +0.086 | +0.138 | +0.266 | +0.276 | **−0.073** | +0.176 |
| 1.0 | +0.147 | +0.101 | +0.119 | +0.103 | +0.149 | +0.017 |
| 1.5 | +0.086 | +0.070 | +0.062 | +0.064 | +0.108 | +0.014 |
| 2.0 | +0.053 | +0.072 | +0.037 | +0.060 | +0.085 | +0.008 |

Every point beats its random-entry and random-direction p95 and is positive
at the Stress tier. All ±20% neighbours of each fixed point are positive over
the span. The fixed-point deflated Sharpes are 0.98-0.996.

### The 2018 independent check (frozen k = 0.5)

Data: 305,647 one-minute bars (89% of the median year), 99.3% of
regular-session minutes quoted, so the year is included. CFD mid 6,331.90
against the published 6,329.96 on 2018-12-31 (+0.03%). The first trade is
2018-01-22, after the 14-session warm-up.

| Rule | k = 0.5 (the check) | Round 1 band stop (information) |
| --- | --- | --- |
| Trades (≥ 150) | 200, pass | 200, pass |
| (a) Avg R after base costs (> 0) | **+0.533**, pass | +0.058, pass |
| (b) minus exposure-matched B&H (> 0) | +0.532, pass | +0.058, pass |
| (c) minus random-entry p95 (> 0) | +0.399 (p95 +0.134), pass | +0.031, pass |
| (e) Avg R at 2.0 pt Stress (> 0) | +0.510, pass | +0.054, pass |
| (f) minus random-direction p95 (> 0) | +0.159 (p95 +0.374), pass | +0.020, pass |
| **Verdict** | **PASS (6 of 6)** | (information) |
| Profit factor | 1.88 | 1.47 |
| Net points per trade | +9.6 | +7.7 |
| Median 1R | 15.6 pt | 104 pt |
| Stops hit | 97 (2 gaps) | 0 |
| Max drawdown in trade order | 23.8 R | 4.2 R |

The T0 gate table, as information, for the k = 0.5 check: it fails the
300-trade gate (200), the Monte Carlo drawdown gate (9.3%) and the year-share
gate (one year is 100% by definition). It passes the rest, including the
deflated Sharpe (1.00) and all ±20% neighbours (+0.40 to +0.63 R).

The 2018 result is very lumpy. The 10 best trades made 91% of the year's R.
By quarter: Q1 +0.89 R a trade (47 trades, the February volatility spike),
Q2 −0.01, Q3 +0.52, Q4 +0.78 (the autumn sell-off). Shorts made +1.04 R a
trade (91), longs +0.11 (109). Both 2018 versions had the same 200 entries.

## What this says (round 2)

- **A closer stop turns the same trades into a bigger R, but it doesn't add
  points.** Round 1 made about +7.7 points a trade. Round 2's verdict run made
  +7.3, and the fixed points made +6.4 to +8.4. The edge in points barely
  moved. What changed is the unit: a 35-70 point 1R instead of 241. That
  lifts R per trade from +0.02 to +0.06 to +0.15. Round 1's write-up
  expected exactly this. The open question was always whether the edge holds
  up when the stop actually gets hit. It mostly does: the k = 1.0 and k = 0.5
  versions cut 277 and 561 trades early and still made similar points.
- **But the declared test failed, for a real reason.** The walk-forward
  drifted to the tightest stop (k = 0.5) because 2022-2023 rewarded it. Then
  validation gave it +0.009 R (PF 1.02), and 2024 lost money. That is the
  pattern of a parameter picked in one regime and failing in the next. Its
  dev-to-validation gap (+0.20 R to +0.01 R) is the main warning in this
  round.
- **k = 1.0 looks like the most stable point, and that can't be used.** Held
  fixed, it is positive in every year (+0.02 to +0.15 R). It makes +0.106 R
  with PF 1.27 on validation and passes every gate a fixed point can be judged
  on except the drawdown gate. But that judgment uses the validation result.
  Choosing k = 1.0 now would be selection on validation, the very thing the
  segments exist to prevent. It is a hypothesis for a future declared round,
  not a result.
- **The 2018 check passed, strongly and independently.** It used the
  pre-declared frozen point, on a year no ATLAS run had ever traded. It beat
  random direction, random entry and buy-and-hold, and was positive at 2.0 pt.
  Round 1's band-stop version also stayed positive in 2018 (+7.7 points a
  trade, the same as 2020-2025). So the strategy's sign held in a third,
  out-of-period year, a bearish one. That is the best evidence ATLAS has that
  this effect is real. The size is not reliable: 2018's +0.53 R comes from 10
  trades in two volatile quarters.
- **The Monte Carlo drawdown gate is now a binding limit.** At 0.4% risk per
  trade, every tighter-stop version has a p95 drawdown of 7.7-21.7% against a
  6% limit. Round 1 passed this gate only because its huge 1R made each trade
  tiny. A 34% win rate with 1R at 35 points gives long losing streaks. Any
  future version needs this gate in mind, as a smaller risk per trade or a
  wider stop.
- **Where this leaves the noise area.** It is still the most consistent
  strategy ATLAS has tested. It is positive in points every OOS year at every
  stop tested, and positive in 2018. It has not passed T0. A third look would
  be a third look at the same data, so the honest next step is new data,
  not a new parameter. Real MNQ/NQ futures prints, or the holdout once the
  operator decides to spend it, could test it fresh. If the operator wants a
  round 3, it should be declared with one fixed stop (no grid) before any run,
  and say which round-2 numbers motivated it.

## Round 2 caveats

- This was a second look at data whose round-1 result was known. The deflated
  Sharpe pools all 6 trials, but it can't fully price the choice to come back
  to this strategy.
- R isn't comparable across rounds; points are. The 2018 check's rules ask
  only for the sign. One year (200 trades) can't confirm +0.10 R, and the
  2018 R is dominated by a few trades.
- Same proxy caveats as round 1: CFD mid re-quoted at one MNQ tick, not
  futures prints. The tight k = 0.5 stop (median 15.6 pt in 2018, 35 pt in
  2020-2025) is the most exposed to fill quality. Its stop fills are modelled
  at the stop plus 1 tick, or at the open on a gap.
- The holdout (July 2025 on) stayed locked and was never loaded.

## Round 2 files

- `atlas_research/configs/mnq.yaml`: `mnq_noise_area_r2_m5` (the 4-point stop
  grid, `report_grid_points`), and `checks:` (the 2018 check and the round-1
  information check).
- `atlas_research/research_setups.py`: `noise_area` gained `stop_k` (version
  0.2.0). The default `None` is round 1's opposite-band stop, unchanged.
- `atlas_research/t0.py`: `points_summary` (net points per trade, median 1R in
  points, in every run's `extra`), and `fixed_point_report` (each grid point
  held fixed over the OOS span, when a strategy sets `report_grid_points`).
- `atlas_research/check.py`:
  - frozen checks can run research setups and the re-entry exit flag;
  - the H4 coverage reference is optional and a regular-session minimum
    (`min_rth_frac`) can be added;
  - only declared pass rules are evaluated, plus two new ones
    (`min_all_in_expectancy_r`, `beat_random_direction_p95`);
  - results carry points, Stress and random-direction figures.
  MES round 3's check is unchanged.
- `tests/research/test_mnq_round2.py`.

Reproduce:

```bash
atlas-research --config atlas_research/configs/mnq.yaml t0 run mnq_noise_area_r2_m5
atlas-research --config atlas_research/configs/mnq.yaml t0 check mnq_noise_area_r2_2018
atlas-research --config atlas_research/configs/mnq.yaml t0 check mnq_noise_area_r1_2018_info
```

# MNQ round 3: an independent check on 2013-2017 (2026-10-06)

## Round 3 declaration (written and committed before any 2013-2017 backtest)

**Why this round.** The noise area (`noise_area`, round 2's volatility stop) is
ATLAS's best candidate so far, and the owner asked to keep testing it. A third
look at 2019-2025 would be a third look at the same data, which round 2's
write-up ruled out. So this round tests it on Nasdaq-100 data it has never
seen: **2013-01-01 to 2017-12-31**. No ATLAS run has loaded Nasdaq-100 data
before 2018 (round 1's download started 2018-01-01). This declaration was
written while the 2013-2017 download was still running, before its coverage
report was looked at, and is committed before any 2013-2017 backtest.

**What a pass would mean, and what it wouldn't.** A pass here does **not**
make the noise area a passed T0 strategy. Round 2's validation (Jan 2024 to
Jun 2025) still fails: +0.009 R, PF 1.02, 2024 negative, 5 of 17 gates failed.
This round only says whether the edge exists outside 2018-2025, in five
earlier years with different regimes (the 2013 and 2017 rallies, the 2015-16
correction, the August 2015 flash crash, the 2016 Brexit and election gaps).
A fail is just as informative: it would say the edge is specific to the
2018-2025 market.

### What runs: two frozen versions, unchanged, each run once

The strategy is `mnq_noise_area_r2_m5` exactly as in round 2: round 1's
entries (bands from the 14-session average move, gap-adjusted, half-hour
checkpoints 10:00 … 15:30), the band-only trail, flat at 16:00, and the
volatility stop at k × the band's half-width, live every minute. Nothing is
retuned, nothing is selected, and no new deflated-Sharpe trial is added. Both
go through `atlas-research t0 check` (`atlas_research/check.py`), as the 2018
check did.

| Check | Version | Status | Why this version |
| --- | --- | --- | --- |
| `mnq_noise_area_r3_2013_2017_k05` | stop_k = 0.5 | **primary: the verdict** | Round 2's verdict point: the whole-dev pick, chosen by the declared method without seeing validation. It is the point the 2018 check froze. |
| `mnq_noise_area_r3_2013_2017_k10` | stop_k = 1.0 | **secondary** | Round 2's declared hypothesis. **It was picked after seeing the 2020-2025 results held fixed, validation included** (positive every year, +0.106 R and PF 1.27 on validation). That is selection on validation, so a pass here is weaker evidence than a k = 0.5 pass, and it can't stand in for one. |

Neither version has ever traded 2013-2017. Both are on round 2's declared
grid (`check.py` refuses an off-grid frozen value).

### Data and the coverage rule (declared before the coverage report)

- **Data:** `USATECHIDXUSD` one-minute bid/ask bars, 2013-01-01 to 2017-12-31,
  downloaded and built with the existing `atlas-research data download/build`
  path, the same proxy as rounds 1-2 (mid re-quoted at one MNQ tick, price
  scale 1,000). Data begins 2013-01-02, so the 14-session σ first exists in
  late January 2013 and no trade comes earlier (as in the 2018 check).
- **Coverage rule (reused from MES round 3 and the 2018 check, unchanged): a
  year is excluded** if it has fewer than 70% of the median full year's
  one-minute bars (342,192, round 1's report for 2019-2024) **or** fewer than
  95% of its regular-session minutes (09:30-16:00 New York, exchange trading
  days) quoted. An excluded year's trades are dropped and it is left out of
  the benchmark windows. Every year's coverage is reported, with the
  `atlas-research data coverage` report, before any backtest.
- **Information only, declared now:** MES round 3 found that Dukascopy's
  pre-2019 index CFDs are often quoted only 07:00-20:00 New York. That cuts
  the one-minute bar count without touching the regular session, which is
  the only part this strategy trades. If the declared rule excludes a year
  that the regular-session rule alone would keep, both versions are also run
  once with the regular-session rule only
  (`mnq_noise_area_r3_2013_2017_k05_rth_only_info`, `..._k10_rth_only_info`).
  Those runs are information and cannot change either verdict. If no year
  is excluded that way, they are not run.
- **Price scale:** the CFD mid at the last quote before 16:00 New York against
  the published Nasdaq-100 closes on 2013-12-31 (3,592.00), 2014-12-31
  (4,236.28), 2015-12-31 (4,593.27), 2016-12-30 (4,863.62) and 2017-12-29
  (6,396.42). More than 1% off stops the run as a scale error, which would be
  fixed in the loader and reported. If a date has no quote at all because of a
  data gap, it is replaced by the nearest earlier trading day with one, and
  this is reported. That is a data fact, not tuning.

### Costs (unchanged)

Base costs, 1.625 points a market-in, market-out round trip (0.75 pt
commission, the 1-tick spread charged ×1.5, 1 tick of slippage on every
market fill), and the review's Stress tier: fills at the mid and a flat
2.0 points a round trip, all in. 2× spread is reported as information.

### Pass rules: all eight must hold, for each version

As the 2018 check, plus (g) and (h):

| Rule | Threshold |
| --- | --- |
| (a) average R after base costs | > 0 |
| (b) average R minus exposure-matched buy-and-hold R | > 0 |
| (c) average R minus the random-entry p95 (50 runs) | > 0 |
| (d) trades | ≥ 150 |
| (e) average R at the 2.0 pt all-in Stress tier | > 0 |
| (f) average R minus the random-direction p95 (50 runs) | > 0 |
| **(g) positive years** | average R after base costs > 0 in at least 4 of the 5 years; if a year is excluded, at most one included year may be ≤ 0 (a year with no trades counts as ≤ 0) |
| **(h) profit factor after base costs** | ≥ 1.25 |

The rules ask mostly for the sign. Profit factor ≥ 1.25 is the T0 gate's
threshold, so (h) also asks for some size. The T0 gate table is reported as
information.

**Also reported for each version**: trades, average R at base and at 2.0 pt,
net points per trade and the median 1R in points, profit factor, every year
(at base and at 2.0 pt), and the share of the total R made by the 10 best
trades. The 2018 check's R was 91% from its top 10 trades, so this is
reported up front.

### Run discipline

Each declared check is run once. No rerun after seeing a result, no change
to the code paths the checks use, and every run, pass or fail, goes into
`research/experiments.jsonl` (`kind: frozen_check`). The holdout (July 2025
on) stays locked; the check period ends 2017-12-31.

### New code for this round (committed with this declaration, tested on synthetic data)

- `atlas_research/check.py`: two new pass rules, `max_losing_years` (g) and
  `min_profit_factor` (h); every check result now also has the share of R from
  the top 10 trades (`top10_share_of_r`) and a by-year table at the Stress tier.
  Earlier checks evaluate exactly as before, because a rule is applied only if
  declared.
- `atlas_research/configs/mnq.yaml`: the four checks above under `checks:`.
- `tests/research/test_mnq_round3.py`.

## Round 3 data quality (reported after the declaration commit `925a6b4`, before any 2013-2017 backtest)

The download (`data download --symbols USATECHIDXUSD --start 2013-01-01 --end
2017-12-31`) took three passes: the first ended with 12 day-files refused by
Dukascopy (HTTP 503), the second with 1, the third fetched the last one. All
3,652 day-files (1,826 days × bid and ask) are in; `data build` gave 1,176,943
one-minute bars. `atlas-research --config atlas_research/configs/mnq.yaml
data coverage` (its `data_checks:` now span 2013-2025):

| Year | M1 bars | % of median 2019-2024 (342,192) | Trading days | Days with RTH quotes | RTH minutes quoted | Quoted hours (New York) | Declared rule | RTH-only rule (information) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2013 | 194,528 | 56.8% | 248 | 180 | 67.9% | ~23 h, but 68 trading days with no regular-session quote | **excluded** (both) | excluded |
| 2014 | 316,579 | 92.5% | 248 | 246 | 96.8% | ~23 h | **included** | included |
| 2015 | 239,268 | 69.9% | 248 | 248 | 99.7% | 02:00-16:00 | **excluded** (M1 count) | included |
| 2016 | 215,422 | 63.0% | 250 | 250 | 99.98% | 02:00-16:00 | **excluded** (M1 count) | included |
| 2017 | 211,146 | 61.7% | 249 | 249 | 99.7% | 02:00-16:00 | **excluded** (M1 count) | included |

**The declared rule keeps one year, 2014.** 2013 fails both parts: 68 trading
days have no regular-session quote at all (February to May is mostly
missing). 2015, 2016 and 2017 fail only the bar count. From 2015 the CFD was
quoted only from 02:00 to 16:00 New York, so the overnight bars are missing,
but the regular session (the only part this strategy trades or reads, up to
the 16:00 close it uses as the next day's prior close) is 99.7-99.98% quoted.
2015 misses the 70% line by 0.1 point (69.9%).

That is exactly the case the declaration foresaw. So, as declared:
- **The verdict for each version is the declared check: 2014 only.** It can
  still meet (d), 150 trades, in one year. (g) is "at most one included year
  ≤ 0", which a single year can't fail by itself, so with one year included,
  (a) carries the sign.
- **The regular-session-only runs (2014-2017, four years) are made as
  information**, because the declared rule excludes 2015-2017, which the
  regular-session rule alone keeps. They can't change either verdict. They
  are the more useful evidence about the strategy, and the write-up says so,
  but the verdict stays the declared one.

**Price scale: confirmed.** The CFD mid at the last quote before 16:00 New
York against published Nasdaq-100 closes:

| Date | CFD mid | Published | Deviation |
| --- | --- | --- | --- |
| 2013-01-31 | 2,731.82 | 2,731.53 | +0.01% |
| 2013-12-31 | 3,591.18 | 3,592.00 | −0.02% |
| 2014-06-30 | 3,850.71 | 3,849.48 | +0.03% |
| 2014-09-30 | 4,052.09 | 4,049.45 | +0.07% |
| 2014-12-31 | 4,273.66 | 4,236.28 | +0.88% (stale, see below) |
| 2015-06-30 | 4,396.49 | 4,396.76 | −0.01% |
| 2015-09-30 | 4,174.37 | 4,181.06 | −0.16% |
| 2015-11-30 | 4,668.43 | 4,664.51 | +0.08% |
| 2015-12-31 | 4,633.13 | 4,593.27 | +0.87% (stale, see below) |
| 2016-01-29 | 4,274.10 | 4,279.17 | −0.12% |
| 2016-12-30 | 4,866.20 | 4,863.62 | +0.05% |
| 2017-01-31 | 5,117.10 | 5,116.77 | +0.01% |
| 2017-12-29 | 6,398.60 | 6,396.42 | +0.03% |

All five declared dates are within the 1% limit, so no run is stopped. But two
of them, 2014-12-31 and 2015-12-31, pass only because the limit is loose: on
both New Year's Eves Dukascopy's quotes stop at 13:00-14:00 New York (dealer
holiday hours), and the index fell about 1% in the afternoon on both days.
Those two checks compare a stale midday quote with the close, so they don't
test the scale. The eight month-end dates above, each a full session, were
added to the data report (`data_checks:` only; the checks' declared
`price_checks` are unchanged) and agree within 0.16%. The scale is right.
Published closes are from Nasdaq's index history, as listed by digrin.com.

The CFD's own median spread was 2.2 points in 2013-2014 and 1.0-1.1 points in
2015-2017. It isn't charged, because the mid is re-quoted at one MNQ tick.
