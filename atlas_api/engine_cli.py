"""``atlas-engine``: run the trading engine and its operations API on the engine host (T4).

    atlas-engine run --config config --state /srv/atlas/engine --tokens engine-tokens.yaml \\
                     --operator-key /srv/atlas/engine/operator.key
    atlas-engine token --tokens engine-tokens.yaml --name atlas-operations/atlas-operations \\
                       --scopes ops:read,ops:disable_trading
    atlas-engine operator keygen --key /srv/atlas/engine/operator.key
    atlas-engine operator enable_trading --key ... --inbox /srv/atlas/engine/operator-inbox \\
                                         --operator yahye --reason "reviewed the incident, all clear"
    atlas-engine show --state /srv/atlas/engine
    atlas-engine scorecard --state /srv/atlas/engine

The API serves the same five operations routes as ``atlas-engine-sim`` (H3),
so the Hermes side (atlas-operations, cron checks, dashboard) works unchanged
when it is pointed at the real engine. It also serves the demo-only trading
routes the ``atlas-trading`` profile uses (atlas_api/trading.py). Operator commands are signed files in
the engine's inbox, never API calls.

``--broker`` must match ``execution.platform`` in atlas.yaml:

    mt5             the ``MetaTrader5`` package and a logged-in terminal on Windows
    fake            the in-process fake MT5 terminal, for drills
    tradovate       Tradovate's API (futures-first, docs/futures.md). Credentials come only from the
                    host environment: ATLAS_TRADOVATE_USER, _PASSWORD, _APP_ID, _APP_VERSION, _CID, _SEC,
                    _DEVICE_ID; ATLAS_TRADOVATE_ENV is demo (default) or live; ATLAS_TRADOVATE_ACCOUNT
                    picks the account when the login has several.
    fake-tradovate  the in-process Tradovate stand-in, for drills

The fakes use no network, no broker and send no orders anywhere.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import threading
import uuid
from pathlib import Path

from atlas_api import auth
from atlas_api.auth import TokenStore
from atlas_api.ops import ENGINE_SCOPES
from atlas_api.trading import ENGINE_ROUTES, EngineService

log = logging.getLogger("atlas_engine")


def _watchdog_reader(path: str | None):
    if not path:
        return None

    def read() -> str | None:
        p = Path(path)
        if not p.exists():
            return None
        # The EA writes UTF-16 (MQL5 FILE_TXT default) or ANSI; accept both.
        raw = p.read_bytes()
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
        return text.strip().splitlines()[0] if text.strip() else None

    return read


def build_engine(args):
    from atlas_engine.adapters.mt5 import MT5Adapter, ServerClock
    from atlas_engine.agent_intents import load_agent_settings
    from atlas_engine.alerts import AlertOutbox
    from atlas_engine.config import load_engine_config
    from atlas_engine.execution import load_execution_settings
    from atlas_engine.journal import Journal
    from atlas_engine.operator import load_key
    from atlas_engine.runtime import TradingEngine
    from atlas_engine.models import ModelRegistry
    from atlas_engine.pipeline import load_decision_settings
    from atlas_engine.strategies import decision_config, load_strategies

    cfg = load_engine_config(args.config, require_read_only=not args.allow_writable_config)
    settings = load_execution_settings(args.config)
    platform = "tradovate" if args.broker in ("tradovate", "fake-tradovate") else "mt5"
    if settings.platform != platform:
        raise SystemExit(f"--broker {args.broker} needs execution.platform: {platform} in atlas.yaml "
                         f"(it says {settings.platform})")
    state = Path(args.state)
    clock = ServerClock(settings.server_tz, settings.server_offset_hours)
    if platform == "tradovate":
        adapter, label = tradovate_venue(args.broker, cfg, settings, state)
    elif args.broker == "mt5":
        try:
            import MetaTrader5 as mt5  # noqa: N813 - Windows engine host only
        except ImportError:
            raise SystemExit("the MetaTrader5 package is not installed; it runs only on Windows next to a terminal")
        label = "mt5"
    else:
        from atlas_engine.adapters.mt5.fake import FakeMT5
        from atlas_engine.runtime import utc_now
        mt5 = FakeMT5(utc_now, clock, symbols=cfg.symbols, live_quotes=True)
        label = "fake-mt5"
    if platform == "mt5":
        adapter = MT5Adapter(mt5, clock, settings.commission_per_lot)
    journal = Journal(state / "journal.db", f"run-{uuid.uuid4().hex[:12]}")
    key = load_key(args.operator_key) if args.operator_key else None
    if key is None:
        log.warning("no --operator-key: operator commands will be refused, so trading can't be enabled")
    watchdog = _watchdog_reader(settings.watchdog_heartbeat_file)
    if label == "fake-mt5" and watchdog is None:
        import time

        def watchdog():  # the fake terminal has no EA; stand in for its heartbeat in drills
            return f"ATLAS-WD 1 {int(time.time())} 0 OK"
    sources = load_strategies(args.config)
    decision = load_decision_settings(args.config)
    priors, assignment = decision_config(sources)
    # GBM loads here once one has been trained and kept by T2 (PRD v3 §23). A strategy that names a model that
    # isn't loaded gets no estimate, so its trades are rejected rather than run on rules by default.
    models = ModelRegistry.default(priors, assignment, timeout_ms=decision.model_timeout_ms,
                                   extra=load_jev(assignment, decision, state))
    return TradingEngine(cfg, settings, adapter, journal, state, sources=sources,
                         alerts=AlertOutbox(state / "alerts.jsonl"), operator_key=key,
                         watchdog=watchdog, broker_label=label, agent=load_agent_settings(args.config),
                         decision=decision, models=models)


def tradovate_venue(broker: str, cfg, settings, state: Path, env: dict | None = None):
    """The Tradovate venue for the pinned contracts, real or the in-process stand-in."""
    from atlas_engine.adapters.tradovate import Credentials, FakeTradovate, TradovateAdapter, TradovateClient
    from atlas_engine.adapters.tradovate.fake import FakeQuoteBook
    from atlas_engine.config import ConfigError
    from atlas_engine.execution import pinned_contracts

    env = os.environ if env is None else env
    today = dt.datetime.now(dt.timezone.utc).date()
    try:
        pins = pinned_contracts(settings, cfg.symbols, today)
    except ConfigError as e:
        raise SystemExit(str(e)) from None
    if not pins:
        raise SystemExit("execution.platform tradovate trades futures; atlas.yaml lists no futures symbols")
    common = dict(ledger_path=state / "tradovate-brackets.json", commission_per_contract=settings.commission_per_lot,
                  poll_s=settings.broker_poll_s)
    if broker == "tradovate":
        try:
            client = TradovateClient(env.get("ATLAS_TRADOVATE_ENV", "demo"), Credentials.from_env(env))
        except ValueError as e:
            raise SystemExit(str(e)) from None
        venue = TradovateAdapter(client, pins, account_name=env.get("ATLAS_TRADOVATE_ACCOUNT"), **common)
        return venue, f"tradovate-{client.env}"
    expiry = dt.datetime.combine(today + dt.timedelta(days=60), dt.time(13, 30), dt.timezone.utc)
    fake = FakeTradovate({code: expiry for code in pins.values()})
    client = TradovateClient("demo", Credentials("drill", "pw", "atlas", "1", "0", "-", "drill"), transport=fake)
    venue = TradovateAdapter(client, pins, quotes=FakeQuoteBook(fake), stream=False, **common)
    return venue, "fake-tradovate"


def load_jev(assignment: dict, decision, state: Path, env: dict | None = None) -> list:
    """Jev on TypeSafe's API, when a strategy is assigned to it and the host has ``TYPESAFE_API_KEY``.

    The calibrator, if T2 has fitted one, is ``<state>/jev_calibration.json``. Without it the pipeline lets Jev
    trade only a demo account (PRD v3 §24).
    """
    if "jev" not in assignment.values():
        return []
    from atlas_engine.adapters.jev import JevAdapter, TypeSafeTransport
    from atlas_engine.calibration import Calibrator
    from atlas_engine.models import JevModel

    transport = TypeSafeTransport.from_env(timeout_s=decision.model_timeout_ms / 1000, env=env)
    if transport is None:
        log.warning("a strategy is assigned to jev but TYPESAFE_API_KEY is not set; its trades will be rejected")
        return []
    cal_path = Path(state) / "jev_calibration.json"
    calibrator = Calibrator.from_json(cal_path.read_text()) if cal_path.exists() else None
    if calibrator is None:
        log.warning("jev has no calibrator at %s; it may trade a demo account only", cal_path)
    adapter = JevAdapter(transport, decision.jev_model, live=True, timeout_ms=decision.model_timeout_ms)
    return [JevModel(adapter, calibrator)]


def main(argv: list[str] | None = None) -> None:
    from atlas_engine.operator import ACTIONS, keygen, load_key, sign

    ap = argparse.ArgumentParser(prog="atlas-engine", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the engine loop and serve the operations API")
    r.add_argument("--config", default="config")
    r.add_argument("--state", required=True, help="engine state directory (journal, inbox, alerts)")
    r.add_argument("--tokens", required=True, help="engine token hash file")
    r.add_argument("--operator-key", help="operator HMAC key file (mode 600)")
    r.add_argument("--broker", choices=["mt5", "fake", "tradovate", "fake-tradovate"], default="mt5")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8742)
    r.add_argument("--poll", type=float, default=1.0, help="seconds between engine steps")
    r.add_argument("--allow-writable-config", action="store_true",
                   help="skip the read-only config check (development only; PRD §12 wants it on)")

    t = sub.add_parser("token", help="issue an engine API token; prints it once")
    t.add_argument("--tokens", required=True)
    t.add_argument("--name", required=True)
    t.add_argument("--scopes", required=True, help="comma-separated: " + ", ".join(ENGINE_SCOPES))

    o = sub.add_parser("operator", help="operator key and signed commands")
    o.add_argument("action", choices=["keygen", *ACTIONS])
    o.add_argument("--key", required=True, help="operator key file")
    o.add_argument("--inbox", help="the engine's operator-inbox directory")
    o.add_argument("--operator", help="who is signing")
    o.add_argument("--reason", help="why (at least 10 characters)")

    s = sub.add_parser("show", help="print the engine's saved state (stopped or running)")
    s.add_argument("--state", required=True)

    a = sub.add_parser("audit", help="reconstruct one decision and its trade from the journal (PRD v3 §31)")
    a.add_argument("--state", required=True)
    a.add_argument("decision_id", nargs="?", help="omit to list the latest decisions")
    a.add_argument("--full", action="store_true", help="every journal row, not just the summary")

    c = sub.add_parser("scorecard", help="closed trades in R after costs by setup, and Hermes's verdict")
    c.add_argument("--state", required=True)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "token":
        print(auth.issue(Path(args.tokens), args.name, [x.strip() for x in args.scopes.split(",") if x.strip()],
                         known=ENGINE_SCOPES))
    elif args.cmd == "operator":
        if args.action == "keygen":
            keygen(args.key)
            print(f"wrote {args.key}; keep it on the engine host, readable only by the operator and the engine user")
            return
        if not (args.inbox and args.operator and args.reason):
            raise SystemExit("--inbox, --operator and --reason are required to sign a command")
        cmd = sign(load_key(args.key), args.action, args.operator, args.reason)
        inbox = Path(args.inbox)
        tmp = inbox / f".{cmd['nonce']}.tmp"
        tmp.write_text(json.dumps(cmd))
        tmp.replace(inbox / f"{cmd['nonce']}.json")  # the engine never sees a half-written file
        print(f"queued {args.action}; the engine applies it on its next step (see its events)")
    elif args.cmd == "show":
        from atlas_engine.journal import Journal
        st = Journal(Path(args.state) / "journal.db", "show").load_state("engine") or {}
        print(json.dumps({k: st.get(k) for k in ("trading", "kill", "last_state", "last_reasons", "risk", "book",
                                                 "requests")}, indent=2, default=str))
    elif args.cmd == "audit":
        from atlas_engine.journal import Journal
        from atlas_engine.journal.audit import reconstruct
        j = Journal(Path(args.state) / "journal.db", "audit")
        if not args.decision_id:
            for r in j.rows("decisions", 20):
                print(f"{r['at']}  {r.get('decision', '-'):6}  {r.get('outcome', ''):12}  {r['decision_id']}")
            return
        rec = reconstruct(j, args.decision_id)
        print(json.dumps(rec if args.full else rec["summary"], indent=2, default=str))
    elif args.cmd == "scorecard":
        from atlas_engine.agent_intents import scorecard
        from atlas_engine.journal import Journal
        j = Journal(Path(args.state) / "journal.db", "scorecard")
        intents = {r["intent_id"]: r for r in j.rows("agent_actions", 100_000)
                   if r.get("action") == "submit_trade_intent" and r.get("intent_id")}
        print(json.dumps(scorecard(j.rows("trades", 100_000), intents), indent=2))
    elif args.cmd == "run":
        from .http import make_server
        engine = build_engine(args)
        server = make_server(EngineService(engine), TokenStore.load(Path(args.tokens), ENGINE_SCOPES),
                             args.host, args.port, routes=ENGINE_ROUTES)
        threading.Thread(target=server.serve_forever, name="ops-api", daemon=True).start()
        log.info("engine (%s) operations API on %s:%d, state %s", engine.source, args.host, args.port, args.state)
        try:
            engine.run_forever(args.poll)
        except KeyboardInterrupt:
            server.shutdown()
            sys.exit(0)


if __name__ == "__main__":
    main()
