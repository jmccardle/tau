"""E6 §2 (S41) — headless ``session_start`` / ``session_shutdown`` wiring.

``run_print`` fires the two lifecycle hooks at the right moments:
  * ``session_start`` once extensions are loaded (before the turn), and
  * ``session_shutdown`` on COMPLETION *and* on SIGINT / SIGTERM.

The lifecycle seam is resolved via ``getattr`` on the backend, so these tests use
a fake backend that records the calls (and, for the signal cases, blocks in
``stream_chat`` until ``abort()`` trips — exactly how a real in-flight turn
unwinds when a signal lands). Sending a real signal to our own process is safe:
``run_print`` installs asyncio loop signal handlers, which replace the default
disposition (no ``KeyboardInterrupt`` / no termination) while the run is live.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S41 (anchor G1).
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

import tau_coding_agent.session_store as store
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import run_print


def _config() -> dict:
    return {
        "models": {
            "local-llm": {
                "backend": "openai",
                "model": "qwen3-32b-kv4b",
                "base_url": "http://localhost:8080/v1",
                "api_key": "not-needed",
            },
        },
        "default_model": "local-llm",
        "system_prompt": "You are helpful.",
    }


class _LifecycleBackend:
    """A fake backend that records lifecycle-hook calls and (optionally) blocks in
    ``stream_chat`` until ``abort()`` is tripped (to exercise the signal path)."""

    def __init__(self, config, *, block: bool = False):
        self.config = config
        self.events: list[str] = []
        self._block = block
        self.started = asyncio.Event()
        self._aborted = asyncio.Event()

    async def load_extensions(
        self, explicit_paths=None, *, discover=True, user_dir=None, extensions_config=None
    ):
        from tau_agent_core.sdk import LoadExtensionsResult

        return LoadExtensionsResult()

    async def emit_session_start(self, reason: str = "startup") -> None:
        self.events.append(f"start:{reason}")

    async def emit_session_shutdown(self, reason: str = "quit") -> None:
        self.events.append(f"shutdown:{reason}")

    def abort(self) -> None:
        self._aborted.set()

    async def stream_chat(self, messages, callback, on_event=None, on_pi_event=None):
        self.started.set()
        if self._block:
            await self._aborted.wait()
        callback("ANSWER")
        new_messages = [{"role": "assistant", "content": [{"type": "text", "text": "ANSWER"}]}]
        return "ANSWER", {"total_tokens": 1}, new_messages, []


@pytest.fixture
def env(monkeypatch, tmp_path):
    holder: dict = {}

    def make_factory(*, block: bool):
        def factory(config):
            be = _LifecycleBackend(config, block=block)
            holder["backend"] = be
            return be

        return factory

    holder["install"] = lambda block=False: monkeypatch.setattr(
        "tau_coding_agent.backends.create_backend", make_factory(block=block)
    )
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    return holder


# ── completion path ──────────────────────────────────────────────────────────


async def test_start_then_shutdown_fire_on_completion(env):
    """A normal headless run fires session_start (after load) then session_shutdown
    (on completion), in that order."""
    env["install"]()
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True), _config())
    assert rc == 0
    assert env["backend"].events == ["start:startup", "shutdown:quit"]


async def test_shutdown_fires_once_even_when_run_raises(env, monkeypatch):
    """If the turn raises, session_shutdown still fires (teardown in ``finally``)."""
    env["install"]()

    def factory(config):
        be = _LifecycleBackend(config)

        async def boom(*a, **k):
            raise RuntimeError("stream exploded")

        be.stream_chat = boom  # type: ignore[method-assign]
        env["backend"] = be
        return be

    monkeypatch.setattr("tau_coding_agent.backends.create_backend", factory)

    with pytest.raises(RuntimeError, match="stream exploded"):
        await run_print(CLIArgs(messages=["hi"], print_mode=True), _config())

    assert env["backend"].events == ["start:startup", "shutdown:quit"]


# ── signal paths ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_shutdown_fires_on_signal(env, sig):
    """SIGINT / SIGTERM trip abort → the blocked turn unwinds → session_shutdown
    fires. The loop signal handler replaces the default disposition, so signalling
    our own process is safe."""
    env["install"](block=True)

    run_task = asyncio.ensure_future(run_print(CLIArgs(messages=["hi"], print_mode=True), _config()))

    # Wait until the (blocking) stream has started — the loop signal handlers are
    # installed before stream_chat runs, so the signal cannot slip past them.
    for _ in range(200):
        be = env.get("backend")
        if be is not None and be.started.is_set():
            break
        await asyncio.sleep(0.005)
    assert env["backend"].started.is_set(), "stream never started"

    os.kill(os.getpid(), sig)

    rc = await asyncio.wait_for(run_task, timeout=5)
    assert rc == 0
    assert env["backend"].events == ["start:startup", "shutdown:quit"]
