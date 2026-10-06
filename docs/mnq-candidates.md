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
