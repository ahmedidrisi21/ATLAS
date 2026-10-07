# MNQ round 3: range fade (declared 2026-10-07, before any run)

Nothing here changes after the first run; a change is a new round with its
own name. yahye asked for this test (2026-10-07, Wealthsimple's futures
strategies page, "range trading"). Rounds 1 and 2 are in
`docs/mnq-candidates.md`; the noise-band failed its unseen-years check in
"MNQ round 3" of that file (a different, earlier use of "round 3").

## The idea and the reading

The page says: buy near support and sell near resistance in a sideways
market, with RSI below 30 at support as confirmation, and keep a stop
because "ranges eventually break". It does not say what a range is, so this
is the declared reading:

- **Range.** The prior regular session's (09:30-16:00 New York) high and
  low. W = high − low. Holidays and early-close days are skipped.
- **Entry.** On 5-minute bars closing between 10:00 and 15:00 New York: a
  close inside the bottom `edge` × W of the range with RSI(14) below 30
  goes long; a close inside the top `edge` × W with RSI above 70 goes short.
  First qualifying entry of the day only. A close outside the range never
  enters (that is a break, not a fade).
- **Stop.** 0.25 × W beyond the range edge, and at least 4 points from the
  entry (twice the 2.0 pt Stress cost). It sets 1R.
- **Exit.** Target at 1.5R (roughly the middle of the range); everything
  flat at 16:00 New York.
- **Grid (4 points, each a deflated-Sharpe trial, pooled with the RSI(2)
  setup's trials):** `edge` 0.1 or 0.2; `calm` off or on. Calm means the
  prior range was at or under the median of the 20 sessions before it, the
  "sideways market" the page asks for.

Same data, costs, walk-forward, segments and gates as `mnq.yaml` rounds 1
and 2: Dukascopy USATECHIDXUSD 2019-01-01 to 2025-06-30 re-quoted at one
MNQ tick, 0.75 pt commission + 1 tick slippage on market fills, 1.5 × spread,
the 2.0 pt Stress tier. The July 2025 holdout is not loaded.

## Pass marks

The strategy passes T0 only if **all 17 gates** of `mnq.yaml` pass, as for
every MNQ candidate: at least 300 out-of-sample trades; average R ≥ +0.10
and profit factor ≥ 1.25 on dev and validation; Monte Carlo drawdown,
daily-breach, deflated Sharpe, walk-forward efficiency, 2x spread, yearly
share, random-entry, neighbours, every year positive, and random-direction
gates as configured; positive at the 2.0 pt Stress tier.

If it passes, one independent check on unseen prices follows, as for the
noise-band: 2018 with the whole-dev parameters frozen before that run
(`mnq_range_fade_2018`, declared at that time), passing only on the six
rules used for the noise-band's 2018 check. The earlier years (2014-2017) are
used only if their coverage passes the same rule. If it fails T0, nothing
is run on other years and the result is reported as a fail.

## What it can't tell us

- The reading of "range" is mine. Another reading (a lookback channel, an
  opening range) is a different strategy and a different round.
- A fail here says this reading failed, not that range trading cannot work.
- Mean-reversion ideas have been weak in this project (the forex fix
  reversal; the Nasdaq RSI(2) dip-buy had 16 trades), so a fail is the more
  likely outcome. It is reported all the same, in R after costs.
