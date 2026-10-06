"""NinjaTrader's MCP server: ATLAS's route to a free NinjaTrader demo account (docs/futures.md, "The MCP route").

NinjaTrader's REST API needs a funded live account and a paid API Access add-on; its MCP server
(docs.ninjatrader.com/mcp, beta, read 2026-10-06) works with an ordinary NinjaTrader login through
OAuth 2.1. This module is the connection only: sign-in, token refresh and tool calls. What ATLAS may
do through it is decided here, not by the server:

- Demo only. ``SERVERS`` has no live entry, so nothing in ATLAS can reach the live MCP server.
- ``READ_TOOLS`` may be called by anything holding a client. ``ORDER_TOOLS`` only by a client built
  with ``orders=True``, which only the futures venue does, behind the engine's decision pipeline.
  ``NEVER_TOOLS`` (risk settings, alerts) are refused always: ATLAS never changes account risk.

What is VERIFIED (docs and live probes, 2026-10-06): the server URL, protected-resource and
authorization-server metadata (dynamic client registration, PKCE S256, public clients), the token
endpoint, the refresh body (JSON with ``resource``; omitting it gives a token the server rejects),
rotating refresh tokens, the tool names and inputs, Streamable HTTP. What is UNVERIFIED until the
first sign-in: whether a free simulation login is accepted, and the shape of every tool's answer.
``capture`` records those answers so the venue is built on what the server actually returns.

Tokens live only in the engine's state directory (mode 600). They are never logged, journaled, put
in an error message or shown to Hermes.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .client import NinjaTraderError

SERVERS = {"demo": "https://mcp-demo.tradovateapi.com/mcp"}  # no live entry: demo first (docs/futures.md)
PROTOCOL_VERSION = "2025-06-18"
SCOPE = "mcp:connect"
# NinjaTrader accepts only allow-listed return addresses. "localhost" worked on 2026-10-06; two earlier tries with
# "127.0.0.1" ended in access_denied (cause not certain: the device used may also have mattered).
REDIRECT = "http://localhost:{port}/callback"
REFRESH_BEFORE_S = 10 * 60
CLIENT_NAME = "ATLAS engine"

READ_TOOLS = frozenset({
    "describe", "my_portfolio", "user_profile", "search_contracts", "market_snapshot", "market_history",
    "dom_snapshot", "estimate_order", "order_history", "fill_history", "order_details", "position_history",
    "cash_history", "daily_balance_history", "performance_summary", "risk_settings", "economic_calendar",
    "earnings_calendar"})
ORDER_TOOLS = frozenset({"place_order", "modify_order", "cancel_order", "close_position"})
NEVER_TOOLS = frozenset({"update_risk_settings", "create_alert", "list_alerts", "dismiss_alert"})


class McpError(NinjaTraderError):
    """The MCP server did not give a usable answer, or the call was refused by ATLAS."""


class NoAnswer(McpError):
    """A call got no usable answer (network, server error): a write may or may not have reached the platform."""


class SignInNeeded(McpError):
    """No usable token: the operator must sign in again (``atlas-engine ninjatrader-mcp login``)."""


def urllib_http(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, dict, bytes]:
    """One HTTP call: (status, lower-cased headers, body). Raises OSError when no answer came back."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


def _json(raw: bytes) -> dict:
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


@dataclass
class Token:
    client_id: str
    resource: str
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0

    def __repr__(self) -> str:  # never print a secret
        return f"Token(client_id={self.client_id!r}, resource={self.resource!r}, expires_at={self.expires_at:.0f})"


