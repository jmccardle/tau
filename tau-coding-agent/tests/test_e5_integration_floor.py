"""E5 §6 (E5.5 / S36) — the automated integration floor for the extension wiring.

Three integration tests that exercise the E5 milestone end-to-end, each through a
REAL surface (a spawned ``tau`` process, an on-disk ``Session``, a live Textual
app), not a unit stub:

(a) :func:`test_headless_subprocess_injects_durable_node` — the headless subprocess
    smoke: ``tau -p -e <demo>.py`` against the in-repo fake OpenAI-compatible
    provider (stdlib ``http.server``, no network / no API key). A ``before_agent_start``
    hook injects a durable node; the assertion is that the hook's node lands in the
    emitted *transcript* — here the persisted session JSONL, which in τ's
    tree-as-truth model IS the transcript (persisted == rendered == sent, one
    artifact, E5 §1). This proves the whole S25–S28 wiring spine fires inside a real
    process, not just in-test.

(b) :func:`test_tui_floor_extensions_listing_and_veto` — the Textual ``Pilot``
    (``run_test``) floor: a real ``Parley`` loads an ``-e`` extension, the user runs
    ``/extensions`` and sees it listed (name/path/tool/command/hook), and a
    ``tool_call`` veto renders as a visibly-blocked ``ToolBox`` (E5 §4–§5).

(c) :func:`test_reload_invariant_byte_identical_model_context` — THE reload-invariant
    check, the invariant's proof (E5 §1, most important). It takes the session the
    demo-injecting subprocess persisted, reloads it, and asserts the model's context
    is byte-identical to the persisted active path across reloads: the file bytes are
    untouched by a load, two independent loads yield identical entries + identical
    model context, there is exactly ONE injected node (no forked "second history"),
    and the durable ``custom`` node still remaps custom→user on the wire (the LLM
    never sees a ``custom`` role). The edit is baked into the path, so reload replays
    the exact bytes the model saw — more deterministic, not less.

Reference: docs/EXTENSIONS-E5-WIRING.md §6 (E5.5 / S36); §1 (the durable-hook
invariant); D-E5-2, D-E5-4.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.messages import convert_to_llm
from tau_coding_agent.backends import create_backend
from tau_coding_agent.session_store import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A recognizable marker only the extension can put on the path — its presence
# proves the HOOK ran (a bare run without the extension has no such node).
_MARKER = "DURABLE_MARKER_S36"

# The demo extension: a before_agent_start hook injecting one durable custom node
# per turn (E5 §3.1). Minimal on purpose — a stateful demo (21_reminders' cooldown)
# would inject non-deterministically in a single subprocess turn.
_INJECT_DEMO = (
    "def register(api):\n"
    "    def hook(event, ctx):\n"
    f'        return {{"message": {{"customType": "reminder", "content": {_MARKER!r}}}}}\n'
    '    api.on("before_agent_start", hook)\n'
)

# A single canned SSE completion the fake provider streams for every request — one
# assistant turn, then stop (no tools → one round-trip, deterministic).
_SSE_BODY = (
    'data: {"id":"cmpl-1","choices":[{"index":0,'
    '"delta":{"role":"assistant","content":"Ack from fake."},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":11,"completion_tokens":5,"total_tokens":16}}\n\n'
    "data: [DONE]\n\n"
)


# ── the in-repo fake provider (a real HTTP server the child talks to) ─────────


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)  # drain the request body
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        body = _SSE_BODY.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence per-request stderr logging
        pass


@pytest.fixture
def fake_provider():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def demo_run(tmp_path, fake_provider):
    """Run ``tau -p -e <demo>.py`` in a real subprocess against the fake provider.

    Builds a temp ``$HOME`` whose ``~/.tau/config.json`` default model points at the
    fake provider (so the child resolves its model + persists its session there),
    spawns the CLI module with the current interpreter (robust in a venv, pi-faithful
    in spirit — pi re-runs its own runtime), and returns the completed process plus
    the single persisted session file.
    """
    home = tmp_path / "home"
    (home / ".tau").mkdir(parents=True)
    config = {
        "models": {
            "fake": {
                "backend": "openai",
                "model": "fake-model",
                "base_url": fake_provider,
                "api_key": "x",
                "tools": [],
            }
        },
        "default_model": "fake",
        "system_prompt": "You are helpful.",
    }
    (home / ".tau" / "config.json").write_text(json.dumps(config))

    demo = tmp_path / "inject_demo.py"
    demo.write_text(_INJECT_DEMO)

    env = dict(os.environ)
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, "-m", "tau_coding_agent.cli", "-p", "-e", str(demo), "hello there"],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=90,
    )
    sessions = list((home / ".tau" / "sessions").rglob("*.jsonl"))
    return {"proc": proc, "demo": demo, "sessions": sessions}


def _custom_entries(entries: list[dict]) -> list[dict]:
    """The durable extension-injected nodes on disk.

    Headless persists a ``before_agent_start`` injection through the on-disk
    ``Session.append_message`` (the loop RETURNS it in prompt()'s messages), so on
    disk it is a ``message`` entry whose stored message carries ``role: "custom"``
    plus its ``customType`` — the durable, reloadable form (E5 §3.1).
    """
    out = []
    for e in entries:
        msg = e.get("message") if isinstance(e, dict) else None
        if e.get("type") == "message" and isinstance(msg, dict) and msg.get("role") == "custom":
            out.append(e)
        elif e.get("type") == "customMessage":
            out.append(e)
    return out


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


# ── (a) headless subprocess smoke ─────────────────────────────────────────────


def test_headless_subprocess_injects_durable_node(demo_run):
    """``tau -p -e demo.py`` fires the hook in a real process; its durable node is
    in the persisted transcript."""
    proc = demo_run["proc"]
    assert proc.returncode == 0, proc.stderr
    # The run completed through the extension: the fake provider's reply reached
    # stdout (the extension neither aborted nor corrupted the loop).
    assert "Ack from fake." in proc.stdout

    # Exactly one session was persisted; it carries the hook's durable node.
    assert len(demo_run["sessions"]) == 1
    entries = Session.load(demo_run["sessions"][0]).entries()

    customs = _custom_entries(entries)
    assert len(customs) == 1, entries  # the hook injected once, no fabricated dupes
    injected = customs[0]["message"]
    assert injected["customType"] == "reminder"
    assert _MARKER in _text_of(injected)

    # The node sits ON the active path between the user turn and the assistant
    # reply — a real tree node, not an out-of-band channel (E5 §1).
    roles = [
        e["message"]["role"]
        for e in entries
        if e.get("type") == "message" and isinstance(e.get("message"), dict)
    ]
    assert roles == ["system", "user", "custom", "assistant"]


# ── (c) THE reload-invariant check (the invariant's proof) ────────────────────


def test_reload_invariant_byte_identical_model_context(demo_run):
    """Reload the demo-injected session; the model's context is byte-identical to
    the persisted active path — no second history."""
    assert demo_run["proc"].returncode == 0, demo_run["proc"].stderr
    path = demo_run["sessions"][0]

    # A load must NOT rewrite the file — the on-disk bytes are the single artifact.
    raw = path.read_bytes()
    first = Session.load(path)
    assert path.read_bytes() == raw

    # Two independent reloads yield identical entries AND identical model context —
    # the fold is deterministic, replaying the exact persisted path (no recompute).
    second = Session.load(path)
    assert first.entries() == second.entries()
    ctx_first = ConversationTree(first.entries(), first.cursor).context_for()
    ctx_second = ConversationTree(second.entries(), second.cursor).context_for()
    assert ctx_first == ctx_second

    # The injected node is on the reconstructed model context, in path order, and it
    # is the ONLY one — the fork this invariant forbids would surface as a duplicate
    # (model-saw copy + disk copy) or a missing node.
    assert [m.get("role") for m in ctx_first] == ["system", "user", "custom", "assistant"]
    assert sum(1 for e in first.entries() if _custom_entries([e])) == 1

    # The durable ``custom`` node remaps custom→user on the wire (pi messages.ts):
    # the reloaded path serializes to roles the LLM accepts (never "custom"), and
    # the injected marker survives as a user message the model reads.
    wire = convert_to_llm(ctx_first)
    assert "custom" not in [m.get("role") for m in wire]
    user_texts = [_text_of(m) for m in wire if m.get("role") == "user"]
    assert user_texts == ["hello there", _MARKER]


# ── (b) the Textual Pilot floor: load → /extensions listing → veto render ─────

# A file extension registering a tool, a command, and a hook — everything the
# /extensions listing surfaces for one loaded extension.
_FULL_EXT = """
async def _exec(tool_call_id, params, signal, on_update, ctx):
    return {"content": [{"type": "text", "text": "ok"}]}

def register(api):
    api.register_tool({
        "name": "probe",
        "description": "a probe tool",
        "parameters": {"type": "object", "properties": {}},
        "execute": _exec,
    })
    api.register_command("hello", {"description": "say hi"})
    api.on("tool_result", lambda event, ctx: None)
"""


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A Parley wired to REAL TauBackends, with sandboxed session storage."""
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    monkeypatch.setattr("tau_coding_agent.app.create_backend", create_backend)
    # Isolate the module-global session-event listener list so a real backend's
    # subscribe_session_events leak can't fire into a later test (see
    # test_app_extension_loading.py for the rationale).
    monkeypatch.setattr(store, "_session_listeners", [])

    from tau_coding_agent.app import Parley

    a = Parley()
    a.config = {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    return a


async def test_tui_floor_extensions_listing_and_veto(app, tmp_path):
    """A live TUI loads an extension, lists it via ``/extensions``, and renders a
    veto as a blocked ToolBox — the whole visible-surface floor in one run."""
    from tau_coding_agent.app import ChatDisplay, ChatInput, MessageBox
    from tau_coding_agent.chat_widgets import ToolBox

    ext = tmp_path / "full_ext.py"
    ext.write_text(_FULL_EXT)
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()

        # The extension bound to THIS backend's live session (loaded, not stubbed).
        runner = app.current_backend.agent_session._extension_runner
        assert runner.has_handlers("tool_result") is True

        # ── /extensions listing — drive the real slash path (user types it) ──
        input_widget = app.query_one("#chat-input", ChatInput)
        input_widget.text = "/extensions"
        input_widget.action_submit()
        await pilot.pause()

        system_boxes = [b for b in app.query(MessageBox) if b.role == "system"]
        assert system_boxes, "no system box rendered for /extensions"
        listing = system_boxes[-1]._content
        assert "full_ext" in listing  # name
        assert str(ext) in listing  # path
        assert "probe" in listing  # registered tool
        assert "hello" in listing  # registered command
        assert "tool_result" in listing  # registered hook
        # Read-only chrome: the listing is NOT a conversation node the model is sent
        # (the invariant — no ephemeral text smuggled onto the path, E5 §1).
        assert not any(m.get("content") == listing for m in app.messages)

        # ── veto render — a tool_call + is_error tool_result (the veto shape),
        # exactly the events TauBackend.stream_chat normalizes for a blocked call ──
        display = app.query_one(ChatDisplay)
        display.add_message("user", "write outside scope")
        await display.begin_exchange()
        display.handle_stream_event({"kind": "turn_start", "turn_index": 0})
        await pilot.pause()
        display.handle_stream_event(
            {"kind": "tool_call", "id": "c1", "name": "write", "arguments": {"path": "/etc/x"}}
        )
        await pilot.pause()
        display.handle_stream_event(
            {
                "kind": "tool_result",
                "id": "c1",
                "name": "write",
                "result": "denied by policy",
                "is_error": True,
            }
        )
        await pilot.pause()

        boxes = list(display.query(ToolBox))
        assert len(boxes) == 1
        box = boxes[0]
        assert box.has_result is True
        assert box.has_class("box-error")  # rendered DISTINCTLY, not dropped
        assert box.title.startswith("✗")
        assert "denied by policy" in box._result_md._markdown
