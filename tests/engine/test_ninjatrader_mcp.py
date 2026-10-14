"""The NinjaTrader MCP connection: sign-in, refresh, tool calls and what ATLAS refuses to call."""

import argparse
import base64
import hashlib
import io
import json
import stat
import urllib.parse

import pytest

from atlas_engine.adapters.ninjatrader import mcp

RESOURCE = mcp.SERVERS["demo"]
ISSUER = "https://demo.tradovateapi.com"


class FakeServer:
    """Discovery, registration, token endpoint and an MCP endpoint, as the docs describe them."""

    def __init__(self, sse: bool = False):
        self.sse, self.calls, self.tool_calls, self.requests = sse, [], [], []
        self.access, self.refresh_n, self.sessions = "at-1", 0, set()
        self.refuse_refresh, self.expire_session, self.reject_next = False, False, False
        self.codes: dict = {}
        self.opened = 0

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append((method, url, headers, body))
        if url.endswith("/.well-known/oauth-protected-resource/mcp"):
            return 200, {}, json.dumps({"resource": RESOURCE, "authorization_servers": [ISSUER],
                                        "scopes_supported": ["mcp:connect"]}).encode()
        if url == ISSUER + "/.well-known/oauth-authorization-server":
            return 200, {}, json.dumps({"issuer": ISSUER, "authorization_endpoint": "https://web.ninjatrader.com/oauth?env=demo",
                                        "token_endpoint": ISSUER + "/auth/oauthtoken",
                                        "registration_endpoint": ISSUER + "/auth/register",
                                        "code_challenge_methods_supported": ["S256"]}).encode()
        if url == ISSUER + "/auth/register":
            req = json.loads(body)
            assert req["token_endpoint_auth_method"] == "none"
            return 201, {}, json.dumps({"client_id": "cid-1", "redirect_uris": req["redirect_uris"]}).encode()
        if url == ISSUER + "/auth/oauthtoken":
            if headers["Content-Type"].startswith("application/x-www-form"):
                f = dict(urllib.parse.parse_qsl(body.decode()))
                want = self.codes.get(f["code"])
                ok = want and base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"].encode()).digest()) \
                    .rstrip(b"=").decode() == want and f["resource"] == RESOURCE
                if not ok:
                    return 400, {}, b'{"error":"invalid_grant"}'
                return 200, {}, json.dumps({"access_token": self.access, "refresh_token": "rt-0",
                                            "expires_in": 4800}).encode()
            f = json.loads(body)
            if self.refuse_refresh or f.get("resource") != RESOURCE or f["refresh_token"] != f"rt-{self.refresh_n}":
                return 400, {}, b'{"error":"invalid_grant"}'
            self.refresh_n += 1
            self.access = f"at-{self.refresh_n + 1}"
            return 200, {}, json.dumps({"access_token": self.access, "refresh_token": f"rt-{self.refresh_n}",
                                        "expires_in": 4800}).encode()
        if url == RESOURCE:
            return self._mcp(headers, json.loads(body))
        return 404, {}, b""

    def _mcp(self, headers, msg):
        if headers.get("Authorization") != "Bearer " + self.access or self.reject_next:
            self.reject_next = False
            return 401, {"www-authenticate": 'Bearer error="invalid_token"'}, b'{"error":"invalid_token"}'
        method = msg["method"]
        self.calls.append(method)
        if method == "initialize":
            self.opened += 1
            sid = f"s{self.opened}"
            self.sessions.add(sid)
            return self._answer(msg, {"protocolVersion": msg["params"]["protocolVersion"],
                                      "serverInfo": {"name": "ninjatrader-mcp"}}, {"mcp-session-id": sid})
        sid = headers.get("Mcp-Session-Id")
        if self.expire_session or sid not in self.sessions:
            self.expire_session = False
            self.sessions.discard(sid)
            return 404, {}, b""
        if method == "notifications/initialized":
            return 202, {}, b""
        if method == "tools/list":
            return self._answer(msg, {"tools": [
                {"name": "search_contracts", "inputSchema": {"properties": {"query": {}}}},
                {"name": "market_snapshot", "inputSchema": {"properties": {"symbol": {}}}},
                {"name": "order_history", "inputSchema": {"properties": {"startDate": {}}}}]})
        if method == "tools/call":
            name, args = msg["params"]["name"], msg["params"]["arguments"]
            self.tool_calls.append((name, args))
            if name == "describe":
                text = "Topics: `place_order_examples`, `my_portfolio`" if args["topic"] == "index" else "shape"
                return self._answer(msg, {"content": [{"type": "text", "text": text}]})
            if name == "market_snapshot" and args.get("symbol") == "BAD":
                return self._answer(msg, {"isError": True, "content": [{"type": "text", "text": "unknown symbol"}]})
            if name == "my_portfolio":
                return self._answer(msg, {"structuredContent": {"accounts": [{"name": "DEMO123"}]},
                                          "content": [{"type": "text", "text": "{}"}]})
            return self._answer(msg, {"content": [{"type": "text", "text": json.dumps({"tool": name})}]})
        return self._answer(msg, None, error={"code": -32601, "message": "no such method"})

    def _answer(self, msg, result, extra=None, error=None):
        body = {"jsonrpc": "2.0", "id": msg["id"], **({"error": error} if error else {"result": result})}
        h = dict(extra or {})
        if self.sse:
            h["content-type"] = "text/event-stream"
            note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
            raw = f"event: message\ndata: {json.dumps(note)}\n\nevent: message\ndata: {json.dumps(body)}\n\n"
            return 200, h, raw.encode()
        h["content-type"] = "application/json"
        return 200, h, json.dumps(body).encode()


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def signed_in(tmp_path, sse=False):
    srv, clock = FakeServer(sse), Clock()
    oauth = mcp.OAuth(mcp.TokenStore(tmp_path / "tok.json"), http=srv, now=clock)
    req = oauth.begin("http://localhost:9999/callback")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.url).query))
    srv.codes["code-1"] = q["code_challenge"]
    oauth.finish(req, f"http://localhost:9999/callback?code=code-1&state={req.state}&iss={ISSUER}")
    return srv, clock, oauth


