"""Running a flow: one step at a time, until it is ready to perform.

:mod:`tau_agent_core.capabilities` says what the flows ARE; this runs them. Two
functions, and between them they are the whole of what a head needs to offer a
gesture it has never heard of:

- :func:`next_step` takes a flow name and the arguments bound so far, and returns
  either the next argument to ask for or a :class:`Ready` naming the mutation and
  its arguments. Pure — it reads no session and performs nothing.
- :func:`enumerate_domain` takes a domain and returns the values that are legal for
  it right now, each with a label a person can read.

The loop is the same one whether the head is a modal wizard, a tab-completion
popup, or a shell. A wizard binds one argument per screen; a completion popup binds
one per keystroke-and-Tab; a shell binds all of them at once and gets
:class:`Ready` on the first call. Nothing in the loop knows which it is talking to.

The four things a dispatched gesture can be
-------------------------------------------

:class:`FlowStep` (an argument is missing), :class:`Ready` (perform it),
:class:`Performed` (it ran, and here is what came back) and :class:`View` (only a
head can open this). The first two were here from the start; the second two were
missing, and their absence is why a two-valued ``performer`` flag was still doing
work — the input side was a union already, and what running a gesture PRODUCED had
no record at all.

**Partial arguments are the dry run.** A flow invoked with nothing bound reports its
first step and changes nothing; with everything bound it is ready to run. There is
no ``-y`` and no confirmation step, because the shape already gives a caller a look
before it commits. What is deliberately NOT covered is a flow invoked complete in
one shot, which shows no intermediate step at all — see docs/ARCHITECTURE.md on why
a general τ-side dry run cannot be honest for a mutation that spends model calls.

Reference: docs/ARCHITECTURE.md, the capability/flow/view model settled 2026-09-03.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tau_llm.docs import agent_facing

from tau_agent_core.capabilities import BUILTIN, Argument, Domain, Flow, Vocabulary
from tau_agent_core.conversation_tree import ConversationTree, MessageIdScope

_ENUMERATION_LIMIT = 50

__all__ = [
    "Dispatched",
    "DomainValue",
    "DomainValues",
    "FlowStep",
    "Performed",
    "Ready",
    "UnknownFlowError",
    "View",
    "bind_command_args",
    "bind_text",
    "enumerate_domain",
    "flow_arguments",
    "flow_form_spec",
    "next_step",
]


class UnknownFlowError(KeyError):
    """A caller named a flow that is not declared.

    Fail-Early: returning ``None`` would let a head silently offer nothing for a
    gesture it believes exists, which is indistinguishable from a flow whose
    arguments happen to all be bound.
    """


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class FlowStep:
    """One argument a flow still needs, and everything required to ask for it.

    A head renders this and calls :func:`next_step` again with one more argument
    bound. It never decides what may be entered: ``domain`` says where the legal
    values come from, and :func:`enumerate_domain` computes them.

    Attributes:
        flow: The flow's name.
        argument: The argument being asked for.
        domain: That argument's :class:`~tau_agent_core.capabilities.Domain`, resolved
            here so a head need not look it up.
        cursor: The entry a scoped ``message_id`` argument is relative to, carried
            through from the :func:`next_step` call so the head hands it straight back
            to :func:`enumerate_domain`.
        bound: The arguments already bound, so a head redrawing a form has them.
    """

    flow: str
    argument: Argument
    domain: Domain
    cursor: str | None
    bound: dict[str, Any]


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class Ready:
    """A flow with every required argument bound: the mutation, and what to call it with.

    A commitment, not a proposal. Once this is returned the arguments are complete,
    and performing it is the caller's business — the core does not ask a second time.
    A head that wants an "are you sure" renders one from this, because it names both
    halves.

    Attributes:
        flow: The flow's name.
        mutation: The capability to perform — the flow's, always. Which mutation runs
            is a property of which flow was named, never of what was bound.
        arguments: What to perform it with, keyed by the mutation's own parameter
            names, so a caller can splat it.
    """

    flow: str
    mutation: str
    arguments: dict[str, Any]


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class Performed:
    """What a capability produced. The past tense of :class:`Ready`.

    :class:`Ready` names a mutation and what to call it with; this names the same
    mutation and what came back. Until it existed a head had no record for that
    half, which is why the four generic mutations reported four unrelated Python
    types — a ``dict``, a ``str``, a ``bool`` and an
    ``ExtensionActionResult`` — and the TUI stringified whichever it got.

    ``data`` is a plain dict rather than a per-capability type because it is what
    crosses the wire and what a ``--mode json`` line holds. Its typing lives in
    :attr:`~tau_agent_core.capabilities.Capability.returns`; nothing re-validates
    it on every call, the way nothing re-validates a params dict in process, and
    ``test_performed_records.py`` is what checks each producer against the schema.

    Attributes:
        flow: The flow that named the mutation, when a flow did. ``None`` when a
            caller performed the capability directly.
        mutation: The capability that ran.
        data: What it returned, keyed as its ``returns`` declares. JSON-able.
        cursor: The session-log cursor after the call, or ``None`` for a session
            with no log. It is the promoted copy of ``data["cursor"]`` wherever the
            capability declares one, so a head reads the same field for every
            mutation instead of knowing which ones carry it.
    """

    flow: str | None
    mutation: str
    data: dict[str, Any]
    cursor: str | None = None

    def __post_init__(self) -> None:
        inner = self.data.get("cursor", self.cursor)
        if inner != self.cursor:
            raise ValueError(
                f"Performed({self.mutation!r}) carries cursor={self.cursor!r} beside "
                f"data['cursor']={inner!r}. Two answers to 'where is the tip' is the "
                "drift the promoted field exists to remove."
            )

    def summary(self) -> str:
        """One line a head can show, from the data alone.

        Written once here for the reason
        :func:`~tau_agent_core.commands.unsupported_command_message` is: three heads
        want the same sentence, and the alternative is each inventing its own.

        A capability whose ``returns`` declares ``message`` has already written the
        line — the three extension actions do — and it is used verbatim. Otherwise
        the fields are named with their values, ``cursor`` excluded because it moves
        on nearly every mutation and says nothing to a reader.

        Returns:
            The line, never empty: a mutation that returned only a cursor still
            names itself.
        """
        message = self.data.get("message")
        if isinstance(message, str) and message:
            return message
        shown = [f"{key}={value!r}" for key, value in self.data.items() if key != "cursor"]
        return f"{self.mutation}: {', '.join(shown)}" if shown else self.mutation


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class View:
    """A named surface only a head can open.

    The third thing a dispatched gesture can be, beside a step and a performed
    mutation. ``/tree`` and ``/extensions`` are the two
    (:data:`~tau_agent_core.capabilities.VIEW_COMMANDS`), and what a view IS stays
    head-local: this record says which one was asked for, and either carries the
    state a head would draw it from or a written reason it cannot be drawn here.

    Exactly one of ``state`` and ``unavailable_because`` is set, and
    ``__post_init__`` enforces it. A ``View`` with neither is a head being handed
    nothing and told nothing, which is the silent no-op
    :class:`~tau_agent_core.commands.UnsupportedCommandError` exists to prevent;
    one with both is two answers to the same question.

    Attributes:
        name: The view's name, a key of
            :data:`~tau_agent_core.capabilities.VIEW_COMMANDS`.
        state: What a head draws the view from. ``None`` everywhere today — no
            capability projects the session tree yet (docs/VSCODE-HEAD.md §6), and
            this is the spot that payload lands in when one does, with no change to
            the union.
        unavailable_because: Why no ``state`` rides with this, in a sentence a head
            can print. A head that has its own view of that name ignores it and
            opens it; a head that has none prints it and does nothing else. Not a
            fallback: it is the same idiom the RPC table's seven
            ``declined_because`` entries already use.
    """

    name: str
    state: dict[str, Any] | None = None
    unavailable_because: str | None = None

    def __post_init__(self) -> None:
        if (self.state is None) == (self.unavailable_because is None):
            raise ValueError(
                f"View({self.name!r}) must carry exactly one of `state` / "
                f"`unavailable_because` — got state={self.state!r}, "
                f"unavailable_because={self.unavailable_because!r}. Neither is a head "
                "handed nothing and told nothing; both is two answers."
            )


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class DomainValue:
    """One legal value for a domain, and the text that identifies it to a person.

    Attributes:
        value: What a caller binds.
        label: What a caller shows. Equal to ``value`` for a domain whose values are
            already readable.
    """

    value: str
    label: str


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class DomainValues:
    """What :func:`enumerate_domain` returns.

    Attributes:
        domain: The domain's name.
        values: The legal values, bounded by the caller's limit.
        total: How many there were before the limit, so a caller can tell an empty
            domain from a truncated listing.
    """

    domain: str
    values: tuple[DomainValue, ...]
    total: int


def _flow(name: str, vocabulary: Vocabulary = BUILTIN) -> Flow:
    found = vocabulary.flow(name)
    if found is not None:
        return found
    raise UnknownFlowError(
        f"no flow named {name!r} — declared flows are {[f.name for f in vocabulary.flows]}"
    )


@agent_facing(topic="sessions")
def next_step(
    flow: str,
    bound: dict[str, Any] | None = None,
    cursor: str | None = None,
    vocabulary: Vocabulary = BUILTIN,
) -> FlowStep | Ready:
    """The next argument ``flow`` needs, or the mutation it is ready to perform.

    The one entry point a head needs to run a flow it has no special knowledge of.
    Pure: it reads no session, performs nothing, and returns the same answer for the
    same inputs.

    Only REQUIRED arguments block. An optional argument is never demanded — a caller
    that wants to offer one reads it off the flow's own ``arguments`` — which is what
    makes a flow with nothing but optional arguments run immediately, the way a
    command with only optional flags does.

    Args:
        flow: The flow's name.
        bound: The arguments bound so far. ``None`` and ``{}`` are the same thing:
            the flow's first step.
        cursor: The entry a ``message_id`` argument's scope is relative to. Required
            of the caller rather than read off a session, for the reason
            ``resolve_command`` takes ``extension_commands`` as a parameter: it keeps
            this callable from a head that is peeking, a runtime that is deciding,
            and a test with neither. A caller stepping a sub-agent's flow passes THAT
            agent's cursor.
        vocabulary: The registry to look the flow up in. A session's own
            (``AgentSession.vocabulary``) also carries the flows its extensions
            declared; the default is τ's alone.

    Returns:
        A :class:`FlowStep` while a required argument is unbound, otherwise a
        :class:`Ready`.

    Raises:
        UnknownFlowError: No flow has that name.
    """
    declared = _flow(flow, vocabulary)
    have = dict(bound or {})

    for argument in declared.arguments:
        if argument.required and argument.name not in have:
            return FlowStep(
                flow=declared.name,
                argument=argument,
                domain=vocabulary.domains[argument.domain],
                cursor=cursor,
                bound=have,
            )

    return Ready(flow=declared.name, mutation=declared.mutation, arguments=have)


@agent_facing(topic="sessions")
def flow_arguments(flow: str, vocabulary: Vocabulary = BUILTIN) -> tuple[Argument, ...]:
    """Every argument ``flow`` declares, in the order it asks for them.

    A head building a whole form needs the list, not just the next one
    :func:`next_step` blocks on. Pure, and the same lookup ``next_step`` uses, so the
    two cannot come to disagree about what a flow takes.

    Args:
        flow: The flow's name.
        vocabulary: The registry to look the flow up in. A session's own
            (``AgentSession.vocabulary``) also carries the flows its extensions
            declared; the default is τ's alone.

    Returns:
        The declared arguments. Empty for a flow that takes none.

    Raises:
        UnknownFlowError: No flow has that name.
    """
    return _flow(flow, vocabulary).arguments


@agent_facing(topic="sessions")
def flow_form_spec(
    flow: str,
    bound: dict[str, Any] | None = None,
    *,
    options: dict[str, list[str]] | None = None,
    vocabulary: Vocabulary = BUILTIN,
) -> dict[str, Any]:
    """The arguments ``flow`` still needs, as a ``ui.form`` spec.

    The join between the two halves of τ's argument vocabulary. Flows describe an
    argument as a :class:`~tau_agent_core.capabilities.Domain` plus a cardinality;
    :func:`~tau_agent_core.extension_types.validate_form_spec` describes one as a
    field kind. :class:`~tau_agent_core.capabilities.Argument`'s docstring has always
    claimed the pair ``(domain, cardinality)`` replaces the five field kinds — this
    is that claim as a function, so one form renderer per head serves built-in flows,
    extension-declared flows and an extension's own ``ui.form`` alike.

    Asks for exactly the arguments :func:`next_step` would block on: required and not
    yet bound. A flow whose remaining arguments are all optional produces no form,
    because it is already runnable and a dialog in front of it would be a
    confirmation step τ does not have (see this module's "Partial arguments are the
    dry run").

    Args:
        flow: The flow's name.
        bound: The arguments bound so far. ``None`` and ``{}`` are the same thing.
        options: The legal values for any argument whose domain renders as a
            ``select``, keyed by ARGUMENT name — from
            :func:`enumerate_domain`, which needs the live objects this function
            deliberately does not take. Pure: the caller reads, this shapes.
        vocabulary: The registry to look the flow up in. A session's own
            (``AgentSession.vocabulary``) also carries the flows its extensions
            declared; the default is τ's alone.

    Returns:
        A spec ``{"title": …, "fields": [...]}`` that
        :func:`~tau_agent_core.extension_types.validate_form_spec` accepts, or an
        empty dict when the flow needs nothing.

    Raises:
        UnknownFlowError: No flow has that name.
        ValueError: An argument renders as a ``select`` and ``options`` carries no
            non-empty list for it. Fail-Early: degrading a select to a free text box
            would silently accept values the domain does not admit, and offering an
            empty select would be a question with no answers.
    """
    declared = _flow(flow, vocabulary)
    have = dict(bound or {})
    supplied = options or {}

    fields: list[dict[str, Any]] = []
    for argument in declared.arguments:
        if not argument.required or argument.name in have:
            continue
        domain = vocabulary.domains[argument.domain]
        kind = domain.field_kind
        if kind == "select" and argument.cardinality == "many":
            kind = "multiselect"
        field: dict[str, Any] = {
            "name": argument.name,
            "kind": kind,
            "label": argument.description,
        }
        if kind in ("select", "multiselect"):
            choices = supplied.get(argument.name)
            if not choices:
                raise ValueError(
                    f"flow {flow!r} argument {argument.name!r} is domain "
                    f"{domain.name!r}, which renders as {kind!r}, and no options were "
                    f"supplied for it. Enumerate {domain.name!r} first "
                    f"(enumerator {domain.enumerator!r}) and pass the values in."
                )
            field["options"] = list(choices)
        fields.append(field)

    if not fields:
        return {}
    return {"title": declared.description, "fields": fields}


@agent_facing(topic="sessions")
def bind_text(argument: Argument, text: str, vocabulary: Vocabulary = BUILTIN) -> Any:
    """Turn what a person TYPED into the value ``argument``'s mutation takes.

    A head that reads a line of text has a string; a mutation taking
    ``enabled: bool`` does not. Written here, once, because the alternative is
    every head inventing its own answer to "does ``/autocompact on`` mean true" —
    and then two heads accepting different words for the same flow. The words a
    fixed-value domain accepts are the words it DECLARES, and nothing else.

    Four cases, by what the domain says about itself. A domain with fixed
    ``values`` matches case-insensitively against exactly those, and the
    ``boolean`` domain additionally hands back a real ``bool``, since its two
    values name Python's. The free ``integer`` and ``number`` domains parse. Every
    other domain — free text, and the ones with an enumerator, whose values are
    strings a caller sends back verbatim — passes the text through unchanged.

    Not a validator for enumerated domains: whether ``a3f9c1`` names an entry is a
    question about a live tree, and :func:`enumerate_domain` is what answers it.
    This converts a TYPE, and refuses only where the type itself is wrong.

    Args:
        argument: The argument being bound, for its domain and its name.
        text: What the person typed, already stripped of the command word.
        vocabulary: The registry ``argument``'s domain is declared in.

    Returns:
        The bound value, ready to go into :func:`next_step`'s ``bound`` mapping.

    Raises:
        ValueError: The text is not a value of that domain, naming what is
            acceptable. Fail-Early: coercing an unrecognised word to ``False``
            would silently turn ``/autocompact yes`` into "off".
    """
    domain = vocabulary.domains[argument.domain]
    if domain.values is not None:
        lowered = text.strip().lower()
        for value in domain.values:
            if value.lower() == lowered:
                return value == "true" if argument.domain == "boolean" else value
        raise ValueError(
            f"{argument.name!r} takes one of {', '.join(domain.values)} — got {text.strip()!r}"
        )
    if argument.domain == "integer":
        try:
            return int(text.strip())
        except ValueError:
            raise ValueError(
                f"{argument.name!r} takes a whole number — got {text.strip()!r}"
            ) from None
    if argument.domain == "number":
        try:
            return float(text.strip())
        except ValueError:
            raise ValueError(f"{argument.name!r} takes a number — got {text.strip()!r}") from None
    return text


Dispatched = FlowStep | Ready | Performed | View
"""What a dispatched gesture resolved to. The four arms, as one type.

A head's whole rule is which arm came back: render the step, perform the mutation,
show what was performed, open the view. It replaced a two-valued ``performer``
flag, which had to answer a different question on each record it sat on — *will*
the core run this on the invocation, *did* it on the outcome — and could say
nothing at all about the other two arms.
"""


@agent_facing(topic="sessions")
def bind_command_args(flow: str, raw: str, vocabulary: Vocabulary = BUILTIN) -> dict[str, Any]:
    """Bind the text typed after a slash command to the flow's argument.

    The one-line half of running a flow from a command line, written here rather
    than in each head so two heads cannot come to accept different words for the
    same gesture — the reason :func:`bind_text` is here, one level down.

    A flow that takes no argument ignores ``raw``, and so does an empty ``raw``:
    both produce ``{}``, which :func:`next_step` turns into the flow's first step
    or into a :class:`Ready`, depending on whether anything was required. Stray
    text after an argument-less command is therefore still discarded, which is a
    known gap (docs/SLASH-COMMANDS.md §4) this function does not close.

    Args:
        flow: The flow's name.
        raw: Everything the reader typed after the command word.
        vocabulary: The registry to look the flow up in. A session's own
            (``AgentSession.vocabulary``) also carries the flows its extensions
            declared; the default is τ's alone.

    Returns:
        The bound arguments, ready for :func:`next_step`.

    Raises:
        UnknownFlowError: No flow has that name.
        ValueError: ``raw`` is not a value of the argument's domain, or the flow
            takes more than one argument — there is no rule for splitting one
            typed line across two, and guessing one is how a head would silently
            bind the wrong halves.
    """
    declared = _flow(flow, vocabulary)
    if not declared.arguments or not raw.strip():
        return {}
    if len(declared.arguments) > 1:
        raise ValueError(
            f"flow {flow!r} takes {len(declared.arguments)} arguments, and one typed line "
            "cannot say which is which. A head binds them one step at a time through "
            "next_step instead."
        )
    argument = declared.arguments[0]
    return {argument.name: bind_text(argument, raw, vocabulary)}


def _model_values(session: Any) -> list[DomainValue]:
    resolver = session.model_resolver
    if resolver is None:
        raise ValueError(
            "enumerating domain 'model_name' needs a model resolver bound to the session "
            "(set_model_resolver, a closure over config 'models'), and none is. set_model "
            "would raise here too — this refuses rather than reporting no models exist."
        )
    names = getattr(resolver, "model_names", None)
    if names is None:
        raise ValueError(
            f"the bound model resolver ({type(resolver).__name__}) does not declare "
            "model_names(), so the names it accepts cannot be listed"
        )
    return [DomainValue(value=name, label=name) for name in names()]


def _session_values(runtime: Any) -> list[DomainValue]:
    return [
        DomainValue(value=info.id, label=info.name or info.first_message or info.id)
        for info in runtime.catalog.list(runtime.cwd)
    ]


def _path_values(runtime: Any, query: str, limit: int) -> tuple[list[DomainValue], int]:
    from pathlib import Path

    from tau_agent_core.attachments import complete_attachment

    cwd = Path(runtime.cwd)
    completion = complete_attachment(f"@{query}", len(query) + 1, cwd=cwd)
    if completion is None:
        return [], 0
    found = [
        DomainValue(value=m.name, label=m.detail or m.name) for m in completion.matches[:limit]
    ]
    return found, completion.total


def _extension_values(session: Any) -> list[DomainValue]:
    return [
        DomainValue(value=path, label=f"{path} ({'enabled' if on else 'disabled'})")
        for path, on in session.list_managed_extensions()
    ]


def _message_values(session: Any, scope: str | None, cursor: str | None, query: str, limit: int):
    log = session.session_log
    tree = ConversationTree(log.entries(), log.cursor)
    found = tree.complete_message_id(
        scope=scope or "in_session",  # type: ignore[arg-type]
        cursor=cursor,
        query=query,
        limit=limit,
    )
    return [DomainValue(value=m.entry_id, label=m.preview) for m in found.matches], found.total


@agent_facing(topic="sessions")
def enumerate_domain(
    domain: str,
    *,
    session: Any = None,
    runtime: Any = None,
    scope: MessageIdScope | None = None,
    cursor: str | None = None,
    query: str = "",
    limit: int = _ENUMERATION_LIMIT,
    vocabulary: Vocabulary = BUILTIN,
) -> DomainValues:
    """The values legal for ``domain`` right now, each with a readable label.

    The other half of the flow loop. A head calls this with the ``domain`` a
    :class:`FlowStep` handed it and renders what comes back — a completion list, a
    select field, a picker — without knowing what the domain means.

    Dispatches to the readers that already exist rather than reimplementing any of
    them, so a listing here and the corresponding RPC verb cannot disagree about what
    exists. A ``free`` domain has no value set and returns none, with ``total`` 0; a
    domain whose values are fixed returns them without touching either object.

    A domain an extension declared is answered by the callable it registered, looked
    up BEFORE τ's own dispatch so an extension cannot be shadowed by a name τ later
    adds. The callable is passed ``query`` and ``limit`` and returns
    ``(value, label)`` pairs; it never sees the session or the runtime, because an
    extension already holds its own context.

    Args:
        domain: The domain's name, a key of ``vocabulary.domains``.
        session: The :class:`~tau_agent_core.agent_session.AgentSession` to read
            models, extensions and the session tree from.
        runtime: The :class:`~tau_agent_core.agent_session_runtime.AgentSessionRuntime`
            to read the session catalog and the working directory from. Separate from
            ``session`` because both live there, not on the session. ``path`` needs it
            as much as ``session_id`` does: completing against the process's own
            directory instead of the runtime's answers a different question than the
            one that was asked, so its absence raises rather than falling back.
        scope: For ``message_id``, which entries are candidates.
        cursor: For a scoped ``message_id``, the entry the scope is relative to.
        query: Filter text. Honoured by the domains whose readers take one; a domain
            with a small fixed set ignores it.
        limit: How many values to return at most.
        vocabulary: The registry to look ``domain`` up in, and whose ``enumerators``
            answer for a domain an extension declared.

    Returns:
        A :class:`DomainValues` carrying the values and the true total.

    Raises:
        KeyError: No domain has that name.
        ValueError: The domain needs an object this call did not supply — a session
            or a runtime. Fail-Early: returning an empty list would say "there are
            none" when the truth is "nothing was asked".
    """
    declared = vocabulary.domains[domain]

    supplied = vocabulary.enumerators.get(domain)
    if supplied is not None:
        found = [DomainValue(value=str(v), label=str(label)) for v, label in supplied(query, limit)]
        return DomainValues(domain=domain, values=tuple(found[:limit]), total=len(found))

    if declared.free:
        return DomainValues(domain=domain, values=(), total=0)
    if declared.values is not None:
        return DomainValues(
            domain=domain,
            values=tuple(DomainValue(value=v, label=v) for v in declared.values),
            total=len(declared.values),
        )

    def _require(obj: Any, what: str) -> Any:
        if obj is None:
            raise ValueError(
                f"enumerating domain {domain!r} needs a {what}, and none was passed. "
                "An empty list here would report 'there are none' for a question that "
                "was never asked."
            )
        return obj

    if domain == "message_id":
        values, total = _message_values(_require(session, "session"), scope, cursor, query, limit)
        return DomainValues(domain=domain, values=tuple(values), total=total)

    if domain == "path":
        values, total = _path_values(_require(runtime, "runtime"), query, limit)
        return DomainValues(domain=domain, values=tuple(values), total=total)

    if domain == "model_name":
        found = _model_values(_require(session, "session"))
    elif domain == "session_id":
        found = _session_values(_require(runtime, "runtime"))
    elif domain == "extension_name":
        found = _extension_values(_require(session, "session"))
    else:
        raise ValueError(
            f"domain {domain!r} declares enumerator {declared.enumerator!r}, which "
            "enumerate_domain has no dispatch for"
        )

    if query:
        needle = query.lower()
        found = [v for v in found if v.value.startswith(query) or needle in v.label.lower()]
    return DomainValues(domain=domain, values=tuple(found[:limit]), total=len(found))
