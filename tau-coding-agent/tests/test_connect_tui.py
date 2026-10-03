"""The TUI driving a daemon under ``tau --connect`` (docs/TAU-SERVE.md §7.1).

The daemon runs in the test's own event loop on an ephemeral port; the TUI is a
real ``TauApp`` under Textual's pilot, holding a :class:`RemoteConnection`.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from websockets.asyncio.server import serve as ws_serve

from tau_llm.streaming import DoneEvent, TextDeltaEvent, ThinkingDeltaEvent
from tau_llm.types import AssistantMessage, TextContent, ThinkingContent, ToolCall, Usage

from tau_coding_agent.serve import protocol as p
from tau_coding_agent.serve.client import Address, ServeClient
from tau_coding_agent.serve.daemon import Daemon
from tau_coding_agent.serve.remote import RemoteBackend, RemoteConnection
from tau_coding_agent.session_store import FileSessionCatalog, Session
from tau_coding_agent.testing.render import save_render
from tau_coding_agent.testing.sandbox import build_tau_app

SHOTS = Path(os.environ.get("TAU_SHOTS", "")) if os.environ.get("TAU_SHOTS") else None

_CONFIG: dict[str, Any] = {
    "default_model": "fake",
    "models": {
        "fake": {
            "backend": "openai",
            "model": "fake-model",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "x",
        },
        "other": {
            "backend": "openai",
            "model": "other-model",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "x",
        },
    },
}


class _Stream:
    def __init__(self, text: str) -> None:
        self._message = AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model="fake-model",
            stop_reason="stop",
            usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
        )

    def __aiter__(self):
        async def gen():
            yield TextDeltaEvent(delta=self._message.content[0].text, partial=self._message)
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return gen()

    def abort(self) -> None:
        pass


async def _fake_stream(model: Any, context: Any, options: Any = None) -> _Stream:
    last = context["messages"][-1]
    content = last.get("content") if isinstance(last, dict) else last.content
    first = content if isinstance(content, str) else content[0]
    text = first if isinstance(first, str) else getattr(first, "text", None) or first["text"]
    return _Stream(f"re: {text}")


class _Submit:
    def __init__(self, value: str) -> None:
        self.value = value


@pytest.fixture
async def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "daemon-home"))
    (tmp_path / "daemon-home").mkdir()
    out = io.StringIO()
    served = Daemon(_CONFIG, FileSessionCatalog(tmp_path / "sessions"), out=out)
    server = await ws_serve(served.handle, "127.0.0.1", 0, max_size=None)
    address = Address("127.0.0.1", server.sockets[0].getsockname()[1])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_stream):
        yield served, address
    server.close()
    await server.wait_closed()
    await served.shutdown()


async def _until(pilot, predicate, tries: int = 400) -> None:
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition never held")


async def test_a_prompt_from_the_tui_runs_in_the_daemon(daemon, tau_home, tmp_path):
    served, address = daemon
    remote = RemoteConnection(address, cwd=str(tmp_path))
    app = build_tau_app(tau_home, remote=remote)
    async with app.run_test() as pilot:
        await _until(pilot, lambda: remote._ready.is_set())
        await app.on_input_submitted(_Submit("hello daemon"))
        await _until(pilot, lambda: not app.is_generating and len(app.messages) >= 3)

        assert isinstance(app.current_backend, RemoteBackend)
        assert served.hosts, "the session lives in the daemon"
        (host,) = served.hosts.values()
        log = host.log
        assert isinstance(log, Session) and log.path is not None
        on_disk = Session.load(log.path).entries()
        assert app.current_session.entries() == on_disk
        texts = [m.get("content") for m in app.messages if m.get("role") == "assistant"]
        assert texts and texts[-1][0]["text"] == "re: hello daemon"
        assert "fake-model" in app.sub_title or "fake" in app.sub_title
    await remote.close()


async def test_a_second_client_sees_the_tui_turn_and_the_tui_resumes_any_session(
    daemon, tau_home, tmp_path
):
    served, address = daemon
    other = await ServeClient.connect(address, client="other")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    created = await other.request(p.CreateSession(cwd=str(elsewhere), name="from-other"))
    replica = await other.attach(created["session_id"])
    await other.request(
        p.Submit(session_id=replica.session_id, cursor_id=replica.head_cursor_id, text="first")
    )

    remote = RemoteConnection(address, cwd=str(tmp_path))
    app = build_tau_app(tau_home, remote=remote)
    async with app.run_test() as pilot:
        await _until(pilot, lambda: remote._ready.is_set())
        await app._remote_open(created["session_id"])
        assert [m["role"] for m in app.messages if m["role"] != "system"] == ["user", "assistant"]

        await app.on_input_submitted(_Submit("second"))
        await _until(pilot, lambda: not app.is_generating and len(app.messages) >= 5)
        await _until(pilot, lambda: replica.seq == served.hosts[replica.session_id].seq)
        assert replica.entries == app.current_session.entries(), "both clients hold one tree"
    await remote.close()
    await other.close()


async def test_fork_under_connect_opens_the_daemons_copy(daemon, tau_home, tmp_path):
    served, address = daemon
    remote = RemoteConnection(address, cwd=str(tmp_path))
    app = build_tau_app(tau_home, remote=remote)
    async with app.run_test() as pilot:
        await _until(pilot, lambda: remote._ready.is_set())
        await app.on_input_submitted(_Submit("before the fork"))
        await _until(pilot, lambda: not app.is_generating and len(app.messages) >= 3)
        source = app.current_session.id
        source_entries = app.current_session.entries()

        await app.on_input_submitted(_Submit("/fork"))
        await _until(pilot, lambda: app.current_session.id != source)

        assert isinstance(app.current_backend, RemoteBackend)
        assert app.current_backend.session_id in served.hosts, "the fork lives in the daemon"
        assert [e["id"] for e in app.current_session.entries()] == [e["id"] for e in source_entries]
        assert served.hosts[source].log.entries() == source_entries, "the source is unchanged"
    await remote.close()


async def test_a_flow_step_under_connect_is_asked_and_stepped_on_the_daemon(
    daemon, tau_home, tmp_path
):
    from textual.widgets import RadioButton, RadioSet

    from tau_coding_agent import modals

    served, address = daemon
    remote = RemoteConnection(address, cwd=str(tmp_path))
    app = build_tau_app(tau_home, remote=remote)
    async with app.run_test() as pilot:
        await _until(pilot, lambda: remote._ready.is_set())
        await app.on_input_submitted(_Submit("hi"))
        await _until(pilot, lambda: not app.is_generating and len(app.messages) >= 3)

        await app.on_input_submitted(_Submit("/model"))
        await _until(pilot, lambda: isinstance(app.screen, modals.ExtensionFormScreen))
        screen = app.screen
        names = [m["name"] for m in app.current_backend.replica.models]
        assert names == ["fake", "other"]
        await _until(pilot, lambda: len(screen.query(RadioButton)) == 2)
        labels = [str(b.label) for b in screen.query_one(RadioSet).query(RadioButton)]
        assert labels == ["fake", "other"], "the options are the daemon's enumeration"
        screen.dismiss({"name": "other"})

        (host,) = served.hosts.values()
        await _until(pilot, lambda: host.agent_session.get_model()["id"] == "other-model")
    await remote.close()


async def test_the_extensions_view_under_connect_lists_the_daemons_load_errors(
    daemon, tau_home, tmp_path
):
    served, address = daemon
    extensions = tmp_path / "daemon-home" / ".tau" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "broken.py").write_text("raise RuntimeError('nope')\n")
    remote = RemoteConnection(address, cwd=str(tmp_path))
    app = build_tau_app(tau_home, remote=remote)
    async with app.run_test() as pilot:
        await _until(pilot, lambda: remote._ready.is_set())
        await app.on_input_submitted(_Submit("hi"))
        await _until(pilot, lambda: not app.is_generating and len(app.messages) >= 3)

        infos, errors = app.current_backend.extension_summary()
        listing = app._format_extension_infos(infos, errors, set())

        assert infos == []
        assert "## Load errors" in listing and "broken.py" in listing and "nope" in listing
    await remote.close()


class _ToolStream:
    """Reasoning, then a streamed answer that calls ``ls``; after the result, a short answer."""

    def __init__(self, after_tool: bool) -> None:
        self._after_tool = after_tool

    def __aiter__(self):
        async def gen():
            if self._after_tool:
                content: list[Any] = [TextContent(text="There is one file, notes.txt.")]
            else:
                content = [
                    ThinkingContent(thinking="The user wants a listing; ls answers that."),
                    TextContent(text="Let me look at the directory first."),
                    ToolCall(id="call-ls", name="ls", arguments={"path": "."}),
                ]
            final = AssistantMessage(
                content=content,
                api="openai-completions",
                provider="openai",
                model="fake-model",
                stop_reason="stop" if self._after_tool else "toolUse",
                usage=Usage(input_tokens=40, output_tokens=12, total_tokens=52),
            )
            for block in content:
                if isinstance(block, ThinkingContent):
                    for word in block.thinking.split(" "):
                        yield ThinkingDeltaEvent(delta=word + " ", partial=final)
                elif isinstance(block, TextContent):
                    for word in block.text.split(" "):
                        yield TextDeltaEvent(delta=word + " ", partial=final)
            yield DoneEvent(final=final, usage=final.usage)

        return gen()

    def abort(self) -> None:
        pass


async def _tool_stream(model: Any, context: Any, options: Any = None) -> _ToolStream:
    last = context["messages"][-1]
    role = last.get("role") if isinstance(last, dict) else last.role
    return _ToolStream(after_tool=role == "toolResult")


async def test_a_streamed_turn_under_connect_renders_as_the_daemons_own_bus_does(
    daemon, tau_home, tmp_path
):
    """Text, reasoning and a tool call: the wire's bounded events, rejoined with the
    entries, give the router what the daemon's in-process bus gives it."""
    served, address = daemon
    (tmp_path / "notes.txt").write_text("notes\n")
    other = await ServeClient.connect(address, client="other")
    created = await other.request(p.CreateSession(cwd=str(tmp_path)))
    session_id = created["session_id"]
    await other.attach(session_id)
    local: list[dict[str, Any]] = []
    served.hosts[session_id].backend.subscribe_render(local.append)

    remote = RemoteConnection(address, cwd=str(tmp_path))
    app = build_tau_app(tau_home, remote=remote)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_tool_stream):
        async with app.run_test(size=(110, 40)) as pilot:
            await _until(pilot, lambda: remote._ready.is_set())
            await app._remote_open(session_id)
            rendered: list[dict[str, Any]] = []
            orphans: list[str] = []
            RemoteBackend(remote, session_id).subscribe_render(
                rendered.append, on_orphan=orphans.append
            )
            await app.on_input_submitted(_Submit("what is here?"))
            await _until(pilot, lambda: not app.is_generating and len(app.messages) >= 5)
            await _until(pilot, lambda: any(e["kind"] == "stream_end" for e in rendered))
            await pilot.pause(0.1)
            if SHOTS is not None:
                from textual.widgets import Collapsible

                for fold in app.query(Collapsible):
                    fold.collapsed = False
                app.refresh(layout=True)
                await pilot.pause(0.5)
                save_render(app, SHOTS, "connect-streamed-turn")

    assert orphans == []
    kinds = [e["kind"] for e in rendered]
    assert {"reasoning_delta", "text_delta", "tool_call", "tool_result"} <= set(kinds)
    call = next(e for e in rendered if e["kind"] == "tool_call")
    assert call["arguments"] == {"path": "."}
    result = next(e for e in rendered if e["kind"] == "tool_result")
    assert "notes.txt" in result["result"]
    assert rendered == local
    end = rendered[-1]
    assert end["kind"] == "stream_end" and end["output"] == 24 and end["context"] == 40
    await remote.close()
    await other.close()
