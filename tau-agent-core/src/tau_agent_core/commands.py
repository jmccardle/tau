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

import difflib
from collections.abc import Collection, Mapping, Sequence
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
from tau_agent_core.compare import split_compare_args
from tau_agent_core.extension_sources import resolve_shorthand
from tau_agent_core.flows import (
    Dispatched,
    DomainValue,
    View,
    bind_command_args,
    flow_arguments,
    next_step,
)
from tau_agent_core.prompt_templates import (
    EMPTY_TEMPLATES,
    PromptTemplate,
    PromptTemplateCatalog,
    expand_prompt_template,
)
from tau_agent_core.resources import BrokenResource, ResourceError
from tau_agent_core.skills import EMPTY_SKILLS, SKILL_PREFIX, Skill, SkillCatalog, expand_skill

CommandOrigin = Literal["builtin", "extension", "template", "skill"]
"""Where a command NAME came from: τ's own vocabulary, a loaded extension, a
prompt template or a skill (docs/PROMPT-TEMPLATES-AND-SKILLS.md).

It says nothing about who runs the command; that is which arm of
:data:`~tau_agent_core.flows.Dispatched` comes back. A palette groups by it.
:func:`resolve_command` only ever returns the first two: a template or a skill
is not dispatched, it is expanded into the prompt (:func:`expand_prompt_command`).
"""

NAMESPACE_PREFIX = "ext:"
"""What every private-registry command name begins with (docs/EXTENSION-NAMESPACE.md).

``ext:pirate:speak`` is the pirate extension's ``speak``, and no other extension can
register it. A typed name like ``/speak`` is contested and a qualified one is not.
Completion hides these until the reader types the prefix. The owner is the
extension's identifier, dots included (docs/EXTENSION-PACKAGES.md §3).
"""


def qualified_command(owner: str, name: str) -> str:
    """The private-registry name for command ``name`` of extension ``owner``.

    Args:
        owner: The extension's identifier
            (:func:`~tau_agent_core.extension_sources.extension_identifier`).
        name: The command as the extension registered it.

    Returns:
        ``ext:<owner>:<name>``.

    Raises:
        ValueError: ``name`` contains a ``:``, either part contains whitespace,
            or either part is empty. An unparseable qualified name is a command
            nobody can type and every head would list.
    """
    if not owner or not name:
        raise ValueError(
            f"qualified_command needs both an owner and a name, got {owner!r}/{name!r}"
        )
    if ":" in name:
        raise ValueError(
            f"command name {name!r} contains a ':', which is the separator "
            f"{NAMESPACE_PREFIX}<extension>:<command> splits on. Rename the command."
        )
    if any(c.isspace() for c in owner + name):
        raise ValueError(
            f"{owner!r}/{name!r} contains whitespace; a command name is the first word of "
            "a line, so whitespace in it makes the rest read as arguments."
        )
    return f"{NAMESPACE_PREFIX}{owner}:{name}"


def split_qualified_command(name: str) -> tuple[str, str] | None:
    """``(owner, command)`` for a private-registry name, else ``None``.

    Splits on the LAST ``:``; :func:`qualified_command` keeps the command half
    free of one, and a module name cannot contain one.
    """
    if not name.startswith(NAMESPACE_PREFIX):
        return None
    body = name[len(NAMESPACE_PREFIX) :]
    colon = body.rfind(":")
    if colon <= 0 or colon == len(body) - 1:
        return None
    return body[:colon], body[colon + 1 :]


def is_qualified_command(name: str) -> bool:
    """Whether ``name`` is a well-formed private-registry name."""
    return split_qualified_command(name) is not None


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

    Attributes:
        name: The command word, without the leading ``/``.
        args: Everything after the first space, verbatim (may be ``""``).
        origin: Where the name came from.
        refusal: Set when the line is in the ``/ext:`` namespace but names no
            registered command (:func:`qualified_command_refusal`). Such a line
            is never a turn; ``submit()`` refuses it with this as the reason.
    """

    name: str
    args: str
    origin: CommandOrigin
    refusal: str | None = None


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

    An unknown ``/…`` returns ``None`` and is sent to the model as ordinary text: refusing every
    unrecognised slash would break pasting a file path. The ``/ext:`` namespace is the
    exception (#57). Nothing but a command starts that way, so an unresolvable one comes back
    with :attr:`CommandInvocation.refusal` set instead of reaching the model as text.
    """
    parsed = parse_command(text)
    if parsed is None:
        return None
    name, args = parsed
    if name in FRONTEND_COMMANDS:
        return CommandInvocation(name=name, args=args, origin="builtin")
    if name in extension_commands:
        return CommandInvocation(name=name, args=args, origin="extension")
    if not name.startswith(NAMESPACE_PREFIX):
        return None
    try:
        expanded = _expand_owner_shorthand(name, extension_commands)
    except ValueError as exc:
        return CommandInvocation(
            name=name, args=args, origin="extension", refusal=f"/{name}: {exc}"
        )
    if expanded is not None:
        return CommandInvocation(name=expanded, args=args, origin="extension")
    return CommandInvocation(
        name=name,
        args=args,
        origin="extension",
        refusal=qualified_command_refusal(name, extension_commands),
    )


