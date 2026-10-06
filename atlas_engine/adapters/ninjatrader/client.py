"""NinjaTrader API REST client. NinjaTrader's official API is the Tradovate API (api.tradovate.com spec,
read 2026-10-06; partner.tradovate.com, read 2026-10-05). Used by ``NinjaTraderAdapter`` only.

- Hosts: ``demo.tradovateapi.com/v1`` and ``live.tradovateapi.com/v1``. Which one
  is the account's demo flag: the engine refuses agent trades unless it is demo.
- Auth: ``POST /auth/accesstokenrequest`` gives an access token for about 80
  minutes; it is renewed with ``GET /auth/renewaccesstoken`` well before then,
  because a fresh request is limited (5 failed tries an hour).
- Rate limits: a request over the limit comes back as HTTP 200 carrying a
  penalty ticket (``p-ticket``, ``p-time`` seconds, sometimes ``p-captcha``).
  The client waits ``p-time`` and resends once with the ticket; a captcha or a
  second ticket stops it. HTTP 429 means wait an hour. Both raise ``RateLimited``.
- Credentials come only from the engine host's environment (``Credentials.from_env``);
  they are never logged, journaled or put in an error message.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from atlas_engine.adapters.broker import BrokerUnavailable

HOSTS = {"demo": "https://demo.tradovateapi.com/v1", "live": "https://live.tradovateapi.com/v1"}
MD_SOCKET = "wss://md.tradovateapi.com/v1/websocket"
RENEW_BEFORE = dt.timedelta(minutes=15)
MAX_PENALTY_WAIT_S = 60
ENV_VARS = {"name": "ATLAS_NINJATRADER_USER", "password": "ATLAS_NINJATRADER_PASSWORD", "app_id": "ATLAS_NINJATRADER_APP_ID",
            "app_version": "ATLAS_NINJATRADER_APP_VERSION", "cid": "ATLAS_NINJATRADER_CID", "sec": "ATLAS_NINJATRADER_SEC",
            "device_id": "ATLAS_NINJATRADER_DEVICE_ID"}


class NinjaTraderError(BrokerUnavailable):
    """The NinjaTrader API did not give a usable answer."""


class RateLimited(NinjaTraderError):
    pass


class OutcomeUnknown(NinjaTraderError):
    """A command was sent and no answer came back: it may or may not have reached the platform."""


@dataclass(frozen=True)
class Credentials:
    name: str
    password: str
    app_id: str
    app_version: str
    cid: str
    sec: str
    device_id: str

    @classmethod
    def from_env(cls, env=None) -> "Credentials":
        env = os.environ if env is None else env
        missing = [v for v in ENV_VARS.values() if not env.get(v)]
        if missing:
            raise ValueError("missing NinjaTrader API credentials in the engine host's environment: " + ", ".join(missing))
        return cls(**{k: env[v] for k, v in ENV_VARS.items()})

    def body(self) -> dict:
        return {"name": self.name, "password": self.password, "appId": self.app_id, "appVersion": self.app_version,
                "cid": self.cid, "sec": self.sec, "deviceId": self.device_id}

    def __repr__(self) -> str:  # never print a secret
        return f"Credentials(name={self.name!r}, app_id={self.app_id!r}, ...)"


def urllib_transport(method: str, url: str, headers: dict, body: dict | None, timeout: float) -> tuple[int, object]:
    """One HTTP call. Returns (status, parsed JSON). Raises OSError when no answer came back."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Accept": "application/json",
                                                                          "Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, None


def _parse_time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


class NinjaTraderClient:
    def __init__(self, env: str, credentials: Credentials, transport=urllib_transport, now=None, sleep=time.sleep,
                 timeout: float = 10.0):
        if env not in HOSTS:
            raise ValueError(f"NinjaTrader API environment must be one of {sorted(HOSTS)}")
        self.env, self.base = env, HOSTS[env]
        self._creds = credentials
        self.transport, self.sleep, self.timeout = transport, sleep, timeout
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.token: str | None = None
        self.md_token: str | None = None
        self.expires: dt.datetime | None = None
        self.user_id: int | None = None
        self.requests_sent = 0

    @property
    def demo(self) -> bool:
        return self.env == "demo"

    # -- auth -------------------------------------------------------------------------------------------

    def _take_token(self, r: dict) -> None:
        if not isinstance(r, dict) or r.get("errorText") or not r.get("accessToken"):
            # errorText is Tradovate's message (e.g. wrong password); it never contains the credentials.
            raise NinjaTraderError(f"the NinjaTrader API refused the login: {(r or {}).get('errorText') or 'no token returned'}")
        self.token = r["accessToken"]
        self.md_token = r.get("mdAccessToken") or self.md_token
        self.expires = _parse_time(r["expirationTime"]) if r.get("expirationTime") else self.now() + dt.timedelta(minutes=80)
        self.user_id = r.get("userId", self.user_id)

    def authenticate(self) -> None:
        self.token = None
        self._take_token(self._send("POST", "/auth/accesstokenrequest", self._creds.body(), auth=False))

    def _ensure_token(self) -> None:
        if self.token is None or self.expires is None or self.now() >= self.expires:
            self.authenticate()
        elif self.expires - self.now() < RENEW_BEFORE:
            try:
                self._take_token(self._send("GET", "/auth/renewaccesstoken", None))
            except NinjaTraderError:
                self.authenticate()

    # -- calls ------------------------------------------------------------------------------------------

    def _send(self, method: str, path: str, body: dict | None, auth: bool = True, params: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
        ticket = None
        for _ in range(2):
            payload = dict(body or {}, **({"p-ticket": ticket} if ticket else {})) if (body is not None or ticket) else None
            self.requests_sent += 1
            try:
                status, data = self.transport(method, url, headers, payload, self.timeout)
            except OSError as e:
                kind = OutcomeUnknown if method == "POST" else NinjaTraderError
                raise kind(f"{method} {path}: no answer ({type(e).__name__})") from None
            if status == 429:
                raise RateLimited(f"{path}: HTTP 429, Tradovate asks to wait an hour")
            if isinstance(data, dict) and data.get("p-ticket"):
                if data.get("p-captcha") or ticket is not None:
                    raise RateLimited(f"{path}: penalty ticket{' with captcha' if data.get('p-captcha') else ''}")
                wait = float(data.get("p-time") or 0)
                if wait > MAX_PENALTY_WAIT_S:
                    raise RateLimited(f"{path}: penalty ticket asks to wait {wait:.0f} s")
                ticket = data["p-ticket"]
                self.sleep(wait)
                continue
            if status == 401 and auth:
                raise NinjaTraderError(f"{path}: HTTP 401 (token rejected)")
            if status >= 400:
                raise NinjaTraderError(f"{method} {path}: HTTP {status}")
            return data
        raise RateLimited(f"{path}: still rate limited after the penalty wait")

    def call(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        self._ensure_token()
        try:
            return self._send(method, path, body, params=params)
        except NinjaTraderError as e:
            if "HTTP 401" not in str(e):
                raise
            self.authenticate()
            return self._send(method, path, body, params=params)

    def get(self, path: str, **params):
        return self.call("GET", path, params=params or None)

    def post(self, path: str, body: dict):
        return self.call("POST", path, body)