class TokenStore:
    """One JSON file in the engine's state directory, readable by its owner only."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Token | None:
        if not self.path.exists():
            return None
        try:
            return Token(**json.loads(self.path.read_text()))
        except (ValueError, TypeError):
            return None

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(token), f)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


@dataclass(frozen=True)
class LoginRequest:
    url: str  # open this in a desktop browser
    state: str
    verifier: str
    redirect_uri: str
    client_id: str


class OAuth:
    """The MCP authorization flow (RFC 9728 discovery, RFC 7591 registration, PKCE) and token refresh."""

    def __init__(self, store: TokenStore, env: str = "demo", http=urllib_http, now=time.time, timeout: float = 20.0):
        if env not in SERVERS:
            raise McpError(f"no NinjaTrader MCP server for {env!r}: ATLAS connects to the demo server only")
        self.resource, self.store, self.http, self.now, self.timeout = SERVERS[env], store, http, now, timeout
        self._meta: dict | None = None

    def _get(self, url: str) -> dict:
        try:
            status, _, raw = self.http("GET", url, {"Accept": "application/json"}, None, self.timeout)
        except OSError as e:
            raise McpError(f"NinjaTrader sign-in server unreachable ({type(e).__name__})") from None
        if status != 200:
            raise McpError(f"NinjaTrader sign-in discovery answered HTTP {status}")
        return _json(raw)

    def metadata(self) -> dict:
        if self._meta is None:
            u = urllib.parse.urlsplit(self.resource)
            prm = self._get(f"{u.scheme}://{u.netloc}/.well-known/oauth-protected-resource{u.path}")
            if prm.get("resource") != self.resource or not prm.get("authorization_servers"):
                raise McpError("NinjaTrader MCP discovery did not name this server or an authorization server")
            issuer = prm["authorization_servers"][0]
            meta = self._get(issuer.rstrip("/") + "/.well-known/oauth-authorization-server")
            need = ("authorization_endpoint", "token_endpoint", "registration_endpoint")
            if meta.get("issuer") != issuer or any(not meta.get(k) for k in need) or \
                    "S256" not in meta.get("code_challenge_methods_supported", []):
                raise McpError("NinjaTrader authorization server metadata is missing what ATLAS needs")
            self._meta = meta
        return self._meta

    def _post(self, url: str, body: bytes, content_type: str) -> tuple[int, dict]:
        try:
            status, _, raw = self.http("POST", url, {"Content-Type": content_type, "Accept": "application/json"},
                                       body, self.timeout)
        except OSError as e:
            raise McpError(f"NinjaTrader sign-in server unreachable ({type(e).__name__})") from None
        return status, _json(raw)

    def register(self, redirect_uri: str) -> str:
        body = {"client_name": CLIENT_NAME, "redirect_uris": [redirect_uri], "grant_types": ["authorization_code",
                "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none", "scope": SCOPE}
        status, out = self._post(self.metadata()["registration_endpoint"], json.dumps(body).encode(),
                                 "application/json")
        if status not in (200, 201) or not out.get("client_id"):
            raise McpError(f"NinjaTrader refused to register ATLAS as a client (HTTP {status}, {out.get('error')})")
        return out["client_id"]

    def begin(self, redirect_uri: str) -> LoginRequest:
        """Register this redirect address and build the sign-in link (PKCE S256, random state)."""
        client_id = self.register(redirect_uri)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPE,
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": self.resource}
        base = self.metadata()["authorization_endpoint"]
        url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode(q)
        return LoginRequest(url, state, verifier, redirect_uri, client_id)

    def finish(self, req: LoginRequest, callback_url: str) -> Token:
        """Check the address the browser came back to and trade its one-time code for tokens."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(callback_url.strip()).query))
        if q.get("error"):
            raise SignInNeeded(f"sign-in was not completed: {q['error']}")
        if q.get("state") != req.state:
            raise SignInNeeded("the sign-in answer does not belong to this sign-in (state mismatch)")
        if "iss" in q and q["iss"] != self.metadata()["issuer"]:
            raise SignInNeeded("the sign-in answer came from an unexpected server")
        if not q.get("code"):
            raise SignInNeeded("the address has no sign-in code")
        form = {"grant_type": "authorization_code", "code": q["code"], "redirect_uri": req.redirect_uri,
                "client_id": req.client_id, "code_verifier": req.verifier, "resource": self.resource}
        status, out = self._post(self.metadata()["token_endpoint"], urllib.parse.urlencode(form).encode(),
                                 "application/x-www-form-urlencoded")
        token = self._token(Token(req.client_id, self.resource), status, out)
        if token is None:
            raise SignInNeeded(f"NinjaTrader did not issue a token (HTTP {status}, {out.get('error')})")
        self.store.save(token)
        return token

    def _token(self, base: Token, status: int, out: dict) -> Token | None:
        if status != 200 or not out.get("access_token"):
            return None
        return Token(base.client_id, base.resource, out["access_token"], out.get("refresh_token") or base.refresh_token,
                     self.now() + float(out.get("expires_in") or 0))

    def refresh(self) -> Token:
        """A fresh access token. A failed refresh keeps the saved token (the docs' third rule)."""
        saved = self.store.load()
        if saved is None or not saved.refresh_token:
            raise SignInNeeded("not signed in to NinjaTrader")
        if saved.resource != self.resource:
            raise SignInNeeded("the saved sign-in is for a different NinjaTrader server")
        body = {"grant_type": "refresh_token", "refresh_token": saved.refresh_token, "client_id": saved.client_id,
                "resource": self.resource}
        status, out = self._post(self.metadata()["token_endpoint"], json.dumps(body).encode(), "application/json")
        token = self._token(saved, status, out)
        if token is None:
            raise SignInNeeded(f"NinjaTrader refused to refresh the sign-in (HTTP {status}, {out.get('error')})")
        self.store.save(token)
        return token

    def access_token(self, force_refresh: bool = False) -> str:
        saved = self.store.load()
        if saved is None:
            raise SignInNeeded("not signed in to NinjaTrader")
        if force_refresh or saved.expires_at - self.now() < REFRESH_BEFORE_S:
            saved = self.refresh()
        return saved.access_token


