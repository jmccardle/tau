"""Headless (non-interactive) run path for the τ CLI's ``--print`` mode.

This drives the *same* agent path the TUI uses — ``create_backend(model_config)``
→ ``backend.stream_submission(submission, context, callback, …)`` — but renders to
stdout instead of Textual widgets. It deliberately does NOT touch
``run_agent_loop.py`` (that file is a meta-orchestrator that shells out to ``pi``
to build τ; it is not a headless τ runner).

Model resolution mirrors ``Parley.action_new_chat`` (``app.py``): a ``--model``
name is looked up in the ``models`` map of ``~/.tau/config.json``; the selected
entry's ``backend``/``model``/``base_url``/``api_key`` are handed to
``create_backend`` unchanged. CLI flags override per-invocation.

**Print mode is a frontend, and says so** (docs/SUBMISSION-LIFECYCLE.md phase 3,
part 3 — the spec's own framing: *"Headless (headless.py:382, run_print) reaches the
model by a different path. The SDK reaches it by a third"*). It now owns a
:class:`~tau_agent_core.submission.Submission` of its own instead of letting
``stream_chat`` derive one, because the record it needs is not the one that
derivation hardcodes — see :func:`build_print_submission` for what each field says
and why.

Reference: docs/CLI-PLAN.md (Core flag set); docs/SUBMISSION-LIFECYCLE.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

# The submission lifecycle (docs/SUBMISSION-LIFECYCLE.md phase 3, part 3). Print
# mode reached the model by its own route — ``backend.stream_chat(messages, …)``,
# which derived a submission for it — and answered "is this a command" with its own
# hand-rolled first-space split. Both now go through the one door: this module
# builds the :class:`Submission` that says what ``tau -p "…"`` MEANS and hands it to
# ``AgentSession.submit`` (via ``TauBackend.stream_submission`` /
# ``TauBackend.submit_command``), and :func:`~tau_agent_core.commands.resolve_command`
# is the same pure function ``submit()`` itself uses.
from tau_agent_core.commands import (
    CommandOutcome,
    UnsupportedCommandError,
    resolve_command,
    unsupported_command_message,
)

# Persistence goes through the storage-agnostic SessionCatalog seam (W10) rather
# than the concrete file Session directly, so a headless run can write a
# picker-visible, resumable session without hardcoding the file store.
# ``store_factory.build_session_catalog`` resolves ``--store``/config
# ``session_store.backend`` (W12, docs/JMFTS-INTEGRATION-PLAN.md §3.1) into
# either the on-disk ``FileSessionCatalog`` (append-only JSONL files
# partitioned by cwd, docs/SESSION-UX-REDESIGN.md — the default, and reads
# TAU_DIR lazily via session_store's module-level helpers, so tests that
# monkeypatch ``session_store.TAU_DIR`` redirect storage without a stale
# module-level copy) or a JMFTS-backed ``JmftsSessionCatalog``.
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog
from tau_agent_core.submission import Submission
from tau_coding_agent.config import ConfigError
from tau_coding_agent.store_factory import build_session_catalog

# Canonical thinking levels live in τ-llm (single source of truth); pi: the same
# set in ``args.ts:57``. A ``model:level`` suffix or the ``--thinking`` flag is
# carried into the model config as ``thinking`` and threaded to the provider as
# ``reasoning_effort``.
from tau_llm.models import is_valid_thinking_level

if TYPE_CHECKING:  # avoid importing the dataclass module at runtime cost
    from tau_coding_agent.cli import CLIArgs


class CLIError(ConfigError):
    """A user-facing CLI error. ``main()`` prints it and exits non-zero.

    Subclasses ``ConfigError`` so the one handler in ``main()`` catches both a bad
    flag and a malformed ``config.json``.
    """


def resolve_no_tools(args: "CLIArgs") -> Literal["all", "builtin"] | None:
    """Collapse ``--no-tools`` + ``--no-builtin-tools`` into ONE resolved policy.

    pi does this at its own argv boundary (``main.ts:424-428``) and everything
    downstream reads the single value (``sdk.ts:59``). τ now does the same, and
    for the same reason: the two flags have no meaning apart from each other, so
    threading them separately leaves every consumer to re-derive the interaction —
    which is how they silently became the same flag.

    - ``"all"`` (``--no-tools``/``-nt``) — the model is offered zero tools:
      no built-ins, and no extension-registered tools either. Extensions still
      LOAD; hooks, event subscriptions, slash commands, message injections and
      per-extension config are untouched. Only callable tools are withheld.
    - ``"builtin"`` (``--no-builtin-tools``/``-nbt``) — built-ins only.
      Extension-registered tools survive and are offered.
    - ``None`` — no suppression.

    ``--no-tools`` wins when both are given: it is the strictly stronger claim,
    so honouring the weaker one would ignore an explicit instruction.
    """
    if args.no_tools:
        return "all"
    if args.no_builtin_tools:
        return "builtin"
    return None


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
    # Tool selection. Both flags empty the BUILT-IN set, so both write
    # ``tools=[]``; what separates them is carried by ``no_tools``, the single
    # resolved policy (see :func:`resolve_no_tools`), which ``TauBackend`` hands
    # to ``AgentSession`` and which alone decides whether extension-registered
    # tools are also withheld (``AgentSession._build_turn_tools``).
    #
    # The two keys do not overlap: ``tools`` is the built-in allowlist, and
    # ``no_tools`` is the run-level policy — nothing derives one from the other,
    # so there is no second source of truth to drift.
    no_tools = resolve_no_tools(args)
    if no_tools is not None:
        model_config["no_tools"] = no_tools
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

    # ``--bus`` (H8): the same capability grant the TUI threads through
    # ``_apply_run_config``. Only ever set TRUE — the absence of the flag must not
    # revoke a ``"bus_available": true`` the model entry granted deliberately.
    if args.bus:
        model_config["bus_available"] = True

    # Appended system-prompt sections (pi appendSystemPrompt, system-prompt.ts:48).
    # ``run_print`` folds them into the stored session prompt via _append_system_prompt
    # (S28); kept off the base ``system_prompt`` so they augment rather than replace it.
    if args.append_system_prompt:
        model_config["append_system_prompt"] = list(args.append_system_prompt)

    # ``--no-context-files``/``-nc`` (pi args.ts:185). Only ever set TRUE, for
    # the same reason ``--bus`` is: the absence of the flag must not revoke a
    # ``"no_context_files": true`` a model entry set deliberately.
    if args.no_context_files:
        model_config["no_context_files"] = True

    # Fold the top-level ``reasoning_replay`` default into the entry when it sets
    # none of its own (per-model wins; else global; else build_model_from_config's
    # "turn"), so headless and the TUI resolve the scope identically.
    if "reasoning_replay" not in model_config and config.get("reasoning_replay") is not None:
        model_config["reasoning_replay"] = config["reasoning_replay"]

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


def _select_session(args: "CLIArgs", catalog: SessionCatalog) -> ConversationSession | None:
    """Resolve the continuation flags to a loaded source session, or None.

    ``--continue`` selects the most recent session in the current cwd
    (:meth:`SessionCatalog.most_recent`); ``--session``/``--fork`` select a
    specific one — a path, an exact id, or a unique id prefix
    (:meth:`SessionCatalog.resolve_ref`, scoped to the current cwd). The flags
    are mutually exclusive at the argparse layer, so at most one is set. Returns
    None for a fresh run. For ``--fork`` this is the *source* — the caller forks
    it.
    """
    if args.continue_session:
        session = catalog.most_recent(os.getcwd())
        if session is None:
            raise CLIError(
                "no saved sessions to continue (this directory has no history in "
                "the session store this run resolved — ~/.tau/sessions unless "
                "--session-dir/--store says otherwise)"
            )
        return session

    selector = args.session or args.fork
    if selector is None:
        return None
    try:
        return catalog.resolve_ref(selector, cwd=os.getcwd())
    except LookupError as exc:
        raise CLIError(str(exc)) from exc


def _apply_resume_metadata(
    session: ConversationSession,
    model_name: str,
    backend_name: str,
    prior: ConversationSession,
    title: str | None,
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


def build_print_submission(prompt_text: str) -> Submission:
    """The record that says what ``tau -p "…"`` means (SUBMISSION-LIFECYCLE phase 3).

    Every field here is a claim about the invocation, and three of them are the
    substance of this work item:

    ``source="interactive"`` / ``submitter="human"`` — a ``tau -p`` invocation IS a
    human at a frontend; the frontend simply does not draw. The alternative reading,
    ``source="rpc"``, would be a claim about the transport that print mode cannot
    make: τ has a real RPC transport (``rpc.py``) and cannot tell a person typing
    ``tau -p "fix the test"`` at a shell from a script that shelled out. What print
    mode CAN say truthfully is who the text came from — the process's own operator —
    which is exactly what ``submitter`` is for ("WHO, not what kind"). pi agrees by
    construction: its print mode calls the same ``session.prompt(message)`` its
    interactive mode does (``modes/print-mode.ts:122``).

    ``allow_user_input=False`` — and this is why the source axis does not have to
    carry non-interactivity in the first place. It is Jupyter's ``allow_stdin``,
    which the spec borrows precisely because *"one process can serve an interactive
    client and a batch job at once; a global is_interactive flag cannot express
    that"*: **who submitted** and **whether a human can be asked a question** are two
    different facts, and print mode is the case that separates them. There is nobody
    to answer a modal, so a hook that opens one under this turn takes the
    headless-answer route — the explicit ``--ui-defaults`` token, or
    :class:`~tau_agent_core.extension_types.HeadlessDialogError`. That composes with
    the pre-existing policy rather than duplicating it: ``ExtensionUI._human_delegate``
    already returns ``None`` in a process with no TUI delegate, so this flag adds
    nothing a print run could previously get around — what it adds is that the
    enforcement no longer depends on the *process* having no delegate, and the raise
    now names the submission instead of the mode. An embedded τ that grows a delegate
    tomorrow inherits the correct behaviour for free.

    ``expand_commands=True`` — print mode declares itself an interactive frontend for
    B2-b's security property, and it is a boundary rather than a convenience. The
    line B2-b draws is between text the process's own operator supplied and text that
    arrived from a third party while τ was running: a NATS payload, a webhook body, a
    timer's stored template. ``prompt_text`` is ``argv`` (or a file it named, via
    ``@file``) — chosen before the process existed, at the same trust level as a
    keystroke in the TUI. It is also what print mode already did: an extension
    ``/name args`` has dispatched here since S46, and answering False would delete a
    shipped feature AND leave this module with a second, drifting answer to "what is
    a command" — the hand-rolled first-space split B2-b called out as a debt.

    The caveat that follows from the same reasoning, stated rather than hidden: a
    caller that puts UNTRUSTED text (a model-authored sub-task, a webhook body) in
    ``argv`` has granted it argv's trust level, and it can now dispatch the four
    built-ins as well as a registered extension command. ``examples/20_delegate.py``
    is not that caller — it prefixes every child prompt with ``"Task: "`` and passes
    ``--no-extensions``, so a child prompt cannot begin with ``/`` at all — but a
    future one must prefix, or refuse a leading ``/`` itself.

    ``multitask_strategy="enqueue"`` — a fresh process has nothing in flight, with
    exactly one exception: an extension's ``session_start`` handler may have spawned
    a task that submits. ``"reject"`` would then refuse the very prompt the user
    invoked τ to run, which is the wrong answer to a race the user cannot see;
    ``"enqueue"`` waits for that turn and then runs, and is guaranteed to run within
    the call (unlike ``deliver_as="nextTurn"``, which parks).

    ``store_history`` is left at its default ``True``: the persistence of record for
    a print run is the :class:`~tau_agent_core.session_catalog.ConversationSession`
    this module appends to itself, but ``False`` would ALSO skip the end-of-prompt
    drain and the ``user_turn_end`` boundary — extension hooks a headless run fires
    today — so it would be a behaviour change wearing a persistence flag's name.
    """
    return Submission(
        text=prompt_text,
        source="interactive",
        submitter="human",
        submission_id=uuid4().hex,
        multitask_strategy="enqueue",
        expand_commands=True,
        allow_user_input=False,
    )


def _extension_command_names(backend: Any) -> list[str]:
    """The names extensions registered as slash commands, for the pre-submission peek.

    ``getattr``-guarded exactly like the app's counterpart
    (``Parley._extension_command_names``) and like every other backend-capability read
    on this path: a test double simply has none, which makes the peek resolve only τ's
    built-ins — those need no backend, being τ's own vocabulary hardcoded in
    :mod:`tau_agent_core.commands`.
    """
    lister = getattr(backend, "get_extension_commands", None)
    if lister is None:
        return []
    return [name for name, _description in lister()]


async def _dispatch_command_submission(backend: Any, submission: Submission, mode: str) -> None:
    """Admit a command submission through the one door and perform its outcome.

    The headless twin of ``Parley._dispatch_command_submission``. The submission goes
    through ``AgentSession.submit`` exactly as a prompt does — same admission, same
    ``input`` hook chain, same provenance stamp — and comes back carrying a typed
    :class:`~tau_agent_core.commands.CommandOutcome` instead of messages.

    Three things that are not "nothing happened", and are therefore not silent:

    - a backend with no ``submit_command`` (a test double) RAISES: the user typed a
      command and there is no door to send it through, and sending it to the model
      instead is precisely the silent fallback this lifecycle removes.
    - ``accepted is False`` raises :class:`CLIError`, so the process exits non-zero
      with the refusal's own reason rather than exiting 0 having done nothing.
    - ``result.command is None`` means ``submit()`` ran a TURN instead — an ``input``
      hook rewrote the text between this module's peek and the core's own resolution.
      That turn really ran, unrendered and unpersisted by this module, so it is
      reported rather than passed over.
    """
    submit_command = getattr(backend, "submit_command", None)
    if submit_command is None:
        raise UnsupportedCommandError(
            f"{type(backend).__name__} has no submit_command(), so the command "
            f"{submission.text!r} cannot be admitted. Command dispatch lives in "
            "AgentSession.submit (docs/SUBMISSION-LIFECYCLE.md phase 3); a backend "
            "that cannot reach it cannot run commands, and sending the text to the "
            "model instead would be the silent fallback this lifecycle removes."
        )
    result = await submit_command(submission)
    if not result.accepted:
        raise CLIError(
            f"{submission.text!r} was refused: {result.rejection_reason or 'no reason given'}"
        )
    if result.command is None:
        raise UnsupportedCommandError(
            f"{submission.text!r} was dispatched as a command by print mode but "
            "AgentSession.submit ran a TURN for it — an `input` hook transformed the "
            "text after print mode resolved it. The turn ran without being rendered "
            "or persisted by this run. Fix the hook, or stop it from rewriting text "
            "that resolves to a command."
        )
    _perform_command_outcome(result.command, mode)


def _perform_command_outcome(outcome: CommandOutcome, mode: str) -> None:
    """Do the half of a dispatched command only this frontend can do (B2-b).

    ``performer="core"`` — the session already ran an extension-registered command and
    all that is left is to show what it returned, on the S46 channel this module
    already had: stdout text, or one ``command_output`` record under ``--mode json``.

    ``performer="frontend"`` — a built-in the core deliberately did not run because it
    needs a frontend, and print mode has none of them:

    - ``/tree`` and ``/fork`` open a modal browser; there is no screen to push it onto.
    - ``/resume`` opens the session picker — same reason, and the flag that would open
      it (``--resume``) is refused under ``--print`` in ``cli.main`` for that reason.
      A print run names its session with ``--continue``/``--session REF`` instead.
    - ``/extensions`` paints a panel (or manages extensions in a process that is about
      to exit, which would be a runtime toggle nothing outlives).
    - ``/compact`` is the one that looks performable and is not, for a specific
      reason worth writing down: ``run_print`` does NOT bind its
      ``ConversationSession`` as the AgentSession's log (unlike the TUI, E3-ctx / D3
      — it owns persistence itself and appends produced messages by hand), so
      ``compact_messages`` would summarize a working list this process discards
      milliseconds later. That is an LLM call whose only result is thrown away —
      strictly worse than saying it cannot be done.

    So this raises :class:`~tau_agent_core.commands.UnsupportedCommandError`, which is
    the seam's designed answer and not a gap: the core is allowed to resolve commands
    a given frontend cannot perform, and the contract is that such a frontend says so
    out loud. The visible change is that ``tau -p "/compact"`` now raises instead of
    sending the eight characters to a model that will be confused by them.
    """
    if outcome.performer == "core":
        _emit_command_output(mode, outcome.name, outcome.output)
        return
    raise UnsupportedCommandError(unsupported_command_message(outcome, "print mode (tau -p)"))


async def run_print(args: "CLIArgs", config: dict, catalog: SessionCatalog | None = None) -> int:
    """Run one headless turn and render to stdout. Returns a process exit code.

    ``--mode text`` streams raw assistant text deltas (a plain transcript).
    ``--mode json`` is pi-faithful (E-json / step S8): the session header line
    FIRST, then one JSON object per line — each a ``type``-discriminated
    ``AgentSessionEvent`` from the agent bus (``message_end`` carries
    usage/model/stop_reason). No legacy ``kind`` schema, no synthetic ``done``.

    Both modes reach the model through ``AgentSession.submit`` — the one door
    (docs/SUBMISSION-LIFECYCLE.md phase 3, part 3) — carrying the record
    :func:`build_print_submission` builds, via ``backend.stream_submission`` for a
    prompt and ``backend.submit_command`` for a command. Neither output shape
    changes: the JSON lines already carry ``submission_id``/``source``/``submitter``/
    ``correlation`` (phase 2 stamped provenance onto every ``AgentEvent``, and the
    serializer's ``model_dump`` has emitted them since), and what this changes is
    that they now attribute the turn to a submission print mode itself minted rather
    than to one the adapter derived on its behalf.

    The run is persisted through ``catalog`` (a :class:`SessionCatalog` — the W10
    seam; ``None`` resolves ``args.store``/``config["session_store"]`` via
    :func:`~tau_coding_agent.store_factory.build_session_catalog`, defaulting to
    the on-disk :class:`~tau_coding_agent.session_store.FileSessionCatalog`) as
    an append-only session under the current cwd — each produced message is
    appended as it is known (no whole-file rewrite). In-place for
    ``--continue``/``--session``; a new forked session for ``--fork``; a fresh
    one otherwise.

    ``--session-dir`` (unit S) rides the same call: print mode's DEFAULT stays
    ``~/.tau/sessions`` — only ``--mode rpc`` moved (docs/RPC-TIER-B.md D-6) —
    but a headless run can be pointed anywhere, including at RPC mode's own
    ``<tmp>/.tau-<uid>/sessions``, which is how ``tau -p -c`` continues a session an
    RPC host started.
    """
    # `persist=not args.no_session`: an ephemeral run asks this catalog for
    # `create_ephemeral` and nothing else — `--no-session` + `--continue`/
    # `--session`/`--fork` is refused a few lines down, so `_select_session`
    # cannot reach the store either — and `create_ephemeral` is in-memory under
    # both stores. Reported by Tectum's prototyping: `tau -p --no-session`
    # exited 2 at startup against an unreachable JMFTS server, refusing a run
    # over a dependency it does not have. See `build_session_catalog`.
    catalog = (
        catalog
        if catalog is not None
        else build_session_catalog(
            config, args.store, args.session_dir, persist=not args.no_session
        )
    )
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
    prior = _select_session(args, catalog)

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
        # --no-session → ephemeral (no on-disk file, appends never touch disk); the
        # create_ephemeral seam is the one-API alternative to create (§E0.2).
        create = catalog.create_ephemeral if args.no_session else catalog.create
        session = create(
            cwd, model_name, backend_name, system_prompt=system_prompt or None, name=args.name
        )
    elif args.fork is not None:
        session = catalog.fork(prior, cwd)
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

        # THE one door (docs/SUBMISSION-LIFECYCLE.md phase 3, part 3). What this
        # invocation MEANS is a record handed to ``AgentSession.submit`` — the same
        # method the TUI and every extension go through — rather than a private
        # route into the loop. See ``build_print_submission`` for every field's
        # justification, in particular the three this work item turns on:
        # source/submitter (a human at a frontend that does not draw),
        # allow_user_input=False (nobody is here to answer a dialog), and
        # expand_commands=True (argv is the operator's own text).
        submission = build_print_submission(prompt_text)

        # Command output channel (E7 §3 / S46), now resolved by the CORE's vocabulary
        # instead of this module's own first-space split: a prompt that is entirely a
        # command (``/name args``) RUNS the command instead of a model turn. The
        # handler's returned value is printed (text) / emitted as a ``command_output``
        # record (json). No user turn is appended and the model is never called, so
        # the report stays display-only chrome and never enters the persisted path
        # (tree-as-truth, E5 §1). An unknown ``/…`` still resolves to None and falls
        # through to the model path below (the text is a legitimate prompt).
        #
        # This peek is a rendering/persistence concern, not a second dispatch: it
        # decides whether the session grows a user turn and whether ``--mode json``
        # writes a session header, both of which must NOT happen for input that never
        # becomes a turn. ``submit()`` remains the authority and resolves again on the
        # post-``input``-hook text; the two can only disagree if a hook rewrites one
        # into the other, which ``_dispatch_command_submission`` reports rather than
        # absorbs.
        if resolve_command(prompt_text, _extension_command_names(backend)) is not None:
            await _dispatch_command_submission(backend, submission, args.mode)
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

            _text, _usage, new_messages, _tcs, result = await backend.stream_submission(
                submission, messages, noop, on_pi_event=on_pi_event
            )
        else:  # text

            def emit(delta: str) -> None:
                sys.stdout.write(delta)
                sys.stdout.flush()

            _text, _usage, new_messages, _tcs, result = await backend.stream_submission(
                submission, messages, emit
            )

        # A refusal is a typed in-band RESULT (LSP ApplyWorkspaceEditResult), and the
        # one thing print mode must not do with it is exit 0 having printed nothing.
        # Unreachable under ``"enqueue"``, which waits rather than refusing — but the
        # strategy is a field on the record, one line from changing, and an adapter
        # that folded ``accepted=False`` into "an empty turn" is exactly the silent
        # drop this lifecycle exists to prevent.
        if not result.accepted:
            raise CLIError(
                f"the prompt was refused: {result.rejection_reason or 'no reason given'}"
            )

        # An ``input`` hook rewrote this text into a command AFTER the peek above
        # resolved it as ordinary prompt text, so ``submit()`` — the authority —
        # dispatched instead of running a turn (``messages`` is empty by
        # construction, not because the model said nothing). Perform the outcome on
        # the same channel the peeked path uses: an extension command has ALREADY
        # RUN inside ``submit()`` and its returned text is here to print, and a
        # built-in raises ``UnsupportedCommandError`` exactly as the argv-supplied
        # ``tau -p "/compact"`` does — which side of the hook the slash arrived from
        # must not decide whether the command is reported or vanishes. Reading only
        # ``result.accepted`` here (and then iterating an empty message list) exited
        # 0 having written a bare newline: the silent no-op this lifecycle exists to
        # remove, and the case the TUI's ``_get_assistant_response`` and both
        # ``_dispatch_command_submission`` halves already handle.
        #
        # NOT undone: the user turn appended above is already on the persisted
        # session, where a dispatched command writes nothing. The append is
        # deliberately ahead of the model call (a crash mid-turn still records the
        # question) and this module's ``ConversationSession`` is append-only, so the
        # honest report is the command output plus a user turn that records what was
        # actually submitted — not a rewritten history.
        if result.command is not None:
            _perform_command_outcome(result.command, args.mode)
            return 0

        # Terminate the ``--mode text`` transcript. After the two checks above, so
        # the newline marks the end of a turn that really ran rather than padding a
        # refusal or a dispatched command's output.
        if args.mode != "json":
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

        # Close the pooled τ-llm providers' HTTP clients for this loop
        # (docs/PROVIDER-LIFETIME.md §6.3) — AFTER session_shutdown, since a
        # handler may itself make a final LLM call. This runs inside the same
        # asyncio.run() that drove the turn (cli.py), so it is the last point
        # the loop is guaranteed still alive to close on.
        from tau_llm.client import aclose_providers

        await aclose_providers()
