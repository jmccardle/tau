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

:data:`~tau_agent_core.flows.Dispatched` is what ``submit()`` returns on
:class:`~tau_agent_core.submission.SubmissionResult`. Four arms, and which one came
back is the whole of a head's rule:

- :class:`~tau_agent_core.flows.Performed` — the core already ran it (an
  extension-registered command, via ``AgentSession.run_extension_command``) and
  ``data["output"]`` is the handler's returned text. Any frontend can render a string.
- :class:`~tau_agent_core.flows.FlowStep` — a required argument is unbound. The head
  binds one, from a picker, a modal, a completion popup or argv, and asks again.
- :class:`~tau_agent_core.flows.Ready` — the mutation and its arguments. The head
  performs it and gets a ``Performed`` back.
- :class:`~tau_agent_core.flows.View` — a named surface only a head can open. A head
  that has one opens it; a head that has none prints ``unavailable_because`` or
  raises :class:`UnsupportedCommandError`, rather than returning silently. That is
  the Fail-Early half of the seam: a ``/tree`` that quietly does nothing under
  ``--mode json`` is the "works in the TUI, no-ops for the web frontend" failure
  class the spec names, and it is indistinguishable from a bug until someone reads
  the source.

This replaced a two-valued ``performer`` flag, whose trouble was that it answered a
different question on each record it sat on — *will* the core run this, on the
invocation; *did* it, on the outcome — and could say nothing at all about the two
arms that are neither.

Why the built-in names come from the CORE rather than from the frontend: if the table were a
registry the frontend populates, a frontend that cannot perform ``/tree`` would simply not
register it, ``resolve_command`` would return ``None``, and "/tree" would be sent to the model as
prompt text — a silent fallback wearing a plausible face. These names are τ's own built-in
vocabulary; every frontend is answerable for them, and one that is not says so out loud.

:data:`FRONTEND_COMMANDS` is now a projection of
:mod:`tau_agent_core.capabilities` rather than a literal typed here, so the flow table and the
slash vocabulary cannot drift. The list this module publishes is unchanged; where it comes from
is not.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from tau_agent_core.capabilities import (
    BUILTIN,
    FLOWS,
    Argument,
    Domain,
    Vocabulary,
    slash_vocabulary,
)
from tau_agent_core.flows import (
    Dispatched,
    DomainValue,
    View,
    bind_command_args,
    flow_arguments,
    next_step,
)

CommandOrigin = Literal["builtin", "extension"]
"""Where a command NAME came from: τ's own vocabulary, or a loaded extension.

It replaced ``CommandPerformer``, which said ``"core"`` / ``"frontend"`` and meant
"who runs this" — a question with four answers, now answered by which arm of
:data:`~tau_agent_core.flows.Dispatched` came back. What is left is a genuine fact
about the NAME, and the fact a head actually wants: a palette groups built-ins apart
from what an extension registered, and ``resolve_command`` resolves built-ins first
so an extension cannot shadow ``/compact``.
"""

EXTENSION_VIEW_VERBS: dict[str, str] = {
    flow.name.removesuffix("_extension"): flow.name
    for flow in FLOWS
    if flow.mutation.endswith("_extension")
}
"""The verb a reader types inside ``/extensions``, mapped to the flow it names.

``/extensions disable my_ext.py`` is sugar for ``/disable_extension my_ext.py``, kept
because it is what readers already type. It lives in the CORE rather than in a head
because ``/extensions`` resolves to a :class:`~tau_agent_core.flows.View`, and a view
carries no argument string — so a head holding this table would have had the verb
resolved out from under it and the target silently dropped.

Derived from :data:`~tau_agent_core.capabilities.FLOWS`, so the sugar cannot offer a
verb the registry does not declare.
"""

FRONTEND_COMMANDS: dict[str, str] = slash_vocabulary()
"""τ's built-in slash vocabulary: command name to one-line description.

Projected from :func:`tau_agent_core.capabilities.slash_vocabulary` — the flow table
plus the two view commands — rather than typed here. Ordering is load-bearing: the
completion popup and the RPC ``get_commands`` listing both render it in iteration
order, and ``compact`` is first.
"""


class UnsupportedCommandError(RuntimeError):
    """A resolved command reached a caller that cannot perform it.

    Raised by a frontend handed a :class:`~tau_agent_core.flows.View` or a
    :class:`~tau_agent_core.flows.Ready` it has no implementation for, and by
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
    origin: CommandOrigin


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
        return CommandInvocation(name=name, args=args, origin="builtin")
    if name in extension_commands:
        return CommandInvocation(name=name, args=args, origin="extension")
    return None


def dispatch_builtin(
    name: str,
    args: str,
    *,
    cursor: str | None = None,
    vocabulary: Vocabulary = BUILTIN,
) -> Dispatched:
    """Which arm a BUILT-IN command is, decided without running anything.

    The pure half of :meth:`~tau_agent_core.agent_session.AgentSession._perform_command`
    — pure for the reason :func:`resolve_command` is: a head peeking, a session
    dispatching and a test with neither must all get the same answer from the same
    function. The impure half is one case, an extension-registered command, which the
    session runs and reports as a :class:`~tau_agent_core.flows.Performed`.

    ``/extensions <verb> <target>`` is rewritten here into the flow that verb names
    (:data:`EXTENSION_VIEW_VERBS`), because a :class:`~tau_agent_core.flows.View`
    carries no argument string — a head left holding that sugar would have had the
    target silently dropped.

    Args:
        name: The command word, without the leading ``/``.
        args: Everything the reader typed after it.
        cursor: The entry a scoped ``message_id`` argument would be relative to,
            threaded through to the :class:`~tau_agent_core.flows.FlowStep`.
        vocabulary: The registry to resolve ``name`` in. A session's own
            (:attr:`~tau_agent_core.agent_session.AgentSession.vocabulary`) also
            carries the flows its extensions declared.

    Returns:
        A :class:`~tau_agent_core.flows.View` for a view command, otherwise the
        :class:`~tau_agent_core.flows.FlowStep` or
        :class:`~tau_agent_core.flows.Ready` the flow is at.

    Raises:
        UnsupportedCommandError: ``/extensions`` was given a verb that names no flow.
        UnknownFlowError: ``name`` is neither a view nor a declared flow.
        ValueError: ``args`` is not a value of the flow argument's domain.
    """
    if name == "extensions" and args.strip():
        verb, _, target = args.strip().partition(" ")
        flow = EXTENSION_VIEW_VERBS.get(verb)
        if flow is None:
            legal = " | ".join(sorted(EXTENSION_VIEW_VERBS))
            raise UnsupportedCommandError(
                f"/extensions {verb!r} names no action (use: {legal}). Refusing rather "
                "than opening the listing and discarding what was typed."
            )
        name, args = flow, target.strip()

    if name in vocabulary.views:
        return View(
            name=name,
            unavailable_because=(
                f"τ projects no state for the {name!r} view yet, so a head opens it from "
                "its own reads (docs/VSCODE-HEAD.md §6)"
            ),
        )
    return next_step(
        name, bind_command_args(name, args, vocabulary), cursor=cursor, vocabulary=vocabulary
    )


NO_EXTENSION_COMMANDS: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class CommandCompletion:
    """One candidate in a completion list: a command, and what it does.

    ``origin`` is carried because it is already known here and a head may want to group
    by it; nothing in τ's own UI renders it today.
    """

    name: str  # the command word, without the leading "/"
    description: str  # what it does, for the reader — may be "" for an extension command
    origin: CommandOrigin


@dataclass(frozen=True)
class CommandCompletions:
    """What a completion UI should show for a half-typed line.

    ``matches`` is EMPTY for a ``/…`` that names nothing, and that is the case the whole
    function exists for: an unknown slash is sent to the model as ordinary text
    (:func:`resolve_command`), which is deliberate but invisible. A frontend that shows an
    empty ``matches`` as "this will be sent as text" turns a silent fallthrough into a
    visible statement, without changing what the fallthrough does.
    """

    token: str  # the first word as typed, without the leading "/" (may be "")
    matches: tuple[CommandCompletion, ...]


def complete_command(
    text: str, extension_commands: Mapping[str, str] = NO_EXTENSION_COMMANDS
) -> CommandCompletions | None:
    """Candidate commands for a partly-typed line. ``None`` means "show nothing".

    Pure, and the same shape as :func:`resolve_command`: it takes the text plus the
    extension vocabulary rather than reading either off a session, so a frontend, a test,
    and an embedded UI all get the same answer from the same code. It runs nothing and
    decides nothing — ``resolve_command`` remains the only thing that says whether a line
    IS a command.

    Matching is a case-sensitive prefix test on the first word, because that is what
    ``resolve_command`` does with the finished line. A ``/`` on its own has an empty
    prefix and therefore matches everything, which is how the whole vocabulary becomes
    browsable. Built-ins come first and an extension that registered a built-in's name is
    dropped, mirroring the resolution order exactly: such a name is unreachable, so
    offering it would advertise a command the user cannot run.

    Args:
        text: The editor's contents as typed. Stripped here, the way ``parse_command``
            strips, so the two agree about what the first word is.
        extension_commands: Extension-registered command names mapped to their
            descriptions (``AgentSession.get_extension_commands``). A frontend with no
            backend yet passes nothing and gets the built-ins.

    Returns:
        A :class:`CommandCompletions` while the first word is still worth commenting on,
        or ``None`` when it is not. ``None`` covers two cases: the line is not
        ``/``-prefixed at all, and the line has an unknown first word FOLLOWED BY A SPACE.
        The second is the "pasted a file path" case — once a space is typed after a word
        that names no command, the line is committed to being prose and a warning about it
        would be noise.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:]
    space = body.find(" ")
    token = body if space == -1 else body[:space]

    matches: list[CommandCompletion] = [
        CommandCompletion(name=name, description=description, origin="builtin")
        for name, description in FRONTEND_COMMANDS.items()
        if name.startswith(token)
    ]
    matches.extend(
        CommandCompletion(name=name, description=description, origin="extension")
        for name, description in extension_commands.items()
        if name.startswith(token) and name not in FRONTEND_COMMANDS
    )

    if not matches and space != -1:
        return None
    return CommandCompletions(token=token, matches=tuple(matches))


