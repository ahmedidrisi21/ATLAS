"""Every word ATLAS sends Jev, and every threshold it applies to Jev's answers, in one place.

TypeSafe's agent skill (github.com/typesafe-ai/skills, skills/typesafe-ai/SKILL.md, read 2026-10-06) asks
for exactly this: questions and thresholds in a single file so a person can review them. The request
format is TypeSafe's HTTP API (docs.typesafe.ai/api, read 2026-10-06): ``POST /v1/systemone`` with
``state``, ``model`` and a map of typed ``questions``; a Noul answers ``{"type": "noul", "noul": p}``,
a Choice ``{"type": "choice", "choice": ..., "probabilities": {...}, "confidence": c}``.

Rules from the skill and the docs that shaped the wording:

- Question ids are for code only and never reach the model, so each question carries its full meaning.
- The judgment goes in ``instructions``; the meaning of each answer in ``criteria``. State fields are
  referenced by backticked name.
- A Noul is a probability of yes, with no separate confidence: ``p_target_first`` is its ``noul`` value.
- Choice confidence describes how concentrated the answer is, not whether it is right, so it only adds a
  reason code here; it never decides a trade.
- Thresholds must be validated on our own data (T2 calibration), not taken from examples.

Any change to this file is a new question version (``QUESTIONS_VERSION``), and so a new strategy version
for every strategy scored by Jev (PRD v3 §24).
"""

from __future__ import annotations

from atlas_engine.decisions.state import REGIMES

QUESTIONS_VERSION = "atlas-jev-q2"

# What each state field means. Distances are signed so that positive is in the trade's favour.
STATE_FIELDS = {
    "setup": "name of the rule-based setup that proposed the trade",
    "stop_atr": "distance from entry to stop, in ATR(M15)",
    "spread_r": "bid-ask spread as a fraction of the stop distance",
    "dist_ema_atr": "distance of price from the fast M15 EMA, in ATR",
    "h1_fast_dist": "distance of price from the fast H1 EMA, in H1 ATR",
    "h1_slow_dist": "distance of price from the slow H1 EMA, in H1 ATR",
    "h1_slope_atr": "slope of the H1 trend line, in H1 ATR per bar",
    "h1_adx": "H1 ADX trend strength, 0 to 100",
    "htf_aligned": "1 when the H1 trend points the trade's way, -1 against it, 0 when flat",
    "atr_pct": "percentile of the current ATR among recent values, 0 to 1",
    "atr_ratio": "M15 ATR over H1 ATR",
    "room_prior_day_atr": "room left to the prior day's high (long) or low (short), in ATR",
    "behind_prior_day_atr": "distance back to the prior day's low (long) or high (short), in ATR",
    "ret_1_atr": "return over the last M15 bar, in ATR",
    "ret_4_atr": "return over the last 4 M15 bars, in ATR",
    "ret_16_atr": "return over the last 16 M15 bars, in ATR",
    "session": "trading session at decision time",
    "regime": "ATLAS's own rule-based regime label",
    "target_r": "distance from entry to target, in R (1R is the distance from entry to stop)",
    "cost_r": "round-trip trading cost, in R",
    "recent_signal_r20": "mean result, in R, of the last 20 closed signals of this setup, traded or not (empty when unknown)",
}


def _regime_text(label: str) -> str:
    trend, vol, _ = label.split("_")
    kind = {"trend": "a directional, trending market", "range": "a sideways, ranging market"}[trend]
    return f"{kind} with {vol} volatility"


QUESTIONS = {
    "p_target_first": {
        "type": "noul",
        "instructions": {
            "question": "A trade has just been opened at market with a fixed stop and a fixed target, as described "
                        "by the state. Will price reach the target before it reaches the stop?",
            "fields": STATE_FIELDS,
        },
        "criteria": {
            "true": "Price reaches the target (`target_r` R in the trade's favour) before the stop",
            "false": "Price reaches the stop (1R against the trade) before the target",
        },
    },
    "regime": {
        "type": "choice",
        "instructions": {
            "question": "Which description best fits the market the state describes at the moment of the trade?",
            "fields": STATE_FIELDS,
        },
        "criteria": {r: _regime_text(r) for r in REGIMES},
    },
}

# A regime answer whose Choice confidence is below this adds the reason code "regime_uncertain". Informational
# only: it never blocks a trade. To be re-checked against T2's calibration data.
REGIME_UNCERTAIN = 0.5
