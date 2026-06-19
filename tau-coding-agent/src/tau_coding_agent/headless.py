"""Headless (non-interactive) run path for the τ CLI's ``--print`` mode.

This drives the *same* agent path the TUI uses — ``create_backend(model_config)``
→ ``backend.stream_chat(messages, callback, on_event)`` — but renders to stdout
instead of Textual widgets. It deliberately does NOT touch ``run_agent_loop.py``
(that file is a meta-orchestrator that shells out to ``pi`` to build τ; it is not
a headless τ runner).

Model resolution mirrors ``Parley.action_new_chat`` (``app.py``): a ``--model``
name is looked up in the ``models`` map of ``~/.tau/config.json``; the selected
entry's ``backend``/``model``/``base_url``/``api_key`` are handed to
``create_backend`` unchanged. CLI flags override per-invocation.

Reference: docs/CLI-PLAN.md (Core flag set).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

# Persistence is the Textual-free session_store module, so a headless run can
# write a sidebar-visible, resumable session without importing the TUI.
from tau_coding_agent.session_store import Chat

if TYPE_CHECKING:  # avoid importing the dataclass module at runtime cost
    from tau_coding_agent.cli import CLIArgs


# pi's thinking levels (``args.ts:57``). We detect a ``model:level`` suffix only
# to FAIL EARLY: τ-ai has no ``reasoning_effort`` send-path yet, so silently
# dropping the level would corrupt the request contract.
THINKING_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh"}
)


class CLIError(Exception):
    """A user-facing CLI error. ``main()`` prints it and exits non-zero."""


def resolve_model_config(config: dict, args: "CLIArgs") -> tuple[str, dict]:
    """Resolve ``--model``/``--provider``/``--tools`` into a backend config dict.

    Returns ``(model_name, model_config)`` where ``model_config`` is the dict
    handed to :func:`tau_coding_agent.backends.create_backend` (keys
    ``backend``/``model``/``base_url``/``api_key`` and optionally ``tools``).

    Resolution order, mirroring the TUI and pi's ``resolveCliModel``:
      1. ``--model NAME`` matching a key in ``config["models"]`` → that entry.
      2. ``provider/id`` shorthand → an ad-hoc entry (provider from the prefix).
      3. a bare id → an ad-hoc entry (provider from ``--provider`` or ``openai``).
    A ``:level`` thinking suffix raises (unsupported; see ``THINKING_LEVELS``).
    """
    models = config.get("models", {})
    spec = args.model or config.get("default_model")
    if not spec:
        raise CLIError(
            "no model specified and no 'default_model' in config; "
            "pass --model NAME or set default_model in ~/.tau/config.json"
        )

    if spec in models:
        # Exact config-key match wins (so a key may legitimately contain a colon).
        model_config = dict(models[spec])
    else:
        # Fail-Early on a thinking suffix (split on the LAST colon, like pi)
        # before treating the remainder as an ad-hoc model id.
        _head, sep, tail = spec.rpartition(":")
        if sep and tail in THINKING_LEVELS:
            raise CLIError(
                f"thinking level ':{tail}' is not yet supported — τ-ai has no "
                "reasoning_effort send-path. See docs/CLI-PLAN.md (deferred flags)."
            )
        # Ad-hoc model not present in the config map.
        if "/" in spec:
            prov, _, mid = spec.partition("/")
        else:
            prov, mid = (args.provider or "openai"), spec
        if not mid:
            raise CLIError(f"invalid --model value: {spec!r}")
        model_config = {"backend": prov, "model": mid}

    # Per-invocation overrides (CLI > config).
    if args.provider:
        model_config["backend"] = args.provider
    if args.no_tools:
        model_config["tools"] = []
    elif args.tools:
        names = [t.strip() for t in args.tools.split(",") if t.strip()]
        if not names:
            raise CLIError("--tools given but no tool names parsed")
        model_config["tools"] = names

    return spec, model_config


def assemble_prompt(messages: list[str]) -> str:
    """Join positional message parts, expanding ``@file`` references.

    A part beginning with ``@`` is a file reference (pi: ``args.ts:186``); its
    contents are inlined. Missing files raise (Fail-Early), surfaced by
    ``main()`` as a clean error rather than a silent skip.
    """
    parts: list[str] = []
    for part in messages:
        if part.startswith("@"):
            ref = part[1:]
            try:
                parts.append(Path(ref).read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise CLIError(f"file not found: {ref}") from exc
            except OSError as exc:
                raise CLIError(f"cannot read {ref}: {exc}") from exc
        else:
            parts.append(part)
    return "\n".join(parts).strip()


async def run_print(args: "CLIArgs", config: dict) -> int:
    """Run one headless turn and render to stdout. Returns a process exit code.

    ``--mode text`` streams raw assistant text deltas (a plain transcript).
    ``--mode json`` emits one JSON object per line: the backend's normalized
    lifecycle events (``turn_start``/``text_delta``/``tool_call``/``tool_result``)
    followed by a final ``{"kind": "done", ...}`` record with usage.
    """
    prompt_text = assemble_prompt(args.messages)
    if not prompt_text:
        raise CLIError(
            "--print requires a message (positional text or @file), e.g. "
            "tau -p \"summarize @README.md\""
        )

    name, model_config = resolve_model_config(config, args)

    # System prompt flows as the first context message (matching the TUI), so
    # the backend's own system_prompt stays empty and is not double-counted.
    system_prompt = (
        args.system_prompt
        if args.system_prompt is not None
        else config.get("system_prompt", "")
    )
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt_text})

    # Imported lazily: keeps `import tau_coding_agent.headless` free of the
    # backend/agent-core import chain until a run actually happens.
    from tau_coding_agent.backends import create_backend

    backend = create_backend(model_config)

    if args.mode == "json":
        def on_event(event: dict) -> None:
            sys.stdout.write(json.dumps(event) + "\n")
            sys.stdout.flush()

        def noop(_delta: str) -> None:
            pass

        text, usage, new_messages, _tcs = await backend.stream_chat(
            messages, noop, on_event=on_event
        )
        sys.stdout.write(
            json.dumps({"kind": "done", "text": text, "usage": usage}) + "\n"
        )
        sys.stdout.flush()
    else:  # text
        def emit(delta: str) -> None:
            sys.stdout.write(delta)
            sys.stdout.flush()

        _text, _usage, new_messages, _tcs = await backend.stream_chat(messages, emit)
        sys.stdout.write("\n")
        sys.stdout.flush()

    # Persist the run as a resumable session (same on-disk format the TUI uses),
    # so `tau -p` conversations appear in the sidebar and can be continued there.
    _save_session(name, model_config, messages, new_messages)

    return 0


def _save_session(
    name: str,
    model_config: dict,
    context_messages: list[dict],
    new_messages: list[dict],
) -> Path:
    """Persist a headless run as a :class:`Chat` under ``~/.tau/chats/``.

    The saved transcript mirrors ``Parley._get_assistant_response``: the
    ``[system?, user]`` context messages followed by the agent loop's non-user
    output (assistant + toolResult messages). ``name`` — the config key or
    shorthand from ``resolve_model_config`` — is stored as the chat's ``model``
    so the TUI can resolve a backend when the session is resumed.
    """
    transcript = list(context_messages)
    transcript.extend(m for m in new_messages if m.get("role") != "user")
    chat = Chat(
        model=name,
        backend=model_config.get("backend", ""),
        messages=transcript,
        created_at=time.time(),
    )
    return chat.save()
