"""Command dispatch: the decision that an input is a command, made in the core.

Reference: docs/SUBMISSION-LIFECYCLE.md, ``submit()`` step 3 ("**Command dispatch**, if
``expand_commands``. This is the logic that must move out of ``on_input_submitted``") and
phase 3 of the phasing table.

Until this module existed, "does a leading ``/`` mean something" was answered inside a Textual
event handler (``tau_coding_agent/app.py`` ``on_input_submitted``): ``/compact``, ``/tree``,
``/fork``, ``/extensions`` were intercepted there, and extension-registered commands were
dispatched there through ``getattr(self.current_backend, "run_extension_command", None)``. A bus
event, a webhook, or an RPC client therefore had no command vocabulary at all, and the TUI's
vocabulary was invisible to every other input source.

The split this module encodes
-----------------------------

**The core decides; the frontend performs.** Some commands are inherently frontend-shaped —
``/tree`` opens a modal, ``/extensions`` renders a panel — and the core cannot push a Textual
screen, so it must not pretend to run them. But the *decision* ("this input is command X with
arguments Y, and it is NOT a prompt") belongs in one place, because it is the decision that
determines whether a turn runs at all.

So :func:`resolve_command` is pure and total — it answers "what command, if any" from the text
plus the set of extension-registered command names — and it is called from exactly two places:
``AgentSession.submit()``, which is the authority (it also enforces
:attr:`Submission.expand_commands`), and a frontend that needs to know *before* it renders a user
bubble whether a turn is coming. Both get the same answer from the same function.

:class:`CommandOutcome` is what ``submit()`` returns on
:class:`~tau_agent_core.submission.SubmissionResult`. Two shapes:

- ``performer="core"`` — the core already ran it (an extension-registered command, via
  ``AgentSession.run_extension_command``) and ``output`` is the handler's returned text. Any
  frontend can render a string.
- ``performer="frontend"`` — the core decided *what* it is and stopped there. The frontend must
  perform it, and a frontend that cannot must raise :class:`UnsupportedCommandError` rather than
  return silently. That is the Fail-Early half of the seam: a ``/tree`` that quietly does nothing
  under ``--mode json`` is the "works in the TUI, no-ops for the web frontend" failure class the
  spec names, and it is indistinguishable from a bug until someone reads the source.

Why the built-in names are hardcoded HERE rather than registered by the frontend: if the table
were a registry the frontend populates, a frontend that cannot perform ``/tree`` would simply not
register it, ``resolve_command`` would return ``None``, and "/tree" would be sent to the model as
prompt text — a silent fallback wearing a plausible face. These four names are τ's own built-in
vocabulary; every frontend is answerable for them, and one that is not says so out loud.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

#: Who runs a resolved command. ``"core"`` = ``AgentSession`` ran it and produced text;
#: ``"frontend"`` = the core identified it and the caller must perform it (or raise).
CommandPerformer = Literal["core", "frontend"]

#: τ's built-in commands, name → what performing one requires of a frontend. These are
#: frontend-shaped by construction (a modal, a panel, a transcript re-render), so the core
#: resolves them and hands the invocation back rather than running them.
#:
#: Deliberately a module constant and not a per-frontend registry — see the module docstring.
#: The descriptions are not chrome: they are what an :class:`UnsupportedCommandError` quotes so
#: the failure names what the frontend was asked to do and could not.
FRONTEND_COMMANDS: dict[str, str] = {
    "compact": "compact the conversation and re-render the transcript",
    "tree": "open the session-tree browser",
    "fork": "open the session-tree browser (pi's alias of /tree)",
    "extensions": "list loaded extensions, or run `enable|disable|reload <name>`",
}


class UnsupportedCommandError(RuntimeError):
    """A resolved command reached a caller that cannot perform it.

    Raised by a frontend handed a ``performer="frontend"``
    :class:`CommandOutcome` it has no implementation for, and by
    :meth:`~tau_agent_core.agent_session.AgentSession.prompt`, whose ``list[dict]`` return type
    has no channel for a command outcome at all.

    Fail-Early: the alternative is returning normally having done nothing, which is the exact
    failure the submission lifecycle exists to remove — the caller believes its input was acted
    on and there is no trace anywhere that it was not.
    """


@dataclass(frozen=True)
class CommandInvocation:
    """The dispatch DECISION: this text is command ``name`` with argument string ``args``.

    Produced by :func:`resolve_command`, which is pure — constructing one runs nothing. It is
    what lets a frontend ask "is a turn coming?" before it renders a user bubble, and what
    ``submit()`` acts on in step 3.
    """

    name: str  # the command word, without the leading "/"
    args: str  # everything after the first space, verbatim (may be "")
    performer: CommandPerformer


@dataclass(frozen=True)
class CommandOutcome:
    """What ``submit()`` reports on :class:`~tau_agent_core.submission.SubmissionResult`.

    Flat scalars only, so it round-trips through ``dataclasses.asdict()``/``json.dumps`` for the
    same reason ``Submission.correlation`` is validated (spec decision 4): this rides out to
    ``--mode json`` and to any embedded frontend, and a live object here would detonate several
    hops downstream.
    """

    name: str
    args: str
    performer: CommandPerformer
    output: str | None = None
    # Only ever set when performer == "core" — the extension handler's returned text, already
    # coerced to display text by ExtensionCommandResult.output_text(). ``None`` means the
    # handler returned nothing, which is a command that ran and had nothing to say; it is NOT
    # the same as a command that did not run (that is an exception, not a None).


def parse_command(text: str) -> tuple[str, str] | None:
    """Split ``text`` into ``(name, args)`` if it is shaped like a command, else ``None``.

    Purely syntactic — it does not know which commands exist. ``/name rest of line`` splits on
    the FIRST space (pi's ``_tryExecuteExtensionCommand``), so arguments keep their internal
    spacing verbatim; a command with no argument yields ``""`` rather than ``None``, because
    "no arguments" and "empty arguments" are the same thing to every handler τ has.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:]
    space = body.find(" ")
    if space == -1:
        return body, ""
    return body[:space], body[space + 1 :].strip()