def test_the_sign_in_link_uses_pkce_the_mcp_scope_and_this_server_as_the_audience(tmp_path):
    srv = FakeServer()
    req = mcp.OAuth(mcp.TokenStore(tmp_path / "t.json"), http=srv).begin("http://localhost:9999/callback")
    assert req.url.startswith("https://web.ninjatrader.com/oauth?env=demo&")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.url).query))
    assert q["code_challenge_method"] == "S256" and q["scope"] == "mcp:connect" and q["resource"] == RESOURCE
    assert q["client_id"] == "cid-1" and q["state"] == req.state and "code_verifier" not in q
    assert q["code_challenge"] == base64.urlsafe_b64encode(hashlib.sha256(req.verifier.encode()).digest()) \
        .rstrip(b"=").decode()


def test_atlas_has_no_route_to_the_live_mcp_server(tmp_path):
    assert set(mcp.SERVERS) == {"demo"}
    with pytest.raises(mcp.McpError, match="demo server only"):
        mcp.OAuth(mcp.TokenStore(tmp_path / "t.json"), env="live")


def test_a_sign_in_answer_from_another_sign_in_or_server_is_refused(tmp_path):
    srv = FakeServer()
    oauth = mcp.OAuth(mcp.TokenStore(tmp_path / "t.json"), http=srv)
    req = oauth.begin("http://localhost:9999/callback")
    for url, why in [("http://localhost:9999/callback?code=c&state=other", "state mismatch"),
                     (f"http://localhost:9999/callback?code=c&state={req.state}&iss=https://evil.example", "unexpected"),
                     ("http://localhost:9999/callback?error=access_denied", "access_denied"),
                     (f"http://localhost:9999/callback?state={req.state}", "no sign-in code"),
                     (f"http://localhost:9999/callback?code=wrong&state={req.state}", "did not issue")]:
        with pytest.raises(mcp.SignInNeeded, match=why):
            oauth.finish(req, url)
    assert not (tmp_path / "t.json").exists()


def test_the_token_file_is_private_and_never_printed(tmp_path):
    _, _, oauth = signed_in(tmp_path)
    path = tmp_path / "tok.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    tok = oauth.store.load()
    assert tok.access_token == "at-1" and "at-1" not in repr(tok) and "rt-0" not in repr(tok)


def test_refresh_sends_the_resource_and_stores_the_rotated_refresh_token(tmp_path):
    srv, clock, oauth = signed_in(tmp_path)
    clock.t += 4800 - 300  # inside the refresh window
    assert oauth.access_token() == "at-2"
    body = json.loads(srv.requests[-1][3])
    assert body == {"grant_type": "refresh_token", "refresh_token": "rt-0", "client_id": "cid-1", "resource": RESOURCE}
    assert oauth.store.load().refresh_token == "rt-1"
    clock.t += 4800 - 300
    assert oauth.access_token() == "at-3"  # the rotated token worked