class McpClient:
    """JSON-RPC over MCP Streamable HTTP, with one session and automatic token refresh."""

    def __init__(self, oauth: OAuth, orders: bool = False, http=None, timeout: float = 30.0):
        self.oauth, self.orders, self.timeout = oauth, orders, timeout
        self.http = http or oauth.http
        self.session_id: str | None = None
        self.server_info: dict = {}
        self._ids = 0
        self._lock = threading.RLock()  # a resend after an expired session re-initializes inside a call

    def _send(self, msg: dict, retried: bool = False) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + self.oauth.access_token(force_refresh=retried)}
        if self.session_id:
            headers.update({"Mcp-Session-Id": self.session_id, "MCP-Protocol-Version": PROTOCOL_VERSION})
        try:
            status, h, raw = self.http("POST", self.oauth.resource, headers, json.dumps(msg).encode(), self.timeout)
        except OSError as e:
            raise NoAnswer(f"NinjaTrader MCP server unreachable ({type(e).__name__})") from None
        if status == 401 and not retried:
            return self._send(msg, retried=True)
        if status == 404 and self.session_id and msg.get("method") != "initialize":
            self.session_id = None  # the server ended an idle session: start a new one and resend once
            self.initialize()
            return self._send(msg, retried=retried)
        if status == 401:
            raise SignInNeeded("NinjaTrader rejected the sign-in token")
        if status == 202:
            return None
        if status != 200:
            raise (NoAnswer if status >= 500 or status == 429 else McpError)(
                f"NinjaTrader MCP server answered HTTP {status}")
        if h.get("mcp-session-id"):
            self.session_id = h["mcp-session-id"]
        if "text/event-stream" in h.get("content-type", ""):
            return _from_stream(raw, msg.get("id"))
        return _json(raw)

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self._ids += 1
            msg = {"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params or {}}
            out = self._send(msg) or {}
        if out.get("error"):
            raise McpError(f"NinjaTrader MCP {method} failed: {out['error'].get('message', 'error')}")
        if "result" not in out:
            raise NoAnswer(f"NinjaTrader MCP {method} gave no result")
        return out["result"]

    def initialize(self) -> dict:
        res = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                       "clientInfo": {"name": "atlas-engine", "version": "3"}})
        self.server_info = res.get("serverInfo", {})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    def _ready(self) -> None:
        if self.session_id is None:
            self.initialize()

    def list_tools(self) -> list[dict]:
        self._ready()
        tools, cursor = [], None
        while True:
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call(self, tool: str, args: dict | None = None) -> dict:
        """One tool call. Returns the tool's structured answer, or its text parsed as JSON, or {"text": ...}."""
        if tool in NEVER_TOOLS:
            raise McpError(f"ATLAS never calls {tool}")
        if tool in ORDER_TOOLS and not self.orders:
            raise McpError(f"{tool} is an order tool; only the engine's futures venue may call it")
        if tool not in READ_TOOLS | ORDER_TOOLS:
            raise McpError(f"{tool} is not a NinjaTrader tool ATLAS knows")
        self._ready()
        res = self._rpc("tools/call", {"name": tool, "arguments": args or {}})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise McpError(f"NinjaTrader {tool} refused: {text[:300] or 'no reason given'}")
        if isinstance(res.get("structuredContent"), dict):
            return res["structuredContent"]
        try:
            out = json.loads(text)
            return out if isinstance(out, dict) else {"items": out}
        except ValueError:
            return {"text": text}


