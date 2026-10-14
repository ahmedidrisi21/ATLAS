# Jev filter test on the MES long-only channel breakout (declared before running)

Written 2026-10-06, before any Jev answer for these trades was seen. Nothing
here may change after the run starts; a change is a new test with its own
name and is reported next to this one.

## The question

Round 2 of the MES search (`docs/mes-candidates.md`) found one strategy
worth a second look: the long-only 4-hour channel breakout. It averaged
+0.145 R a trade after costs over 231 out-of-sample trades and still failed
7 of 15 checks. This test asks one thing: if ATLAS skips the trades Jev
rates as unlikely to reach their target, does what is left do better than
taking every trade, by enough to trust?

## What is fixed

- **Trades.** `mes_channel_breakout_long_h4` exactly as T0 traded it out of
  sample (walk-forward picks from its 4 grid points on dev, the final 20-bar
  channel and 1.5 × ATR stop on validation; 2R target, Friday 20:00 UTC flatten),
  config `atlas_research/configs/mes.yaml`, dev 2019-01-01 to 2024-01-01 and
  validation 2024-01-01 to 2025-07-01. Same costs as round 2 (1.5 × spread,
  1-tick slippage, $1.50 round turn). The July 2025 holdout is not loaded.
- **Why not the T2 harness.** `atlas_research/selection` can't score this
  strategy. Its walk-forward training windows need 150 closed candidates and
  this strategy has about 40 trades a year (231 over 5.5 years), so every fold would be skipped. Its EV
  gate also assumes every trade ends at the stop or the 2R target, but only
  about 18% of these trades reach the target; most close at the Friday
  flatten. Fed a calibrated 18%, the gate would refuse nearly every trade
  for any model.
- **Procedure** (`atlas_research/jev_filter.py`):
  1. Rebuild T0's out-of-sample windows and the parameters each traded (the
     walk-forward picks on dev, the final parameters on validation).
  2. Ask Jev for every candidate signal of those parameter sets from
     2019-01-01; the 2019 answers only set the first cut-off.
  3. **The rule:** skip a trade when Jev's `p_target_first` is below the
     lowest third (33rd percentile) of Jev's answers on the same parameter
     set's earlier candidates. Until 30 earlier answers exist, nothing is
     skipped. A failed Jev call (timeout, error) skips the trade. The cut-off
     is relative because Jev's raw numbers are not calibrated to this market,
     and it only uses earlier answers, so nothing from the future leaks in.
  4. Re-simulate what's left with the one-position rule, window by window,
     and compare with every candidate re-simulated the same way ("plain").
  5. Control: 200 runs that skip the same share of trades at random in each
     window. Beating plain only counts if it also beats random thinning.
- **Jev.** Model `jev-1.13.0` (pinned), questions `atlas-jev-q2`
  (`atlas_engine/adapters/jev/questions.py`), unchanged. One request per
  candidate. The state is the whitelisted one from
  `atlas_engine/decisions/state.py`: distances and slopes in ATR, fixed
  labels, the setup name, the target and the cost in R. No dates, times,
  prices, symbols or news text; the adapter's leakage guard refuses a
  request that has them. A timeout or error is a skip, and a skipped trade
  is not taken.
- **Pass mark.** Jev's version of the strategy passes only if all hold:
  - at least 300 out-of-sample trades (PRD §17);
  - its average R after costs beats plain with bootstrap probability at
    least 0.95 (the T2 harness's own test);
  - its average beats the 95th percentile of the random-skip control;
  - average R after costs at least +0.10 and profit factor at least 1.25,
    on dev and on validation separately (the T0 gates).
- Research calls allow Jev 5 seconds instead of the live 500 ms, so a slow
  answer doesn't count as a skip here. Every answer is saved
  (`jev_answers.jsonl`) so the run can be replayed without calling TypeSafe.

## What can and can't come out of it

- **It cannot pass.** The plain strategy has 231 out-of-sample trades and
  Jev can only remove trades, so Jev's arm will have fewer than 231, short
  of the 300 minimum. The run says whether Jev looks useful enough to test
  on more trades (more years or the demo account), not whether to trade it.
- **Jev may know this history.** Jev was trained on data that may include
  2019 to 2025 markets. Even with no dates in the state, a good result here
  could be memory rather than skill, so it counts only once confirmed on
  trades Jev hasn't seen: the demo account, forward.
- Every arm's result is reported in R after costs, including a loss or no
  difference, along with the number of skipped requests and the TypeSafe
  usage.