def test_a_failed_refresh_keeps_the_saved_sign_in(tmp_path):
    srv, clock, oauth = signed_in(tmp_path)
    srv.refuse_refresh = True
    clock.t += 4800
    with pytest.raises(mcp.SignInNeeded, match="refused to refresh"):
        oauth.access_token()
    assert oauth.store.load().refresh_token == "rt-0"


@pytest.mark.parametrize("sse", [False, True])
def test_tool_answers_are_read_from_json_or_an_event_stream(tmp_path, sse):
    srv, _, oauth = signed_in(tmp_path, sse=sse)
    c = mcp.McpClient(oauth)
    assert c.call("my_portfolio") == {"accounts": [{"name": "DEMO123"}]}  # structured answer preferred
    assert c.call("order_history") == {"tool": "order_history"}  # text parsed as JSON
    assert c.call("describe", {"topic": "x"}) == {"text": "shape"}
    assert srv.calls[:2] == ["initialize", "notifications/initialized"] and c.session_id == "s1"
    with pytest.raises(mcp.McpError, match="unknown symbol"):
        c.call("market_snapshot", {"symbol": "BAD"})


def test_a_rejected_token_is_refreshed_once_and_an_ended_session_is_restarted(tmp_path):
    srv, _, oauth = signed_in(tmp_path)
    c = mcp.McpClient(oauth)
    c.initialize()
    srv.reject_next = True
    assert c.call("order_history") == {"tool": "order_history"}
    assert oauth.store.load().refresh_token == "rt-1"
    srv.expire_session = True
    assert c.call("order_history") == {"tool": "order_history"}
    assert c.session_id == "s2"


def test_order_tools_need_an_order_client_and_risk_and_alert_tools_are_never_called(tmp_path):
    srv, _, oauth = signed_in(tmp_path)
    reader = mcp.McpClient(oauth)
    for tool in sorted(mcp.ORDER_TOOLS):
        with pytest.raises(mcp.McpError, match="only the engine's futures venue"):
            reader.call(tool, {})
    for c in (reader, mcp.McpClient(oauth, orders=True)):
        for tool in sorted(mcp.NEVER_TOOLS) + ["pulse"]:
            with pytest.raises(mcp.McpError):
                c.call(tool, {})
    assert srv.tool_calls == []
    assert mcp.McpClient(oauth, orders=True).call("cancel_order", {"orderId": 1}) == {"tool": "cancel_order"}


def test_capture_records_read_answers_and_never_calls_an_order_tool(tmp_path):
    srv, _, oauth = signed_in(tmp_path)
    summary = mcp.capture(mcp.McpClient(oauth), tmp_path / "cap")
    called = [name for name, _ in srv.tool_calls]
    assert not set(called) & (mcp.ORDER_TOOLS | mcp.NEVER_TOOLS)
    assert ("search_contracts", {"query": "MES"}) in srv.tool_calls
    assert ("market_snapshot", {"symbol": "MES"}) in srv.tool_calls
    assert ("describe", {"topic": "place_order_examples"}) in srv.tool_calls
    assert summary["my_portfolio"] == "ok" and (tmp_path / "cap" / "describe_place_order_examples.json").exists()
    text = "".join(p.read_text() for p in (tmp_path / "cap").iterdir())
    assert "at-1" not in text and "rt-0" not in text


def test_the_cli_signs_in_with_a_pasted_address(tmp_path, monkeypatch):
    from atlas_api import engine_cli

    srv, printed = FakeServer(), []
    real = mcp.OAuth.__init__
    monkeypatch.setattr(mcp.OAuth, "__init__", lambda self, store, **kw: real(self, store, http=srv))

    class PastedAddress:  # the operator pastes the address the browser ended on, after the link was printed
        def readline(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(printed[1].strip()).query))
            srv.codes["code-9"] = q["code_challenge"]
            return f"http://localhost:5555/callback?code=code-9&state={q['state']}\n"

    args = argparse.Namespace(action="login", state=str(tmp_path), port=5555, paste=True)
    engine_cli.ninjatrader_mcp(args, stdin=PastedAddress(), out=lambda s: printed.extend(s.split("\n\n")))
    assert (tmp_path / "ninjatrader-mcp-token.json").exists()
    assert any("Signed in" in p for p in printed)
    assert not any("at-1" in p for p in printed)
