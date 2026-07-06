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
        [
            "--model",
            "gpt-4o",
            "--provider",
            "openai",
            "-t",
            "read,bash",
            "-p",
            "--mode",
            "json",
            "do it",
        ]
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


# ── extension / tool-filter / session flags (E0/S2, pi args.ts:104-153) ──────


def test_extension_flag_is_repeatable_path():
    # -e / --extension append; None-default normalizes to [].
    assert parse_cli_args(["-p", "x"]).extensions == []
    args = parse_cli_args(["-e", "a.py", "--extension", "b.py", "-p", "x"])
    assert args.extensions == ["a.py", "b.py"]


def test_no_extensions_flag_aliases():
    assert parse_cli_args(["-ne", "-p", "x"]).no_extensions is True
    assert parse_cli_args(["--no-extensions", "-p", "x"]).no_extensions is True
    assert parse_cli_args(["-p", "x"]).no_extensions is False


def test_no_extensions_keeps_explicit_extension():
    # -ne disables DISCOVERY only; an explicit -e must survive alongside it.
    args = parse_cli_args(["-e", "keep.py", "-ne", "-p", "x"])
    assert args.extensions == ["keep.py"]
    assert args.no_extensions is True


def test_exclude_tools_flag_aliases():
    assert parse_cli_args(["-xt", "bash,write", "-p", "x"]).exclude_tools == "bash,write"
    assert parse_cli_args(["--exclude-tools", "read", "-p", "x"]).exclude_tools == "read"
    assert parse_cli_args(["-p", "x"]).exclude_tools is None


def test_no_builtin_tools_flag_aliases():
    assert parse_cli_args(["-nbt", "-p", "x"]).no_builtin_tools is True
    assert parse_cli_args(["--no-builtin-tools", "-p", "x"]).no_builtin_tools is True
    assert parse_cli_args(["-p", "x"]).no_builtin_tools is False


def test_no_session_flag():
    assert parse_cli_args(["--no-session", "-p", "x"]).no_session is True
    assert parse_cli_args(["-p", "x"]).no_session is False


def test_append_system_prompt_is_repeatable():
    assert parse_cli_args(["-p", "x"]).append_system_prompt == []
    args = parse_cli_args(
        ["--append-system-prompt", "one", "--append-system-prompt", "two", "-p", "x"]
    )
    assert args.append_system_prompt == ["one", "two"]


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


def test_resolve_folds_top_level_reasoning_replay_default():
    """A top-level ``reasoning_replay`` in config is folded into the entry when the
    entry sets none of its own (per-model wins; else this global default)."""
    cfg = {**_config(), "reasoning_replay": "off"}
    _name, mc = resolve_model_config(cfg, CLIArgs(model="gpt-4o"))
    assert mc["reasoning_replay"] == "off"


def test_resolve_per_model_reasoning_replay_beats_global():
    """A per-model ``reasoning_replay`` is NOT overwritten by the global default."""
    cfg = _config()
    cfg["models"]["gpt-4o"]["reasoning_replay"] = "all"
    cfg["reasoning_replay"] = "off"
    _name, mc = resolve_model_config(cfg, CLIArgs(model="gpt-4o"))
    assert mc["reasoning_replay"] == "all"


def test_resolve_no_reasoning_replay_leaves_key_absent():
    """With neither a per-model nor a global value, the key is absent — so
    build_model_from_config applies its own ``turn`` default (not staged here)."""
    _name, mc = resolve_model_config(_config(), CLIArgs(model="gpt-4o"))
    assert "reasoning_replay" not in mc


def test_resolve_tools_allowlist():
    _name, mc = resolve_model_config(_config(), CLIArgs(model="gpt-4o", tools="read, bash"))
    assert mc["tools"] == ["read", "bash"]


def test_resolve_no_builtin_tools_empties_builtins():
    # --no-builtin-tools drops the built-in set (tools=[]); extension tools survive
    # the later _build_turn_tools merge, so this is now distinct from --no-tools once
    # extensions load (E5 S28). resolve_model_config only stages the built-in side.
    _name, mc = resolve_model_config(_config(), CLIArgs(model="gpt-4o", no_builtin_tools=True))
    assert mc["tools"] == []


def test_resolve_exclude_tools_reaches_run_config():
    _name, mc = resolve_model_config(
        _config(), CLIArgs(model="gpt-4o", exclude_tools="bash, write")
    )
    assert mc["exclude_tools"] == ["bash", "write"]


def test_resolve_exclude_tools_empty_raises():
    with pytest.raises(CLIError, match="no tool names parsed"):
        resolve_model_config(_config(), CLIArgs(model="gpt-4o", exclude_tools=" , "))


def test_resolve_extensions_reach_run_config():
    _name, mc = resolve_model_config(
        _config(), CLIArgs(model="gpt-4o", extensions=["a.py", "b.py"])
    )
    assert mc["extensions"] == ["a.py", "b.py"]
    # No discovery toggle unless asked.
    assert "no_extensions" not in mc


