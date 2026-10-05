"""Network transport for Jev on TypeSafe's System One API (PRD v3 §7).

``TypeSafeTransport`` plugs into ``JevAdapter`` like ``ReplayTransport``. The
adapter has already run the leakage guard, so the state this sends holds only
normalised numbers in ATR/R units, fixed labels and the setup name: no dates,
prices, symbols, news text, balances or credentials.

One request asks two questions about that state (docs.typesafe.ai/api):

- ``p_target_first``, a Noul. Its value, the probability of "yes", is the
  model's p_target_first.
- ``regime``, a Choice over ``REGIMES``. The chosen option is the regime; a
  low Choice confidence adds the reason code ``regime_uncertain``.

The answer is mapped back to the adapter's schema, with ``model_version`` set
from the response's ``model`` field. The adapter then refuses any answer from a
model other than the pinned one, so an alias moving to a new release can't
change answers silently.

The API key comes only from the engine host's environment
(``TYPESAFE_API_KEY``), never from the repository, a profile or a skill. There
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

from atlas_engine.decisions.state import REGIMES

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
API_KEY_ENV = "TYPESAFE_API_KEY"
DEFAULT_MODEL = "jev-1.13.0"  # pinned; jev-latest is refused live by the adapter
REGIME_UNCERTAIN = 0.5  # Choice confidence below this adds "regime_uncertain"

_TREND = {"trend": "a directional, trending market", "range": "a sideways, ranging market"}
_VOL = {"low": "low volatility", "normal": "normal volatility", "high": "high volatility"}


def _regime_text(label: str) -> str:
    trend, vol, _ = label.split("_")
    return f"{_TREND[trend]} with {_VOL[vol]}"


def typesafe_questions(questions: dict) -> dict:
    """The adapter's pinned question text as typed System One questions."""
    return {
        "p_target_first": {
            "type": "noul",
            "instructions": {
                "question": questions["p_target_first"],
                "units": "Distances are in ATR multiples; `target_r` and `cost_r` are in R, where 1R is the "
                         "distance from entry to stop.",
            },
            "criteria": {
                "true": "Price reaches the target before the stop",
                "false": "Price reaches the stop before the target",
            },
        },
        "regime": {
            "type": "choice",
            "instructions": questions["regime"],
            "criteria": {r: _regime_text(r) for r in REGIMES},
        },
    }


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
    api_key: str = field(repr=False)
    timeout_s: float = 0.5
    url: str = ENDPOINT
    post: Callable[[str, bytes, dict, float], tuple[int, bytes]] = _urllib_post
    usage: dict = field(default_factory=lambda: {"requests": 0, "input_tokens": 0, "output_tokens": 0})

    def __post_init__(self):
        if not self.api_key:
            raise ValueError(f"no TypeSafe API key; set {API_KEY_ENV} on the engine host")
        if not self.url.startswith("https://"):
            raise ValueError("the TypeSafe endpoint must be https")

    @classmethod
    def from_env(cls, timeout_s: float = 0.5, env: dict | None = None) -> "TypeSafeTransport | None":
        """A transport when the host has a key, else ``None`` (Jev stays unloaded)."""
        key = (env if env is not None else os.environ).get(API_KEY_ENV, "").strip()
        return cls(key, timeout_s) if key else None

    def body(self, request: dict) -> dict:
        return {"state": request["state"], "model": request["model_version"],
                "questions": typesafe_questions(request["questions"])}

    def __call__(self, request: dict) -> dict:
        payload = json.dumps(self.body(request)).encode()
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        status, raw = self.post(self.url, payload, headers, self.timeout_s)
        if status != 200:
            raise TypeSafeError(f"http_{status}")
        return self.parse(request["setup_id"], json.loads(raw))

    def parse(self, setup_id: str, resp: dict) -> dict:
        """Map a System One response onto the adapter's answer schema; the adapter validates ranges."""
        try:
            answers = resp["answers"]
            noul, choice = answers["p_target_first"], answers["regime"]
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
