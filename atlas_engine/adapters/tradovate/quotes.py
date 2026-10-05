"""Tradovate quotes over the market-data WebSocket (partner.tradovate.com, "Accessing Market Data").

Tradovate serves market data only over a WebSocket: open
``wss://md.tradovateapi.com/v1/websocket``, send ``authorize\\n<id>\\n\\n<md token>``,
then ``md/subscribequote\\n<id>\\n\\n{"symbol": "MESZ6"}`` per contract. Frames are
``o`` (open), ``h`` (server heartbeat), ``c`` (closed) or ``a[...]`` (a JSON array
of messages); quote events are ``{"e": "md", "d": {"quotes": [...]}}``. The client
sends ``[]`` every 2.5 s or the server drops the socket.

``QuoteBook`` turns quote events into ``Tick`` records and is what the adapter
reads; it is plain and tested. ``QuoteStream`` runs the socket in a thread and
needs the optional ``websocket-client`` package (``pip install .[tradovate]``),
imported only when it starts.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time

from atlas_engine.adapters.broker import BrokerUnavailable, Tick

HEARTBEAT_S = 2.5


def parse_frame(text: str) -> list[dict]:
    """The messages in one WebSocket frame (none for o / h / c frames)."""
    if not text or text[0] != "a":
        return []
    out = json.loads(text[1:])
    return [m for m in out if isinstance(m, dict)]


class QuoteBook:
    """Latest bid / ask / last / volume per contract, from quote events. Thread-safe."""

    def __init__(self, names: dict[int, str] | None = None):
        self.names = dict(names or {})  # Tradovate contract id -> contract name (MESZ6)
        self._ticks: dict[str, Tick] = {}
        self._lock = threading.Lock()

    def apply(self, msg: dict) -> None:
        if msg.get("e") != "md":
            return
        for q in (msg.get("d") or {}).get("quotes", []):
            name = self.names.get(q.get("contractId"))
            if name is None:
                continue
            e = q.get("entries") or {}
            with self._lock:
                old = self._ticks.get(name)
                bid = (e.get("Bid") or {}).get("price", old.bid if old else None)
                ask = (e.get("Offer") or {}).get("price", old.ask if old else None)
                if bid is None or ask is None:
                    continue
                last = (e.get("Trade") or {}).get("price", old.last if old else None)
                vol = (e.get("TotalTradeVolume") or {}).get("size", old.volume if old else None)
                when = dt.datetime.fromisoformat(q["timestamp"].replace("Z", "+00:00"))
                self._ticks[name] = Tick(name, when, float(bid), float(ask), last, vol)

    def tick(self, contract: str) -> Tick:
        with self._lock:
            t = self._ticks.get(contract)
        if t is None:
            raise BrokerUnavailable(f"no Tradovate quote for {contract} yet")
        return t


class QuoteStream:
    """The market-data socket, in a background thread, feeding a ``QuoteBook``."""

    def __init__(self, book: QuoteBook, token, contracts: list[str], url: str):
        self.book, self.token, self.contracts, self.url = book, token, list(contracts), url
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self) -> None:
        try:
            import websocket  # noqa: F401  (optional dependency: websocket-client)
        except ImportError as e:
            raise BrokerUnavailable("Tradovate quotes need websocket-client: pip install '.[tradovate]'") from e
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="tradovate-quotes", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        import websocket

        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(self.url, timeout=HEARTBEAT_S / 2)
                beat, n = time.monotonic(), 1
                ws.send(f"authorize\n{n}\n\n{self.token()}")
                for c in self.contracts:
                    n += 1
                    ws.send(f"md/subscribequote\n{n}\n\n{json.dumps({'symbol': c})}")
                while not self._stop.is_set():
                    try:
                        frame = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        frame = ""
                    if frame == "c":
                        break
                    for m in parse_frame(frame):
                        self.book.apply(m)
                    if time.monotonic() - beat >= HEARTBEAT_S:
                        ws.send("[]")
                        beat = time.monotonic()
                self.error = None
            except Exception as e:  # noqa: BLE001 - reconnect on any socket failure; the engine sees stale ticks
                self.error = f"{type(e).__name__}: {e}"
                self._stop.wait(5)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001
                        pass
