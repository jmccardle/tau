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
# write a sidebar-visible, resumable session without importing the TUI. The
# module itself is imported (not just `Chat`) so `session_store.TAU_DIR` is read
# dynamically — tests monkeypatch it, and a stale module-level copy would miss.
from tau_coding_agent import session_store
from tau_coding_agent.session_store import Chat

# Canonical thinking levels live in τ-ai (single source of truth); pi: the same
# set in ``args.ts:57``. A ``model:level`` suffix or the ``--thinking`` flag is
# carried into the model config as ``thinking`` and threaded to the provider as
# ``reasoning_effort``.
from tau_ai.models import is_valid_thinking_level

if TYPE_CHECKING:  # avoid importing the dataclass module at runtime cost
    from tau_coding_agent.cli import CLIArgs


class CLIError(Exception):
    """A user-facing CLI error. ``main()`` prints it and exits non-zero."""


def resolve_model_config(
    config: dict, args: "CLIArgs", fallback_model: str | None = None
) -> tuple[str, dict]:
    """Resolve ``--model``/``--provider``/``--tools`` into a backend config dict.

    Returns ``(model_name, model_config)`` where ``model_config`` is the dict
    handed to :func:`tau_coding_agent.backends.create_backend` (keys
    ``backend``/``model``/``base_url``/``api_key`` and optionally ``tools``).

    Resolution order, mirroring the TUI and pi's ``resolveCliModel``:
      1. ``--model NAME`` matching a key in ``config["models"]`` → that entry.
      2. ``provider/id`` shorthand → an ad-hoc entry (provider from the prefix).
      3. a bare id → an ad-hoc entry (provider from ``--provider`` or ``openai``).
    A ``model:level`` thinking suffix (or the ``--thinking`` flag) sets the
    requested reasoning level on ``model_config["thinking"]``.

    ``fallback_model`` is the model to use when ``--model`` is absent — for a
    resumed session this is the stored session's model, so a bare ``tau -p -c``
    continues on the same model (pi: a continued session keeps its model unless
    ``--model`` overrides). It takes precedence over ``default_model``.
    """
    models = config.get("models", {})
    spec = args.model or fallback_model or config.get("default_model")
    if not spec:
        raise CLIError(
            "no model specified and no 'default_model' in config; "
            "pass --model NAME or set default_model in ~/.tau/config.json"
        )

    suffix_thinking: str | None = None
    if spec in models:
        # Exact config-key match wins (so a key may legitimately contain a colon).
        model_config = dict(models[spec])
    else:
        # Parse a ``model:level`` thinking suffix (split on the LAST colon, like
        # pi resolveCliModel) before treating the remainder as an ad-hoc id.
        head, sep, tail = spec.rpartition(":")
        spec_id = spec
        if sep and is_valid_thinking_level(tail):
            suffix_thinking = tail
            spec_id = head
        # Ad-hoc model not present in the config map.
        if "/" in spec_id:
            prov, _, mid = spec_id.partition("/")
        else:
            prov, mid = (args.provider or "openai"), spec_id
        if not mid:
            raise CLIError(f"invalid --model value: {spec!r}")
        model_config = {"backend": prov, "model": mid}

    # Requested thinking level: an explicit ``--thinking`` flag wins over a
    # ``:level`` suffix (pi: ``cliThinking ?? fallbackThinking``). argparse has
    # already validated ``args.thinking`` against the known levels.
    thinking = args.thinking or suffix_thinking
    if thinking is not None:
        model_config["thinking"] = thinking

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


def _resolve_selector(selector: str) -> tuple[Path, Chat]:
    """Resolve a ``--session``/``--fork`` REF to an existing ``(path, Chat)``.

    A REF is either a path to a ``.json`` chat file, or a filename *stem* — the
    integer timestamp ``~/.tau/chats/<stem>.json`` is named with. An exact stem
    match wins; otherwise a unique substring match is accepted. Zero or multiple
    matches raise (Fail-Early: never guess which session was meant).
    """
    p = Path(selector)
    if p.suffix == ".json" and p.exists():
        return p, Chat.load(p)

    recent = Chat.list_recent(limit=10_000)
    exact = [c for c in recent if c.stem == selector]
    matches = exact or [c for c in recent if selector in c.stem]
    if not matches:
        raise CLIError(
            f"no session matches {selector!r} (looked for a .json path and a "
            f"filename stem under ~/.tau/chats)"
        )
    if len(matches) > 1:
        names = ", ".join(sorted(c.stem for c in matches))
        raise CLIError(f"{selector!r} matches multiple sessions ({names}); be more specific")
    return matches[0], Chat.load(matches[0])


