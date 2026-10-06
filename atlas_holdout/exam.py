"""The holdout exam: one frozen strategy, run once on the locked holdout, by the operator (PRD §22, T6).

Research code refuses the holdout (``atlas_research.data``) and so does every
agent tool. This is the one place that reads it, and it is meant to be run by
a person at a terminal, never by an agent:

- it refuses when it sees an agent session (Claude Code, Hermes) in the
  environment, when stdin is not an interactive terminal, and unless the
  operator types the exam's name back;
- the exam (strategy, frozen parameters, period, pass marks) is declared in
  the config under ``holdout_exams:`` and committed before anyone runs it;
- each exam runs once: the result is written to ``research/holdout_exams/<name>.json``
  and a second run on the same machine is refused;
- holdout prices are kept apart from research data, under ``data/holdout/``,
  so the research loaders never find them.

    python -m atlas_holdout --config atlas_research/configs/mnq.yaml <exam name>

The exam prints a short result block for the operator to paste back.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import yaml

AGENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT", "HERMES_HOME", "HERMES_PROFILE")
HOLDOUT_ROOT = Path("data/holdout")
RESULTS_DIR = Path("research/holdout_exams")


class RefusedError(PermissionError):
    pass


def refuse_agents(env: dict | None = None, interactive: bool | None = None) -> None:
    env = os.environ if env is None else env
    seen = [k for k in AGENT_ENV if env.get(k)]
    if seen:
        raise RefusedError(f"the holdout exam is run by the operator, not by an agent (found {', '.join(seen)})")
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise RefusedError("the holdout exam needs an interactive terminal")


def run_exam(name: str, cfg: dict, load_m1: Callable[[str, pd.Timestamp, pd.Timestamp], pd.DataFrame], seed: int = 0) -> dict:
    """Trades of the frozen strategy over the declared holdout period, judged against the declared pass marks."""
    from atlas_engine.setups import EdgeFilters
    from atlas_research import metrics
    from atlas_research.backtest import ExitPolicy
    from atlas_research.research_setups import ALL_SETUPS
    from atlas_research.t0 import Runner, _ts, in_window, points_summary, prepare_market, random_direction

    exam = cfg["holdout_exams"][name]
    scfg = cfg["strategies"][exam["strategy"]]
    start, end = _ts(exam["period"][0]), _ts(exam["period"][1])
    if start < _ts(cfg["segments"]["holdout_start"]):
        raise ValueError("an exam period starts at or after the holdout start")
    setup = ALL_SETUPS[scfg.get("setup", exam["strategy"])]
    params = {**setup.defaults, **exam["params"]}
    warm = start - pd.Timedelta(days=int(exam.get("warmup_days", 40)))
    markets = [prepare_market(s, load_m1(s, warm, end), cfg, scfg.get("bar", "15min")) for s in scfg["symbols"]]
    ex = {**cfg["exits"], **scfg.get("exits", {})}
    exits = ExitPolicy(rr=ex["rr"], friday_flatten_utc=ex["friday_flatten_utc"],
                       atr_trail_mult=ex.get("atr_trail_mult"), atr_trail_after_r=ex.get("atr_trail_after_r", 1.5),
                       reenter_at_exit_bar=bool(ex.get("reenter_at_exit_bar", False)))
    runner = Runner(setup, markets, exits, EdgeFilters(**{**cfg["filters"], **scfg.get("filters", {})}))
    mult = cfg["costs"]["spread_mult"]
    w = (start, end)
    trades = in_window(runner.trades(params, mult), w)
    has_all_in = all(m.all_in is not None for m in markets)
    stress = in_window(runner.trades(params, mult, all_in=True), w) if has_all_in else None
    s = metrics.summary(trades)
    rd = random_direction(runner, trades, int(exam.get("random_direction_runs", 50)), seed) if len(trades) else np.array([])
    rules = exam["pass_rules"]
    ref = float(exam["reference_expectancy_r"])
    checks = {"trades": (len(trades), ">=", rules["min_trades"], len(trades) >= rules["min_trades"]),
              "expectancy_vs_reference": (s["expectancy_r"], ">=", rules["min_frac_of_reference"] * ref,
                                          s["expectancy_r"] >= rules["min_frac_of_reference"] * ref),
              "expectancy_positive": (s["expectancy_r"], ">", 0.0, s["expectancy_r"] > 0)}
    if stress is not None and rules.get("stress_positive", True):
        se = metrics.summary(stress)["expectancy_r"]
        checks["stress_positive"] = (se, ">", 0.0, se > 0)
    if rules.get("beat_random_direction_p95") and len(rd):
        p95 = float(np.percentile(rd, 95))
        checks["beats_random_direction_p95"] = (s["expectancy_r"], ">", p95, s["expectancy_r"] > p95)
    y = pd.DatetimeIndex(trades["entry_time"]).strftime("%Y-%m") if len(trades) else []
    return {
        "exam": name, "strategy": exam["strategy"], "params": params, "period": [str(start), str(end)],
        "data": {"first_bar": str(markets[0].m1.index.min()) if len(markets[0].m1) else None,
                 "last_bar": str(markets[0].m1.index.max()) if len(markets[0].m1) else None},
        "summary": s, "points": points_summary(trades),
        "stress_all_in": metrics.summary(stress) if stress is not None else None,
        "by_month": {str(k): {"trades": int(len(g)), "r": float(g["r"].sum())} for k, g in trades.groupby(y)} if len(trades) else {},
        "checks": {k: {"value": v[0], "op": v[1], "threshold": v[2], "pass": bool(v[3])} for k, v in checks.items()},
        "passed": all(v[3] for v in checks.values()),
    }


def _download(cfg: dict, exam: dict, scfg: dict, root: Path) -> Callable:
    from atlas_engine.market_data import bars, dukascopy, store
    from atlas_research.data import quoted_at_spread
    from atlas_research.t0 import _ts

    start, end = _ts(exam["period"][0]), _ts(exam["period"][1])
    warm = (start - pd.Timedelta(days=int(exam.get("warmup_days", 40)))).date()
    last = (end - pd.Timedelta(days=1)).date()
    raw = root / "raw"
    for sym in scfg["symbols"]:
        src = (cfg["data"].get("proxies") or {}).get(sym, {}).get("source", sym)
        for attempt in range(3):
            try:
                dukascopy.download_range(src, warm, last, raw, workers=2)
                break
            except RuntimeError as e:
                if attempt == 2:
                    raise
                print(f"  some days failed, retrying ({e})")
        store.save_m1(root, src, bars.build_m1_from_dukascopy(src, warm, last, raw))

    def load(sym, a, b):
        proxy = (cfg["data"].get("proxies") or {}).get(sym)
        src = proxy["source"] if proxy else sym
        m1 = store.load_m1(root, src, a, b)
        return quoted_at_spread(m1, float(proxy["spread"])) if proxy else m1
    return load


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m atlas_holdout", description="Run one declared holdout exam, once.")
    ap.add_argument("exam")
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    try:
        refuse_agents()
    except RefusedError as e:
        raise SystemExit(f"refused: {e}")
    cfg = yaml.safe_load(Path(args.config).read_text())
    if args.exam not in (cfg.get("holdout_exams") or {}):
        raise SystemExit(f"no exam named {args.exam!r} under holdout_exams: in {args.config}")
    out = RESULTS_DIR / f"{args.exam}.json"
    if out.exists():
        raise SystemExit(f"refused: {args.exam} has already been run ({out}); the holdout is used once per candidate")
    exam = cfg["holdout_exams"][args.exam]
    print(f"Holdout exam {args.exam}: {exam['strategy']} {exam['params']} over {exam['period'][0]} .. {exam['period'][1]}")
    print("This uses the locked holdout once. It cannot be repeated for this strategy.")
    if input(f"Type the exam name ({args.exam}) to go ahead: ").strip() != args.exam:
        raise SystemExit("stopped; nothing was loaded")
    print("Downloading holdout prices (slow: the price server rate-limits) ...")
    load = _download(cfg, exam, cfg["strategies"][exam["strategy"]], HOLDOUT_ROOT)
    res = run_exam(args.exam, cfg, load)
    res["run_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2, default=str))
    s = res["summary"]
    print("\n===== paste everything below back to ATLAS =====")
    print(f"exam {res['exam']}  {'PASS' if res['passed'] else 'FAIL'}")
    print(f"trades {s['trades']}  avg R {s['expectancy_r']:+.3f}  PF {s['profit_factor']:.2f}  win {s['win_rate']:.1%}")
    if res["stress_all_in"]:
        print(f"stress avg R {res['stress_all_in']['expectancy_r']:+.3f}")
    print(f"points/trade {res['points'].get('net_points_per_trade')}  data {res['data']['first_bar']} .. {res['data']['last_bar']}")
    for k, c in res["checks"].items():
        print(f"  {k}: {c['value']:.4g} {c['op']} {c['threshold']:.4g}  {'pass' if c['pass'] else 'FAIL'}")
    for m, v in res["by_month"].items():
        print(f"  {m}: {v['trades']} trades, {v['r']:+.2f} R")
    print("================================================")
    print(f"full result: {out}")


if __name__ == "__main__":
    main()
