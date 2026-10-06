"""The human-run holdout exam (atlas_holdout): refuses agents, judges a frozen strategy once. Synthetic data only."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from atlas_engine.market_data import synthetic
from atlas_holdout.exam import RefusedError, main, refuse_agents, run_exam

ROOT = Path(__file__).resolve().parents[2]


def test_refuses_agent_sessions_and_non_interactive_runs():
    with pytest.raises(RefusedError):
        refuse_agents({"CLAUDECODE": "1"}, interactive=True)
    with pytest.raises(RefusedError):
        refuse_agents({"HERMES_HOME": "/x"}, interactive=True)
    with pytest.raises(RefusedError):
        refuse_agents({}, interactive=False)
    refuse_agents({}, interactive=True)


def test_main_refuses_in_this_session_before_reading_anything(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(SystemExit, match="refused"):
        main(["anything", "--config", "missing.yaml"])


def test_no_research_agent_or_engine_code_imports_the_exam():
    for d in ("atlas_research", "atlas_engine", "atlas_mcp", "atlas_api", "atlas_plugins"):
        for p in (ROOT / d).rglob("*.py"):
            assert "atlas_holdout" not in p.read_text(), p


def test_exam_judges_the_frozen_strategy_on_its_period_only():
    cfg = yaml.safe_load((ROOT / "atlas_research/configs/mnq.yaml").read_text())
    cfg["segments"]["holdout_start"] = "2020-07-01"
    cfg["holdout_exams"] = {"t": {"strategy": "mnq_noise_area_r2_m5", "params": {"stop_k": 1.0},
                                  "period": ["2020-07-01", "2020-10-01"], "reference_expectancy_r": 0.1,
                                  "random_direction_runs": 3,
                                  "pass_rules": {"min_trades": 10, "min_frac_of_reference": 0.5, "stress_positive": True,
                                                 "beat_random_direction_p95": True}}}
    m1 = synthetic.random_walk_m1("EURUSD", "2020-05-01", "2020-10-01", seed=3) * 10000
    seen = []

    def load(sym, a, b):
        seen.append((a, b))
        return m1.loc[(m1.index >= a) & (m1.index < b)]

    res = run_exam("t", cfg, load)
    assert seen and seen[0][1] == pd.Timestamp("2020-10-01", tz="UTC")
    assert res["summary"]["trades"] > 0 and set(res["checks"]) == {
        "trades", "expectancy_vs_reference", "expectancy_positive", "stress_positive", "beats_random_direction_p95"}
    assert all(pd.Timestamp(m + "-01") >= pd.Timestamp("2020-07-01") for m in res["by_month"])
    assert res["passed"] == all(c["pass"] for c in res["checks"].values())
