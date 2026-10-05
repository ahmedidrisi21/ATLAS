"""An engine on the Tradovate stand-in with the example futures config (deploy/futures-config).

Not a conftest.py, so other scripts can import it without pytest.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from atlas_engine.adapters.tradovate import Credentials, FakeTradovate, TradovateAdapter, TradovateClient
from atlas_engine.alerts import AlertOutbox
from atlas_engine.config import load_engine_config
from atlas_engine.execution import FuturesExecutionAdapter, load_execution_settings
from atlas_engine.journal import Journal
from atlas_engine.operator import sign
from atlas_engine.runtime import TradingEngine
from atlas_engine.strategies import Signal

from t4help import KEY, Clock, stand_in_models

FUTURES_CONFIG = Path(__file__).resolve().parents[2] / "deploy" / "futures-config"
UTC = dt.timezone.utc
# Tuesday 2026-10-06 14:00 UTC = 09:00 Chicago (CDT): regular trading hours, well before the 15:10 flat-by.
F0 = dt.datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
MESZ6_LAST_TRADE = dt.datetime(2026, 12, 18, 14, 30, tzinfo=UTC)
CREDS = Credentials("atlas", "pw", "atlas", "1", "0", "secret-sec", "dev")


def venue(clock, fake: FakeTradovate | None = None, ledger: Path | None = None, pins=None, env: str = "demo"):
    fake = fake or FakeTradovate({"MESZ6": MESZ6_LAST_TRADE}, now=clock)
    client = TradovateClient(env, CREDS, transport=fake, now=clock, sleep=lambda s: None)
    v = TradovateAdapter(client, pins or {"MES": "MESZ6"}, ledger_path=ledger, stream=False, poll_s=0,
                         account_poll_s=0, today=lambda: clock().date())
    return v, fake


@dataclass
class FRig:
    engine: TradingEngine
    venue: TradovateAdapter
    fake: FakeTradovate
    journal: Journal
    clock: Clock
    root: Path

    def quote(self, bid: float, ask: float | None = None, code: str = "MESZ6") -> None:
        ask = bid + 0.25 if ask is None else ask
        self.fake.set_quote(code, bid, ask)
        self.venue.quotes.apply(self.fake.quote_message(code, bid, ask))

    def step(self, seconds: float = 1.0):
        self.clock.advance(seconds=seconds)
        bid, ask = self.fake.quotes["MESZ6"]
        self.quote(bid, ask)  # a fresh quote each step
        return self.engine.step()

    def operator(self, action: str, reason: str = "operator drill in the test suite") -> dict:
        cmd = sign(KEY, action, "yahye", reason, self.clock())
        (self.engine.inbox / f"{cmd['nonce']}.json").write_text(json.dumps(cmd))
        self.step()
        return cmd

    def enable(self) -> None:
        self.operator("enable_trading")
        assert self.engine.trading["enabled"], self.engine.events()["events"][-3:]

    def signal(self, direction: int = 1, stop_points: float = 10.0, setup: str = "trend_pullback") -> Signal:
        bid, ask = self.fake.quotes["MESZ6"]
        entry = ask if direction == 1 else bid
        return Signal("MES", setup, "1", direction, entry - direction * stop_points, self.clock().replace(second=0))


def build(root: Path, clock: Clock | None = None, start: bool = True, env: str = "demo", **settings_over) -> FRig:
    from dataclasses import replace

    clock = clock or Clock(F0)
    cfg = load_engine_config(FUTURES_CONFIG)
    settings = replace(load_execution_settings(FUTURES_CONFIG), **settings_over)
    v, fake = venue(clock, ledger=root / "brackets.json", env=env)
    fake.set_quote("MESZ6", 5000.0, 5000.25)
    v.quotes.apply(fake.quote_message("MESZ6", 5000.0, 5000.25))
    journal = Journal(root / "journal.db", "run-test")
    engine = TradingEngine(cfg, settings, v, journal, root / "state", now=clock,
                           alerts=AlertOutbox(root / "alerts.jsonl", senders=[]), operator_key=KEY,
                           broker_label="fake-tradovate", models=stand_in_models(),
                           execution=FuturesExecutionAdapter(v, settings, pause=lambda s: None))
    rig = FRig(engine, v, fake, journal, clock, root)
    if start:
        engine.start()
    return rig
