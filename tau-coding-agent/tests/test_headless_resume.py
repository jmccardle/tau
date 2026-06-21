"""Headless session continuation: --continue / --session / --fork / --name.

These drive ``run_print`` against a sandboxed ``~/.tau/chats`` and a fake backend
(no live LLM), asserting the load → context → persist wiring:

- ``--continue`` resumes the most recent session and grows it **in place**.
- ``--session REF`` resumes a specific session (by path or filename stem).
- ``--fork REF`` continues into a **new** file, leaving the source untouched.
- ``--name`` sets/updates the session title.
- a resumed run keeps the session's model unless ``--model`` overrides it.

Selector ambiguity / misses and the ``--system-prompt`` + resume combination
raise (Fail-Early), never guess.

Reference: docs/CLI-PLAN.md §3 (session continuation), ROADMAP Tier 3 #5.
"""

from __future__ import annotations

import json
import os

import pytest

import tau_coding_agent.session_store as store
from tau_coding_agent.cli import CLIArgs
from tau_coding_agent.headless import CLIError, run_print
from tau_coding_agent.session_store import Chat


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
    """Sandbox ~/.tau and the backend factory; expose a chat-seeding helper."""
    holder: dict = {}

    def factory(config):
        be = _FakeBackend(config)
        holder["backend"] = be
        return be

    monkeypatch.setattr("tau_coding_agent.backends.create_backend", factory)
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    holder["tau_dir"] = tmp_path

    def seed(model, messages, created_at, title=None):
        chat = Chat(
            model=model, backend="openai", messages=messages,
            created_at=created_at, title=title,
        )
        path = chat.save()
        # Pin mtime to created_at so list_recent() ordering is deterministic.
        os.utime(path, (created_at, created_at))
        return path

    holder["seed"] = seed
    holder["chats_dir"] = tmp_path / "chats"
    return holder


def _files(env) -> list:
    return sorted((env["chats_dir"]).glob("*.json"))


def _load(path) -> dict:
    return json.loads(path.read_text())


def _convo(user_text: str, answer: str = "r") -> list[dict]:
    return [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": [{"type": "text", "text": answer}]},
    ]


# ── --continue ──────────────────────────────────────────────────────────────

async def test_continue_loads_most_recent_and_updates_in_place(env):
    a = env["seed"]("local-llm", _convo("a1"), created_at=1000.0)
    b = env["seed"]("local-llm", _convo("b1"), created_at=2000.0)
    a_before = a.read_text()

    rc = await run_print(
        CLIArgs(messages=["next"], print_mode=True, continue_session=True),
        _config(),
    )
    assert rc == 0

    # Context handed to the backend = B's stored transcript + the new user turn.
    ctx = env["backend"].messages
    assert ctx[:3] == _convo("b1")
    assert ctx[-1] == {"role": "user", "content": "next"}

    # No new file; B grew in place; A untouched.
    assert _files(env) == [a, b]
    assert a.read_text() == a_before
    saved_b = _load(b)
    roles = [m["role"] for m in saved_b["messages"]]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert saved_b["messages"][-2] == {"role": "user", "content": "next"}


async def test_continue_empty_store_errors(env):
    with pytest.raises(CLIError, match="no saved sessions"):
        await run_print(
            CLIArgs(messages=["hi"], print_mode=True, continue_session=True),
            _config(),
        )


async def test_resume_keeps_stored_model_when_no_model_flag(env):
    # Seeded session's model is local-llm; config default is gpt-4o.
    env["seed"]("local-llm", _convo("b1"), created_at=2000.0)

    await run_print(
        CLIArgs(messages=["next"], print_mode=True, continue_session=True),
        _config(),
    )
    # The backend was built from the stored model's config, not the default.
    assert env["backend"].config["model"] == "qwen3-32b-kv4b"
    # And the file keeps that model so it stays resumable to the same backend.
    assert _load(_files(env)[0])["model"] == "local-llm"


