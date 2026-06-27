"""Headless session continuation: --continue / --session / --fork / --name.

These drive ``run_print`` against a sandboxed ``~/.tau/sessions`` and a fake
backend (no live LLM), asserting the load → context → persist wiring over the
append-only JSONL store (docs/SESSION-UX-REDESIGN.md):

- ``--continue`` resumes the most recent session **in the current cwd** and grows
  it **in place** (append-only — no rewrite, no new file).
- ``--session REF`` resumes a specific session by **id** (full or unique prefix)
  or by ``.jsonl`` path.
- ``--fork REF`` continues into a **new** file, leaving the source untouched.
- ``--name`` sets/updates the session name.
- a resumed run keeps the session's model unless ``--model`` overrides it (which
  appends a ``model_change`` entry — latest wins on load).

Selector ambiguity / misses and the ``--system-prompt`` + resume combination
raise (Fail-Early), never guess.
"""

from __future__ import annotations

import os

import pytest

import tau_coding_agent.session_store as store
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import CLIError, run_print
from tau_coding_agent.session_store import Session


# ── config / fakes ──────────────────────────────────────────────────────────


def _config() -> dict:
    return {
        "models": {
            "local-llm": {
                "backend": "openai",
                "model": "qwen3-32b-kv4b",
                "base_url": "http://localhost:8080/v1",
                "api_key": "not-needed",
            },
            "gpt-4o": {
                "backend": "openai",
                "model": "gpt-4o",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-xxx",
            },
        },
        # default differs from the seeded sessions' model so the resume-keeps-
        # model test proves the stored model wins over default_model.
        "default_model": "gpt-4o",
        "system_prompt": "You are helpful.",
    }


