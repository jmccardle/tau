"""``tau --mode rpc``: the JSON-RPC 2.0 protocol server run path.

Unit 2D (docs/REMOTE-CONTROL.md §3, §4[1] T2/T4, §4[7] P1, §9 R-T1, §10
"resolved" #9/#10, open question 3). This is the FIRST thing that makes
``tau_agent_core.rpc.RPCHandler`` reachable from the shipped CLI —
``docs/REMOTE-CONTROL.md`` §3 states the problem this module fixes: "blocks
2, 3, 4 exist partially, in rpc.py, unreachable: cli.py:141-145 accepts
--mode text|json and the file contains no reference to RPCHandler."

**Session/model/extension setup follows the print-mode path, not a second
one.** ``resolve_model_config``/``create_backend``/``load_extensions`` are
the exact functions ``tau_coding_agent.headless.run_print`` calls — the only
divergence from that path is what happens AFTER setup (``RPCHandler.run()``
instead of one ``stream_submission`` call).

**Session lifecycle (H1, phase 3).** This module now builds a
``SessionCatalog`` (``store_factory.build_session_catalog`` — the SAME seam
the TUI/headless use) and an ``AgentSessionRuntime`` over it, and binds the
initial ``AgentSession`` onto a catalog-produced ``ConversationSession``
(``create``) before ``RPCHandler.run()`` starts — establishing, from the
first moment, the invariant ``AgentSessionRuntime.fork()`` depends on
(``session_log`` is always a ``ConversationSession``, never the bare scratch
``InMemorySessionLog`` ``TauBackend.__init__`` constructs).

**Every run still starts a FRESH conversation — and by DEFAULT a PERSISTED
one** (Blocker 2 of the Tier B review). Until Tier B, the startup session was
``create_ephemeral``, and this module's convention was written as "fresh,
unpersisted". Freshness is the part that was ever load-bearing: a host
spawning τ must not inherit somebody else's transcript. ``create`` is equally
fresh; it merely also has a location, so the session is listable, resumable,
and — the reason this changed — capable of KEEPING what the durability-
promising verbs write to it. ``set_model`` (D-2) and ``set_session_name``
append ``model_change``/``session_info`` entries and return a cursor; on an
ephemeral session both appends landed in a list nobody ever wrote, so the
cursor implied a durable write that never happened, on the one session every
host starts on. That is the same class of silent no-op §1.1 of
docs/RPC-TIER-B.md exists to prevent, one layer up.

**``--no-session`` selects the other one, and is the ONLY startup flag that
moves it.** Blocker 2 changed the DEFAULT from ephemeral to persisted; it did
not decide that persistence is mandatory, and for one release this module
read the flag's absence and its presence identically — ``--no-session`` was
parsed by ``cli.py``, rejected by nothing, and never read here, so a host
that asked for an unpersisted run got a persisted one with no diagnostic.
That flag is the startup expression of the same choice
``AgentSessionRuntime.new_session`` takes as its REQUIRED ``persist``
keyword, and the startup session is the one session in the process no wire
call can state a value for — which is exactly why it needs a flag rather
than a hardcoded ``True``. ``--store jmfts`` is covered by the same line:
``JmftsSessionCatalog.create_ephemeral`` answers with a real in-memory
session rather than writing to the document server, so ``--no-session``
means "no durable write" against either store, not "no file" against one.

Choosing it has a consequence a host must expect, and it is the designed one
rather than a defect: on an unpersisted session the appending verbs
(``set_model``, ``set_session_name``, ``compact`` — D-7) refuse with
``-32004 SESSION_NOT_PERSISTED``, and ``get_state`` reports
``addressable: false`` so a host can learn that BEFORE tripping the refusal.
Durability is still reachable at any moment without a respawn:
``new_session {"persist": true}`` moves the connection onto a persisted
session, and the catalog this module builds is live either way.

The remaining CLI restrictions are untouched: ``--continue``/``--session``/
``--fork``/``--resume``/``--name``/``--store`` remain rejected at the CLI
layer (``cli.py``) for STARTUP, exactly as before — reading ``--no-session``
relaxes none of them, because none of them is a persistence choice. It is
also still true that a host can ask for an unpersisted conversation over the
wire (``new_session {"persist": false}``); the flag adds the case where the
process must never have had a persisted one in the first place.

**Persisted, but not into the user's session list** (unit S, the regression
that fix caused). ``SessionCatalog.create`` writes a session header
immediately and unconditionally, so "every RPC run leaves a session file
behind" (D-6's stated cost) turned out to mean *in* ``~/.tau/sessions``, one
per spawn, including a child that asks ``get_capabilities`` and exits — which
silently destroys ``tau -p -c`` for the human working in the same directory
(``headless._select_session`` is exactly ``catalog.most_recent(cwd)``) and
fills the TUI picker with nameless 0-message rows. The fix separates the
LOCATION per mode rather than filtering the listing: ``--mode rpc``'s DEFAULT
session base is ``<tmp>/.tau-<uid>/sessions`` (``session_store
.rpc_default_session_base``), the TUI's and ``--print``'s stay
``~/.tau/sessions``, and ``--session-dir`` overrides either — including
``--session-dir ~/.tau/sessions``, the way a host states that it does want to
appear in the user's list. See ``_resolve_rpc_session_dir`` for the three
cases and the one gap (a JMFTS-backed run has no directory to move).

The cost of that default, stated here because it is a promise the wire makes:
most systems clear the temp dir on reboot, so an RPC session's durability is
bounded by machine uptime. A cursor from ``set_model``/``set_session_name``
still names a real entry a replay can find within the session's life — which
is what D-6 was about — but "forever" is not what it means, and both verbs'
``notes`` say so on the wire rather than leaving a host to find out.

**T2 — stdout takeover happens before ANY of this module's setup runs**, not
merely before ``RPCHandler.run()``. Model/backend construction and extension
loading are ordinary Python that MAY write to stdout — most plausibly a
loaded extension's own ``register()``/``session_start`` handler calling
``print()`` — and by the time a host has spawned this process and is reading
its stdout, every byte on that stream is protocol, not a place for a stray
banner. ``transport._take_over_stdout()``/``_release_stdout()`` are
reference-counted (see ``transport.py``'s own module note) specifically so a
caller ABOVE ``RPCHandler`` can claim the exclusive-stdout invariant early
and let ``RPCHandler.run()`` claim it again (a second matched claim, not a
conflict) — this module is that caller.

**T4 — what τ puts on stderr in RPC mode, stated exhaustively (the whole
point of naming it: a host draining stderr unread is only reasonable once
τ has promised nothing important lives there, and PI_RPC_REPLACEMENT.md
§1.3 records a consumer that already does this and would otherwise never
see a τ-side error)**:

1. Extension load failures — ``f"[τ] failed to load extension {path}: {error}"``
   (this module, mirroring ``run_print``'s wording exactly).
2. A ``submit``/``prompt`` turn that fails AFTER admission (so the C3
   acceptance response has already gone out and cannot carry the failure) —
   ``f"[τ-rpc] submission {id!r} failed after admission: {exc!r}"``
   (``tau_agent_core.rpc.commands._submit_and_acknowledge`` — the ordinary
   ``AgentEvent`` stream already reports this too, via ``agent_end``'s
   ``is_error``/``error`` fields; the stderr line is a second, deliberate
   surface of the same failure, not a place carrying information the wire
   does not).
3. Extension notify/status/panel lines — ``f"[τ] {level}: {message}"`` /
   ``f"[τ] status {key}: ..."`` / ``f"[τ] panel {key}: ..."``
   (``tau_agent_core.extension_types.ExtensionUI``'s DEFAULT sink, unchanged
   from ``--mode text``: this module does not install a UI delegate or a
   record sink, so extension activity that would otherwise need a human or a
   JSON record channel falls back to stderr exactly as it does for a plain
   headless run).
4. ``--verbose``'s one-line arg dump (``cli.py``'s ``main()``, printed BEFORE
   this module is even reached, already to stderr).

Nothing else. In particular: no logging framework writes here, no periodic
heartbeat, no config-load message — the RPC wire's OWN error taxonomy
(JSON-RPC error responses, ``-32603`` for an unplanned handler exception)
is how a request-scoped failure gets reported; stderr is reserved for the
four bullet points above, all of which are either process-lifetime setup
issues or failures that have already left the request/response shape (a
background turn, an extension side-channel).

Reference: docs/CLI-PLAN.md; docs/REMOTE-CONTROL.md.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

from tau_agent_core.rpc import RPCHandler, transport
from tau_coding_agent.headless import (
    CLIError,
    parse_ext_config_overrides,
    parse_ui_defaults,
    resolve_extensions_config,
    resolve_model_config,
    resolve_ui_defaults,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tau_coding_agent.cli import CLIArgs

__all__ = ["run_rpc"]


def _resolve_rpc_session_dir(session_dir_flag: str | None, store_name: str) -> "Path | None":
    """Where THIS ``--mode rpc`` run stores sessions (unit S, D-6).

    Three cases, in the order they are decided:

    1. ``--session-dir DIR`` given → DIR, always, including
       ``--session-dir ~/.tau/sessions``. That is the documented way a host
       says "yes, I really do want these in the user's list"; the flag is
       deliberately absent from ``cli.py``'s ``--mode rpc`` rejection set for
       exactly this reason.
    2. No flag, file store → ``<tmp>/.tau-<uid>/sessions``
       (:func:`~tau_coding_agent.session_store.rpc_default_session_base`, which
       also creates it ``0o700`` and refuses a hostile pre-existing path). This
       is the whole fix: ``catalog.create`` writes a session header
       immediately and unconditionally, so before this every spawn — including
       a child that asks ``get_capabilities`` and exits — left a durable,
       listable, 0-message session in ``~/.tau/sessions/<dashed-cwd>/``, and
       ``tau -p -c`` (``most_recent(cwd)``) then resumed THAT instead of the
       human's work.
    3. No flag, non-file store → ``None``. The default in case 2 is the FILE
       store's own base directory; it is a location, not a request, and a
       document store has no directory to put it in. Refusing here would break
       a working ``session_store.backend = "jmfts"`` config over a flag its
       owner never passed — whereas an EXPLICIT ``--session-dir`` on that same
       config still refuses, loudly, in ``build_session_catalog``.

       Known gap, stated rather than hidden: case 3 means a JMFTS-backed RPC
       run still lands in the same document tree the user's own sessions do —
       the per-mode separation this unit ships is a file-store mechanism. The
       JMFTS-side equivalent would be a distinct ``session_store.parent_id``
       for RPC runs, which is a different (unclaimed) piece of work.
    """
    from tau_coding_agent.session_store import rpc_default_session_base
    from tau_coding_agent.store_factory import resolve_session_dir

    explicit = resolve_session_dir(session_dir_flag)
    if explicit is not None:
        return explicit
    if store_name == "file":
        return rpc_default_session_base()
    return None


async def run_rpc(args: "CLIArgs", config: dict[str, Any]) -> int:
    """Run the RPC protocol server until stdin closes or a shutdown signal fires.

    Returns the process exit code: 0 on a clean stdin-EOF shutdown, or
    ``RPCHandler.exit_code`` (143/129 — P1) when a SIGTERM/SIGHUP drove it.
    ``cli.py`` has already rejected every flag combination that would be
    silently ignored here (``--print``, positional messages, session
    continuation) — see its ``--mode rpc`` validation block.
    """
    # T2: see module docstring. Balanced by the `finally` below; RPCHandler
    # .run() takes its own (nested) claim and releases it before this one is
    # released, so `sys.stdout` is never restored while setup could still be
    # running.
    transport._take_over_stdout()
    try:
        model_name, model_config = resolve_model_config(config, args)

        # System prompt: folded directly into the model config, the TUI's
        # convention (`cli.py:_launch_tui`'s `overrides["system_prompt"]`) —
        # NOT headless.py's "store it as the session's first message" scheme,
        # which exists only because that path has a SessionCatalog persisting
        # the session; RPC mode has no such layer yet (H1, phase 3), so the
        # backend's own `system_prompt` is the sole place it can live.
        base_system_prompt = (
            args.system_prompt
            if args.system_prompt is not None
            else config.get("system_prompt", "")
        )
        if base_system_prompt:
            model_config["system_prompt"] = base_system_prompt
        # ``append_system_prompt`` is left on the entry for ``TauBackend`` to
        # apply. Folding it in here would append it twice now that the backend
        # does the same thing for every frontend.

        # Imported lazily, matching headless.py's own comment: keeps a bare
        # `import tau_coding_agent.rpc_mode` free of the backend/agent-core
        # import chain until a run actually happens.
        from tau_coding_agent.backends import create_backend, make_model_resolver

        backend = create_backend(model_config)

        agent_session = getattr(backend, "agent_session", None)
        if agent_session is not None and hasattr(agent_session, "set_model_resolver"):
            agent_session.set_model_resolver(make_model_resolver(config.get("models", {})))

        # AgentSessionRuntime (phase 3, H1): the session-lifecycle layer behind
        # the new_session/fork/switch_session verbs. `cli.py`'s --mode rpc
        # validation still rejects --store/--session/etc at STARTUP (a
        # separate, not-yet-relaxed restriction — every run still starts
        # fresh), so `args.store` is always None here and
        # `build_session_catalog` resolves the same default every other mode
        # falls back to with no --store flag. Built and bound BEFORE
        # extensions load (matching app.py's own ordering: _bind_backend_session
        # precedes _load_backend_extensions) — which session_log is bound does
        # not affect extension registration, only persistence.
        from tau_agent_core.agent_session_runtime import AgentSessionRuntime
        from tau_coding_agent.store_factory import build_session_catalog, resolve_backend_name

        store_name = resolve_backend_name(config, args.store)
        session_dir = _resolve_rpc_session_dir(args.session_dir, store_name)
        # `persist=not args.no_session` — the same scoping `run_print` applies,
        # and it means something WEAKER here, deliberately. In print mode an
        # ephemeral run can never reach the store at all. Over the wire it can:
        # `list_sessions`, `switch_session`, `fork` and `new_session {"persist":
        # true}` stay reachable under `--no-session`, which is why the catalog
        # is still built rather than skipped. What `persist=False` drops is the
        # startup CONTACT (JMFTS's `GET /`), so a `--no-session` host that never
        # asks for any of those four never needs the server up — and one that
        # does ask meets the failure as an error response to that request,
        # rather than as an exit-2 the host cannot distinguish from a bad flag.
        session_catalog = build_session_catalog(
            config, args.store, session_dir, persist=not args.no_session
        )
        cwd = os.getcwd()
        backend_name = model_config.get("backend", "")
        runtime: "AgentSessionRuntime | None" = None
        if agent_session is not None:
            # Establishes the invariant AgentSessionRuntime.fork() depends on:
            # session_log is ALWAYS a catalog-produced ConversationSession from
            # this point on, never the bare scratch InMemorySessionLog
            # TauBackend.__init__ constructs (see agent_session_runtime.py's
            # fork() docstring for what breaks if this invariant is skipped).
            # PERSISTED BY DEFAULT (`create`) — Blocker 2 of the Tier B
            # review, see this module's docstring: still fresh, but with a
            # location, so the entries set_model (D-2) and set_session_name
            # append to the startup session outlive the process instead of
            # landing in a list nobody writes.
            #
            # `--no-session` selects `create_ephemeral` instead. Two properties
            # make that a one-line change rather than a new mode: every
            # SessionCatalog must implement `create_ephemeral` (it is on the
            # ABC, and the contract suite exercises it), and both shipped
            # stores answer it honestly — the file store with a `path`-less
            # Session whose `_persist_*` are no-ops, JMFTS with
            # `_EphemeralConversationSession` rather than a silent write to the
            # document server. Nothing downstream needs to know which it got:
            # `session_log_is_addressable` reads the same declared-location
            # attributes D-7 checks, so the refusals and `get_state`'s
            # `addressable` follow from the object itself.
            #
            # `run_print` makes the same choice on the same seam
            # (`headless.py`: `catalog.create_ephemeral if args.no_session else
            # catalog.create`), and this is deliberately the identical shape —
            # the flag meant one thing in `--print` and nothing at all here,
            # which is the defect being closed.
            create_session = (
                session_catalog.create_ephemeral if args.no_session else session_catalog.create
            )
            initial_session = create_session(cwd, model_name, backend_name)
            bind_session_log = getattr(backend, "bind_session_log", None)
            if bind_session_log is not None:
                bind_session_log(initial_session)
            runtime = AgentSessionRuntime(
                agent_session, session_catalog, cwd, model_name, backend_name, store_name
            )

        # Headless dialog policy (same seam run_print uses). RC3's v1 policy
        # for an extension-opened UI method is "fail fast, never hang"; with
        # no `--ui-defaults`/config "ui_defaults" policy that fail-fast IS a
        # raise (HeadlessDialogError), which is the correct default for a
        # process with no reverse channel (§7.1, deferred) to ask a host
        # through.
        set_ui_defaults = getattr(backend, "set_headless_ui_defaults", None)
        if set_ui_defaults is not None:
            ui_defaults = resolve_ui_defaults(config, parse_ui_defaults(args.ui_defaults))
            try:
                set_ui_defaults(ui_defaults)
            except ValueError as exc:
                raise CLIError(str(exc)) from exc

        # Extensions: identical call to run_print's (explicit -e paths +
        # discovery toggle + per-extension config overrides). A discovered
        # load failure goes to stderr (T4 bullet 1); an explicit -e failure
        # raises out of load_extensions (Fail-Early — the operator named it).
        explicit_extensions = model_config.get("extensions") or None
        discover_extensions = not model_config.get("no_extensions", False)
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

        emit_session_start = getattr(backend, "emit_session_start", None)
        if emit_session_start is not None:
            await emit_session_start("startup")

        assert agent_session is not None, (
            "create_backend() returned a backend with no .agent_session — "
            "RPCHandler has nothing to dispatch against"
        )
        handler = RPCHandler(agent_session, runtime=runtime)
        try:
            await handler.run()
        finally:
            # AgentSessionRuntime.dispose() (H1) — the SAME session_shutdown
            # ("quit") firing every other frontend's own teardown already
            # does, routed through the runtime for symmetry with
            # new_session/fork/switch_session rather than reaching past it
            # to backend.emit_session_shutdown directly.
            if runtime is not None:
                await runtime.dispose()
            else:
                emit_session_shutdown = getattr(backend, "emit_session_shutdown", None)
                if emit_session_shutdown is not None:
                    await emit_session_shutdown("quit")

            # Close the pooled τ-llm providers' HTTP clients for this loop
            # (docs/PROVIDER-LIFETIME.md §6.3), same placement/reasoning as
            # run_print's own finally: after session_shutdown (a handler may
            # itself make a final LLM call), inside the same asyncio.run()
            # that drove the server, the last point the loop is guaranteed
            # still alive to close on.
            from tau_llm.client import aclose_providers

            await aclose_providers()

        return handler.exit_code if handler.exit_code is not None else 0
    finally:
        transport._release_stdout()
