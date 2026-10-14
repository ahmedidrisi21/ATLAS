"""``atlas-research``: data pipeline, T0 edge discovery and T1 exit research.

    atlas-research data download --symbols EURUSD GBPUSD
    atlas-research data build    --symbols EURUSD GBPUSD
    atlas-research data quality  --symbols EURUSD GBPUSD
    atlas-research data import-mt5 --symbol EURUSD --file EURUSD_M1.csv
    atlas-research t0 run --all
    atlas-research t0 check <name>
    atlas-research t0 info <name>
    atlas-research data coverage
    atlas-research t1 run trend_pullback
    atlas-research t2 run trend_pullback
    atlas-research prop-mc --trades research/runs/<run>/oos_trades.csv
    atlas-research prop-mc --reference breakeven
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import yaml

from atlas_engine.market_data import bars, dukascopy, quality, store, synthetic

from . import data as rdata
from .registry import Registry

DEFAULT_CONFIG = Path(__file__).parent / "configs" / "t0.yaml"
DEFAULT_T2_CONFIG = Path(__file__).parent / "configs" / "t2.yaml"
DEFAULT_REGISTRY = Path("research/experiments.jsonl")
SYNTHETIC_MARKER = "SYNTHETIC"


def load_config(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def _dates(cfg: dict, start: str | None, end: str | None) -> tuple[dt.date, dt.date]:
    holdout = pd.Timestamp(cfg["segments"]["holdout_start"])
    s = pd.Timestamp(start or cfg["segments"]["dev"][0]).date()
    e = pd.Timestamp(end).date() if end else (holdout - pd.Timedelta(days=1)).date()
    rdata.check_not_holdout(pd.Timestamp(e) + pd.Timedelta(days=1), holdout)
    return s, e


def cmd_download(args, cfg) -> None:
    s, e = _dates(cfg, args.start, args.end)
    for sym in args.symbols:
        n = dukascopy.download_range(sym, s, e, Path(cfg["data"]["raw_cache"]), workers=args.workers)
        print(f"{sym}: fetched {n} day-files for {s}..{e}")


def cmd_build(args, cfg) -> None:
    s, e = _dates(cfg, args.start, args.end)
    root = Path(args.data_root or cfg["data"]["root"])
    for sym in args.symbols:
        m1 = bars.build_m1_from_dukascopy(sym, s, e, Path(cfg["data"]["raw_cache"]))
        paths = store.save_m1(root, sym, m1)
        print(f"{sym}: {len(m1):,} M1 bars -> {len(paths)} files under {root}")


def cmd_quality(args, cfg) -> None:
    root = Path(args.data_root or cfg["data"]["root"])
    s, e = _dates(cfg, args.start, args.end)
    for sym in args.symbols:
        m1 = rdata.load_m1(root, sym, s, pd.Timestamp(e) + pd.Timedelta(days=1), cfg["segments"]["holdout_start"])
        print(f"\n{sym}")
        print(quality.quality_report(m1, sym).to_string())


def cmd_coverage(args, cfg) -> None:
    """The declared data-quality report (config ``data_checks:``): coverage per year and price checks. No trades, no R."""
    from .check import price_check, rth_coverage

    root = Path(args.data_root or cfg["data"]["root"])
    dc = cfg["data_checks"]
    y0, y1 = dc["years"]
    end = min(pd.Timestamp(f"{y1 + 1}-01-01"), pd.Timestamp(cfg["segments"]["holdout_start"]))
    m1 = rdata.load_m1(root, dc["symbol"], f"{y0}-01-01", end, cfg["segments"]["holdout_start"])
    cov = rth_coverage(m1, (y0, y1), tuple(dc["reference_years"]), dc["min_frac"], dc["min_rth_frac"])
    print(f"{dc['symbol']}: {len(m1):,} M1 bars, {m1.index.min()} .. {m1.index.max()}")
    print(cov.to_string())
    spread = (m1["ask_c"] - m1["bid_c"])
    print("\nCFD median spread by year (points):")
    print(spread.groupby(m1.index.year).median().to_string())
    for c in price_check(m1, dc["price_checks"]):
        print(f"  {c['date']}: CFD {c['cfd_mid']:.2f} vs published {c['published']:.2f} ({c['deviation']:+.2%}) {'ok' if c['ok'] else 'SCALE ERROR'}")


def cmd_import_mt5(args, cfg) -> None:
    from atlas_engine.market_data.mt5_csv import read_mt5_bars

    root = Path(args.data_root or cfg["data"]["root"])
    m1 = read_mt5_bars(Path(args.file), args.symbol, ny_offset_hours=args.ny_offset_hours)
    m1 = m1.loc[m1.index < pd.Timestamp(cfg["segments"]["holdout_start"], tz="UTC")]
    paths = store.save_m1(root, args.symbol, m1)
    print(f"{args.symbol}: {len(m1):,} MT5 bars (holdout rows dropped) -> {len(paths)} files under {root}")


def cmd_synthetic(args, cfg) -> None:
    root = Path(args.data_root)
    s, e = _dates(cfg, args.start, args.end)
    root.mkdir(parents=True, exist_ok=True)
    (root / SYNTHETIC_MARKER).write_text("Synthetic random-walk data. Not market data.\n")
    for i, sym in enumerate(args.symbols):
        m1 = synthetic.random_walk_m1(sym, str(s), str(pd.Timestamp(e) + pd.Timedelta(days=1)), seed=args.seed + i)
        store.save_m1(root, sym, m1)
        print(f"{sym}: {len(m1):,} synthetic M1 bars -> {root}")


def cmd_t0(args, cfg) -> None:
    from .t0 import run_t0

    root = Path(args.data_root or cfg["data"]["root"])
    registry_path = Path(args.registry)
    if (root / SYNTHETIC_MARKER).exists() and registry_path.resolve() == DEFAULT_REGISTRY.resolve():
        sys.exit("refusing to record synthetic-data runs in the real experiment registry; pass --registry")
    load = lambda sym, start, end: rdata.load_research_m1(root, cfg, sym, start, end)  # noqa: E731
    names = list(cfg["strategies"]) if args.all else [args.strategy]
    for name in names:
        res = run_t0(name, cfg, load, Registry(registry_path), Path(args.out))
        failed = [f"{g['gate']} ({g['scope']})" for g in res["gates"] if not g["passed"]]
        print(f"\n{res['experiment_id']}: {'PASS' if res['passed'] else 'FAIL'}")
        print(json.dumps(res["kanban_metadata"], indent=2, default=str))
        if failed:
            print("failed gates:\n  " + "\n  ".join(failed))


def cmd_check(args, cfg) -> None:
    from .check import run_check

    root = Path(args.data_root or cfg["data"]["root"])
    registry_path = Path(args.registry)
    if (root / SYNTHETIC_MARKER).exists() and registry_path.resolve() == DEFAULT_REGISTRY.resolve():
        sys.exit("refusing to record synthetic-data runs in the real experiment registry; pass --registry")
    load = lambda sym, start, end: rdata.load_research_m1(root, cfg, sym, start, end)  # noqa: E731
    res = run_check(args.check, cfg, load, Registry(registry_path), Path(args.out))
    print(f"\n{res['experiment_id']}: {'PASS' if res['passed'] else 'FAIL'} (frozen {res['final_params']})")
    for sym, rows in res["coverage"].items():
        for c in rows:
            h4 = f"  {c['h4_bars']:>5,} H4 ({c['h4_frac']:.0%})" if c.get("h4_frac") is not None else ""
            rth = f"  RTH quoted {c['rth_frac']:.1%}" if c.get("rth_frac") is not None else ""
            print(f"  {sym} {c['year']}: {c['m1_bars']:>7,} M1 ({c['m1_frac']:.0%}){h4}{rth}"
                  f"  {'included' if c['included'] else 'EXCLUDED'}")
    for y in res["years"]:
        print(f"  {y['year']}: {y['trades']:>4} trades  {y['expectancy_r']:+.3f} R  win {y['win_rate']:.1%}  max DD {y['max_dd_r']:.1f} R")
    pts = res["points"]
    print(f"  net {pts['net_points_per_trade']:+.1f} pt/trade, median 1R {pts['median_risk_points']:.1f} pt;"
          f" all-in Stress {res['all_in']['expectancy_r']:+.3f} R" if res.get("all_in") else
          f"  net {pts['net_points_per_trade']:+.1f} pt/trade, median 1R {pts['median_risk_points']:.1f} pt")
    for g in res["pass_rules"]:
        print(f"  {'pass' if g['passed'] else 'FAIL'}  {g['gate']}: {g['value']:+.3f} ({g['rule']})")
    print(json.dumps(res["kanban_metadata"], indent=2, default=str))


def cmd_info(args, cfg) -> None:
    from .daily_trend import run_information

    root = Path(args.data_root or cfg["data"]["root"])
    registry_path = Path(args.registry)
    if (root / SYNTHETIC_MARKER).exists() and registry_path.resolve() == DEFAULT_REGISTRY.resolve():
        sys.exit("refusing to record synthetic-data runs in the real experiment registry; pass --registry")
    load = lambda sym, start, end: rdata.load_research_m1(root, cfg, sym, start, end)  # noqa: E731
    res = run_information(args.name, cfg, load, Registry(registry_path))
    out = Path(args.out) / res["experiment_id"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "information.json").write_text(json.dumps(res, indent=2, default=str))
    print(f"{res['experiment_id']} (information only) -> {out}")
    for tier, r in res["tiers"].items():
        for seg in ("all", "dev", "validation"):
            s, b = r[seg]["strategy"], r[seg]["buy_and_hold"]
            print(f"  {tier:<6} {seg:<10} filter {s['total_return']:+.1%} CAGR {s['cagr']:+.1%} DD {s['max_drawdown']:.1%} Sharpe {s['sharpe']:.2f}"
                  f" | B&H {b['total_return']:+.1%} CAGR {b['cagr']:+.1%} DD {b['max_drawdown']:.1%} Sharpe {b['sharpe']:.2f}"
                  f" | exposure {r[seg]['exposure']:.0%} round trips {r[seg]['round_trips']}")
        print(f"  {tier:<6} trades {r['trades']['count']}  {r['trades']['expectancy_r']:+.3f} R after costs ({r['trades']['gross_r']:+.3f} gross)")


def cmd_t1(args, cfg) -> None:
    from .t1 import run_t1

    root = Path(args.data_root or cfg["data"]["root"])
    registry_path = Path(args.registry)
    if (root / SYNTHETIC_MARKER).exists() and registry_path.resolve() == DEFAULT_REGISTRY.resolve():
        sys.exit("refusing to record synthetic-data runs in the real experiment registry; pass --registry")
    load = lambda sym, start, end: rdata.load_research_m1(root, cfg, sym, start, end)  # noqa: E731
    t1cfg = load_config(Path(args.t1_config))
    params = json.loads(args.params) if args.params else None
    names = list(cfg["strategies"]) if args.all else [args.strategy]
    for name in names:
        res = run_t1(name, cfg, t1cfg, load, Registry(registry_path), Path(args.out), params=params)
        failed = [f"{g['gate']} ({g['scope']})" for g in res["gates"] if not g["passed"]]
        print(f"\n{res['experiment_id']}: {'PASS' if res['passed'] else 'FAIL'}; chosen exit {res['chosen_variant']}"
              f" (entries: {res['entry_params_source']})")
        for row in res["variants"]:
            print(f"  {row['variant']:<16} dev {row['dev_expectancy_r']:+.3f} R ({row['dev_trades']})"
                  f"  val {row['val_expectancy_r']:+.3f} R ({row['val_trades']})")
        if failed:
            print("failed gates:\n  " + "\n  ".join(failed))


def cmd_t2(args, cfg) -> None:
    from .t2 import run_t2

    root = Path(args.data_root or cfg["data"]["root"])
    registry_path = Path(args.registry)
    if (root / SYNTHETIC_MARKER).exists() and registry_path.resolve() == DEFAULT_REGISTRY.resolve():
        sys.exit("refusing to record synthetic-data runs in the real experiment registry; pass --registry")
    load = lambda sym, start, end: rdata.load_research_m1(root, cfg, sym, start, end)  # noqa: E731
    res = run_t2(args.strategy, cfg, load_config(Path(args.t2_config)), load, Registry(registry_path), Path(args.out))
    print(f"\n{res['experiment_id']}: best arm {res['best_arm']} {'KEPT' if res['passed'] else 'not kept'}")
    for arm, s in res["arms"].items():
        print(f"  {arm:<12} trades {s['trades']:>5}  expectancy {s['expectancy_r']:+.3f} R  PF {s['profit_factor']:.2f}")
    print(json.dumps(res["kanban_metadata"], indent=2, default=str))


def cmd_prop_mc(args, cfg) -> None:
    from atlas_engine.config import load_engine_config

    from .prop_sim import reference_trades, simulate_evaluation

    engine_cfg = load_engine_config(args.engine_config)
    if args.reference:
        trades = reference_trades(args.reference, seed=args.seed)
    else:
        trades = pd.read_csv(args.trades)
        for c in ("entry_time", "exit_time"):
            trades[c] = pd.to_datetime(trades[c], utc=True, format="ISO8601")
        holdout = pd.Timestamp(cfg["segments"]["holdout_start"], tz="UTC")
        if len(trades) and trades["exit_time"].max() >= holdout:
            sys.exit("refusing a trade list that reaches the locked holdout (PRD §22)")
    out = {}
    for engine in (True, False):
        out["engine" if engine else "firm_rules_only"] = simulate_evaluation(
            trades, engine_cfg, args.sims, args.horizon_days, use_engine=engine, seed=args.seed)
    out["gate"] = {"max_breach": args.max_breach, "passed": out["engine"]["p_firm_breach"] < args.max_breach}
    print(json.dumps(out, indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="atlas-research")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="group", required=True)

    d = sub.add_parser("data").add_subparsers(dest="cmd", required=True)
    for name, fn in (("download", cmd_download), ("build", cmd_build), ("quality", cmd_quality), ("synthetic", cmd_synthetic)):
        p = d.add_parser(name)
        p.add_argument("--symbols", nargs="+", default=["EURUSD", "GBPUSD"])
        p.add_argument("--start")
        p.add_argument("--end", help="inclusive; defaults to the day before the holdout")
        p.add_argument("--data-root", required=name == "synthetic")
        p.add_argument("--workers", type=int, default=8)
        p.add_argument("--seed", type=int, default=7)
        p.set_defaults(fn=fn)

    p = d.add_parser("coverage", help="the config's declared data-quality report (data_checks:)")
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--data-root")
    p.set_defaults(fn=cmd_coverage)

    p = d.add_parser("import-mt5", help="load a MetaTrader 5 'Export bars' file instead of Dukascopy")
    p.add_argument("--symbol", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--data-root")
    p.add_argument("--ny-offset-hours", type=int, default=7, help="broker server time minus New York time")
    p.set_defaults(fn=cmd_import_mt5)

    t = sub.add_parser("t0").add_subparsers(dest="cmd", required=True)
    r = t.add_parser("run")
    r.add_argument("strategy", nargs="?")
    r.add_argument("--all", action="store_true")
    r.add_argument("--data-root")
    r.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    r.add_argument("--out", default="research/runs")
    r.set_defaults(fn=cmd_t0)
    r = t.add_parser("check", help="one frozen-parameter run over a declared period (config `checks:`)")
    r.add_argument("check")
    r.add_argument("--data-root")
    r.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    r.add_argument("--out", default="research/runs")
    r.set_defaults(fn=cmd_check)

    r = t.add_parser("info", help="an information-only run declared under `information:` (e.g. a daily trend filter)")
    r.add_argument("name")
    r.add_argument("--data-root")
    r.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    r.add_argument("--out", default="research/runs")
    r.set_defaults(fn=cmd_info)

    t1 = sub.add_parser("t1", help="exit research: §18 exit variants vs the fixed 2R baseline").add_subparsers(dest="cmd", required=True)
    r = t1.add_parser("run")
    r.add_argument("strategy", nargs="?")
    r.add_argument("--all", action="store_true")
    r.add_argument("--params", help="entry parameters as JSON; default: the latest T0 run's final parameters")
    r.add_argument("--t1-config", default=str(Path(__file__).parent / "configs" / "t1.yaml"))
    r.add_argument("--data-root")
    r.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    r.add_argument("--out", default="research/runs")
    r.set_defaults(fn=cmd_t1)

    t2 = sub.add_parser("t2").add_subparsers(dest="cmd", required=True)
    r = t2.add_parser("run", help="selection layer keep/kill: rules-only vs rules + GBM")
    r.add_argument("strategy")
    r.add_argument("--t2-config", default=str(DEFAULT_T2_CONFIG))
    r.add_argument("--data-root")
    r.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    r.add_argument("--out", default="research/runs")
    r.set_defaults(fn=cmd_t2)

    m = sub.add_parser("prop-mc", help="prop evaluation Monte Carlo through the risk engine (T3)")
    src = m.add_mutually_exclusive_group(required=True)
    src.add_argument("--trades", help="CSV with entry_time, exit_time, r and ideally mae_r, symbol, direction")
    src.add_argument("--reference", help="synthetic reference profile: " + ", ".join(
        ["edge_0.20R", "breakeven", "losing_-0.15R", "busy_breakeven"]))
    m.add_argument("--engine-config", default="config")
    m.add_argument("--sims", type=int, default=2_000)
    m.add_argument("--horizon-days", type=int, default=60)
    m.add_argument("--max-breach", type=float, default=0.02)
    m.add_argument("--seed", type=int, default=0)
    m.set_defaults(fn=cmd_prop_mc)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    if args.fn in (cmd_t0, cmd_t1) and not (args.all or args.strategy):
        ap.error(f"{args.group} run needs a strategy name or --all")
    args.fn(args, load_config(Path(args.config)))


if __name__ == "__main__":
    main()