@dataclass(frozen=True)
class ArgumentSlot:
    """The VALUE a half-typed command line is asking for, and where it sits in the text.

    The second half of slash completion, and the same split as the first: this says
    *which argument, of what domain, over which span*, and it says it purely —
    listing the values is :func:`~tau_agent_core.flows.enumerate_domain`, which needs
    a live session or runtime and therefore cannot be answered here.

    Attributes:
        command: The FLOW the value belongs to, which is not always the word that
            was typed: ``/extensions disable x`` resolves to ``disable_extension``,
            the rewrite :func:`dispatch_builtin` performs.
        argument: The flow argument being typed.
        domain: ``argument``'s domain, resolved so a caller need not look it up.
        query: What has been typed of the value, stripped the way
            :func:`parse_command` strips, so the filter and the eventual binding
            agree about what the word is.
        start: Offset into the ORIGINAL text where the value begins. A caller
            replacing ``text[start:end]`` with a chosen value gets a line
            :func:`resolve_command` binds to that value.
        end: Offset one past the value, always the end of the text — a flow takes
            one typed line as one argument (:func:`bind_command_args`), so there is
            never a token after it.
    """

    command: str
    argument: Argument
    domain: Domain
    query: str
    start: int
    end: int


def complete_command_argument(text: str, vocabulary: Vocabulary = BUILTIN) -> ArgumentSlot | None:
    """Which argument ``text`` is asking for. ``None`` means "offer no values".

    Pure, and the counterpart to :func:`complete_command`: that one completes the
    command WORD, this one completes what follows it. They are disjoint by
    construction — this returns ``None`` until a space follows a word that names a
    command, which is exactly when the word is settled.

    ``None`` covers every case with nothing to offer: the line is not a command; the
    name is still being typed; the word names an extension command that did not
    declare what it takes (``api.register_flow``, docs/EXTENSION-FLOWS.md — one that
    DID is completed exactly like a built-in); the command is a view; the flow takes
    no argument; or the argument's domain is ``free``, where any text is legal and a
    candidate list would misstate what is accepted.

    Args:
        text: The editor's contents as typed.
        vocabulary: The registry to resolve the command in. A session's own carries
            the flows its extensions declared, so passing it is what makes an
            extension gesture complete like a built-in.

    Returns:
        The :class:`ArgumentSlot` a head should enumerate and offer, or ``None``.
    """
    stripped = text.lstrip()  # only the LEAD: a trailing space is the reader asking for the list
    lead = len(text) - len(stripped)
    if not stripped.startswith("/"):
        return None

    body = stripped[1:]
    space = body.find(" ")  # parse_command splits on the first SPACE, so a newline is not one
    if space == -1:
        return None
    name = body[:space]
    if name not in FRONTEND_COMMANDS and vocabulary.flow(name) is None:
        return None

    value_start = 1 + space
    while value_start < len(stripped) and stripped[value_start] == " ":
        value_start += 1

    if name in vocabulary.views:
        if name != "extensions":
            return None
        verb, sep, _ = stripped[value_start:].partition(" ")
        if not sep:
            return None
        flow = EXTENSION_VIEW_VERBS.get(verb)
        if flow is None:
            return None
        name = flow
        value_start += len(verb) + len(sep)
        while value_start < len(stripped) and stripped[value_start] == " ":
            value_start += 1

    arguments = flow_arguments(name, vocabulary)
    if len(arguments) != 1:
        return None
    argument = arguments[0]
    domain = vocabulary.domains[argument.domain]
    if domain.free:
        return None

    return ArgumentSlot(
        command=name,
        argument=argument,
        domain=domain,
        query=stripped[value_start:].strip(),
        start=lead + value_start,
        end=lead + len(stripped),
    )