def qualified_command_refusal(name: str, extension_commands: Collection[str]) -> str:
    """Why ``/<name>``, in the ``ext:`` namespace, is not a command, and what to type (#57).

    The nearest registered qualified name is suggested: first the 2.x spelling
    ``ext:<owner>.<command>`` read as ``ext:<owner>:<command>`` (owner shorthand
    allowed), then the closest by :func:`difflib.get_close_matches`.

    Args:
        name: The typed command word, without the ``/``; starts with ``ext:``.
        extension_commands: The registered command names.

    Returns:
        One sentence naming the qualified form, plus a suggestion when one is close.
    """
    qualified = sorted(n for n in extension_commands if is_qualified_command(n))
    suggestion: str | None = None
    body = name[len(NAMESPACE_PREFIX) :]
    if ":" not in body and "." in body:
        try:
            suggestion = _expand_owner_shorthand(
                NAMESPACE_PREFIX + body[: body.rfind(".")] + ":" + body[body.rfind(".") + 1 :],
                qualified,
            )
        except ValueError:
            suggestion = None
    if suggestion is None:
        close = difflib.get_close_matches(name, qualified, n=1, cutoff=0.6)
        suggestion = close[0] if close else None
    sentence = (
        f"/{name} is not a command; qualified names are /{NAMESPACE_PREFIX}<identifier>:<command>"
    )
    return f"{sentence}, e.g. /{suggestion}" if suggestion else sentence


def _expand_owner_shorthand(name: str, extension_commands: Collection[str]) -> str | None:
    """``ext:planner:budget`` as the one registered ``ext:<owner>:budget`` whose owner ends ``planner``.

    docs/EXTENSION-PACKAGES.md §4: resolved fresh against the loaded commands,
    never stored.

    Raises:
        ValueError: Two loaded owners share the typed segment.
    """
    split = split_qualified_command(name)
    if split is None:
        return None
    token, command = split
    owners = [
        parts[0]
        for parts in (split_qualified_command(n) for n in extension_commands)
        if parts is not None and parts[1] == command
    ]
    resolved = resolve_shorthand(token, owners)
    return None if resolved is None else qualified_command(resolved, command)


def expand_prompt_command(
    text: str,
    templates: PromptTemplateCatalog = EMPTY_TEMPLATES,
    skills: SkillCatalog = EMPTY_SKILLS,
) -> str | None:
    """The prompt ``text`` expands to, when it invokes a template or a skill.

    ``None`` means ``text`` invokes neither, and goes on as it is. The caller asks
    :func:`resolve_command` FIRST: a built-in or an extension command with the
    same name wins, so this is only reached for a line that is not a command.

    ``/skill:<name> args`` looks in ``skills``; any other ``/<name> args`` looks
    in ``templates``. An unknown name returns ``None``, the same fall-through an
    unknown command has.

    Args:
        text: The submitted line, after the ``input`` hooks.
        templates: The session's templates.
        skills: The session's skills.

    Returns:
        The expanded prompt, or ``None``.

    Raises:
        ResourceError: The name belongs to a file that could not be read, or the
            template cannot be expanded with these arguments.
    """
    parsed = parse_command(text)
    if parsed is None:
        return None
    name, args = parsed
    entry: PromptTemplate | Skill | BrokenResource | None
    if name.startswith(SKILL_PREFIX):
        entry = skills.get(name[len(SKILL_PREFIX) :])
    else:
        entry = templates.get(name)
    if entry is None:
        return None
    if isinstance(entry, BrokenResource):
        raise ResourceError(f"/{name} cannot run: {entry.source.path}: {entry.error}")
    if isinstance(entry, Skill):
        return expand_skill(entry, args)
    return expand_prompt_template(entry, args)


