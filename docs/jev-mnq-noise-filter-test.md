# Jev filter test on the Nasdaq noise-area strategy (declared before running)

Written 2026-10-06, before any Jev answer for these trades was asked for or
seen. Nothing here may change after the run starts; a change is a new test
with its own name and is reported next to this one. It reuses the procedure
of `docs/jev-mes-filter-test.md` (the S&P test, never run), adapted to this
strategy where the two differ.

## The question

MNQ round 2 (`docs/mnq-candidates.md`) left the noise-area breakout as the
most consistent strategy ATLAS has tested: +0.150 R a trade after costs over
1,164 out-of-sample trades (2020 to mid-2025), but only +0.009 R on
validation (2024 to mid-2025), so it failed 5 of 17 gates. The operator chose
(2026-10-06, 15:18Z) to ask: **if ATLAS skips the trades Jev rates least
likely to work, does what is left do better than taking every trade, and
better than skipping the same number of trades at random?**

## What is fixed

- **Trades.** `mnq_noise_area_r2_m5` exactly as T0 traded it out of sample:
  the walk-forward picks among its 4 stop grid points (`stop_k` 0.5, 1.0,
  1.5, 2.0) on dev, then the final whole-dev pick (`stop_k` 0.5) on
  validation; band-only trail, half-hour checkpoints, flat at 16:00 New
  York. Config `atlas_research/configs/mnq.yaml`, dev 2019-01-01 to
  2024-01-01, validation 2024-01-01 to 2025-07-01, data Dukascopy
  USATECHIDXUSD re-quoted at one MNQ tick. Same costs as round 2 (1.5 ×
  spread, 1 tick slippage on market fills, $1.50 round turn = 0.75 pt), and
  the 2.0 pt all-in Stress tier reported alongside. The July 2025 holdout is
  not loaded; the guarded loader refuses it.
- **Why a new question.** Jev's existing questions (`atlas-jev-q2`) ask
  whether a forex-style trade with a fixed target reaches its target before
  its stop, described with 15-minute and hourly fields. This trade has no
  target and closes when price falls back inside its band or at the 16:00
  close. So it gets its own research-only question set,
  `atlas-jev-noise-q1` (`atlas_engine/adapters/jev/questions.py`), never
  used live:
  - one Noul: "Will the trade close with a profit after its trading cost?"
    (its probability is the score), plus the same regime Choice as q2
    (reported, not used);
  - the state, all known at the decision checkpoint: side (long/short); the
    checkpoint's number in the session (1 = 10:00, 12 = 15:30, no clock
    time); entry number that day; how far past the band's base price the
    close is, the overnight gap and the stop distance, all in the strategy's
    own noise units; one noise unit as a percentage of price; stop in
    5-minute ATR; 5-minute ATR percentile; hourly ADX, trend alignment,
    slope and distance from the slow hourly EMA; room to the prior day's
    high/low; the last 16 bars' return; cost in R; and the mean R of the
    last 20 closed signals. No dates, clock times, prices, symbols or news
    text; the adapter's leakage guard refuses a request with any other field.
- **Procedure** (`atlas_research/jev_filter.py`):
  1. Rebuild T0's out-of-sample windows and the stop each traded.
  2. Ask Jev about every candidate signal of those stop settings from
     2019-01-01; the 2019 answers only set the first cut-off.
  3. **The rule:** skip a trade when Jev's probability is below the lowest
     third (33rd percentile) of Jev's answers on the same stop setting's
     earlier candidates. Until 30 earlier answers exist, nothing is skipped.
     A failed Jev call (timeout, error) skips the trade. The cut-off uses
     only earlier answers, so nothing from the future leaks in.
  4. Re-simulate what is left window by window, and compare with every
     candidate re-simulated the same way ("plain").
  5. Control: 200 runs that skip the same number of trades at random in
     each window.
- **Jev.** Model `jev-1.13.0` (pinned), questions `atlas-jev-noise-q1`.
  One request per candidate, 5-second research timeout, up to 4 requests
  in parallel. Every answer is saved (`jev_answers.jsonl`) so the run
  replays without calling TypeSafe.

## Pass mark

Jev's version of the strategy passes only if **all** hold:

1. at least 300 out-of-sample trades (PRD §17);
2. its average R after costs beats plain with bootstrap probability at
   least 0.95;
3. its average R beats the 95th percentile of the random-skip control;
4. average R after costs at least +0.10 and profit factor at least 1.25, on
   dev and on validation separately (the T0 gates);