def test_resolve_no_extensions_keeps_explicit_extension_in_run_config():
    # -ne suppresses discovery (no_extensions flag reaches the config) while an
    # explicit -e path still lands in the run config for the loader to honor.
    _name, mc = resolve_model_config(
        _config(), CLIArgs(model="gpt-4o", extensions=["keep.py"], no_extensions=True)
    )
    assert mc["extensions"] == ["keep.py"]
    assert mc["no_extensions"] is True


def test_resolve_append_system_prompt_reaches_run_config():
    _name, mc = resolve_model_config(
        _config(), CLIArgs(model="gpt-4o", append_system_prompt=["extra rule"])
    )
    assert mc["append_system_prompt"] == ["extra rule"]


def test_resolve_provider_override():
    _name, mc = resolve_model_config(_config(), CLIArgs(model="gpt-4o", provider="anthropic"))
    assert mc["backend"] == "anthropic"


def test_resolve_provider_slash_id_shorthand():
    name, mc = resolve_model_config(_config(), CLIArgs(model="openai/gpt-4o-mini"))
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
    _name, mc = resolve_model_config(_config(), CLIArgs(model="gpt-4o", thinking="medium"))
    assert mc["thinking"] == "medium"


def test_resolve_thinking_flag_overrides_suffix():
    # An explicit --thinking wins over a :level suffix (pi: cliThinking ?? suffix).
    _name, mc = resolve_model_config(_config(), CLIArgs(model="some-model:low", thinking="high"))
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
    args = parse_cli_args(["-p", "--session", "1718", "--name", "My chat", "go"])
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

    async def load_extensions(
        self, explicit_paths=None, *, discover=True, user_dir=None, extensions_config=None
    ):
        from tau_agent_core.sdk import LoadExtensionsResult

        self.loaded_extensions = (explicit_paths, discover)  # capture for wiring assertions
        self.loaded_ext_config = extensions_config  # S40: capture the resolved config map
        return LoadExtensionsResult()

    async def stream_chat(self, messages, callback, on_event=None, on_pi_event=None):
        self.messages = messages
        deltas = ["Hello ", "world"]
        if on_event is not None:
            on_event({"kind": "turn_start", "turn_index": 0})
        for d in deltas:
            callback(d)
            if on_event is not None:
                on_event({"kind": "text_delta", "delta": d})
        # pi-faithful ``--mode json`` sink (step S8): the real TauBackend feeds
        # this from the AgentEvent bus; here a minimal but shaped stand-in proves
        # headless writes the header FIRST and forwards these ``type``-discriminated
        # events (no ``kind``/``done``). The message_end carries usage/model/
        # stop_reason, the per-child limit signal the delegate reads.
        if on_pi_event is not None:
            on_pi_event({"type": "turn_start", "turn_index": 0})
            on_pi_event(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hello world"}],
                        "usage": {"total_tokens": 3},
                        "model": "qwen3-32b-kv4b",
                        "stop_reason": "stop",
                    },
                }
            )
            on_pi_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Hello world"}]},
                    ],
                }
            )
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
    # Sandbox session persistence: run_print() appends to a JSONL Session under
    # ~/.tau/sessions, and tests must not write into the user's real dir. The
    # store reads session_store.TAU_DIR at call time, so redirecting it suffices.
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    holder["tau_dir"] = tmp_path
    return holder


def _session_files(tau_dir) -> list:
    """Every persisted session file under the sandboxed sessions dir (any cwd)."""
    return list((tau_dir / "sessions").rglob("*.jsonl"))


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
    # pi-faithful --mode json (step S8): the session HEADER line first, then
    # ``type``-discriminated AgentSessionEvents. No legacy ``kind`` key, no
    # synthetic ``done`` line.
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True, mode="json"), _config())
    assert rc == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]

    # Header FIRST (pi print-mode.ts:113-116): the raw session header entry.
    assert lines[0]["type"] == "session"

    # Every line is ``type``-discriminated — never the legacy ``kind`` schema.
    assert all("kind" not in e for e in lines)
    types = [e["type"] for e in lines]
    assert types == ["session", "turn_start", "message_end", "agent_end"]

    # The message_end carries the per-message usage/model/stop_reason the delegate
    # (step S9) reads for per-child limits + the stop_reason taxonomy.
    (message_end,) = [e for e in lines if e["type"] == "message_end"]
    assert message_end["message"]["usage"] == {"total_tokens": 3}
    assert message_end["message"]["model"] == "qwen3-32b-kv4b"
    assert message_end["message"]["stop_reason"] == "stop"


async def test_run_print_requires_message(fake_backend):
    with pytest.raises(CLIError, match="requires a message"):
        await run_print(CLIArgs(messages=[], print_mode=True), _config())


async def test_run_print_system_prompt_override(fake_backend, capsys):
    await run_print(
        CLIArgs(messages=["hi"], print_mode=True, system_prompt="ROLE"),
        _config(),
    )
    assert fake_backend["backend"].messages[0] == {"role": "system", "content": "ROLE"}