def _from_stream(raw: bytes, want_id) -> dict:
    """The JSON-RPC answer with ``want_id`` from a server-sent-event body."""
    for block in re.split(r"\r?\n\r?\n", raw.decode("utf-8", "replace")):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if not data:
            continue
        msg = _json(data.encode())
        if msg.get("id") == want_id and ("result" in msg or "error" in msg):
            return msg
    return {}


def wait_for_callback(port: int, timeout_s: float = 600.0) -> str:
    """Listen on this computer (``localhost``) for the browser's return from the sign-in page; returns the full address."""
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if urllib.parse.urlsplit(self.path).path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            got["url"] = REDIRECT.format(port=port).replace("/callback", "") + self.path
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ATLAS received the NinjaTrader sign-in. You can close this tab.")

        def log_message(self, *a):  # the address carries a one-time code: keep it out of logs
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    srv.timeout = 1.0
    end = time.monotonic() + timeout_s
    try:
        while "url" not in got and time.monotonic() < end:
            srv.handle_request()
    finally:
        srv.server_close()
    if "url" not in got:
        raise SignInNeeded("no sign-in arrived in time")
    return got["url"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


CAPTURE_PRODUCT = "MES"


def _arg_for(schema: dict, *names: str) -> str | None:
    props = (schema or {}).get("properties", {})
    return next((n for n in names if n in props), None)


def capture(client: McpClient, out_dir: Path, product: str = CAPTURE_PRODUCT) -> dict:
    """Record what the server actually answers, read tools only, so the venue is built on real shapes.

    Writes one JSON file per call under ``out_dir`` and returns a summary {call: "ok" | error}. Account
    numbers appear in the files; tokens never do. Nothing here can place, change or cancel an order.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict = {}

    def keep(name: str, fn):
        try:
            data = fn()
            summary[name] = "ok"
        except McpError as e:
            data, summary[name] = {"error": str(e)}, str(e)
        (out_dir / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))
        return data

    keep("initialize", client.initialize)
    tools = keep("tools_list", lambda: {"tools": client.list_tools()}).get("tools", [])
    schemas = {t.get("name"): t.get("inputSchema", {}) for t in tools}
    index = keep("describe_index", lambda: client.call("describe", {"topic": "index"}))
    topics = sorted(set(re.findall(r"`([a-z][a-z0-9_]{2,40})`", json.dumps(index))))[:40]
    for topic in topics:
        if topic != "index":
            keep(f"describe_{topic}", lambda t=topic: client.call("describe", {"topic": t}))
    keep("my_portfolio", lambda: client.call("my_portfolio"))
    keep("risk_settings", lambda: client.call("risk_settings"))
    q = _arg_for(schemas.get("search_contracts", {}), "query", "text", "symbol", "name")
    if q:
        keep("search_contracts", lambda: client.call("search_contracts", {q: product}))
    s = _arg_for(schemas.get("market_snapshot", {}), "symbol", "symbols", "contract")
    if s:
        keep("market_snapshot", lambda: client.call("market_snapshot", {s: [product] if s == "symbols" else product}))
    h = _arg_for(schemas.get("market_history", {}), "symbol", "contract")
    if h:
        keep("market_history", lambda: client.call("market_history", {h: product}))
    for tool in ("order_history", "fill_history", "position_history"):
        keep(tool, lambda t=tool: client.call(t, {"startDate": "last 30 days"} if "startDate" in
                                              schemas.get(t, {}).get("properties", {}) else {}))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary
