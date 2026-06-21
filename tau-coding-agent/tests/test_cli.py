"""Tests for the τ CLI: argument parsing, model resolution, headless print mode.

These exercise the wiring without a live LLM — the headless run is driven
through a fake backend that calls the streaming ``callback``/``on_event`` the
way ``TauBackend`` does.

Reference: docs/CLI-PLAN.md (Core flag set).
"""

from __future__ import annotations

import json

import pytest

from tau_coding_agent import cli
from tau_coding_agent.cli import CLIArgs, parse_cli_args
from tau_coding_agent.headless import (
    CLIError,
    assemble_prompt,
    resolve_model_config,
    run_print,
)


# ── a config like ~/.tau/config.json ───────────────────────────────────────

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
        "default_model": "local-llm",
        "system_prompt": "You are helpful.",
    }


# ── argument parsing ────────────────────────────────────────────────────────

def test_defaults():
    args = parse_cli_args([])
    assert args.messages == []
    assert args.print_mode is False
    assert args.mode == "text"
    assert args.model is None and args.provider is None
    assert args.no_tools is False and args.verbose is False


def test_print_eats_message():
    args = parse_cli_args(["-p", "hello world"])
    assert args.print_mode is True
    assert args.messages == ["hello world"]


def test_core_flags_parse():
    args = parse_cli_args(
        ["--model", "gpt-4o", "--provider", "openai", "-t", "read,bash",
         "-p", "--mode", "json", "do it"]
    )
    assert args.model == "gpt-4o"
    assert args.provider == "openai"
    assert args.tools == "read,bash"
    assert args.print_mode is True
    assert args.mode == "json"
    assert args.messages == ["do it"]


def test_no_tools_flag():
    assert parse_cli_args(["-nt", "-p", "x"]).no_tools is True
    assert parse_cli_args(["--no-tools", "-p", "x"]).no_tools is True


def test_mode_choices_validated():
    # argparse rejects an invalid --mode with SystemExit(2)
    with pytest.raises(SystemExit):
        parse_cli_args(["--mode", "xml", "-p", "x"])


def test_version_flag_exits():
    with pytest.raises(SystemExit) as exc:
        parse_cli_args(["-v"])
    assert exc.value.code == 0


# ── model resolution ────────────────────────────────────────────────────────

def test_resolve_config_key():
    name, mc = resolve_model_config(_config(), CLIArgs(model="gpt-4o"))
    assert name == "gpt-4o"
    assert mc["model"] == "gpt-4o" and mc["backend"] == "openai"
    assert mc["base_url"] == "https://api.openai.com/v1"


def test_resolve_uses_default_model():
    name, mc = resolve_model_config(_config(), CLIArgs(model=None))
    assert name == "local-llm"
    assert mc["model"] == "qwen3-32b-kv4b"


def test_resolve_no_tools_empties_tools():
    _name, mc = resolve_model_config(_config(), CLIArgs(model="gpt-4o", no_tools=True))
    assert mc["tools"] == []


def test_resolve_tools_allowlist():
    _name, mc = resolve_model_config(
        _config(), CLIArgs(model="gpt-4o", tools="read, bash")
    )
    assert mc["tools"] == ["read", "bash"]


def test_resolve_provider_override():
    _name, mc = resolve_model_config(
        _config(), CLIArgs(model="gpt-4o", provider="anthropic")
    )
    assert mc["backend"] == "anthropic"


def test_resolve_provider_slash_id_shorthand():
    name, mc = resolve_model_config(
        _config(), CLIArgs(model="openai/gpt-4o-mini")
    )
    assert name == "openai/gpt-4o-mini"
    assert mc == {"backend": "openai", "model": "gpt-4o-mini"}


def test_resolve_bare_id_uses_provider_default():
    _name, mc = resolve_model_config(_config(), CLIArgs(model="some-model"))
    assert mc == {"backend": "openai", "model": "some-model"}


def test_resolve_thinking_suffix_sets_level():
    # gpt-4o:high is not a config key, so the :high suffix is parsed off the
    # ad-hoc id and lands on model_config["thinking"].
    name, mc = resolve_model_config(_config(), CLIArgs(model="gpt-4o:high"))
    assert mc["thinking"] == "high"
    assert mc["model"] == "gpt-4o"


def test_resolve_thinking_flag_on_config_model():
    # --thinking applies to a config-key model too.
    _name, mc = resolve_model_config(
        _config(), CLIArgs(model="gpt-4o", thinking="medium")
    )
    assert mc["thinking"] == "medium"


def test_resolve_thinking_flag_overrides_suffix():
    # An explicit --thinking wins over a :level suffix (pi: cliThinking ?? suffix).
    _name, mc = resolve_model_config(
        _config(), CLIArgs(model="some-model:low", thinking="high")
    )
    assert mc["thinking"] == "high"


def test_resolve_no_thinking_leaves_key_absent():
    _name, mc = resolve_model_config(_config(), CLIArgs(model="gpt-4o"))
    assert "thinking" not in mc


def test_parse_thinking_flag():
    args = parse_cli_args(["--thinking", "high", "-p", "hi"])
    assert args.thinking == "high"


def test_parse_invalid_thinking_level_rejected():
    with pytest.raises(SystemExit):
        parse_cli_args(["--thinking", "bogus"])


# ── session continuation flags ──────────────────────────────────────────────

def test_parse_continue_flag():
    assert parse_cli_args(["-p", "-c", "go"]).continue_session is True
    assert parse_cli_args(["-p", "--continue", "go"]).continue_session is True


def test_parse_session_fork_name():
    args = parse_cli_args(
        ["-p", "--session", "1718", "--name", "My chat", "go"]
    )
    assert args.session == "1718"
    assert args.name == "My chat"
    assert parse_cli_args(["-p", "--fork", "1718", "go"]).fork == "1718"
    assert parse_cli_args(["-p", "-n", "Title", "go"]).name == "Title"


def test_parse_resume_flag():
    assert parse_cli_args(["-r"]).resume is True
    assert parse_cli_args(["--resume"]).resume is True


def test_continuation_flags_mutually_exclusive():
    # --continue and --session can't be combined (argparse exits 2).
    with pytest.raises(SystemExit):
        parse_cli_args(["-p", "-c", "--session", "x", "go"])
    with pytest.raises(SystemExit):
        parse_cli_args(["-p", "--fork", "x", "--resume", "go"])


def test_main_resume_is_deferred_error(capsys):
    rc = cli.main(["--resume"])
    assert rc == 2
    assert "interactive picker" in capsys.readouterr().err


def test_main_continue_without_print_errors(capsys):
    # The continuation/print check runs before load_config(), so no config needed.
    rc = cli.main(["-c"])
    assert rc == 2
    assert "require --print" in capsys.readouterr().err


def test_resolve_no_model_no_default_raises():
    with pytest.raises(CLIError, match="no model"):
        resolve_model_config({"models": {}}, CLIArgs(model=None))


# ── @file / prompt assembly ─────────────────────────────────────────────────

def test_assemble_joins_parts():
    assert assemble_prompt(["hello", "world"]) == "hello\nworld"


def test_assemble_inlines_at_file(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("file body")
    assert assemble_prompt(["summarize", f"@{f}"]) == "summarize\nfile body"


def test_assemble_missing_file_raises():
    with pytest.raises(CLIError, match="file not found"):
        assemble_prompt(["@/no/such/file.txt"])


# ── headless run_print (fake backend) ───────────────────────────────────────

class _FakeBackend:
    def __init__(self, config):
        self.config = config

    async def stream_chat(self, messages, callback, on_event=None):
        self.messages = messages
        deltas = ["Hello ", "world"]
        if on_event is not None:
            on_event({"kind": "turn_start", "turn_index": 0})
        for d in deltas:
            callback(d)
            if on_event is not None:
                on_event({"kind": "text_delta", "delta": d})
        # A realistic (if minimal) agent-loop transcript so the persistence
        # test sees an assistant message land in the saved session.
        new_messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "Hello world"}]},
        ]
        return "Hello world", {"total_tokens": 3}, new_messages, []


@pytest.fixture
def fake_backend(monkeypatch, tmp_path):
    holder = {}

    def factory(config):
        be = _FakeBackend(config)
        holder["backend"] = be
        return be

    monkeypatch.setattr("tau_coding_agent.backends.create_backend", factory)
    # Sandbox session persistence: run_print() now saves a Chat, and tests must
    # not write into the user's real ~/.tau/chats. Chat.save() reads
    # session_store.TAU_DIR at call time, so redirecting it here is sufficient.
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    holder["tau_dir"] = tmp_path
    return holder


