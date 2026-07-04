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
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Persistence is the Textual-free session_store module, so a headless run can
# write a picker-visible, resumable session without importing the TUI. Sessions
# are append-only JSONL files partitioned by cwd (docs/SESSION-UX-REDESIGN.md);
# the listing helpers read the dir lazily, so tests that monkeypatch
# ``session_store.TAU_DIR`` redirect storage without a stale module-level copy.
from tau_coding_agent.session_store import Session, list_sessions, most_recent

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
    # Tool selection. --no-tools disables everything; --no-builtin-tools DEGENERATES
    # to the same until E1 lands registered (extension) tools in the loop — pi keeps
    # them distinct (noTools "all" vs "builtin", args.ts:104-142; main.ts:423-427),
    # but with no non-builtin tools yet, "disable builtins" == "disable all". Documented
    # in docs/EXTENSIONS-IMPLEMENTATION.md §E0.2 / §8 S2.
    if args.no_tools or args.no_builtin_tools:
        model_config["tools"] = []
    elif args.tools:
        names = [t.strip() for t in args.tools.split(",") if t.strip()]
        if not names:
            raise CLIError("--tools given but no tool names parsed")
        model_config["tools"] = names

    # --exclude-tools denylist (pi excludeTools, args.ts:143-153). Carried on the run
    # config; the filter is applied at tool resolution in E1 (§8 S3+).
    if args.exclude_tools is not None:
        excluded = [t.strip() for t in args.exclude_tools.split(",") if t.strip()]
        if not excluded:
            raise CLIError("--exclude-tools given but no tool names parsed")
        model_config["exclude_tools"] = excluded

    # Extensions: explicit --extension paths + the discovery toggle (pi args.ts:150-153).
    # Loaded/bound to the live session in E1 (§8 S3+); staged on the run config here.
    if args.extensions:
        model_config["extensions"] = list(args.extensions)
    if args.no_extensions:
        model_config["no_extensions"] = True

    # Appended system-prompt sections (pi appendSystemPrompt, system-prompt.ts:48).
    # Combined with the base coding prompt by the E1 system-prompt builder (§8 S3+);
    # kept off the base ``system_prompt`` so it augments rather than replaces it.
    if args.append_system_prompt:
        model_config["append_system_prompt"] = list(args.append_system_prompt)

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


def _resolve_session_ref(selector: str, *, all_sessions: bool = False) -> Session:
    """Resolve a ``--session``/``--fork`` REF to a loaded :class:`Session`.

    A REF is either a path to a ``.jsonl`` session file, or a session **id** (the
    uuid in the header/filename) — an exact id match wins; otherwise a unique id
    *prefix* is accepted. Resolution is scoped to the **current cwd's** session
    dir (``all_sessions`` widens to every dir). Zero or multiple matches raise
    (Fail-Early: never guess which session was meant).
    """
    p = Path(selector)
    if p.suffix == ".jsonl" and p.exists():
        return Session.load(p)

    infos = list_sessions(None if all_sessions else os.getcwd())
    exact = [i for i in infos if i.id == selector]
    matches = exact or [i for i in infos if i.id.startswith(selector)]
    if not matches:
        scope = "any directory" if all_sessions else "this directory"
        raise CLIError(
            f"no session matches {selector!r} (looked for a .jsonl path and a "
            f"session id under {scope}'s ~/.tau/sessions history)"
        )
    if len(matches) > 1:
        ids = ", ".join(sorted(i.id for i in matches))
        raise CLIError(f"{selector!r} matches multiple sessions ({ids}); be more specific")
    return Session.load(matches[0].path)


def _select_session(args: "CLIArgs") -> Session | None:
    """Resolve the continuation flags to a loaded source :class:`Session`, or None.

    ``--continue`` selects the most recent session in the current cwd;
    ``--session``/``--fork`` select a specific one. The flags are mutually
    exclusive at the argparse layer, so at most one is set. Returns None for a
    fresh run. For ``--fork`` this is the *source* — the caller forks it.
    """
    if args.continue_session:
        path = most_recent(os.getcwd())
        if path is None:
            raise CLIError(
                "no saved sessions to continue (this directory has no ~/.tau/sessions history)"
            )
        return Session.load(path)

    selector = args.session or args.fork
    if selector is None:
        return None
    return _resolve_session_ref(selector)


