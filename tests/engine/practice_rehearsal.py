"""End-to-end rehearsal of the demo practice run (docs/demo-practice.md) without a broker.

The engine runs with the real practice config (deploy/demo-practice), the real strategy loader and the real
model wiring of ``atlas-engine run``, against ``FakeMcpNinjaTrader`` (the MCP server stand-in, recorded shapes).
Prices are 1-minute MES bars: synthetic sessions by default, or replayed MES-proxy minutes when the research
data is on this machine (``proxy_sessions``). Each simulated minute the stand-in's quote moves through the
bar's low, high and close (so resting stops and targets can fill), then the engine takes one step.

Not a conftest.py, so it can be imported by a script as well as by pytest.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo

from atlas_api.engine_cli import practice_models
from atlas_engine.adapters.ninjatrader.mcp_venue import NinjaTraderMcpAdapter
from atlas_engine.agent_intents import load_agent_settings
from atlas_engine.alerts import AlertOutbox
from atlas_engine.config import load_engine_config
from atlas_engine.execution import FuturesExecutionAdapter, load_execution_settings
from atlas_engine.journal import Journal
from atlas_engine.models import ModelRegistry
from atlas_engine.operator import sign
from atlas_engine.pipeline import load_decision_settings
from atlas_engine.runtime import TradingEngine
from atlas_engine.strategies import decision_config, load_strategies

from fake_mcp import FakeMcpNinjaTrader
from t4help import KEY, Clock

PRACTICE_CONFIG = Path(__file__).resolve().parents[2] / "deploy" / "demo-practice"
UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
TICK = 0.25
CONTRACT = "MESZ6"


def ny(day: dt.date, hh: int, mm: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm), NY).astimezone(UTC)


def grid(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


def cme_open(t: dt.datetime) -> bool:
    """CME equity-index hours: Sunday 18:00 to Friday 17:00 New York, less the daily 17:00-18:00 break."""
    local = t.astimezone(NY)
    wd, hm = local.weekday(), local.hour * 60 + local.minute
    if wd == 5 or (wd == 6 and hm < 18 * 60) or (wd == 4 and hm >= 17 * 60):
        return False
    return not 17 * 60 <= hm < 18 * 60


# Waypoints (New York minute of the day -> offset from the day's base price) for each kind of day.
# The opening range (09:30-09:45) runs base-5 .. base+5; the 15-minute bar that closes at 10:00 breaks it.
RANGE = [(9 * 60 + 30, 0.0), (9 * 60 + 34, 5.0), (9 * 60 + 39, -5.0), (9 * 60 + 44, 0.0), (9 * 60 + 47, 1.0)]
DAYS = {
    "target": RANGE + [(9 * 60 + 58, 8.0), (10 * 60 + 40, 30.0), (11 * 60, 45.0), (16 * 60, 42.0)],
    "stop": RANGE + [(9 * 60 + 58, -8.0), (10 * 60 + 15, -2.0), (10 * 60 + 30, 10.0), (16 * 60, 4.0)],
    "flat_by": RANGE + [(9 * 60 + 58, 8.0), (10 * 60 + 30, 12.0), (14 * 60, 10.0), (15 * 60 + 50, 14.0),
                        (16 * 60, 13.0)],
}


def _interp(points: list[tuple[int, float]], m: int) -> float:
    if m <= points[0][0]:
        return points[0][1]
    for (m0, p0), (m1, p1) in zip(points, points[1:]):
        if m0 <= m <= m1:
            return p0 + (p1 - p0) * (m - m0) / (m1 - m0)
    return points[-1][1]


def synthetic_bars(days: list[tuple[dt.date, str]], base: float = 6000.0, history_from: dt.datetime | None = None) -> list[dict]:
    """1-minute bars from ``history_from`` (default: the Sunday 18:00 New York open before the first day)
    to 16:59 New York of the last day; each listed day follows its waypoints, the rest is flat with a zigzag."""
    first = days[0][0]
    start = history_from or ny(first - dt.timedelta(days=(first.weekday() + 1) % 7), 18, 0)
    end = ny(days[-1][0], 17, 0)
    kinds = {d: k for d, k in days}
    bases = {d: base + 20.0 * i for i, (d, _) in enumerate(days)}
    out, t, prev = [], start, None
    while t < end:
        if cme_open(t):
            local = t.astimezone(NY)
            m = local.hour * 60 + local.minute
            day = local.date() if m < 17 * 60 else local.date() + dt.timedelta(days=1)
            b = bases.get(day, bases.get(local.date(), base))
            kind = kinds.get(local.date()) if m < 17 * 60 else None
            off = _interp(DAYS[kind], m) if kind and m >= 9 * 60 + 30 else (0.5 if m % 2 else -0.5)
            close = grid(b + off)
            o = close if prev is None else prev
            out.append({"t": t, "open": o, "high": max(o, close) + TICK, "low": min(o, close) - TICK, "close": close})
            prev = close
        t += dt.timedelta(minutes=1)
    return out


def proxy_bars(days: list[dt.date], shift_days: int) -> list[dict] | None:
    """Replayed MES-proxy minutes (S&P 500 CFD mid, on the 0.25 grid) for ``days`` and the week before, moved
    ``shift_days`` later (whole weeks, so weekdays and daylight-saving offsets stay put). None when the research
    data is not on this machine. Only validation-period dates are used; the holdout is never asked for."""
    try:
        import yaml

        from atlas_research.data import load_research_m1
    except ImportError:
        return None
    repo = Path(__file__).resolve().parents[2]
    cfg = yaml.safe_load((repo / "atlas_research" / "configs" / "mes.yaml").read_text())
    if not (repo / cfg["data"]["root"] / "m1" / cfg["data"]["proxies"]["MES"]["source"]).exists():
        return None
    start, end = days[0] - dt.timedelta(days=7), days[-1] + dt.timedelta(days=1)
    assert str(end) <= cfg["segments"]["holdout_start"], "the rehearsal never reads the holdout"
    try:
        m1 = load_research_m1(repo / cfg["data"]["root"], cfg, "MES", str(start), str(end))
    except Exception:  # noqa: BLE001 - missing or partial local data: fall back to synthetic prices
        return None
    out = []
    shift = dt.timedelta(days=shift_days)
    for t, r in m1.iterrows():
        mids = {c: grid((r[f"bid_{c}"] + r[f"ask_{c}"]) / 2) for c in "ohlc"}
        out.append({"t": t.to_pydatetime().astimezone(UTC) + shift, "open": mids["o"], "high": mids["h"],
                    "low": mids["l"], "close": mids["c"]})
    return out or None


@dataclass
class Rehearsal:
    engine: TradingEngine
    venue: NinjaTraderMcpAdapter
    fake: FakeMcpNinjaTrader
    journal: Journal
    clock: Clock
    root: Path
    bars: list[dict]
    by_time: dict = field(default_factory=dict)

    def enable(self) -> None:
        cmd = sign(KEY, "enable_trading", "yahye", "demo practice rehearsal in the test suite", self.clock())
        (self.engine.inbox / f"{cmd['nonce']}.json").write_text(json.dumps(cmd))
        self.engine.step()
        assert self.engine.trading["enabled"], self.engine.events()["events"][-3:]

    def minute(self, t: dt.datetime) -> None:
        """Play the bar that ended at ``t`` through the quote, then step the engine 5 s later."""
        bar = self.by_time.get(t - dt.timedelta(minutes=1))
        self.clock.t = t
        if bar is not None:
            for px in (bar["low"], bar["high"], bar["close"]):
                self.fake.set_quote(CONTRACT, px, px + TICK, at=t)
        self.clock.t = t + dt.timedelta(seconds=5)
        bid, ask, _ = self.fake.quotes[CONTRACT]
        self.fake.set_quote(CONTRACT, bid, ask, at=self.clock.t)  # a fresh quote for this step
        self.engine.step()

    def session(self, day: dt.date, start=(9, 0), end=(16, 10), hook=None) -> None:
        t, stop = ny(day, *start), ny(day, *end)
        while t <= stop:
            self.minute(t)
            if hook is not None:
                hook(self, t)
            t += dt.timedelta(minutes=1)


def build(root: Path, bars: list[dict], start_at: dt.datetime, slip_ticks: int = 1,
          config: Path = PRACTICE_CONFIG, enable: bool = True, **settings_over) -> Rehearsal:
    """The engine as ``atlas-engine run --broker ninjatrader-mcp --config deploy/demo-practice`` builds it,
    with the stand-in MCP server in place of NinjaTrader's."""
    clock = Clock(start_at)
    fake = FakeMcpNinjaTrader(clock, slip_ticks=slip_ticks)
    fake.bars[CONTRACT] = bars
    last = [b for b in bars if b["t"] < start_at][-1]
    fake.set_quote(CONTRACT, last["close"], last["close"] + TICK)
    cfg = load_engine_config(config)
    settings = replace(load_execution_settings(config), **settings_over)
    venue = NinjaTraderMcpAdapter(fake, {"MES": CONTRACT}, ledger_path=root / "state" / "ninjatrader-brackets.json",
                                  commission_per_contract=settings.commission_per_lot, poll_s=0, quote_s=0,
                                  leg_wait_s=0, sleep=lambda s: None, today=lambda: clock().date())
    sources = load_strategies(config)
    decision = load_decision_settings(config)
    priors, assignment = decision_config(sources)
    models = ModelRegistry.default(priors, assignment, timeout_ms=decision.model_timeout_ms,
                                   extra=practice_models(assignment, decision))
    journal = Journal(root / "state" / "journal.db", "run-rehearsal")
    engine = TradingEngine(cfg, settings, venue, journal, root / "state", sources=sources, now=clock,
                           alerts=AlertOutbox(root / "alerts.jsonl", senders=[]), operator_key=KEY,
                           broker_label="ninjatrader-mcp-demo", agent=load_agent_settings(config),
                           decision=decision, models=models,
                           execution=FuturesExecutionAdapter(venue, settings, pause=lambda s: None))
    engine.start()
    r = Rehearsal(engine, venue, fake, journal, clock, root, bars, {b["t"]: b for b in bars})
    if enable:
        r.enable()
    return r
