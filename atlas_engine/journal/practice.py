"""The demo practice run's report card, from the journal alone (docs/demo-practice.md).

The practice run proves the machinery, not a strategy, so its pass marks are machinery marks, fixed before
the first trade:

    no unprotected position        zero positions found or left without a working stop
    every order reconciles         zero mismatches between the journal and the broker at reconciliation
    slippage within 2 ticks        the average slippage of entries and exits against the planned price
    at least 20 trades             closed trades, all sources together

Everything else is reported for reading, not judged: orders sent / filled / rejected / unknown, protection
fixes and halts, refusals by reason, time from signal to fill, and R per trade after fees next to what the
backtest expected. Rule strategies and the Hermes agent (setup ``hermes``) are scored separately.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter

from atlas_engine.agent_intents import SETUP as AGENT_SETUP
from atlas_engine.agent_intents import _stats

from .store import Journal

TICK = {"MES": 0.25}
BACKTEST_SLIPPAGE_TICKS = 1.0  # the MES research charged 1 tick on every market fill, 0 on targets
PASS = {"unprotected_positions": 0, "unreconciled_orders": 0, "max_avg_slippage_ticks": 2.0, "min_trades": 20}
# What each setup's backtest expected, after costs (docs/mes-candidates.md). Hermes has no backtest:
# its bar is 0R after costs (docs/hermes-trader.md).
EXPECTED = {
    "opening_range_breakout": {"expectancy_r": -0.012, "before_costs_r": 0.011, "trades": 1353,
                               "source": "docs/mes-candidates.md, MES round 1, out of sample"},
    AGENT_SETUP: {"expectancy_r": None, "bar_r": 0.0, "source": "no backtest possible; the bar is 0R after costs"},
}
MACHINERY_NOTE = "machinery test, not expected to profit"
ROWS = 1_000_000


def _t(s) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def _avg(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def practice_report(journal: Journal) -> dict:
    decisions = journal.rows("decisions", ROWS)
    events = journal.rows("execution_events", ROWS)
    orders = journal.rows("orders", ROWS)
    trades = journal.rows("trades", ROWS)
    system = journal.rows("system_events", ROWS)
    agent = journal.rows("agent_actions", ROWS)
    by_id = {d["decision_id"]: d for d in decisions if d.get("decision_id")}
    setup_of = {k: d.get("setup") for k, d in by_id.items()}
    dir_of = {k: d.get("direction") for k, d in by_id.items()}

    # -- orders -------------------------------------------------------------------------------------------
    kinds = Counter(e.get("event") for e in events)
    entries_sent = sum(1 for e in events if e.get("event") == "order_submitted" and not e.get("purpose"))
    exits_sent = sum(1 for e in events if e.get("event") == "order_submitted" and e.get("purpose"))
    outcomes = Counter(d.get("outcome") for d in decisions if d.get("decision") == "ALLOW")
    order_block = {
        "entries_sent": entries_sent, "exits_sent": exits_sent,
        "acknowledged": kinds["order_acknowledged"],
        "filled": outcomes["filled"] + outcomes["partial"],
        "rejected_by_platform": kinds["order_rejected"],
        "unknown_outcome": kinds["order_outcome_unknown"] + outcomes["unknown"],
        "entry_outcomes": dict(outcomes),
    }

    # -- slippage (ticks, positive = worse than planned) --------------------------------------------------
    planned = {k: (d.get("pipeline") or {}).get("entry") for k, d in by_id.items()}
    entry_slip, signal_to_fill = [], []
    by_setup_slip: dict[str, list[float]] = {}
    for o in orders:
        did = o.get("decision_id")
        if not did or o.get("action") or o.get("status") not in ("filled", "partial") or o.get("price") is None:
            continue
        dec = by_id.get(did, {})
        sym = dec.get("symbol") or "MES"
        tick = TICK.get(sym, 0.25)
        plan, direction = planned.get(did), dir_of.get(did) or 1
        if plan is not None:
            s = round(direction * (o["price"] - plan) / tick, 2)
            entry_slip.append(s)
            by_setup_slip.setdefault(setup_of.get(did) or "unknown", []).append(s)
        t0, t1 = _t(dec.get("decision_time")), _t(o.get("at"))
        if t0 and t1:
            signal_to_fill.append((t1 - t0).total_seconds())
    exit_slip, exits_by_kind = [], Counter()
    for t in trades:
        reason, tick = t.get("exit_reason"), TICK.get(t.get("symbol"), 0.25)
        exits_by_kind[{"sl": "stop", "tp": "target"}.get(reason, "atlas_close")] += 1
        if t.get("exit") is None:
            continue
        if reason == "sl" and t.get("stop") is not None:
            exit_slip.append(round(t["direction"] * (t["stop"] - t["exit"]) / tick, 2) + 0.0)
        elif reason == "tp" and t.get("target") is not None:
            exit_slip.append(round(t["direction"] * (t["target"] - t["exit"]) / tick, 2) + 0.0)
    for o in orders:  # ATLAS's own market exits (flat-by time, kill, protection): against the quote when sent
        if o.get("action") == "close" and o.get("status") == "closed" and o.get("slippage_points") is not None:
            exit_slip.append(float(o["slippage_points"]))
    all_slip = entry_slip + exit_slip
    slippage = {"entry_avg_ticks": _avg(entry_slip), "exit_avg_ticks": _avg(exit_slip), "all_avg_ticks": _avg(all_slip),
                "entries_measured": len(entry_slip), "exits_measured": len(exit_slip),
                "entry_worst_ticks": max(entry_slip) if entry_slip else None,
                "exit_worst_ticks": max(exit_slip) if exit_slip else None,
                "entry_avg_ticks_by_setup": {k: _avg(v) for k, v in sorted(by_setup_slip.items())},
                "backtest_assumed_ticks": BACKTEST_SLIPPAGE_TICKS, "exits_by_kind": dict(exits_by_kind)}

    # -- protection, halts, reconciliation ----------------------------------------------------------------
    unprotected_tickets = set()
    for e in events:
        if e.get("event") == "protection_failed":
            unprotected_tickets.add(("failed", e.get("client_id")))
    mismatches: dict[str, dict] = {}
    for e in events:
        if e.get("event") != "reconciliation_completed":
            continue
        for m in e.get("mismatches") or []:
            key = f"{m.get('kind')}:{m.get('ticket')}"
            mismatches.setdefault(key, {**m, "first_seen": e.get("at")})
            if m.get("kind") == "unprotected_position":
                unprotected_tickets.add(("found", m.get("ticket")))
    unprotected_tickets |= {("closed", d.get("decision_id")) for d in decisions if d.get("outcome") == "unprotected_closed"}
    halts = [s for s in system if s.get("event") == "state_change" and s.get("state") in ("HALT", "KILL")]
    protection = {
        "checks_needing_a_fix": kinds["protection_unverified"],
        "fixed": kinds["protection_unverified"] - kinds["protection_failed"],
        "pair_replaced": kinds["protection_replaced"],
        "could_not_fix_position_closed": kinds["protection_failed"],
        "halts": len(halts),
        "halt_reasons": dict(Counter(r for h in halts for r in h.get("reasons") or [])),
    }

    # -- refusals -----------------------------------------------------------------------------------------
    refusals: dict[str, Counter] = {}
    for d in decisions:
        if d.get("decision") == "ALLOW" and d.get("outcome") in ("filled", "partial", "duplicate"):
            continue
        for r in d.get("reasons") or ["(no reason given)"]:
            refusals.setdefault(d.get("setup") or "unknown", Counter())[str(r)] += 1
    for a in agent:  # agent intents refused before the pipeline (its own limits, bad arguments)
        if a.get("action") == "submit_trade_intent" and a.get("outcome") == "refused":
            for r in a.get("reasons") or []:
                refusals.setdefault(AGENT_SETUP, Counter())[r] += 1

    # -- results ------------------------------------------------------------------------------------------
    by_setup: dict[str, list[float]] = {}
    for t in trades:
        if t.get("r") is not None:
            by_setup.setdefault(t.get("setup") or "unknown", []).append(float(t["r"]))
    results = {}
    for s in sorted(set(by_setup) | {k for k in EXPECTED if k in by_setup or k in setup_of.values()}):
        results[s] = {**_stats(by_setup.get(s, [])), "expected": EXPECTED.get(s),
                      "note": MACHINERY_NOTE if s != AGENT_SETUP else "Hermes's own demo trades"}

    n_trades = sum(len(v) for v in by_setup.values())
    marks = {
        "unprotected_positions": {"value": len(unprotected_tickets), "pass": len(unprotected_tickets) == 0,
                                  "mark": "zero"},
        "unreconciled_orders": {"value": len(mismatches), "pass": not mismatches, "mark": "zero"},
        "average_slippage_ticks": {"value": slippage["all_avg_ticks"],
                                   "pass": slippage["all_avg_ticks"] is not None
                                   and abs(slippage["all_avg_ticks"]) <= PASS["max_avg_slippage_ticks"],
                                   "mark": f"within {PASS['max_avg_slippage_ticks']:g} ticks"},
        "completed_trades": {"value": n_trades, "pass": n_trades >= PASS["min_trades"],
                             "mark": f"at least {PASS['min_trades']}"},
    }
    return {
        "verdict": "PASS" if all(m["pass"] for m in marks.values()) else "NOT YET",
        "pass_marks": marks,
        "orders": order_block,
        "slippage": slippage,
        "signal_to_fill_s": {"median": round(statistics.median(signal_to_fill), 1) if signal_to_fill else None,
                             "max": round(max(signal_to_fill), 1) if signal_to_fill else None,
                             "fills": len(signal_to_fill)},
        "protection": protection,
        "unreconciled": list(mismatches.values()),
        "refusals": {s: dict(c.most_common()) for s, c in sorted(refusals.items())},
        "results": results,
    }


def _fmt(x, unit: str = "") -> str:
    return "n/a" if x is None else f"{x:+.3f}{unit}" if unit == "R" else f"{x}{unit}"


def render(rep: dict) -> str:
    """The report in plain words, for the operator."""
    m, o, s, p = rep["pass_marks"], rep["orders"], rep["slippage"], rep["protection"]
    tick = lambda ok: "PASS" if ok else "not yet"  # noqa: E731
    lines = [
        "ATLAS demo practice run: does the machinery work? (a machinery test, not a profit test)",
        "",
        f"Overall: {rep['verdict']}",
        f"  {tick(m['unprotected_positions']['pass']):8} positions without protection: "
        f"{m['unprotected_positions']['value']} (must be zero)",
        f"  {tick(m['unreconciled_orders']['pass']):8} orders the journal can't match with NinjaTrader: "
        f"{m['unreconciled_orders']['value']} (must be zero)",
        f"  {tick(m['average_slippage_ticks']['pass']):8} average slippage: {_fmt(s['all_avg_ticks'], ' ticks')} "
        "(must be within 2 ticks)",
        f"  {tick(m['completed_trades']['pass']):8} completed trades: {m['completed_trades']['value']} "
        "(need at least 20)",
        "",
        "Orders",
        f"  {o['entries_sent']} entries sent, {o['filled']} filled, {o['rejected_by_platform']} refused by NinjaTrader, "
        f"{o['unknown_outcome']} with an unknown outcome; {o['exits_sent']} exits sent by ATLAS.",
        "",
        "Slippage, in ticks against the price ATLAS planned (positive = worse; the backtest assumed "
        f"{s['backtest_assumed_ticks']:g} tick)",
        f"  entries: average {_fmt(s['entry_avg_ticks'])}, worst {_fmt(s['entry_worst_ticks'])} "
        f"({s['entries_measured']} measured)",
        f"  exits:   average {_fmt(s['exit_avg_ticks'])}, worst {_fmt(s['exit_worst_ticks'])} "
        f"({s['exits_measured']} measured; by kind {s['exits_by_kind']})",
        "",
        f"Signal to fill: median {_fmt(rep['signal_to_fill_s']['median'], ' s')}, "
        f"slowest {_fmt(rep['signal_to_fill_s']['max'], ' s')}",
        "",
        "Protection (stop and target at NinjaTrader)",
        f"  {p['checks_needing_a_fix']} fills needed their stop or target moved onto the planned price; "
        f"{p['fixed']} fixed, {p['could_not_fix_position_closed']} could not be fixed and were closed.",
        f"  The engine halted {p['halts']} time(s)" + (f": {p['halt_reasons']}" if p["halt_reasons"] else "."),
        "",
        "Refusals by reason (a refusal is the engine saying no, which is often correct)",
    ]
    if not rep["refusals"]:
        lines.append("  none")
    for setup, reasons in rep["refusals"].items():
        lines.append(f"  {setup}: " + ", ".join(f"{r} x{n}" for r, n in reasons.items()))
    lines += ["", "Results per trade, in R after fees (1R = the amount risked)"]
    if not rep["results"]:
        lines.append("  no closed trades yet")
    for setup, r in rep["results"].items():
        exp = r.get("expected") or {}
        want = (f"backtest expected {_fmt(exp.get('expectancy_r'), 'R')}" if exp.get("expectancy_r") is not None
                else "no backtest; the bar is 0R")
        lines.append(f"  {setup} ({r['note']}): {r['trades']} trades, average {_fmt(r['expectancy_r'], 'R')}, "
                     f"total {r['total_r']:+.2f}R; {want}")
    if rep["unreconciled"]:
        lines += ["", "Unmatched with NinjaTrader:"] + [f"  {u.get('kind')} ticket {u.get('ticket')}: {u.get('detail')}"
                                                      for u in rep["unreconciled"]]
    return "\n".join(lines)
