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