class _FakeBackend:
    def __init__(self, config):
        self.config = config

    async def stream_chat(self, messages, callback, on_event=None):
        self.messages = messages  # capture the context the loop was given
        callback("ANSWER")
        if on_event is not None:
            on_event({"kind": "text_delta", "delta": "ANSWER"})
        new_messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "ANSWER"}]},
        ]
        return "ANSWER", {"total_tokens": 1}, new_messages, []


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Sandbox ~/.tau + the backend factory; expose a session-seeding helper.

    Timestamps are driven by a deterministic, strictly-increasing fake clock so
    most-recent ordering and filenames are stable regardless of wall-clock
    granularity.
    """
    holder: dict = {}

    def factory(config):
        be = _FakeBackend(config)
        holder["backend"] = be
        return be

    monkeypatch.setattr("tau_coding_agent.backends.create_backend", factory)
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)

    clock = {"t": 0}

    def fake_now() -> str:
        clock["t"] += 1
        return f"2026-06-23T00:00:00.{clock['t']:03d}Z"

    monkeypatch.setattr(store, "_now_iso", fake_now)

    def seed(model: str, user_text: str, *, name: str | None = None, id: str | None = None):
        session = Session.create(
            os.getcwd(), model, "openai", system_prompt="You are helpful.", name=name, id=id
        )
        session.append_message({"role": "user", "content": user_text})
        session.append_message({"role": "assistant", "content": [{"type": "text", "text": "r"}]})
        return session

    holder["seed"] = seed
    holder["tau_dir"] = tmp_path
    return holder


def _files(env) -> list:
    """Every persisted session file under the sandboxed sessions dir (any cwd)."""
    return sorted((env["tau_dir"] / "sessions").rglob("*.jsonl"))


def _seeded_convo(user_text: str) -> list[dict]:
    return [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": [{"type": "text", "text": "r"}]},
    ]


# ── --continue ──────────────────────────────────────────────────────────────


async def test_continue_loads_most_recent_and_updates_in_place(env):
    a = env["seed"]("local-llm", "a1")
    b = env["seed"]("local-llm", "b1")  # most recent
    a_before = a.path.read_bytes()

    rc = await run_print(
        CLIArgs(messages=["next"], print_mode=True, continue_session=True),
        _config(),
    )
    assert rc == 0

    # Context handed to the backend = B's stored transcript + the new user turn.
    ctx = env["backend"].messages
    assert ctx[:3] == _seeded_convo("b1")
    assert ctx[-1] == {"role": "user", "content": "next"}

    # No new file; B grew in place; A untouched.
    assert _files(env) == sorted([a.path, b.path])
    assert a.path.read_bytes() == a_before
    reloaded_b = Session.load(b.path)
    roles = [m["role"] for m in reloaded_b.messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert reloaded_b.messages[-2] == {"role": "user", "content": "next"}


async def test_continue_empty_store_errors(env):
    with pytest.raises(CLIError, match="no saved sessions"):
        await run_print(
            CLIArgs(messages=["hi"], print_mode=True, continue_session=True),
            _config(),
        )


async def test_resume_keeps_stored_model_when_no_model_flag(env):
    # Seeded session's model is local-llm; config default is gpt-4o.
    b = env["seed"]("local-llm", "b1")

    await run_print(
        CLIArgs(messages=["next"], print_mode=True, continue_session=True),
        _config(),
    )
    # The backend was built from the stored model's config, not the default.
    assert env["backend"].config["model"] == "qwen3-32b-kv4b"
    # And the session keeps that model so it stays resumable to the same backend.
    assert Session.load(b.path).model == "local-llm"


async def test_explicit_model_overrides_stored_on_resume(env):
    b = env["seed"]("local-llm", "b1")
    await run_print(
        CLIArgs(
            messages=["next"], print_mode=True,
            continue_session=True, model="gpt-4o",
        ),
        _config(),
    )
    assert env["backend"].config["model"] == "gpt-4o"
    # A model_change entry was appended; latest wins on load.
    assert Session.load(b.path).model == "gpt-4o"


# ── --session REF ───────────────────────────────────────────────────────────


async def test_session_by_id_selects_specific(env):
    a = env["seed"]("local-llm", "a1")
    b = env["seed"]("local-llm", "b1")  # most recent
    b_before = b.path.read_bytes()

    await run_print(
        CLIArgs(messages=["go"], print_mode=True, session=a.id),
        _config(),
    )
    # A (the older, non-most-recent one) was selected and grew; B untouched.
    assert env["backend"].messages[:3] == _seeded_convo("a1")
    assert b.path.read_bytes() == b_before
    assert [m["role"] for m in Session.load(a.path).messages][-2:] == ["user", "assistant"]


async def test_session_by_id_prefix_selects_specific(env):
    a = env["seed"]("local-llm", "a1", id="abc11111")
    env["seed"]("local-llm", "b1", id="def22222")
    await run_print(
        CLIArgs(messages=["go"], print_mode=True, session="abc"),
        _config(),
    )
    assert env["backend"].messages[:3] == _seeded_convo("a1")
    assert Session.load(a.path).messages[-2] == {"role": "user", "content": "go"}


async def test_session_by_path_selects_specific(env):
    a = env["seed"]("local-llm", "a1")
    await run_print(
        CLIArgs(messages=["go"], print_mode=True, session=str(a.path)),
        _config(),
    )
    assert env["backend"].messages[:3] == _seeded_convo("a1")


async def test_session_ambiguous_errors(env):
    # Two ids sharing a common prefix → the prefix is ambiguous.
    env["seed"]("local-llm", "a1", id="abc11111")
    env["seed"]("local-llm", "b1", id="abc22222")
    with pytest.raises(CLIError, match="matches multiple sessions"):
        await run_print(
            CLIArgs(messages=["go"], print_mode=True, session="abc"),
            _config(),
        )


async def test_session_no_match_errors(env):
    env["seed"]("local-llm", "a1")
    with pytest.raises(CLIError, match="no session matches"):
        await run_print(
            CLIArgs(messages=["go"], print_mode=True, session="zzzzzzzz"),
            _config(),
        )


# ── --fork REF ──────────────────────────────────────────────────────────────


async def test_fork_creates_new_file_and_leaves_source(env):
    b = env["seed"]("local-llm", "b1")
    b_before = b.path.read_bytes()

    await run_print(
        CLIArgs(messages=["branch"], print_mode=True, fork=b.id),
        _config(),
    )

    files = _files(env)
    assert len(files) == 2  # original + fork
    assert b.path.read_bytes() == b_before  # source untouched

    fork_path = next(p for p in files if p != b.path)
    forked = Session.load(fork_path)
    # The fork carries B's history plus this turn, and points back at its parent.
    assert forked.parent == b.id
    assert forked.messages[:3] == _seeded_convo("b1")
    assert forked.messages[-2] == {"role": "user", "content": "branch"}


# ── --name ──────────────────────────────────────────────────────────────────


async def test_name_sets_title_on_fresh_run(env):
    await run_print(
        CLIArgs(messages=["hi"], print_mode=True, name="My session"),
        _config(),
    )
    assert Session.load(_files(env)[0]).name == "My session"


async def test_name_updates_title_on_continue(env):
    b = env["seed"]("local-llm", "b1")
    await run_print(
        CLIArgs(
            messages=["next"], print_mode=True,
            continue_session=True, name="Renamed",
        ),
        _config(),
    )
    assert Session.load(b.path).name == "Renamed"


# ── Fail-Early combinations ─────────────────────────────────────────────────


async def test_system_prompt_with_resume_errors(env):
    env["seed"]("local-llm", "b1")
    with pytest.raises(CLIError, match="system-prompt can't be combined"):
        await run_print(
            CLIArgs(
                messages=["next"], print_mode=True,
                continue_session=True, system_prompt="ROLE",
            ),
            _config(),
        )
