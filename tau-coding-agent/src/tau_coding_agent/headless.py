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

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    # Tool selection. --no-tools disables everything; --no-builtin-tools drops the
    # built-in set (``tools=[]``) but extension-registered tools survive, because
    # they merge in later (AgentSession._build_turn_tools) independent of this key —
    # so the two now read distinctly once extensions load (E5 S28), matching pi's
    # noTools "all" vs "builtin" (args.ts:104-142). ``tools=[]`` here means
    # "no built-ins"; a bare-model run with an extension tool still exposes it.
    if args.no_tools or args.no_builtin_tools:
        model_config["tools"] = []
    elif args.tools:
        names = [t.strip() for t in args.tools.split(",") if t.strip()]
        if not names:
            raise CLIError("--tools given but no tool names parsed")
        model_config["tools"] = names

    # --exclude-tools denylist (pi excludeTools, args.ts:143-153). Carried on the run
    # config; TauBackend applies it to the resolved built-ins at construction (S28).
    if args.exclude_tools is not None:
        excluded = [t.strip() for t in args.exclude_tools.split(",") if t.strip()]
        if not excluded:
            raise CLIError("--exclude-tools given but no tool names parsed")
        model_config["exclude_tools"] = excluded

    # Extensions: explicit --extension paths + the discovery toggle (pi args.ts:150-153).
    # ``run_print`` loads them into the live session after create_backend (E5 S27).
    if args.extensions:
        model_config["extensions"] = list(args.extensions)
    if args.no_extensions:
        model_config["no_extensions"] = True

    # Appended system-prompt sections (pi appendSystemPrompt, system-prompt.ts:48).
    # ``run_print`` folds them into the stored session prompt via _append_system_prompt
    # (S28); kept off the base ``system_prompt`` so they augment rather than replace it.
    if args.append_system_prompt:
        model_config["append_system_prompt"] = list(args.append_system_prompt)

    return spec, model_config


def _decode_ext_config_value(raw: str) -> Any:
    """Decode a ``--ext-config`` value (S40).

    JSON-decode when it parses — so ``ceiling=5.0`` → ``float``,
    ``enabled=true`` → ``bool``, ``paths=["a","b"]`` → ``list`` — matching the
    typed values a config.json entry carries; keep it as a plain ``str`` otherwise
    (a bare unquoted word like ``mode=strict`` stays ``"strict"``). This is a
    deliberate, predictable coercion rule, NOT a fallback that papers over a
    subproblem — an override's type is exactly what its JSON says, or a string.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def parse_ext_config_overrides(items: list[str]) -> dict[str, dict[str, Any]]:
    """Parse ``--ext-config <name>.<key>=<value>`` items into ``{name: {key: value}}`` (S40).

    Each item is split on the FIRST ``=`` (so a value may contain ``=``) into
    ``NAME.KEY`` and the value; the left side is split on the FIRST ``.`` into the
    extension ``NAME`` (its file stem) and the ``KEY``. The value is decoded by
    :func:`_decode_ext_config_value`. Fail-Early: a malformed item (no ``=``, no
    ``.`` in the key part, or an empty name/key) RAISES :class:`CLIError` rather
    than being silently dropped.
    """
    overrides: dict[str, dict[str, Any]] = {}
    for item in items:
        if "=" not in item:
            raise CLIError(f"--ext-config must be NAME.KEY=VALUE, got {item!r} (missing '=')")
        lhs, _, raw_value = item.partition("=")
        if "." not in lhs:
            raise CLIError(
                f"--ext-config must be NAME.KEY=VALUE, got {item!r} (the NAME.KEY part has no '.')"
            )
        name, _, key = lhs.partition(".")
        name, key = name.strip(), key.strip()
        if not name or not key:
            raise CLIError(f"--ext-config NAME and KEY must both be non-empty, got {item!r}")
        overrides.setdefault(name, {})[key] = _decode_ext_config_value(raw_value)
    return overrides


def resolve_extensions_config(
    config: dict, overrides: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Merge the config.json per-extension config with ``--ext-config`` overrides (S40).

    Base slices come from ``~/.tau/config.json`` ``"extensions": {"<name>": {…}}``
    (keyed by extension file stem); ``overrides`` (from
    :func:`parse_ext_config_overrides`) apply on top per key, so CLI beats
    config.json. Returns ``{name: {…}}`` handed to
    ``AgentSession.load_extensions(extensions_config=…)``; an extension with no
    entry reads ``{}``. Fail-Early: a non-object ``"extensions"`` block (or a
    non-object entry within it) RAISES :class:`CLIError` — a malformed config is a
    real error, not a thing to silently coerce.
    """
    base = config.get("extensions", {})
    if not isinstance(base, dict):
        raise CLIError(
            '~/.tau/config.json "extensions" must be a JSON object mapping '
            "extension name -> config object"
        )
    merged: dict[str, dict[str, Any]] = {}
    for name, ext_conf in base.items():
        if not isinstance(ext_conf, dict):
            raise CLIError(f'~/.tau/config.json "extensions.{name}" must be a JSON object')
        merged[name] = dict(ext_conf)
    for name, kv in overrides.items():
        merged.setdefault(name, {}).update(kv)
    return merged


def parse_ui_defaults(raw: str | None) -> dict[str, str]:
    """Parse ``--ui-defaults METHOD=ANSWER,…`` into ``{method: token}`` (E7 §3 / S48).

    Splits a comma-separated string (e.g. ``"confirm=yes,select=first"``) on
    commas, then each item on the FIRST ``=``. Fail-Early: an item missing ``=`` or
    with an empty method/answer RAISES :class:`CLIError`. The method/token pairs
    are NOT validated against the allowed set here — that is
    :meth:`ExtensionUI.set_headless_defaults`'s job (a single source of truth,
    surfaced as a clean CLI error where the policy is applied). ``None``/empty →
    ``{}`` (no policy → headless dialogs raise).
    """
    policy: dict[str, str] = {}
    if not raw:
        return policy
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise CLIError(f"--ui-defaults must be METHOD=ANSWER, got {item!r} (missing '=')")
        method, _, token = item.partition("=")
        method, token = method.strip(), token.strip()
        if not method or not token:
            raise CLIError(f"--ui-defaults METHOD and ANSWER must both be non-empty, got {item!r}")
        policy[method] = token
    return policy


def resolve_ui_defaults(config: dict, overrides: dict[str, str]) -> dict[str, str]:
    """Merge config.json ``"ui_defaults"`` with ``--ui-defaults`` overrides (S48).

    The base policy comes from ``~/.tau/config.json`` ``"ui_defaults": {method:
    answer}``; ``overrides`` (from :func:`parse_ui_defaults`) apply on top per
    method, so CLI beats config.json (same precedence as ``--ext-config``).
    Fail-Early: a non-object ``"ui_defaults"`` block RAISES :class:`CLIError`.
    Answer values are stringified so a JSON ``true`` in config reads as a token the
    policy validator recognises.
    """
    base = config.get("ui_defaults", {})
    if not isinstance(base, dict):
        raise CLIError(
            '~/.tau/config.json "ui_defaults" must be a JSON object mapping dialog method -> answer'
        )
    merged: dict[str, str] = {str(k): str(v) for k, v in base.items()}
    merged.update(overrides)
    return merged


def _append_system_prompt(base: str, sections: list[str] | None) -> str:
    """Append ``--append-system-prompt`` sections to a base system prompt.

    Sections augment rather than replace the base (pi ``appendSystemPrompt``,
    system-prompt.ts:48), joined by blank lines. An empty/absent list returns the
    base unchanged; an empty base with sections yields just the sections. Shared by
    the headless and TUI paths (E5 §2.3 / S28).
    """
    if not sections:
        return base
    parts = [base, *sections] if base else list(sections)
    return "\n\n".join(parts)


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


def _emit_command_output(mode: str, command: str, text: str | None) -> None:
    """Render an extension command's output on the headless channel (E7 §3 / S46).

    ``--mode text`` prints the output text to stdout (nothing when the command
    produced no output). ``--mode json`` emits ONE ``command_output`` record — a
    separate record family alongside the closed ``AgentEvent`` set (the same
    pattern as the session header line), carrying ``output: null`` when the
    command ran without a returned value, so an orchestrator reading the stream
    still sees that the command ran. Display-only: never persisted onto the path.
    """
    if mode == "json":
        record = {"type": "command_output", "command": command, "output": text}
        sys.stdout.write(json.dumps(record) + "\n")
        sys.stdout.flush()
        return
    if text is not None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


async def run_print(args: "CLIArgs", config: dict) -> int:
    """Run one headless turn and render to stdout. Returns a process exit code.

    ``--mode text`` streams raw assistant text deltas (a plain transcript).
    ``--mode json`` is pi-faithful (E-json / step S8): the session header line
    FIRST, then one JSON object per line — each a ``type``-discriminated
    ``AgentSessionEvent`` from the agent bus (``message_end`` carries
    usage/model/stop_reason). No legacy ``kind`` schema, no synthetic ``done``.

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
        # --append-system-prompt sections augment (not replace) the base prompt
        # (pi appendSystemPrompt) — E5 §2.3 / S28. Appended to the STORED session
        # prompt (the first message), which is what the model actually sees on this
        # path; the backend's own system_prompt stays empty. Only on a fresh run —
        # a resumed session already carries its (possibly-augmented) prompt.
        system_prompt = _append_system_prompt(
            system_prompt, model_config.get("append_system_prompt")
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

    # Imported lazily: keeps `import tau_coding_agent.headless` free of the
    # backend/agent-core import chain until a run actually happens.
    from tau_coding_agent.backends import create_backend, make_model_resolver

    backend = create_backend(model_config)

    # Bind the model-name resolver (S45) so an extension's ctx.set_model(name)
    # resolves NAME through the same config "models" map --model uses. Guarded via
    # getattr so a non-``TauBackend`` test double is a transparent no-op.
    agent_session = getattr(backend, "agent_session", None)
    if agent_session is not None and hasattr(agent_session, "set_model_resolver"):
        agent_session.set_model_resolver(make_model_resolver(config.get("models", {})))

    # Headless dialog policy (E7 §3 / S48 — anchor G9, D-E6-2). With no policy a
    # dialog opened by a loaded extension RAISES rather than silently auto-answering
    # a gate; ``--ui-defaults confirm=yes,select=first`` (over config.json
    # "ui_defaults", CLI wins) opts back into the explicit auto-answer. Applied
    # BEFORE the load/lifecycle below so an extension's ``register`` / ``session_start``
    # dialog is already governed. Validation errors surface as a clean CLI error.
    set_ui_defaults = getattr(backend, "set_headless_ui_defaults", None)
    if set_ui_defaults is not None:
        ui_defaults = resolve_ui_defaults(config, parse_ui_defaults(args.ui_defaults))
        try:
            set_ui_defaults(ui_defaults)
        except ValueError as exc:
            raise CLIError(str(exc)) from exc

    # Extension activity on the JSON stream (E7 §3 / S49 — anchor G10). In
    # ``--mode json`` install a record sink so every loaded extension's
    # ``api.ui.notify(...)`` (and the S44 error surface) emits a
    # ``{"type": "extension", …}`` record — a parallel record family alongside the
    # closed ``AgentEvent`` set, like the session header line — instead of the bare
    # stderr line. Set BEFORE the load/lifecycle below so a ``register`` /
    # ``session_start`` notify is already captured. ``--mode text`` leaves the sink
    # unset (stderr, unchanged); a non-``TauBackend`` test double without the seam is
    # a transparent no-op (same ``getattr`` guard as the other seams).
    if args.mode == "json":
        set_record_sink = getattr(backend, "set_extension_record_sink", None)
        if set_record_sink is not None:

            def _emit_extension_record(record: dict[str, Any]) -> None:
                sys.stdout.write(json.dumps(record) + "\n")
                sys.stdout.flush()

            set_record_sink(_emit_extension_record)

    # Session-lifecycle hooks (E6 §2 / S41). ``session_start`` fires once
    # extensions are loaded; ``session_shutdown`` fires on headless COMPLETION and
    # on SIGINT/SIGTERM. Resolved via ``getattr`` so a non-``TauBackend`` test
    # double without the seam is a transparent no-op (same guard as the TUI's
    # ``set_ui_delegate``). Signal handlers are installed only when the backend
    # exposes the shutdown seam, so the existing fake-backend tests keep the plain
    # KeyboardInterrupt disposition unchanged.
    emit_session_start = getattr(backend, "emit_session_start", None)
    emit_session_shutdown = getattr(backend, "emit_session_shutdown", None)
    abort = getattr(backend, "abort", None)
    installed_signals: list[signal.Signals] = []
    if emit_session_shutdown is not None:
        loop = asyncio.get_running_loop()

        def _on_terminate() -> None:
            # Trip the in-flight abort so the loop unwinds to the ``finally``
            # below, which fires ``session_shutdown`` exactly once. Not fired from
            # here directly: a signal callback is sync and cannot await the async
            # dispatch. ``abort`` is safe to call when nothing is running.
            if abort is not None:
                abort()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_terminate)
                installed_signals.append(sig)
            except NotImplementedError:
                # Signal handlers are unavailable on this event loop / platform
                # (e.g. Windows ProactorEventLoop). Nothing to fabricate — the
                # completion path still fires ``session_shutdown``.
                pass

    try:
        # Load file-path extensions into the live session (E5 §2.2). Explicit
        # ``-e`` paths come from ``--extension``; the ``~/.tau/extensions`` global
        # dir is discovered unless ``-ne`` (``no_extensions``) was passed. A
        # discovered load failure is collected and surfaced to stderr here; an
        # explicit ``-e`` failure raises out of ``load_extensions`` (Fail-Early —
        # the user named it), which ``main()`` renders as a clean CLI error.
        explicit_extensions = model_config.get("extensions") or None
        discover_extensions = not model_config.get("no_extensions", False)
        # Per-extension config (E6 §2 / S40): config.json ``"extensions"`` slices +
        # per-run ``--ext-config NAME.KEY=VALUE`` overrides (CLI > config.json).
        # Sliced per extension by file stem inside the session, handed to
        # ``api.config``.
        extensions_config = resolve_extensions_config(
            config, parse_ext_config_overrides(args.ext_config)
        )
        ext_result = await backend.load_extensions(
            explicit_extensions,
            discover=discover_extensions,
            extensions_config=extensions_config,
        )
        for ext_error in ext_result.errors:
            print(
                f"[τ] failed to load extension {ext_error.path}: {ext_error.error}",
                file=sys.stderr,
            )

        # ``session_start`` after the load, so a handler's ``ctx.entries()``
        # reconstruction / watcher setup runs with its registration in place (S41).
        if emit_session_start is not None:
            await emit_session_start("startup")

        # Command output channel (E7 §3 / S46): a prompt that is entirely a
        # registered extension slash-command (``/name args``) RUNS the command
        # instead of a model turn — mirroring the TUI's pre-model dispatch
        # (``app.on_input_submitted``). The handler's returned value is printed
        # (text) / emitted as a ``command_output`` record (json). No user turn is
        # appended and the model is never called, so the report stays display-only
        # chrome and never enters the persisted path (tree-as-truth, E5 §1). An
        # unknown ``/…`` is NOT a command — it falls through to the model path
        # below (the text is a legitimate prompt).
        run_cmd = getattr(backend, "run_extension_command", None)
        if run_cmd is not None and prompt_text.startswith("/"):
            stripped = prompt_text[1:]
            space = stripped.find(" ")
            cmd_name = stripped if space == -1 else stripped[:space]
            cmd_args = "" if space == -1 else stripped[space + 1 :]
            cmd_result = await run_cmd(cmd_name, cmd_args)
            if cmd_result.handled:
                _emit_command_output(args.mode, cmd_name, cmd_result.output_text())
                return 0

        # This turn's user message, then hand the active-path CONTEXT to the backend
        # (cursor + compaction/branch splices applied) — not the raw linear fold, so
        # a resumed compacted/branched session gives the model the right history
        # (§2.6). Appended here (after the command check) so a command run never
        # persists a user turn.
        session.append_message({"role": "user", "content": prompt_text})
        messages: list[dict] = session.context

        if args.mode == "json":
            # pi-faithful ``--mode json`` (E-json / step S8, D-delegate). Emit the
            # session HEADER line FIRST (pi ``print-mode.ts:113-116``), then every
            # bus event serialized to its ``type``-discriminated pi
            # ``AgentSessionEvent`` shape (NOT the legacy ``kind`` schema, and no
            # synthetic ``done`` line): each ``message_end`` carries
            # usage/model/stop_reason, which is the real per-child limit / failure
            # signal the delegate (step S9) consumes. The delegate prices its own
            # budget from those per-message tokens × config ``cost`` (E4.cost), so
            # no ``cost_usd`` rides the json stream.
            sys.stdout.write(json.dumps(session.header) + "\n")
            sys.stdout.flush()

            def on_pi_event(event: dict) -> None:
                sys.stdout.write(json.dumps(event) + "\n")
                sys.stdout.flush()

            def noop(_delta: str) -> None:
                pass

            _text, _usage, new_messages, _tcs = await backend.stream_chat(
                messages, noop, on_pi_event=on_pi_event
            )
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
    finally:
        # Uninstall the lifecycle signal handlers (never leak them onto the loop a
        # subsequent run — or the test harness — shares) and fire ``session_shutdown``
        # exactly once, whether the run completed normally or a signal tripped abort.
        for sig in installed_signals:
            loop.remove_signal_handler(sig)
        if emit_session_shutdown is not None:
            await emit_session_shutdown("quit")
