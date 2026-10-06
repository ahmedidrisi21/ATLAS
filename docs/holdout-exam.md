# The holdout exam (human-run)

ATLAS keeps the newest prices (July 2025 onward) locked away as a final exam
(PRD §22, Phase T6). Every research tool and every agent is refused those
prices. The exam is the one exception, and only the operator runs it:
`atlas_holdout/exam.py`.

## How it works

1. **The exam is declared first.** Under `holdout_exams:` in the strategy's
   config: which strategy, its frozen settings, the exam period, the
   reference result it is compared with, and the pass marks. That is
   committed before anyone runs it, so the pass mark can't move afterwards.
2. **The operator runs it once**, on their own computer, at a terminal. It
   refuses to run inside an agent session (Claude Code, Hermes), without an
   interactive terminal, or unless the exam's name is typed back.
3. **It downloads the exam prices** into `data/holdout/`, away from the
   research data, runs the frozen strategy over the exam period with the
   same costs as research, and prints a short block to paste back.
4. **It runs once.** The result is saved to `research/holdout_exams/<name>.json`
   and a second run of the same exam is refused.

The PRD's T6 pass mark is at least half the average R per trade the strategy
made on validation; each exam also states its own minimum trade count and
asks for a positive result at the Stress cost tier.

## Running it (operator)

On a computer with Python 3.11 or newer and git:

```bash
git clone https://github.com/yahyeameer/ATLAS.git && cd ATLAS
git checkout <branch named in the exam's PR>
python -m pip install -e .
python -m atlas_holdout --config atlas_research/configs/mnq.yaml <exam name>
```

The download is slow (the price server rate-limits; expect 10 to 30
minutes). Paste everything between the two `=====` lines back to ATLAS.