def resolve_command(
    text: str, extension_commands: Collection[str] = ()
) -> CommandInvocation | None:
    """The dispatch decision. ``None`` means "this is a prompt, send it to the model".

    ``extension_commands`` is the set of names extensions registered via
    ``api.register_command`` (``AgentSession.get_extension_commands``). It is a parameter rather
    than something read off a session so this function stays pure and callable from a frontend
    that is peeking, from ``submit()`` which is deciding, and from a test with neither.

    Resolution order mirrors what ``on_input_submitted`` did before this moved: τ's built-ins
    (:data:`FRONTEND_COMMANDS`) win over an extension that registered the same name, so an
    extension cannot shadow ``/compact``.

    An unknown ``/…`` returns ``None`` and is sent to the model as ordinary text — unchanged
    behaviour, and deliberately so: refusing every unrecognised slash would break pasting a file
    path, and the TUI has always fallen through here.
    """
    parsed = parse_command(text)
    if parsed is None:
        return None
    name, args = parsed
    if name in FRONTEND_COMMANDS:
        return CommandInvocation(name=name, args=args, performer="frontend")
    if name in extension_commands:
        return CommandInvocation(name=name, args=args, performer="core")
    return None


def unsupported_command_message(outcome: CommandOutcome, frontend: str) -> str:
    """The message :class:`UnsupportedCommandError` carries — one wording, every frontend.

    Names the command, what performing it requires, and which frontend could not, so the
    traceback identifies the culprit instead of merely the symptom (Fail-Early).
    """
    requirement = FRONTEND_COMMANDS.get(outcome.name, "be performed by the frontend")
    return (
        f"/{outcome.name} resolved to a frontend-performed command ({requirement}), "
        f"but {frontend} cannot perform it. docs/SUBMISSION-LIFECYCLE.md phase 3: the core "
        "decides what a command IS and the frontend performs it; a frontend that cannot must "
        "say so rather than return having silently done nothing."
    )