async def test_run_print_text_mode(fake_backend, capsys):
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True), _config())
    assert rc == 0
    out = capsys.readouterr().out
    assert out == "Hello world\n"
    # system prompt + user message were passed to the backend
    msgs = fake_backend["backend"].messages
    assert msgs[0] == {"role": "system", "content": "You are helpful."}
    assert msgs[-1] == {"role": "user", "content": "hi"}


async def test_run_print_json_mode(fake_backend, capsys):
    rc = await run_print(
        CLIArgs(messages=["hi"], print_mode=True, mode="json"), _config()
    )
    assert rc == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    kinds = [e["kind"] for e in lines]
    assert kinds == ["turn_start", "text_delta", "text_delta", "done"]
    assert lines[-1]["text"] == "Hello world"
    assert lines[-1]["usage"] == {"total_tokens": 3}


async def test_run_print_requires_message(fake_backend):
    with pytest.raises(CLIError, match="requires a message"):
        await run_print(CLIArgs(messages=[], print_mode=True), _config())


async def test_run_print_system_prompt_override(fake_backend, capsys):
    await run_print(
        CLIArgs(messages=["hi"], print_mode=True, system_prompt="ROLE"),
        _config(),
    )
    assert fake_backend["backend"].messages[0] == {"role": "system", "content": "ROLE"}


# ── headless persistence (sessions resumable from the TUI) ──────────────────

async def test_run_print_persists_resumable_session(fake_backend, capsys):
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True), _config())
    assert rc == 0

    # Exactly one session file written to the sandboxed chats dir.
    chats = list((fake_backend["tau_dir"] / "chats").glob("*.json"))
    assert len(chats) == 1
    saved = json.loads(chats[0].read_text())

    # Resumable from the TUI: `model` is a configured key (on_chat_selected looks
    # it up in config["models"]), and the transcript is [system, user, *loop].
    assert saved["model"] == "local-llm"
    assert saved["backend"] == "openai"
    roles = [m["role"] for m in saved["messages"]]
    assert roles == ["system", "user", "assistant"]
    # The user message is preserved verbatim as the resume anchor / title source.
    assert saved["messages"][1] == {"role": "user", "content": "hi"}


async def test_run_print_persists_in_json_mode_too(fake_backend, capsys):
    # Persistence is independent of output format — json mode saves a session too.
    rc = await run_print(
        CLIArgs(messages=["hi"], print_mode=True, mode="json"), _config()
    )
    assert rc == 0
    assert len(list((fake_backend["tau_dir"] / "chats").glob("*.json"))) == 1


async def test_run_print_save_failure_propagates(fake_backend, monkeypatch):
    # Fail-Early: if persistence fails, surface it — don't swallow it so the run
    # silently "succeeds" without a resumable session.
    import tau_coding_agent.session_store as store

    def boom(self):
        raise OSError("disk full")

    monkeypatch.setattr(store.Chat, "save", boom)
    with pytest.raises(OSError, match="disk full"):
        await run_print(CLIArgs(messages=["hi"], print_mode=True), _config())


# ── main() dispatch ─────────────────────────────────────────────────────────

def test_main_messages_without_print_is_error(capsys):
    rc = cli.main(["hello"])
    assert rc == 2
    assert "without --print" in capsys.readouterr().err


def test_main_json_without_print_is_error(capsys):
    rc = cli.main(["--mode", "json"])
    assert rc == 2
    assert "--mode json" in capsys.readouterr().err


def test_main_print_dispatches_headless(monkeypatch):
    seen = {}

    async def fake_run_print(args, config):
        seen["args"] = args
        return 7

    monkeypatch.setattr(cli, "run_print", fake_run_print)
    monkeypatch.setattr(cli, "load_config", lambda: _config())
    rc = cli.main(["-p", "hello"])
    assert rc == 7
    assert seen["args"].messages == ["hello"]


def test_main_launches_tui_with_overrides(monkeypatch):
    captured = {}

    class FakeParley:
        def __init__(self, cli_overrides=None):
            captured["overrides"] = cli_overrides

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr("tau_coding_agent.app.Parley", FakeParley)
    monkeypatch.setattr(cli, "load_config", lambda: _config())
    rc = cli.main(["--model", "gpt-4o"])
    assert rc == 0 and captured["ran"] is True
    assert captured["overrides"]["default_model"] == "gpt-4o"
    assert "gpt-4o" in captured["overrides"]["models"]