5. every out-of-sample calendar year's average R after costs above zero
   (the MNQ config's extra gate);
6. average R at the 2.0 pt all-in Stress tier above zero.

Reported as information, never as a pass mark: how Jev's score lines up
with results (rank correlation, mean R of kept vs skipped, mean R by score
third), Jev's skip count and TypeSafe usage, and the same rule applied to
the frozen 2018 check's trades (`stop_k` 0.5, 2018-01-01 to 2019-01-01),
compared with plain and random skips there.

## What can and can't come out of it

- Plain made only +0.009 R on validation. To pass gate 4, Jev's skips must
  lift validation past +0.10 R, so a pass needs Jev to find a lot that the
  rules miss.
- **Jev may know this history.** It was trained on data that may include
  2018 to 2025 markets. Even with no dates in the state, a good result here
  could be memory rather than skill. Any pass counts only once confirmed on
  trades Jev can't have seen: the holdout (operator's call) or forward on
  the demo account.
- **The strategy itself has not passed T0.** A Jev pass would make a
  filtered strategy worth a forward test, not one worth trading.
- Every arm's result is reported in R after costs, including a loss or no
  difference.

# Results (run 2026-10-06, `mnq_noise_area_r2_m5+jev-filter-20261006-160302`)

Run exactly as declared above; nothing was changed after the first answer.
Jev answered all 4,253 requests (1,164 traded candidates plus the other stop
settings' candidates used for the cut-offs, and the 2018 check), with no
timeouts or errors. Model `jev-1.13.0`, questions `atlas-jev-noise-q1`.
TypeSafe usage: 8.0 M input and 0.39 M output tokens.

**Verdict: FAIL, 3 of 9 marks passed.** Skipping the trades Jev rated lowest
did no better than taking every trade, and worse than skipping the same
number of trades at random.

| After MNQ costs | Trades | Avg R | Profit factor | Win rate | Avg R at 2.0 pt Stress |
|---|---|---|---|---|---|
| Plain (every trade, = T0's 1,164) | 1,164 | +0.150 | 1.30 | 36.7% | +0.140 |
| Jev skips lowest third | 809 | +0.153 | 1.30 | 36.0% | +0.143 |
| Random skips, same count (200 runs) | 809 | mean +0.163, p95 +0.213 | | | |

| Pass mark | Jev arm | Result |
|---|---|---|
| 1. At least 300 trades | 809 | pass |
| 2. Beats plain, bootstrap ≥ 0.95 | 0.51 | **fail** |
| 3. Beats random-skip p95 (+0.213) | +0.153 | **fail** |
| 4a. Dev avg R ≥ +0.10 | +0.192 (plain +0.198) | pass |
| 4b. Validation avg R ≥ +0.10 | +0.034 (plain +0.009) | **fail** |
| 4c. Dev profit factor ≥ 1.25 | 1.40 (plain 1.43) | pass |
| 4d. Validation profit factor ≥ 1.25 | 1.06 (plain 1.02) | **fail** |
| 5. Every year above zero | 2025 H1 −0.064 | **fail** |
| 6. Stress tier above zero | +0.143 | pass |

By year, avg R (trades): plain 2020 +0.089 (204), 2021 +0.146 (211), 2022
+0.266 (248), 2023 +0.276 (206), 2024 −0.073 (198), 2025 H1 +0.176 (97).
Jev 2020 +0.035 (130), 2021 +0.207 (154), 2022 +0.156 (176), 2023 +0.357
(150), 2024 +0.085 (131), 2025 H1 −0.064 (68). Jev helped in 2024 and hurt
in 2025; over the whole span the two cancel.

**Does Jev's number track results at all?** No, if anything slightly
backwards. Rank correlation between Jev's probability and the trade's R is
−0.10 over the 1,164 trades. By Jev-score third, lowest first: +0.170,
+0.068, +0.211 R. Kept trades averaged +0.153 R, skipped ones +0.142 R.
Jev's average probability was 0.46 against a real profit rate of 0.37.

**2018 check (information only).** 200 trades, Jev kept 145. Plain +0.533 R
(PF 1.88), Jev +0.493 R (PF 1.80), random skips mean +0.533, p95 +0.725.
At the Stress tier plain +0.510, Jev +0.469. Rank correlation −0.05; the
trades Jev skipped averaged +0.637 R, better than the ones it kept.

## What this says

- Jev, asked this way, can't tell the better noise-band trades from the
  worse ones. On both the 2020-2025 trades and 2018, its skips land no better
  than a coin flip, and a little worse than random thinning.
- The validation lift (+0.009 to +0.034 R) is within what random skips give
  and far short of +0.10.
- The possible memory caveat cuts the other way here: if Jev had seen this
  history, that did not help it.
- The noise-band strategy itself still hasn't passed T0, with or without Jev.
- This is one question wording and one state. Another wording would be a new,
  separately declared test; nothing here suggests which one would work.

Files: the run's `result.json` and every Jev answer (`jev_answers.jsonl`, for
replay with `--replay`) are under `research/runs/` (not committed) and copied
to the project folder `jev-mnq-noise/`.