def _select_chat(args: "CLIArgs") -> tuple[Path, Chat] | None:
    """Resolve the continuation flags to an existing ``(path, Chat)``, or None.

    ``--continue`` selects the most recent session; ``--session``/``--fork``
    select a specific one. The flags are mutually exclusive at the argparse
    layer, so at most one is set. Returns None for a fresh run.
    """
    if args.continue_session:
        recent = Chat.list_recent(limit=1)
        if not recent:
            raise CLIError("no saved sessions to continue (~/.tau/chats is empty)")
        return recent[0], Chat.load(recent[0])

    selector = args.session or args.fork
    if selector is None:
        return None
    return _resolve_selector(selector)


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
            'tau -p "summarize @README.md"'
        )

    # Resolve a session to continue/fork (None for a fresh run).
    selection = _select_chat(args)
    prior_chat = selection[1] if selection else None

    # The stored session already carries its system message; injecting another
    # (or silently dropping an override) would both be wrong — reject the combo.
    if prior_chat is not None and args.system_prompt is not None:
        raise CLIError(
            "--system-prompt can't be combined with --continue/--session/--fork; "
            "the resumed session already has a system prompt"
        )

    # A resumed run keeps the session's model unless --model overrides it.
    fallback_model = prior_chat.model if prior_chat is not None else None
    name, model_config = resolve_model_config(config, args, fallback_model=fallback_model)

    if prior_chat is not None:
        # Resume/fork: the stored transcript (system + every prior turn) is the
        # context; append only this turn's new user message.
        messages: list[dict] = list(prior_chat.messages)
        messages.append({"role": "user", "content": prompt_text})
    else:
        # Fresh run: system prompt flows as the first context message (matching
        # the TUI), so the backend's own system_prompt stays empty and is not
        # double-counted.
        system_prompt = (
            args.system_prompt
            if args.system_prompt is not None
            else config.get("system_prompt", "")
        )
        messages = []
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
        sys.stdout.write(json.dumps({"kind": "done", "text": text, "usage": usage}) + "\n")
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
    # In-place for --continue/--session; a new file for --fork or a fresh run.
    _persist_session(
        name,
        model_config,
        messages,
        new_messages,
        prior_chat=prior_chat,
        is_fork=args.fork is not None,
        title=args.name,
    )

    return 0


def _persist_session(
    name: str,
    model_config: dict,
    context_messages: list[dict],
    new_messages: list[dict],
    *,
    prior_chat: Chat | None = None,
    is_fork: bool = False,
    title: str | None = None,
) -> Path:
    """Persist a headless run as a :class:`Chat` under ``~/.tau/chats/``.

    The saved transcript mirrors ``Parley._get_assistant_response``: the context
    messages (system + any prior turns + this turn's user message) followed by
    the agent loop's non-user output (assistant + toolResult messages). Three
    modes:

    - **fresh run** (``prior_chat is None``) → a new ``<created_at>.json``.
    - **continue/resume** (``prior_chat`` set, ``is_fork`` False) → overwrite the
      same file in place, preserving its ``created_at`` and growing its history.
    - **fork** (``is_fork`` True) → a new file holding ``prior_chat``'s history
      plus this turn, leaving the source session file untouched.

    ``name`` — the config key or shorthand from ``resolve_model_config`` — is
    stored as the chat's ``model`` so the TUI can resolve a backend on resume.
    """
    transcript = list(context_messages)
    transcript.extend(m for m in new_messages if m.get("role") != "user")
    backend = model_config.get("backend", "")

    if prior_chat is not None and not is_fork:
        # In-place continue: Chat.save() targets <created_at>.json, so reusing
        # the loaded chat's created_at overwrites (grows) the same file.
        prior_chat.messages = transcript
        prior_chat.model = name
        prior_chat.backend = backend
        if title is not None:
            prior_chat.title = title
        return prior_chat.save()

    # Fresh run or fork → a new file. Guard against same-second filename
    # collisions (the store keys files on int(created_at)) so a fork never
    # clobbers its source and a rapid fresh run never overwrites a sibling.
    chats_dir = session_store.TAU_DIR / "chats"
    created = time.time()
    while (chats_dir / f"{int(created)}.json").exists():
        created += 1.0
    chat = Chat(
        model=name,
        backend=backend,
        messages=transcript,
        created_at=created,
        title=title,
    )
    return chat.save()
