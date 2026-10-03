"""``/compare`` in the TUI, in-process and under ``--connect`` (docs/TAU-SERVE.md §8).

Both paths reach the same core (``start_comparison``, ``Comparison.end``) and
draw the same :class:`CompareScreen`, built from the ``compare`` correlation the
turns carry rather than from who started them.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import patch

import pytest
from websockets.asyncio.server import serve as ws_serve

from test_serve_compare import CONFIG, fake_stream

from tau_coding_agent.compare_view import CompareScreen
from tau_coding_agent.serve.client import Address
from tau_coding_agent.serve.daemon import Daemon
from tau_coding_agent.serve.remote import RemoteBackend, RemoteConnection
from tau_coding_agent.session_store import FileSessionCatalog
from tau_coding_agent.testing.sandbox import build_tau_app


class _Submit:
    def __init__(self, value: str) -> None:
        self.value = value


async def _until(pilot: Any, predicate: Any, tries: int = 500) -> None:
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition never held")


def _texts(app: Any) -> list[str]:
    return [
        block["text"]
        for message in app.messages
        if message.get("role") == "assistant"
        for block in message["content"]
        if block.get("type") == "text"
    ]


async def test_compare_in_process_draws_one_column_per_model_and_keeps_one(make_app):
    app = make_app(config={**CONFIG, "system_prompt": "sys"})
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake_stream):
        async with app.run_test(size=(140, 36)) as pilot:
            await app.on_input_submitted(_Submit("first"))
            await _until(pilot, lambda: not app.is_generating and len(_texts(app)) == 1)
            before = list(app.messages)

            await app.on_input_submitted(_Submit("/compare a b -- side by side"))
            await _until(pilot, lambda: isinstance(app.screen, CompareScreen))
            screen = app.screen
            assert isinstance(screen, CompareScreen)
            await _until(pilot, lambda: all(c.finished for c in screen.columns))

            assert [c.text for c in screen.columns] == [
                "[a-model] side by side",
                "[b-model] side by side",
            ]
            assert app.messages == before, "the compare streams stay out of the transcript"
            comparison = app.current_backend.comparisons[screen.comparison_id]
            kept_leaf = comparison.cursors[1].leaf

            await pilot.press("2")
            await _until(pilot, lambda: not isinstance(app.screen, CompareScreen))

            assert app._cursor.leaf == kept_leaf
            assert _texts(app) == ["[base-model] first", "[b-model] side by side"]
            session = app.current_backend.agent_session
            assert session.cursors == (session.cursor,), "every compare cursor closed"
            assert app.current_backend.comparisons == {}


async def test_escape_keeps_none_and_leaves_the_head(make_app):
    app = make_app(config={**CONFIG, "system_prompt": "sys"})
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake_stream):
        async with app.run_test(size=(140, 36)) as pilot:
            await app.on_input_submitted(_Submit("first"))
            await _until(pilot, lambda: not app.is_generating and len(_texts(app)) == 1)
            head = app._cursor.leaf

            await app.on_input_submitted(_Submit("/compare a slow -- go"))
            await _until(pilot, lambda: isinstance(app.screen, CompareScreen))
            screen = app.screen
            assert isinstance(screen, CompareScreen)
            await _until(pilot, lambda: screen.columns[0].finished and screen.columns[1].text)

            await pilot.press("2")
            await pilot.pause()
            assert isinstance(app.screen, CompareScreen), "a running answer cannot be kept"

            await pilot.press("escape")
            await _until(pilot, lambda: not isinstance(app.screen, CompareScreen))
            assert app._cursor.leaf == head
            session = app.current_backend.agent_session
            assert session.cursors == (session.cursor,)


@pytest.fixture
async def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "daemon-home"))
    (tmp_path / "daemon-home").mkdir()
    served = Daemon(CONFIG, FileSessionCatalog(tmp_path / "sessions"), out=io.StringIO())
    server = await ws_serve(served.handle, "127.0.0.1", 0, max_size=None)
    address = Address("127.0.0.1", server.sockets[0].getsockname()[1])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake_stream):
        yield served, address
    server.close()
    await server.wait_closed()
    await served.shutdown()


async def test_compare_under_connect_runs_in_the_daemon_and_keeps_one(daemon, tau_home, tmp_path):
    served, address = daemon
    remote = RemoteConnection(address, cwd=str(tmp_path))
    app = build_tau_app(tau_home, remote=remote)
    async with app.run_test(size=(140, 36)) as pilot:
        await _until(pilot, lambda: remote._ready.is_set())
        await app.on_input_submitted(_Submit("first"))
        await _until(pilot, lambda: not app.is_generating and len(_texts(app)) == 1)
        assert isinstance(app.current_backend, RemoteBackend)

        await app.on_input_submitted(_Submit("/compare a b -- over the wire"))
        await _until(pilot, lambda: isinstance(app.screen, CompareScreen))
        screen = app.screen
        assert isinstance(screen, CompareScreen)
        await _until(pilot, lambda: all(c.finished for c in screen.columns))
        assert [c.text for c in screen.columns] == [
            "[a-model] over the wire",
            "[b-model] over the wire",
        ]
        (host,) = served.hosts.values()
        comparison = host.backend.comparisons[screen.comparison_id]
        kept_leaf = comparison.cursors[0].leaf

        await pilot.press("1")
        await _until(pilot, lambda: not isinstance(app.screen, CompareScreen))

        assert app._cursor.leaf == kept_leaf
        assert _texts(app) == ["[base-model] first", "[a-model] over the wire"]
        assert host.agent_session.cursors == (host.agent_session.cursor,)
    await remote.close()