async def test_explicit_model_overrides_stored_on_resume(env):
    env["seed"]("local-llm", _convo("b1"), created_at=2000.0)
    await run_print(
        CLIArgs(
            messages=["next"], print_mode=True,
            continue_session=True, model="gpt-4o",
        ),
        _config(),
    )
    assert env["backend"].config["model"] == "gpt-4o"
    assert _load(_files(env)[0])["model"] == "gpt-4o"


# ── --session REF ───────────────────────────────────────────────────────────

async def test_session_by_stem_selects_specific(env):
    a = env["seed"]("local-llm", _convo("a1"), created_at=1000.0)
    b = env["seed"]("local-llm", _convo("b1"), created_at=2000.0)
    b_before = b.read_text()

    await run_print(
        CLIArgs(messages=["go"], print_mode=True, session="1000"),
        _config(),
    )
    # A (the older, non-most-recent one) was selected and grew; B untouched.
    assert env["backend"].messages[:3] == _convo("a1")
    assert b.read_text() == b_before
    assert [m["role"] for m in _load(a)["messages"]][-2:] == ["user", "assistant"]


async def test_session_by_path_selects_specific(env):
    a = env["seed"]("local-llm", _convo("a1"), created_at=1000.0)
    await run_print(
        CLIArgs(messages=["go"], print_mode=True, session=str(a)),
        _config(),
    )
    assert env["backend"].messages[:3] == _convo("a1")


async def test_session_ambiguous_errors(env):
    env["seed"]("local-llm", _convo("a1"), created_at=1700000001.0)
    env["seed"]("local-llm", _convo("b1"), created_at=1700000002.0)
    with pytest.raises(CLIError, match="matches multiple sessions"):
        await run_print(
            CLIArgs(messages=["go"], print_mode=True, session="170000000"),
            _config(),
        )


async def test_session_no_match_errors(env):
    env["seed"]("local-llm", _convo("a1"), created_at=1000.0)
    with pytest.raises(CLIError, match="no session matches"):
        await run_print(
            CLIArgs(messages=["go"], print_mode=True, session="9999"),
            _config(),
        )


# ── --fork REF ──────────────────────────────────────────────────────────────

async def test_fork_creates_new_file_and_leaves_source(env):
    b = env["seed"]("local-llm", _convo("b1"), created_at=2000.0)
    b_before = b.read_text()

    await run_print(
        CLIArgs(messages=["branch"], print_mode=True, fork="2000"),
        _config(),
    )

    files = _files(env)
    assert len(files) == 2  # original + fork
    assert b.read_text() == b_before  # source untouched

    fork_path = next(p for p in files if p != b)
    forked = _load(fork_path)
    # The fork carries B's history plus this turn.
    assert forked["messages"][:3] == _convo("b1")
    assert forked["messages"][-2] == {"role": "user", "content": "branch"}


# ── --name ──────────────────────────────────────────────────────────────────

async def test_name_sets_title_on_fresh_run(env):
    await run_print(
        CLIArgs(messages=["hi"], print_mode=True, name="My session"),
        _config(),
    )
    assert _load(_files(env)[0])["title"] == "My session"


async def test_name_updates_title_on_continue(env):
    b = env["seed"]("local-llm", _convo("b1"), created_at=2000.0)
    await run_print(
        CLIArgs(
            messages=["next"], print_mode=True,
            continue_session=True, name="Renamed",
        ),
        _config(),
    )
    assert _load(b)["title"] == "Renamed"


# ── Fail-Early combinations ─────────────────────────────────────────────────

async def test_system_prompt_with_resume_errors(env):
    env["seed"]("local-llm", _convo("b1"), created_at=2000.0)
    with pytest.raises(CLIError, match="system-prompt can't be combined"):
        await run_print(
            CLIArgs(
                messages=["next"], print_mode=True,
                continue_session=True, system_prompt="ROLE",
            ),
            _config(),
        )
