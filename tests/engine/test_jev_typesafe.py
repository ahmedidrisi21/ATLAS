"""Jev on TypeSafe's System One API (PRD v3 §7, §24, §25): request shape, answer mapping, pinning, fail-safe."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pytest

from atlas_engine.adapters.jev import JevAdapter, JevResult, Skip, TypeSafeTransport
from atlas_engine.adapters.jev.typesafe import _urllib_post
from atlas_engine.decisions.state import REGIMES
from atlas_engine.models import ModelEstimate, ModelRegistry
from atlas_engine.pipeline import ConfigError, DecisionSettings, load_decision_settings

from t4help import TEST_PRIOR, build
from test_jev_adapter import STATE

MODEL = "jev-1.13.0"


def reply(model=MODEL, p=0.62, regime="trend_normal_vol", conf=0.8, **answers):
    return {"model": model, "usage": {"input_tokens": 400, "output_tokens": 30}, "answers": {
        "p_target_first": {"type": "noul", "noul": p},
        "regime": {"type": "choice", "choice": regime, "confidence": conf,
                   "probabilities": {r: (1.0 if r == regime else 0.0) for r in REGIMES}},
        **answers}}


class FakePost:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.calls = status, body if body is not None else reply(), []

    def __call__(self, url, payload, headers, timeout_s):
        self.calls.append((url, json.loads(payload), headers, timeout_s))
        return self.status, json.dumps(self.body).encode()


def jev(post, model=MODEL, **kw):
    return JevAdapter(TypeSafeTransport("ts-key", post=post), model, live=True, **kw)


def test_the_request_is_the_documented_shape_with_only_the_checked_state():
    post = FakePost()
    res = jev(post).evaluate_setup("trend_pullback:1", STATE, 2.0, 0.08)
    assert isinstance(res, JevResult)
    url, body, headers, timeout_s = post.calls[0]
    assert url == "https://api.typesafe.ai/v1/systemone" and headers["Authorization"] == "Bearer ts-key"
    assert timeout_s == 0.5
    assert set(body) == {"state", "model", "questions"} and body["model"] == MODEL
    assert body["state"]["target_r"] == 2.0 and body["state"]["cost_r"] == 0.08
    assert set(body["state"]) <= set(STATE) | {"target_r", "cost_r", "recent_signal_r20"}
    q = body["questions"]
    assert q["p_target_first"]["type"] == "noul" and set(q["p_target_first"]["criteria"]) == {"true", "false"}
    assert q["regime"]["type"] == "choice" and list(q["regime"]["criteria"]) == list(REGIMES)
    # the setup id never leaves the engine; nothing date- or symbol-like is in the payload
    text = json.dumps(body)
    assert "trend_pullback:1" not in text and "EURUSD" not in text and "ts-key" not in text


def test_answers_map_onto_the_bounded_schema():
    res = jev(FakePost(body=reply(p=0.41, regime="range_high_vol", conf=0.3))).evaluate_setup("S", STATE, 2.0, 0.08)
    assert isinstance(res, JevResult)
    assert (res.p_target_first, res.regime, res.model_version) == (0.41, "range_high_vol", MODEL)
    assert res.reason_codes == ("regime_uncertain",)


def test_usage_is_counted():
    post = FakePost()
    a = jev(post)
    a.evaluate_setup("S", STATE, 2.0, 0.08)
    assert a.transport.usage == {"requests": 1, "input_tokens": 400, "output_tokens": 30}


def test_an_answer_from_another_model_is_refused():
    # An alias that moved to a new release must not change answers silently.
    res = jev(FakePost(body=reply(model="jev-1.14.0"))).evaluate_setup("S", STATE, 2.0, 0.08)
    assert isinstance(res, Skip) and res.reason == "schema:model_version_mismatch"


@pytest.mark.parametrize("post,reason", [
    (FakePost(status=429, body={"error": "rate limited"}), "transport_error:TypeSafeError"),
    (FakePost(status=401, body={"error": "bad key"}), "transport_error:TypeSafeError"),
    (FakePost(body={"model": MODEL, "answers": {}}), "transport_error:TypeSafeError"),
    (FakePost(body=reply(p=1.7)), "out_of_range:p_target_first"),
    (FakePost(body=reply(regime="bull")), "schema:regime"),
])
def test_errors_and_bad_answers_are_skips_never_guesses(post, reason):
    res = jev(post).evaluate_setup("S", STATE, 2.0, 0.08)
    assert isinstance(res, Skip) and res.reason == reason


def test_an_alias_is_refused_live():
    with pytest.raises(ValueError):
        jev(FakePost(), model="jev-latest")


def test_the_key_comes_only_from_the_host_environment():
    assert TypeSafeTransport.from_env(env={}) is None
    assert TypeSafeTransport.from_env(env={"TYPESAFE_API_KEY": "  "}) is None
    t = TypeSafeTransport.from_env(timeout_s=0.25, env={"TYPESAFE_API_KEY": "ts-secret-9"})
    assert t.api_key == "ts-secret-9" and t.timeout_s == 0.25 and "ts-secret-9" not in repr(t)
    with pytest.raises(ValueError):
        TypeSafeTransport("")
    with pytest.raises(ValueError):
        TypeSafeTransport("k", url="http://api.typesafe.ai/v1/systemone")


def test_the_http_client_posts_json_and_returns_error_bodies():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["auth"] = self.headers["Authorization"]
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            code = 200 if seen["body"].get("ok") else 529
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'{"answer": 1}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/v1/systemone"
        assert _urllib_post(url, b'{"ok": true}', {"Authorization": "Bearer k"}, 2.0) == (200, b'{"answer": 1}')
        assert seen == {"auth": "Bearer k", "body": {"ok": True}}
        assert _urllib_post(url, b'{"ok": false}', {}, 2.0)[0] == 529
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- engine wiring

def test_the_engine_loads_jev_only_when_assigned_and_keyed(tmp_path):
    from atlas_api.engine_cli import load_jev
    from atlas_engine.calibration import Calibrator

    s = DecisionSettings()
    key = {"TYPESAFE_API_KEY": "k"}
    assert load_jev({"trend_pullback": "rules"}, s, tmp_path, env=key) == []
    assert load_jev({"trend_pullback": "jev"}, s, tmp_path, env={}) == []
    [m] = load_jev({"trend_pullback": "jev"}, s, tmp_path, env=key)
    assert m.name == "jev" and m.version == MODEL and m.calibrator is None and m.adapter.live
    rng = np.random.default_rng(0)
    raw = rng.uniform(size=300)
    cal = Calibrator.fit(raw, (rng.uniform(size=300) < raw).astype(int))
    (Path(tmp_path) / "jev_calibration.json").write_text(cal.to_json())
    [m] = load_jev({"trend_pullback": "jev"}, s, tmp_path, env=key)
    assert m.calibrator is not None


def test_jev_model_must_be_pinned_in_config(tmp_path):
    (tmp_path / "atlas.yaml").write_text("decision: {jev_model: jev-1.13.0}\n")
    assert load_decision_settings(tmp_path).jev_model == "jev-1.13.0"
    (tmp_path / "atlas.yaml").write_text("decision: {jev_model: jev-latest}\n")
    with pytest.raises(ConfigError):
        load_decision_settings(tmp_path)


class StubJev:
    name, version = "jev", MODEL

    def __init__(self, calibration):
        self.calibration = calibration

    def evaluate_setup(self, setup, state):
        return ModelEstimate(0.6, "jev", MODEL, self.calibration, regime="trend_normal_vol")


def _rig(tmp_path, calibration, demo):
    from atlas_engine.setups import SETUPS

    models = ModelRegistry.default({s: TEST_PRIOR for s in SETUPS}, {"trend_pullback": "jev"},
                                   extra=[StubJev(calibration)])
    r = build(tmp_path, models=models)
    r.mt5.demo = demo
    r.step()
    r.enable()
    return r


@pytest.mark.parametrize("calibration,demo,decision", [
    ("none", True, "ALLOW"),
    ("none", False, "REJECT"),
    ("iso-abc123", False, "ALLOW"),
])
def test_uncalibrated_jev_trades_a_demo_account_only(tmp_path, calibration, demo, decision):
    r = _rig(tmp_path, calibration, demo)
    out = r.engine.submit(r.signal())
    assert out["decision"] == decision, out
    if decision == "REJECT":
        assert out["reasons"] == ["uncalibrated_model_needs_demo_account"]
    else:
        assert out["pipeline"]["model"]["model"] == "jev"


def test_every_question_and_threshold_lives_in_one_file_and_every_state_field_is_explained():
    """TypeSafe's agent skill: keep questions and thresholds in one reviewable file; ids never reach the model."""
    from pathlib import Path

    from atlas_engine.adapters.jev import adapter as jev_adapter
    from atlas_engine.adapters.jev.questions import QUESTIONS, STATE_FIELDS

    pkg = Path(jev_adapter.__file__).parent
    others = [p.name for p in pkg.glob("*.py") if p.name != "questions.py"
              and any(w in p.read_text() for w in ('"instructions"', '"criteria"', "REGIME_UNCERTAIN ="))]
    assert others == []
    assert set(jev_adapter.REQUEST_KEYS) <= set(STATE_FIELDS)
    for q in QUESTIONS.values():
        assert q["instructions"]["question"].endswith("?") and q["instructions"]["fields"] is STATE_FIELDS



def test_behind_a_proxy_that_adds_the_key_no_key_is_needed_or_sent():
    """A Claude cloud environment can hold the key as a Bearer credential that its proxy adds to each request."""
    assert TypeSafeTransport.from_env(env={"ATLAS_TYPESAFE_PROXY_AUTH": "0"}) is None
    t = TypeSafeTransport.from_env(env={"ATLAS_TYPESAFE_PROXY_AUTH": "1"})
    assert t.proxy_auth and t.api_key == ""
    post = FakePost()
    t.post = post
    res = JevAdapter(t, MODEL, live=True).evaluate_setup("trend_pullback:1", STATE, 2.0, 0.08)
    assert isinstance(res, JevResult)
    assert "Authorization" not in post.calls[0][2]
    # a key on the host still wins over the switch
    assert TypeSafeTransport.from_env(env={"TYPESAFE_API_KEY": "k", "ATLAS_TYPESAFE_PROXY_AUTH": "1"}).api_key == "k"
    with pytest.raises(ValueError):
        TypeSafeTransport("")