@dataclass(frozen=True)
class ArgumentCompletions:
    """An :class:`ArgumentSlot` joined to the values that are legal for it right now.

    Built by a head, because filling it needs
    :func:`~tau_agent_core.flows.enumerate_domain` and therefore a live session or
    runtime. It is declared here so two heads describe the same thing the same way,
    the way :class:`CommandCompletions` already does for the command word.

    Attributes:
        slot: What was being asked for.
        matches: The legal values, in the order a head should offer them.
        total: How many there were before the enumeration's limit.
        error: Why there are no values, when the reason is that the domain could not
            be enumerated at all. Carried rather than raised because this is redrawn
            on every keystroke, and carried rather than dropped because an empty list
            would say "there are none" for a question that failed — the same
            distinction ``enumerate_domain`` refuses to blur.
    """

    slot: ArgumentSlot
    matches: tuple[DomainValue, ...]
    total: int
    error: str | None = None


def unsupported_command_message(name: str, frontend: str) -> str:
    """The message :class:`UnsupportedCommandError` carries — one wording, every frontend.

    Names the command, what performing it requires, and which frontend could not, so the
    traceback identifies the culprit instead of merely the symptom (Fail-Early).

    Args:
        name: The command word, without the leading ``/``.
        frontend: What could not perform it, named so the traceback says which.

    Returns:
        The one sentence every head raises with.
    """
    requirement = FRONTEND_COMMANDS.get(name, "be performed by the frontend")
    return (
        f"/{name} resolved to a command this caller must perform ({requirement}), "
        f"but {frontend} cannot perform it. docs/SUBMISSION-LIFECYCLE.md phase 3: the core "
        "decides what a command IS and the frontend performs it; a frontend that cannot must "
        "say so rather than return having silently done nothing."
    )
