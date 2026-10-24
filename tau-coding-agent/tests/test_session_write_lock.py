"""A file-store session is held by one writing process at a time (docs/STEP-DEBUGGER.md §3.7.3, §6.2)."""

from __future__ import annotations

import asyncio
import fcntl
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

from tau_agent_core import session_lock
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.agent_session_runtime import AgentSessionRuntime
from tau_agent_core.rpc.commands import _handle_get_state
from tau_agent_core.session_lock import (
    READ_ONLY_WARNING_KEY,
    SessionReadOnlyError,
    held_descriptor_count,
    read_only_banner,
)
from tau_agent_core.submission import Submission
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import CLIError, run_print
from tau_coding_agent.session_store import FileSessionCatalog, Session
from tau_coding_agent.store_factory import StoreError, resolve_allow_unlocked_fs

_TS = 1_700_000_000_000

HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from tau_agent_core.session_lock import SessionWriteLock, set_head_label
    set_head_label(sys.argv[2])
    lock = SessionWriteLock(Path(sys.argv[1]))
    lock.retain()
    assert not lock.access().read_only, lock.access()
    print("held", flush=True)
    sys.stdin.read()
    """
)


class Holder:
    """Another process hosting ``session`` for writing, until :meth:`stop`."""

    def __init__(self, session: Session, head: str = "tui") -> None:
        assert session.write_lock is not None
        self.proc = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(session.write_lock.sidecar), head],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        assert self.proc.stdout.readline().strip() == "held"

    def stop(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=30)


def can_lock(session: Session) -> bool:
    """Whether another open file description could take ``session``'s lock now."""
    assert session.write_lock is not None
    fd = os.open(session.write_lock.sidecar, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


def _host(log: Any) -> AgentSession:
    return AgentSession(session_log=log, model=_model(), tools=[])


def _sub(text: str) -> Submission:
    return Submission(
        text=text,
        source="interactive",
        submitter="human",
        submission_id=f"s-{text}",
        multitask_strategy="enqueue",
    )


class _Stream:
    def __init__(self, text: str) -> None:
        self._message = AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model="m",
            stop_reason="stop",
            timestamp=_TS,
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    def __aiter__(self):
        async def _gen():
            yield TextDeltaEvent(delta=self._message.content[0].text, partial=self._message)
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return _gen()

    async def result(self) -> AssistantMessage:
        return self._message

    def abort(self) -> None:
        pass


class _Gated:
    """A provider holding every call open until its gate is set."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls = 0

    async def stream(self, model: Any, context: Any, options: Any = None) -> _Stream:
        self.calls += 1
        await self.gate.wait()
        return _Stream("ok")


@pytest.fixture
def catalog(tmp_path, monkeypatch) -> FileSessionCatalog:
    monkeypatch.setattr(session_lock, "lock_blind_filesystem", lambda _d: None)
    return FileSessionCatalog(tmp_path / "sessions")


def _new(catalog: FileSessionCatalog) -> Session:
    return catalog.create(os.getcwd(), "m", "openai", system_prompt="sys")


async def test_a_second_process_opens_read_only_and_every_write_names_the_holder(catalog):
    session = _new(catalog)
    holder = Holder(session, head="tui")
    try:
        host = _host(catalog.load(str(session.path)))
        access = host.write_access
        assert access.read_only and access.holder is not None
        assert access.holder.pid == holder.proc.pid and access.holder.head == "tui"
        result = await host.submit(_sub("hi"))
        assert not result.accepted
        assert str(holder.proc.pid) in (result.rejection_reason or "")
        assert "(tui)" in (result.rejection_reason or "")
        with pytest.raises(SessionReadOnlyError, match=str(holder.proc.pid)):
            await host.cursor.append_message({"role": "user", "content": "x"})
        banner = read_only_banner(host.extension_warnings())
        assert banner is not None and str(holder.proc.pid) in banner
        state = await _handle_get_state(MagicMock(session=host), 1, {})
        assert state["read_only"] is True
        assert state["lock_holder"]["pid"] == holder.proc.pid
        assert state["lock_holder"]["head"] == "tui"
        await host.emit_session_shutdown()
    finally:
        holder.stop()


async def test_a_writable_session_reports_it_and_holds_the_lock(catalog):
    session = _new(catalog)
    host = _host(session)
    assert not host.write_access.read_only
    state = await _handle_get_state(MagicMock(session=host), 1, {})
    assert state["read_only"] is False and state["lock_holder"] is None
    assert not can_lock(session)
    assert READ_ONLY_WARNING_KEY not in {w.key for w in host.extension_warnings()}
    await host.emit_session_shutdown()
    assert can_lock(session), "shutdown releases the lock"


async def test_two_session_objects_on_one_file_share_one_descriptor(catalog):
    session = _new(catalog)
    before = held_descriptor_count()
    first = _host(catalog.load(str(session.path)))
    second = _host(catalog.load(str(session.path)))
    assert held_descriptor_count() == before + 1
    await first.cursor.append_message({"role": "user", "content": "one"})
    await second.cursor.append_message({"role": "user", "content": "two"})
    await first.emit_session_shutdown()
    assert not can_lock(session), "the second host still holds it"
    await second.emit_session_shutdown()
    assert can_lock(session)
    assert held_descriptor_count() == before


async def test_a_lane_turn_overlapping_a_head_turn_in_one_process_is_not_refused(catalog):
    host = _host(_new(catalog))
    lane = await host.open_cursor(host.cursor.leaf, owner=host.cursor, label="lane")
    provider = _Gated()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        head_turn = asyncio.create_task(host.submit(_sub("head")))
        lane_turn = asyncio.create_task(host.submit(_sub("lane"), cursor=lane))
        while provider.calls < 2:
            await asyncio.sleep(0)
        provider.gate.set()
        results = await asyncio.gather(head_turn, lane_turn)
    assert all(r.accepted for r in results), [r.rejection_reason for r in results]
    await host.emit_session_shutdown()


async def test_a_swap_keeps_the_lock_while_a_lane_writes_and_releases_it_after(catalog):
    old = _new(catalog)
    host = _host(old)
    lane = await host.open_cursor(host.cursor.leaf, label="lane")
    host.session_log = _new(catalog)
    assert not can_lock(old), "a cursor still writes the old session"
    await lane.append_message({"role": "user", "content": "still here"})
    await host.close_cursor(lane)
    assert can_lock(old)
    await host.emit_session_shutdown()


async def test_after_new_session_another_process_can_open_the_previous_one(catalog):
    previous = _new(catalog)
    host = _host(previous)
    runtime = AgentSessionRuntime(host, catalog, os.getcwd(), "m", "openai", "file")
    assert not can_lock(previous)
    result = await runtime.new_session(persist=True)
    assert not result["cancelled"]
    assert can_lock(previous)
    await host.emit_session_shutdown()


async def test_a_sidecar_unlinked_mid_session_is_retaken_at_admission(catalog):
    session = _new(catalog)
    host = _host(session)
    assert session.write_lock is not None
    session.write_lock.sidecar.unlink()
    assert host._write_refusal(host.cursor) is None
    assert session.write_lock.sidecar.exists() and not can_lock(session)
    await host.emit_session_shutdown()


async def test_a_sidecar_replaced_by_another_writer_refuses_the_next_turn(catalog):
    session = _new(catalog)
    host = _host(session)
    assert session.write_lock is not None
    session.write_lock.sidecar.unlink()
    holder = Holder(session, head="rpc")
    try:
        result = await host.submit(_sub("hi"))
        assert not result.accepted and "(rpc)" in (result.rejection_reason or "")
        assert read_only_banner(host.extension_warnings()) is not None
    finally:
        holder.stop()
        await host.emit_session_shutdown()


async def test_a_fork_gets_a_fresh_sidecar(catalog):
    source = _new(catalog)
    host = _host(source)
    assert source.write_lock is not None
    fork = catalog.fork(source, os.getcwd())
    assert fork.write_lock is not None
    assert fork.write_lock.sidecar != source.write_lock.sidecar
    assert not fork.write_lock.sidecar.exists(), "nothing copied the source's sidecar"
    assert can_lock(fork)
    assert {i.id for i in catalog.list(os.getcwd())} == {source.id, fork.id}
    await host.emit_session_shutdown()


async def test_a_deferred_session_takes_its_lock_with_its_first_write(catalog):
    deferred = catalog.create_deferred(os.getcwd(), "m", "openai")
    host = _host(deferred)
    assert deferred.write_lock is not None
    assert not deferred.write_lock.sidecar.parent.exists(), "an unused session leaves nothing"
    await host.cursor.append_message({"role": "user", "content": "first"})
    assert not can_lock(deferred)
    await host.emit_session_shutdown()


async def test_a_lock_blind_filesystem_raises_a_session_warning_unless_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(session_lock, "lock_blind_filesystem", lambda _d: "cifs")
    host = _host(FileSessionCatalog(tmp_path / "a").create(os.getcwd(), "m", "openai"))
    messages = [w.message for w in host.extension_warnings()]
    assert "cross-process exclusion unavailable on cifs; a second writer is not detected" in messages
    assert not host.write_access.read_only
    await host.emit_session_shutdown()
    assert host.extension_warnings() == []

    trusted = FileSessionCatalog(tmp_path / "b", allow_unlocked_fs=True)
    quiet = _host(trusted.create(os.getcwd(), "m", "openai"))
    assert quiet.extension_warnings() == []
    await quiet.emit_session_shutdown()


def test_allow_unlocked_fs_is_read_from_config_and_checked():
    assert resolve_allow_unlocked_fs({}) is False
    assert resolve_allow_unlocked_fs({"sessions": {"allow_unlocked_fs": True}}) is True
    with pytest.raises(StoreError, match="allow_unlocked_fs"):
        resolve_allow_unlocked_fs({"sessions": {"allow_unlocked_fs": "yes"}})


async def test_print_mode_refuses_a_held_session_naming_the_holder(tmp_path, monkeypatch):
    monkeypatch.setenv("TAU_HOME", str(tmp_path))
    monkeypatch.setattr(session_lock, "lock_blind_filesystem", lambda _d: None)
    session = Session.create(os.getcwd(), "gpt-4o", "openai", system_prompt="sys")
    config = {
        "models": {
            "gpt-4o": {
                "backend": "openai",
                "model": "gpt-4o",
                "base_url": "http://127.0.0.1:1/v1",
                "api_key": "sk-x",
            }
        },
        "default_model": "gpt-4o",
    }
    holder = Holder(session, head="tui")
    try:
        with pytest.raises(CLIError, match=f"pid {holder.proc.pid} \\(tui\\)"):
            await run_print(
                CLIArgs(messages=["hi"], print_mode=True, session=str(session.path)), config
            )
    finally:
        holder.stop()
    assert Path(str(session.path)).read_text().count("\n") == len(session.entries()) + 1


async def test_the_tui_shows_a_read_only_banner_naming_the_holder(make_app, monkeypatch):
    from tau_coding_agent import extension_ui
    from tau_coding_agent.backends import create_backend
    from tau_coding_agent.chat_widgets import ChatSelected

    monkeypatch.setattr(session_lock, "lock_blind_filesystem", lambda _d: None)
    app = make_app(create_backend=create_backend)
    async with app.run_test() as pilot:
        await pilot.pause()
        held = app.session_catalog.create(os.getcwd(), "m", "openai", system_prompt="sys")
        assert isinstance(held, Session)
        holder = Holder(held, head="tui")
        try:
            await app.on_chat_selected(ChatSelected(str(held.path)))
            await pilot.pause()
            banner = app.query_one(extension_ui.ReadOnlyBanner)
            assert banner.display is True
            assert f"pid {holder.proc.pid} (tui)" in str(banner.render())
            await app.action_new_chat()
            await pilot.pause()
            assert banner.display is False
        finally:
            holder.stop()