async def test_run_print_appends_system_prompt(fake_backend, capsys):
    """--append-system-prompt augments (not replaces) the base prompt (S28)."""
    await run_print(
        CLIArgs(messages=["hi"], print_mode=True, append_system_prompt=["EXTRA RULE"]),
        _config(),
    )
    sys_msg = fake_backend["backend"].messages[0]
    assert sys_msg["role"] == "system"
    # Base config prompt is kept; the appended section follows it.
    assert "You are helpful." in sys_msg["content"]
    assert "EXTRA RULE" in sys_msg["content"]


async def test_run_print_plumbs_ext_config(fake_backend, capsys):
    """--ext-config merges over config.json "extensions" and reaches the backend (S40)."""
    config = {**_config(), "extensions": {"budget": {"ceiling": 1.0, "warn": 0.5}}}
    await run_print(
        CLIArgs(messages=["hi"], print_mode=True, ext_config=["budget.ceiling=9.0"]),
        config,
    )
    # CLI override wins on ceiling; config.json warn survives.
    assert fake_backend["backend"].loaded_ext_config == {"budget": {"ceiling": 9.0, "warn": 0.5}}


async def test_run_print_ext_config_empty_by_default(fake_backend, capsys):
    """No config.json "extensions" and no --ext-config → an empty map (S40)."""
    await run_print(CLIArgs(messages=["hi"], print_mode=True), _config())
    assert fake_backend["backend"].loaded_ext_config == {}


# ── headless persistence (sessions resumable from the TUI) ──────────────────


async def test_run_print_persists_resumable_session(fake_backend, capsys):
    from tau_coding_agent.session_store import Session

    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True), _config())
    assert rc == 0

    # Exactly one session file written to the sandboxed sessions dir.
    files = _session_files(fake_backend["tau_dir"])
    assert len(files) == 1
    saved = Session.load(files[0])

    # Resumable from the TUI: `model` is a configured key (on_chat_selected looks
    # it up in config["models"]), and the transcript is [system, user, *loop].
    assert saved.model == "local-llm"
    assert saved.backend == "openai"
    roles = [m["role"] for m in saved.messages]
    assert roles == ["system", "user", "assistant"]
    # The user message is preserved verbatim as the resume anchor / title source.
    assert saved.messages[1] == {"role": "user", "content": "hi"}


async def test_run_print_persists_in_json_mode_too(fake_backend, capsys):
    # Persistence is independent of output format — json mode saves a session too.
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True, mode="json"), _config())
    assert rc == 0
    assert len(_session_files(fake_backend["tau_dir"])) == 1


async def test_run_print_no_session_is_ephemeral(fake_backend, capsys):
    # --no-session runs against an in-memory session (path=None): the turn still
    # streams, but nothing is written to the sandboxed sessions dir.
    rc = await run_print(CLIArgs(messages=["hi"], print_mode=True, no_session=True), _config())
    assert rc == 0
    assert capsys.readouterr().out == "Hello world\n"
    assert _session_files(fake_backend["tau_dir"]) == []


async def test_run_print_no_session_rejects_continue(fake_backend):
    with pytest.raises(CLIError, match="--no-session can't be combined"):
        await run_print(
            CLIArgs(messages=["hi"], print_mode=True, no_session=True, continue_session=True),
            _config(),
        )


async def test_run_print_save_failure_propagates(fake_backend, monkeypatch):
    # Fail-Early: if persistence fails, surface it — don't swallow it so the run
    # silently "succeeds" without a resumable session.
    import tau_coding_agent.session_store as store

    def boom(self, entry):
        raise OSError("disk full")

    monkeypatch.setattr(store.Session, "_persist_entry", boom)
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
        def __init__(self, cli_overrides=None, cli_run_config=None):
            captured["overrides"] = cli_overrides
            captured["run_config"] = cli_run_config

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr("tau_coding_agent.app.Parley", FakeParley)
    monkeypatch.setattr(cli, "load_config", lambda: _config())
    rc = cli.main(
        [
            "--model",
            "gpt-4o",
            "-e",
            "demo.py",
            "-xt",
            "bash, write",
            "--append-system-prompt",
            "RULE",
        ]
    )
    assert rc == 0 and captured["ran"] is True
    assert captured["overrides"]["default_model"] == "gpt-4o"
    assert "gpt-4o" in captured["overrides"]["models"]
    # Run-level flags reach the app separately from the model overrides (S28):
    # extensions, the parsed exclude-tools denylist, and the appended prompt.
    rcfg = captured["run_config"]
    assert rcfg["extensions"] == ["demo.py"]
    assert rcfg["no_extensions"] is False
    assert rcfg["exclude_tools"] == ["bash", "write"]
    assert rcfg["no_builtin_tools"] is False
    assert rcfg["append_system_prompt"] == ["RULE"]
