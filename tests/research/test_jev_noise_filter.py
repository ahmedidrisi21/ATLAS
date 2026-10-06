"""Jev filter on the noise-area strategy (docs/jev-mnq-noise-filter-test.md): synthetic data, stand-in Jev."""

import math
from pathlib import Path

import pandas as pd
import pytest
import yaml

from atlas_engine.adapters.jev import JevAdapter, LeakageError, ReplayTransport
from atlas_engine.adapters.jev.adapter import NOISE, check_state
from atlas_engine.adapters.jev.questions import NOISE_QUESTIONS, NOISE_STATE_FIELDS
from atlas_engine.adapters.jev.typesafe import TypeSafeTransport
from atlas_engine.market_data import synthetic
from atlas_research.jev_filter import RecordingTransport, run, run_check

CFG = Path(__file__).resolve().parents[2] / "atlas_research" / "configs" / "mnq.yaml"


def _cfg():
    c = yaml.safe_load(CFG.read_text())
    c["segments"] = {"dev": ["2019-01-01", "2020-07-01"], "validation": ["2020-07-01", "2020-10-01"], "holdout_start": "2020-10-01"}
    c["walk_forward"] = {"train_months": 6, "test_months": 3, "min_train_trades": 5}
    c["strategies"]["mnq_noise_area_r2_m5"]["grid"] = {"stop_k": [0.5, 1.0]}
    c["checks"]["mnq_noise_area_r2_2018"]["period"] = ["2018-09-01", "2019-01-01"]
    return c


def _load(seen):
    m1 = synthetic.random_walk_m1("EURUSD", "2018-09-01", "2020-10-01", seed=3) * 10000

    def load(sym, a, b):
        seen.append(b)
        return m1.loc[(m1.index >= a) & (m1.index < b)]
    return load


def _jev(requests, p=lambda state: 0.5):
    def answer(req):
        requests.append(req)
        return {"setup_id": req["setup_id"], "p_target_first": p(req["state"]), "regime": "trend_normal_vol",
                "reason_codes": [], "model_version": "jev-test"}
    return answer


def test_noise_requests_carry_only_the_declared_state_and_question():
    requests, seen = [], []
    rec = RecordingTransport(_jev(requests))
    res = run("mnq_noise_area_r2_m5", _cfg(), _load(seen), [JevAdapter(rec, "jev-test", question_set=NOISE)] * 2, controls=3)
    assert max(seen) <= pd.Timestamp("2020-10-01", tz="UTC")
    assert res["questions_version"] == "atlas-jev-noise-q1" and res["plain"]["trades"] > 0
    assert res["jev"]["trades"] == res["plain"]["trades"]  # constant answers skip nothing
    assert "stress_all_in_positive" in res["checks"] and "every_year_positive" in res["checks"]
    for req in requests:
        assert set(req["state"]) == set(NOISE_STATE_FIELDS) and req["questions"] is NOISE_QUESTIONS
        assert req["state"]["side"] in ("long", "short") and 1 <= req["state"]["checkpoint"] <= 12
        assert req["state"]["breakout_noise"] > 1  # a breakout is outside the noise area by construction
    # The recorded answers replay to the same result.
    replay = JevAdapter(ReplayTransport({r["request_hash"]: r["response"] for r in rec.records}), "jev-test", question_set=NOISE)
    again = run("mnq_noise_area_r2_m5", _cfg(), _load([]), replay, controls=3)
    assert again["jev"]["trades"] == res["jev"]["trades"] and not again["jev_skips"]


def test_low_scores_are_skipped_and_the_2018_check_runs_as_information():
    requests = []
    jev = JevAdapter(_jev(requests, lambda s: 1 / (1 + math.exp(-(s["ret_16_atr"] or 0.0)))), "jev-test", question_set=NOISE)
    res = run("mnq_noise_area_r2_m5", _cfg(), _load([]), jev, controls=3)
    assert res["jev"]["trades"] < res["plain"]["trades"]
    chk = run_check("mnq_noise_area_r2_2018", _cfg(), _load([]), jev, controls=3)
    assert chk["candidates"] > 0 and chk["kept"] <= chk["candidates"] and "passed" not in chk


def test_noise_state_refuses_anything_outside_its_fields():
    good = {k: 1.0 for k in NOISE_STATE_FIELDS} | {"setup": "noise_area", "side": "long"}
    assert check_state(good, NOISE.keys, NOISE.categorical)["side"] == "long"
    with pytest.raises(LeakageError):
        check_state(good | {"decision_time": 1.0}, NOISE.keys, NOISE.categorical)
    with pytest.raises(LeakageError):
        check_state(good | {"side": "2020-01-01"}, NOISE.keys, NOISE.categorical)


def test_typesafe_reads_the_noise_noul():
    resp = {"model": "jev-1.13.0", "answers": {"p_profit": {"type": "noul", "noul": 0.42},
                                               "regime": {"type": "choice", "choice": "range_low_vol", "confidence": 0.9}}}
    out = TypeSafeTransport("k").parse("c1", resp, "p_profit")
    assert out["p_target_first"] == 0.42 and out["regime"] == "range_low_vol"
    for q in NOISE_QUESTIONS.values():
        assert q["instructions"]["question"].endswith("?") and q["instructions"]["fields"] is NOISE_STATE_FIELDS