def prompt_command_completions(
    templates: PromptTemplateCatalog = EMPTY_TEMPLATES,
    skills: SkillCatalog = EMPTY_SKILLS,
    taken: Collection[str] = (),
) -> tuple[CommandCompletion, ...]:
    """The template and skill command words a reader can type, for a completion list.

    A word that is a built-in or in ``taken`` (the extension commands) is left
    out, because :func:`resolve_command` would never reach it. A broken entry is
    listed, with its error as the description, so the reader sees why the
    command will refuse.

    Args:
        templates: The session's templates.
        skills: The session's skills, listed as ``skill:<name>``.
        taken: Command names that resolve before a template or skill does.

    Returns:
        One candidate per reachable template, then per reachable skill.
    """
    rows = [
        CommandCompletion(template.name, _completion_text(template), "template")
        for template in templates.entries
    ]
    rows.extend(
        CommandCompletion(f"{SKILL_PREFIX}{skill.name}", _completion_text(skill), "skill")
        for skill in skills.entries
    )
    return tuple(row for row in rows if row.name not in FRONTEND_COMMANDS and row.name not in taken)


def _completion_text(entry: PromptTemplate | Skill | BrokenResource) -> str:
    """The one line a completion list shows for a template or skill."""
    if isinstance(entry, BrokenResource):
        return f"broken: {entry.error}"
    if isinstance(entry, PromptTemplate) and entry.argument_hint:
        return f"{entry.argument_hint} — {entry.description}"
    return entry.description


def dispatch_builtin(
    name: str,
    args: str,
    *,
    leaf: str | None = None,
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

    ``/compare`` is the one flow whose line binds two arguments, split by
    :func:`~tau_agent_core.compare.split_compare_args` (``models -- prompt``).

    Args:
        name: The command word, without the leading ``/``.
        args: Everything the reader typed after it.
        leaf: The entry a scoped ``message_id`` argument would be relative to,
            threaded through to the :class:`~tau_agent_core.flows.FlowStep`.
        vocabulary: The registry to resolve ``name`` in. A session's own
            (:attr:`~tau_agent_core.agent_session.AgentSession.vocabulary`) also
            carries the flows its extensions declared.

    Returns:
        A :class:`~tau_agent_core.flows.View` for a view command, otherwise the
        :class:`~tau_agent_core.flows.FlowStep` or
        :class:`~tau_agent_core.flows.Ready` the flow is at.

    Raises:
        UnsupportedCommandError: ``/extensions`` was given a verb that names no flow,
            or a view was given an argument it has nowhere to put.
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

    if name == "compare" and vocabulary.flow("compare") is not None:
        return next_step(name, split_compare_args(args), leaf=leaf, vocabulary=vocabulary)

    if name in vocabulary.views:
        if args.strip():
            raise UnsupportedCommandError(
                f"/{name} takes no arguments, so {args.strip()!r} would be discarded. "
                "A view is a surface a head opens; it carries no argument string for a "
                "head to read. Refusing rather than opening it and dropping what was "
                "typed (docs/SLASH-COMMANDS.md §4)."
            )
        return View(
            name=name,
            unavailable_because=(
                f"τ projects no state for the {name!r} view yet, so a head opens it from "
                "its own reads (docs/VSCODE-HEAD.md §6)"
            ),
        )
    return next_step(
        name, bind_command_args(name, args, vocabulary), leaf=leaf, vocabulary=vocabulary
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
    text: str,
    extension_commands: Mapping[str, str] = NO_EXTENSION_COMMANDS,
    prompt_commands: Sequence[CommandCompletion] = (),
) -> CommandCompletions | None:
    """Candidate commands for a partly-typed line. ``None`` means "show nothing".

    Pure, like :func:`resolve_command`, and it decides nothing. Matching is a
    case-sensitive prefix test on the first word, which is what dispatch does with
    the finished line; a bare ``/`` matches everything. Order is resolution
    order — built-ins, then extension commands, then templates and skills — and a
    name an earlier group already holds is dropped, because it cannot be reached.

    Args:
        text: The editor's contents as typed.
        extension_commands: Extension command names mapped to their descriptions
            (``AgentSession.get_extension_commands``).
        prompt_commands: Template and skill candidates
            (``AgentSession.get_prompt_commands``).

    Returns:
        A :class:`CommandCompletions`, or ``None`` when the line is not
        ``/``-prefixed, or when an unknown first word is followed by a space —
        the line is then prose (a pasted path), and a warning would be noise.
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
    matches.extend(
        candidate
        for candidate in prompt_commands
        if candidate.name.startswith(token)
        and candidate.name not in FRONTEND_COMMANDS
        and candidate.name not in extension_commands
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