def _apply_resume_metadata(
    session: Session, model_name: str, backend_name: str, prior: Session, title: str | None
) -> None:
    """On resume/fork, record a model switch and/or a rename if they changed."""
    if model_name != prior.model or backend_name != prior.backend:
        session.append_model_change(model_name, backend_name)
    if title is not None:
        session.append_session_info(title)


async def run_print(args: "CLIArgs", config: dict) -> int:
    """Run one headless turn and render to stdout. Returns a process exit code.

    ``--mode text`` streams raw assistant text deltas (a plain transcript).
    ``--mode json`` emits one JSON object per line: the backend's normalized
    lifecycle events (``turn_start``/``text_delta``/``tool_call``/``tool_result``)
    followed by a final ``{"kind": "done", ...}`` record with usage.

    The run is persisted as an append-only JSONL session under the current cwd's
    ``~/.tau/sessions`` dir — each produced message is appended as it is known
    (no whole-file rewrite). In-place for ``--continue``/``--session``; a new
    forked file for ``--fork``; a fresh file otherwise.
    """
    prompt_text = assemble_prompt(args.messages)
    if not prompt_text:
        raise CLIError(
            "--print requires a message (positional text or @file), e.g. "
            'tau -p "summarize @README.md"'
        )

    # --no-session runs ephemerally (no on-disk file), so resuming/forking a
    # persisted session is contradictory — reject it rather than silently ignore
    # either flag (Fail-Early). The continuation flags are mutually exclusive at
    # the argparse layer, so at most one is set here.
    if args.no_session and (args.continue_session or args.session or args.fork):
        raise CLIError(
            "--no-session can't be combined with --continue/--session/--fork "
            "(those resume or fork a persisted session)"
        )

    # Resolve a source session to continue/fork (None for a fresh run).
    prior = _select_session(args)

    # The stored session already carries its system message; injecting another
    # (or silently dropping an override) would both be wrong — reject the combo.
    if prior is not None and args.system_prompt is not None:
        raise CLIError(
            "--system-prompt can't be combined with --continue/--session/--fork; "
            "the resumed session already has a system prompt"
        )

    # A resumed run keeps the session's model unless --model overrides it.
    fallback_model = prior.model if prior is not None else None
    model_name, model_config = resolve_model_config(config, args, fallback_model=fallback_model)
    backend_name = model_config.get("backend", "")
    cwd = os.getcwd()

    if prior is None:
        # Fresh run: system prompt is stored as the first message entry (matching
        # the TUI), so the backend's own system_prompt stays empty and is not
        # double-counted.
        system_prompt = (
            args.system_prompt
            if args.system_prompt is not None
            else config.get("system_prompt", "")
        )
        # --no-session → ephemeral (path=None, appends never touch disk); the
        # create_in_memory seam is the one-API alternative to create (§E0.2).
        create = Session.create_in_memory if args.no_session else Session.create
        session = create(
            cwd, model_name, backend_name, system_prompt=system_prompt or None, name=args.name
        )
    elif args.fork is not None:
        session = Session.fork(prior, cwd)
        _apply_resume_metadata(session, model_name, backend_name, prior, args.name)
    else:  # --continue / --session: append in place
        session = prior
        _apply_resume_metadata(session, model_name, backend_name, prior, args.name)

    # This turn's user message, then hand the active-path CONTEXT to the backend
    # (cursor + compaction/branch splices applied) — not the raw linear fold, so a
    # resumed compacted/branched session gives the model the right history (§2.6).
    session.append_message({"role": "user", "content": prompt_text})
    messages: list[dict] = session.context

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
        # The final ``done`` is the emit boundary for the headless json path. Cost
        # was priced in the backend from ``model_config``'s optional per-model
        # ``cost`` block (E4.cost / step S7) and rides inside ``usage`` as
        # ``cost_usd`` — present only when the block is configured (an absent block
        # stays tokens-only; never a fabricated ``$0``). Final event only.
        sys.stdout.write(json.dumps({"kind": "done", "text": text, "usage": usage}) + "\n")
        sys.stdout.flush()
    else:  # text

        def emit(delta: str) -> None:
            sys.stdout.write(delta)
            sys.stdout.flush()

        _text, _usage, new_messages, _tcs = await backend.stream_chat(messages, emit)
        sys.stdout.write("\n")
        sys.stdout.flush()

    # Append the loop's non-user output (assistant + toolResult); the user turn
    # was already appended above, so skip any echoed user message.
    for message in new_messages:
        if message.get("role") != "user":
            session.append_message(message)

    return 0
