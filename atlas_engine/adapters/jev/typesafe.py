"""Network transport for Jev on TypeSafe's System One API (PRD v3 §7).

``TypeSafeTransport`` plugs into ``JevAdapter`` like ``ReplayTransport``. The
adapter has already run the leakage guard, so the state this sends holds only
normalised numbers in ATR/R units, fixed labels and the setup name: no dates,
prices, symbols, news text, balances or credentials.

One request asks two questions about that state (docs.typesafe.ai/api); their wording and the
threshold below live in ``questions.py``:

- ``p_target_first``, a Noul. Its value, the probability of "yes", is the
  model's p_target_first.
- ``regime``, a Choice over ``REGIMES``. The chosen option is the regime; a
  low Choice confidence adds the reason code ``regime_uncertain``.

The answer is mapped back to the adapter's schema, with ``model_version`` set
from the response's ``model`` field. The adapter then refuses any answer from a
model other than the pinned one, so an alias moving to a new release can't
change answers silently.

The API key comes only from the engine host's environment
(``TYPESAFE_API_KEY``), never from the repository, a profile or a skill. In a
Claude cloud environment that holds the key as a Bearer credential, the proxy
adds the header and ``ATLAS_TYPESAFE_PROXY_AUTH=1`` tells ATLAS to send none. There
is no retry: a 429, a 529 or a slow answer is a skip and the trade is
rejected, as PRD v3 §25 asks.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .questions import REGIME_UNCERTAIN

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
API_KEY_ENV = "TYPESAFE_API_KEY"
# Set to 1 where an outbound proxy adds the Authorization header itself (a Claude cloud environment holding
# the key as a Bearer credential for api.typesafe.ai): the key never enters the process, so none is sent.
PROXY_AUTH_ENV = "ATLAS_TYPESAFE_PROXY_AUTH"
DEFAULT_MODEL = "jev-1.13.0"  # pinned; jev-latest is refused live by the adapter

class TypeSafeError(RuntimeError):
    """The API answered with an error status or a body that isn't the documented shape."""


def _urllib_post(url: str, body: bytes, headers: dict, timeout_s: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - fixed https endpoint
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@dataclass
class TypeSafeTransport:
    api_key: str = field(repr=False)  # "" only with proxy_auth
    timeout_s: float = 0.5
    url: str = ENDPOINT
    post: Callable[[str, bytes, dict, float], tuple[int, bytes]] = _urllib_post
    usage: dict = field(default_factory=lambda: {"requests": 0, "input_tokens": 0, "output_tokens": 0})
    proxy_auth: bool = False

    def __post_init__(self):
        if not self.api_key and not self.proxy_auth:
            raise ValueError(f"no TypeSafe API key; set {API_KEY_ENV} on the engine host")
        if not self.url.startswith("https://"):
            raise ValueError("the TypeSafe endpoint must be https")

    @classmethod
    def from_env(cls, timeout_s: float = 0.5, env: dict | None = None) -> "TypeSafeTransport | None":
        """A transport when the host has a key (or a proxy that adds it), else ``None`` (Jev stays unloaded)."""
        env = env if env is not None else os.environ
        key = env.get(API_KEY_ENV, "").strip()
        if key:
            return cls(key, timeout_s)
        if env.get(PROXY_AUTH_ENV, "").strip() == "1":
            return cls("", timeout_s, proxy_auth=True)
        return None

    def body(self, request: dict) -> dict:
        return {"state": request["state"], "model": request["model_version"],
                "questions": request["questions"]}  # already typed System One questions (questions.py)

    def __call__(self, request: dict) -> dict:
        payload = json.dumps(self.body(request)).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        status, raw = self.post(self.url, payload, headers, self.timeout_s)
        if status != 200:
            raise TypeSafeError(f"http_{status}")
        nouls = [k for k, q in request["questions"].items() if q.get("type") == "noul"]
        return self.parse(request["setup_id"], json.loads(raw), nouls[0] if nouls else "p_target_first")

    def parse(self, setup_id: str, resp: dict, yes_id: str = "p_target_first") -> dict:
        """Map a System One response onto the adapter's answer schema; the adapter validates ranges.

        ``yes_id`` is the request's Noul (``p_target_first``, or ``p_profit`` for the research noise set);
        its probability is returned as ``p_target_first``.
        """
        try:
            answers = resp["answers"]
            noul, choice = answers[yes_id], answers["regime"]
            if noul.get("type") != "noul" or choice.get("type") != "choice":
                raise TypeSafeError("answer_type")
            codes = []
            conf = choice.get("confidence")
            if isinstance(conf, (int, float)) and conf < REGIME_UNCERTAIN:
                codes.append("regime_uncertain")
            usage = resp.get("usage") or {}
        except (KeyError, TypeError, AttributeError) as e:
            raise TypeSafeError(f"shape:{type(e).__name__}") from e
        self.usage["requests"] += 1
        for k in ("input_tokens", "output_tokens"):
            if isinstance(usage.get(k), int):
                self.usage[k] += usage[k]
        return {"setup_id": setup_id, "model_version": resp.get("model"), "p_target_first": noul.get("noul"),
                "regime": choice.get("choice"), "reason_codes": codes}
