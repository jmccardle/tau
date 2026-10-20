"""#69 in the heads: a model with no key is offered, labelled, and its 401 is one line (docs/PROVIDER-CREDENTIALS.md)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tau_agent_core.flows import enumerate_domain
from tau_coding_agent.backends import make_model_resolver

_REPO = Path(__file__).resolve().parents[2]
_TREES = ["tau-llm", "tau-agent-core", "tau-coding-agent", "tau-jmfts", "tau-meta"]
_KEY_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def _config(base_url: str = "http://127.0.0.1:1") -> dict:
    return {
        "default_model": "keyless",
        "models": {
            "keyless": {"backend": "anthropic", "model": "claude-x", "base_url": base_url},
            "keyed": {"backend": "anthropic", "model": "claude-y", "api_key": "k"},
            "broken": {"backend": "no-such-provider", "model": "z"},
        },
    }


@pytest.fixture
def no_key_env(monkeypatch):
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def test_the_picker_offers_a_keyless_model_and_labels_it(no_key_env):
    resolver = make_model_resolver(_config())
    assert resolver.models_without_key() == ["keyless"]
    labels = {v.value: v.label for v in enumerate_domain("model_name", models=resolver).values}
    assert labels == {"keyed": "keyed", "keyless": "keyless (no API key)"}


def test_an_environment_key_clears_the_label(no_key_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert make_model_resolver(_config()).models_without_key() == []


class _Refuses401(BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []

    def do_POST(self) -> None:  # noqa: N802 — the stdlib's name
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        body = json.dumps(
            {"type": "error", "error": {"type": "authentication_error", "message": "key required"}}
        ).encode()
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def test_print_mode_sends_without_a_key_and_reports_the_401_in_one_line(tmp_path):
    pytest.importorskip("anthropic")
    server = HTTPServer(("127.0.0.1", 0), _Refuses401)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp_path / "config.json").write_text(
            json.dumps(_config(f"http://127.0.0.1:{server.server_port}"))
        )
        env = {k: v for k, v in os.environ.items() if k not in (*_KEY_VARS, "TAU_PORTABLE_BASE")}
        env["TAU_HOME"] = str(tmp_path)
        env["ANTHROPIC_AUTH_TOKEN"] = "must-not-be-sent"
        env["PYTHONPATH"] = os.pathsep.join(str(_REPO / t / "src") for t in _TREES)
        proc = subprocess.run(
            [sys.executable, "-m", "tau_coding_agent.cli", "-p", "--no-session", "hi"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    finally:
        server.shutdown()
    assert proc.returncode == 1, proc.stderr
    assert "Traceback" not in proc.stderr
    [line] = proc.stderr.strip().splitlines()
    assert line.startswith("tau: error: HTTP 401 from model 'claude-x'")
    assert line.endswith(
        'key required. To send a key, set ANTHROPIC_API_KEY or "api_key" on the model\'s '
        '"models" entry in τ\'s config.json.'
    )
    [headers] = _Refuses401.seen
    assert "x-api-key" not in headers and "authorization" not in headers
