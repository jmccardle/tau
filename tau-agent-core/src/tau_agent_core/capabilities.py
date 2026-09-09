"""The capability registry: what τ can do, said once, for every head.

τ says what it can do in five separate vocabularies — slash commands, the Textual
command palette, key bindings, RPC verbs, CLI flags — and until this module they
agreed only by hand. This is the one table they are meant to project from.

Three records, and the line between them is what a head has to supply
-------------------------------------------------------------------

:class:`Capability` is one read or one mutation. It is declared here, performed
by the core, and addressable by name from anywhere. Neither a read nor a mutation
needs a screen, so no capability is ever "frontend-performed". It declares what it
TAKES as well as what it is: ``arguments`` is written once here and read by the
wire schema, by the flow that ends in it, and by the head that performs it, so
those three cannot come to disagree about what a caller sends. It declares what it
GIVES BACK the same way: ``returns`` is one JSON Schema per capability, covering
reads as well as mutations, and the RPC table's ``result_schema`` is derived from
it rather than written a second time.

:class:`Flow` is an ordered argument list ending in one mutation. It is declared
here and PERFORMED by a head — not because the head can do something the core
cannot, but because the head is what has a person in front of it. A flow is closer
to a user story than to a function: the same flow is one modal in the TUI, a
tab-completed slash line in the editor, and two separate shell invocations
headlessly.

A *view* is the third thing and has no record here on purpose. A view composes
reads, holds its own selection state, and starts flows from that state. The tree
browser is a view. Views are head code, so declaring them centrally would only
force every head to pretend it had the same ones.

What is still missing, stated rather than implied
-------------------------------------------------

:data:`VIEW_COMMANDS` holds the slash names that open a view — ``/tree`` and
``/extensions``. It is permanent, and it declares a NAME and nothing else: a head that
did not resolve ``/tree`` would send the word to the model as prose, which is the
silent fallback ``commands.py`` was built to prevent. Which views a head has is still
head-local; it says so by what it offers and by refusing what it cannot perform.

:data:`FRONTEND_COMMANDS` in :mod:`tau_agent_core.commands` is derived from this
module rather than typed there, so the flow table and the slash vocabulary cannot
disagree. What used to be a ``CommandPerformer`` flag beside it is gone: which arm of
:data:`~tau_agent_core.flows.Dispatched` a command resolves to is the answer, and
``CommandOrigin`` carries the one fact that was genuinely about the NAME.

What is deliberately absent
---------------------------

There is no way to say that one gesture ends in one of several mutations. That was
tried — a ``mutations`` tuple with a ``discriminator`` argument choosing among them —
and it was wrong twice over. The pairing was positional, so reordering a domain's
values silently rewired the flow past every check; and the arguments were declared per
flow rather than per branch, so a branch could be asked for an argument it has no
parameter for. Both disappear once the answer is "those are two flows, and the gesture
that offers a choice between them is a view".

Reference: docs/ARCHITECTURE.md, the capability/flow/view model settled 2026-09-03.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, get_args

from tau_llm.docs import agent_facing

from tau_agent_core.conversation_tree import MessageIdScope

CapabilityKind = Literal["read", "mutation"]

Cardinality = Literal["one", "many"]

__all__ = [
    "BUILTIN",
    "CAPABILITIES",
    "Argument",
    "DomainEnumerator",
    "FlowDeclaration",
    "Capability",
    "CapabilityKind",
    "Cardinality",
    "DOMAINS",
    "Domain",
    "FLOWS",
    "Flow",
    "VIEW_COMMANDS",
    "Vocabulary",
    "slash_vocabulary",
]


_SINGLE_VALUE_FIELD_KINDS: frozenset[str] = frozenset({"text", "number", "confirm", "select"})

_FIELD_KINDS_BY_ANSWER: dict[str, frozenset[str]] = {
    "free": frozenset({"text", "number"}),
    "values": frozenset({"select", "confirm"}),
    "enumerator": frozenset({"select", "text"}),
}


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class Domain:
    """A named type in τ's object model, and how its values are found.

    What a flow argument carries instead of a list of strings. A domain is what lets
    a value set be COMPUTED — the sessions that exist right now, the entries in this
    tree — and what lets an enumerator return a label beside each value, so a person
    chooses between "the turn where the tests failed" rather than between two hex
    strings.

    Exactly one of the three answers applies, and ``__post_init__`` enforces it:
    ``free`` (any value is legal — text, a number, a boolean), ``values`` (a fixed
    set known here), or ``enumerator`` (a capability that computes the set).

    Those three say how a value is FOUND. :attr:`field_kind` says how it is ASKED
    FOR, and the two are not the same question: ``session_id`` and ``model_name``
    both compute their values, but a head can offer every model at once and cannot
    offer every session. Nothing else in the registry records that difference, which
    is why it is stated rather than derived. See docs/TUI-STYLE-GUIDE.md §2.

    Attributes:
        name: The domain's name, as an argument declares it.
        description: What a value of this domain means, for a person reading a form.
        free: Whether any value is legal. A free domain has no enumerator and no
            fixed values, and a head renders it as a plain field.
        values: The fixed legal values, when there are few and they never change.
        enumerator: The name of the :class:`Capability` that computes the legal
            values, when they depend on live state.
        field_kind: Which of
            :data:`~tau_agent_core.extension_types.FORM_FIELD_KINDS` a head renders a
            SINGLE value of this domain as. ``"select"`` asserts the whole legal set
            can be put on screen at once; a domain whose set is unbounded or merely
            large says ``"text"`` and is completed against instead. A head may
            substitute a richer control than the kind names — the TUI answers
            ``session_id`` with its filtered picker — and may never substitute a
            poorer one.
    """

    name: str
    description: str
    free: bool = False
    values: tuple[str, ...] | None = None
    enumerator: str | None = None
    field_kind: str = "text"

    def __post_init__(self) -> None:
        answers = [self.free, self.values is not None, self.enumerator is not None]
        if sum(1 for a in answers if a) != 1:
            raise ValueError(
                f"domain {self.name!r} must say exactly one of free / values / enumerator — "
                f"got free={self.free}, values={self.values!r}, enumerator={self.enumerator!r}. "
                "A domain with none of the three is a field no head can render, and one with "
                "two is two disagreeing answers to the same question."
            )
        if self.field_kind not in _SINGLE_VALUE_FIELD_KINDS:
            raise ValueError(
                f"domain {self.name!r} declares field_kind {self.field_kind!r}, which is not "
                f"one of {sorted(_SINGLE_VALUE_FIELD_KINDS)}. 'multiselect' is not sayable "
                "here: it is what an argument of this domain becomes at cardinality 'many', "
                "not a property of the domain."
            )
        legal = _FIELD_KINDS_BY_ANSWER[
            "free" if self.free else "values" if self.values is not None else "enumerator"
        ]
        if self.field_kind not in legal:
            raise ValueError(
                f"domain {self.name!r} computes its values by "
                f"{'free' if self.free else 'values' if self.values is not None else 'enumerator'}"
                f", so its field_kind must be one of {sorted(legal)}, not {self.field_kind!r}"
            )


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class Argument:
    """One argument a flow needs before it can run.

    A head renders this and sends back a bound value; it never decides what may be
    entered. The pair ``(domain, cardinality)`` replaces the five form field kinds:
    a single-select and a multi-select are the same domain at two cardinalities, and
    a checkbox is the ``boolean`` domain.

    Attributes:
        name: The argument's name, as the bound-argument mapping keys it.
        domain: The name of its :class:`Domain`, a key of :data:`DOMAINS`.
        description: The prompt a head shows for it.
        cardinality: ``"one"`` for a single value, ``"many"`` for a list.
        required: Whether the flow can run without it. An optional argument is
            offered as a step and may be skipped.
        scope: For the ``message_id`` domain, which entries are candidates — one of
            ``ConversationTree.complete_message_id``'s scopes. ``None`` everywhere
            else.
    """

    name: str
    domain: str
    description: str
    cardinality: Cardinality = "one"
    required: bool = True
    scope: MessageIdScope | None = None


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class Capability:
    """One read, or one mutation. The unit every head can address by name.

    Attributes:
        name: The capability's name. Where a capability is already an RPC verb, this
            is that verb's name, because hosts depend on it.
        kind: ``"read"`` returns data and changes nothing; ``"mutation"`` changes
            state. The distinction is the one the RPC layer's E5 rule already
            enforces — a mutation's completion carries a cursor, a read never does.
        description: What it does, in one line.
        on_wire: Whether ``rpc.COMMAND_TABLE`` exposes it today. ``False`` is a
            statement about the wire, not about the capability: it is callable
            in-process either way.
        arguments: What it takes, said once for every caller — the wire schema,
            the flow that ends in it and the head that performs it all read this
            tuple. ``()`` means it takes nothing. ``None`` means its parameters
            cannot be written in this vocabulary and the hand-written wire schema
            is their only statement; ``submit`` is the case, carrying images and a
            correlation object that no :class:`Domain` describes. ``None`` is not a
            default: a capability says which of the three it is.
        returns: What it gives BACK, in the same JSON Schema vocabulary
            ``rpc.commands._assert_supported_schema`` accepts — ``type``,
            ``properties``, ``required``. Every capability declares one and
            ``_check_registry`` refuses a ``None``, so "what comes back" is
            answerable without reading a head's source. Read by
            :func:`tau_agent_core.rpc.schema.result_schema_for`, which is what the
            wire publishes.
    """

    name: str
    kind: CapabilityKind
    description: str
    on_wire: bool = False
    arguments: tuple[Argument, ...] | None = ()
    returns: dict[str, Any] | None = None


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class Flow:
    """An ordered argument list ending in one mutation.

    One mutation, named outright. Two mutations that a head might reach from the same
    gesture are two flows, because they differ in what they take or in what they cost:
    ``navigate`` and ``summarize_and_navigate`` differ in both, and ``enable_extension``
    and ``reload_extension`` take the same argument but not the same risk — one of them
    raises on a file that no longer imports. A gesture that offers a choice between
    flows is a view, and it composes them in head code.

    Attributes:
        name: The flow's name. It is the slash command, the CLI subcommand and the
            palette entry, all three.
        description: What it does, shown in completion and in the palette.
        mutation: The mutation capability this flow ends in.
        arguments: The arguments, in the order a head should ask for them. A
            discriminated flow would need per-branch arguments, which is the second
            reason there is no such thing here.
    """

    name: str
    description: str
    mutation: str
    arguments: tuple[Argument, ...] = ()

    def __post_init__(self) -> None:
        if not self.mutation:
            raise ValueError(
                f"flow {self.name!r} declares no mutation. A gesture that ends in no state "
                "change is a view, and a view is head code — the core declares the flows it "
                "starts and nothing about how it composes them."
            )


DomainEnumerator = Callable[[str, int], Sequence[tuple[str, str]]]
"""What an extension domain's values come from: ``(query, limit) -> [(value, label)]``.

Not handed the session or the runtime, unlike τ's own enumerators, because an
extension already holds its own context and the two would not be the same object.
"""


@dataclass(frozen=True)
class FlowDeclaration:
    """What an extension command TAKES, and where its argument's values come from.

    Reference: docs/EXTENSION-FLOWS.md. Held by
    :class:`~tau_agent_core.extensions.registry.ExtensionRegistry` and assembled into
    a :class:`Vocabulary` by :attr:`~tau_agent_core.agent_session.AgentSession.vocabulary`.

    Attributes:
        flow: The flow. Its ``mutation`` is the command's own name — an extension's
            mutation is its handler, and there is no wire verb behind it.
        domain: The argument's domain, when the extension declared one rather than
            reusing a domain τ already has.
        enumerator: What lists ``domain``'s values. ``None`` for a domain that is
            ``free`` or carries fixed ``values``, neither of which needs live state.
    """

    flow: Flow
    domain: Domain | None = None
    enumerator: DomainEnumerator | None = None


DOMAINS: dict[str, Domain] = {
    "text": Domain("text", "Free text.", free=True, field_kind="text"),
    "number": Domain("number", "A number.", free=True, field_kind="number"),
    "integer": Domain("integer", "A whole number.", free=True, field_kind="number"),
    "boolean": Domain("boolean", "True or false.", values=("true", "false"), field_kind="confirm"),
    "model_name": Domain(
        "model_name",
        "A model configuration name from the running config.",
        enumerator="get_models",
        field_kind="select",
    ),
    # A person may have hundreds of sessions; the whole set is not offerable.
    "session_id": Domain(
        "session_id",
        "A session id, or a unique prefix of one.",
        enumerator="list_sessions",
        field_kind="text",
    ),
    # Any path is legal, including one that does not exist yet, so completions suggest.
    "path": Domain(
        "path",
        "A filesystem path, resolved against the process working directory.",
        enumerator="complete_path",
        field_kind="text",
    ),
    # A long conversation has thousands of entries; the whole set is not offerable.
    "message_id": Domain(
        "message_id",
        "An entry id in the session tree. Scoped by the argument that carries it.",
        enumerator="complete_message_id",
        field_kind="text",
    ),
    "extension_name": Domain(
        "extension_name",
        "A managed extension's path, as the extension registry knows it.",
        enumerator="list_managed_extensions",
        field_kind="select",
    ),
    "message_id_scope": Domain(
        "message_id_scope",
        "Which entries a message_id enumeration considers.",
        values=("in_session", "ancestors_of_cursor", "descendants_of_cursor"),
        field_kind="select",
    ),
}


_SUBMIT_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "accepted": {
            "type": "boolean",
            "description": "Always true here — a rejected submission is an RPCError, not this shape.",
        },
        "submission_id": {
            "type": "string",
            "description": "Echoes the request's submission_id (caller-supplied, or a minted uuid4 for prompt).",
        },
        "rejection_reason": {
            "type": "null",
            "description": "Always null on this success shape; a real rejection is SUBMISSION_REJECTED instead.",
        },
        "command": {
            "type": "object",
            "description": (
                "Present ONLY when this acceptance is also the submission's only "
                "completion: a core (extension-registered) slash command resolved "
                "synchronously with no turn started, so there is no later agent_end "
                "to carry it. {name, output} — `name` is the command that ran, which "
                "an input hook may have rewritten. Only an extension-registered "
                "command reaches this shape; a built-in resolves to a step, a ready "
                "flow or a view, each of which this wire refuses with "
                "COMMAND_NOT_SUPPORTED. Absent for an ordinary turn — poll "
                "get_messages / watch for agent_end instead."
            ),
        },
        "view": {
            "type": "object",
            "description": (
                "Present ONLY when this submission resolved to a VIEW command — "
                "/tree or /extensions. {name, state, unavailable_because}: `name` is "
                "the view asked for, `state` is what a head draws it from, and "
                "`unavailable_because` is a sentence saying why no state rides along. "
                "Exactly one of the last two is non-null, never both and never "
                "neither. τ projects no view state yet (docs/VSCODE-HEAD.md §6), so "
                "today every one of these carries the reason; a host with its own "
                "browser opens it from its own reads, and a host without one prints "
                "the reason. This is a SUCCESS response, not the "
                "COMMAND_NOT_SUPPORTED a view used to raise: the wire says what was "
                "asked for and what it can supply, and the payload lands in `state` "
                "when there is one, with no shape change for a host."
            ),
        },
        "attachments": {
            "type": "object",
            "description": (
                "Present exactly when the request set expand_attachments: true — "
                "absent is 'expansion did not run', which is a different "
                "statement from 'expansion found nothing'. "
                "{expanded: int, images: int, unresolved: [str], failures: [str]}. "
                "`unresolved` names the @words that matched no file and were "
                "therefore left in the text as prose. `failures` names the ones "
                "that resolved but could not be sent, each with the reason; the "
                'model is told the same thing through a <reference error="…"> '
                "block, so neither side is left believing an attachment landed "
                "when it did not. A host that shows neither list turns a visible "
                "failure back into a silent one."
            ),
        },
    },
    "required": ["accepted", "submission_id", "rejection_reason"],
}

_ABORT_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["aborted"], "description": "Always 'aborted'."},
        "compaction_id": {
            "type": ["string", "null"],
            "description": (
                "The compaction this abort's signal was delivered to, or null "
                "when none was in flight (finding 5, Tier B review). Present "
                "so a host knows to expect a compaction_end carrying "
                "cancelled: true for that id. Whether the compaction actually "
                "stopped is reported THERE and not here — same signal-vs-"
                "outcome split that keeps `cursor` off this response."
            ),
        },
    },
    "required": ["status", "compaction_id"],
}

_GET_STATE_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "AgentSession.state.session_id."},
        "status": {
            "type": "string",
            "enum": ["idle", "running"],
            "description": "AgentSession.state.status.",
        },
        "is_streaming": {"type": "boolean", "description": "AgentSession.is_streaming."},
        "model": {
            "type": "object",
            "description": "AgentSession.get_model(): {id, provider, context_window}.",
        },
        "usage": {
            "type": ["object", "null"],
            "description": "AgentSession.get_usage() — null before the first completion.",
        },
        "message_count": {"type": "integer", "description": "len(AgentSession.messages)."},
        "cursor": {
            "type": ["string", "null"],
            "description": "session_log.cursor (F3: no host may cache 'the tip').",
        },
        "addressable": {
            "type": "boolean",
            "description": (
                "Whether the CURRENT session is persisted: true if list_sessions "
                "returns it and switch_session can reach it later. The same "
                "predicate new_session/fork/switch_session publish on their "
                "session tuple, asked about the session this connection is on "
                "right now. False means the appending verbs (set_model, "
                "set_session_name, compact — D-7) will refuse with -32004 "
                "SESSION_NOT_PERSISTED, and nothing this connection does is "
                "written to the store. Reachable without a respawn: "
                'new_session {"persist": true} moves onto a persisted session.'
            ),
        },
    },
    "required": [
        "session_id",
        "status",
        "is_streaming",
        "model",
        "usage",
        "message_count",
        "cursor",
        "addressable",
    ],
}

_GET_MESSAGES_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "description": "AgentSession.messages — the terminal, flat message array (E2's pull side).",
            "items": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": (
                            "'system', 'user', 'assistant' or 'toolResult'. An "
                            "extension-injected node carries 'custom' and the loop remaps "
                            "it to 'user' before a provider sees it."
                        ),
                    },
                    "content": {
                        "type": ["string", "array"],
                        "description": (
                            "A plain string on a system message; elsewhere a list of "
                            "content blocks, each carrying its own `type` — text, image, "
                            "thinking or toolCall."
                        ),
                    },
                    "timestamp": {
                        "type": ["integer", "null"],
                        "description": (
                            "Epoch MILLISECONDS at the moment the message happened: a "
                            "user message's send, a toolResult's collection, an "
                            "assistant message's end (or its cancellation, for a "
                            "partial). null means no clock applies — a synthetic "
                            "message an extension built, or a system message. Never 0; "
                            "a session written before τ fixed this carries 0 on disk "
                            "and every store maps it to null on load, so a host never "
                            "sees one (docs/MESSAGE-TIMESTAMPS.md). Consecutive "
                            "timestamps are what let a host compute per-call latency "
                            "and whether a prompt cache entry had expired, and they "
                            "read identically live and after a reload."
                        ),
                    },
                    "usage": {
                        "type": "object",
                        "description": (
                            "Present on assistant messages: the ONE completion that "
                            "produced this message, as {input_tokens, output_tokens, "
                            "cache_read_tokens, cache_write_tokens, cache_reported, "
                            "total_tokens, extra}. Per-completion, never cumulative — "
                            "each prompt already contains every earlier one, so summing "
                            "total_tokens over a conversation counts turn 1 once per "
                            "turn. cache_reported false means the server accounts for no "
                            "prompt cache, so its cache_read_tokens of 0 is silence and "
                            "not a miss; cache_read_tokens at 0 WITH cache_reported true "
                            "is the signal described in docs/PROMPT-CACHING.md §7."
                        ),
                    },
                },
                "required": ["role", "content"],
            },
        },
    },
    "required": ["messages"],
}

_GET_COMMANDS_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "commands": {
            "type": "array",
            "description": (
                "Every slash command that resolves right now, built-ins first. A `name` "
                "has no leading '/' — submit it as ordinary text with "
                "expand_commands=true, not as an RPC method."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The command word, with no leading '/'.",
                    },
                    "description": {
                        "type": "string",
                        "description": "The one line a completion list or a palette shows.",
                    },
                    "origin": {
                        "type": "string",
                        "enum": ["builtin", "extension"],
                        "description": (
                            "Where the NAME came from: 'builtin' is τ's own vocabulary, "
                            "'extension' is one a loaded extension registered. Built-ins "
                            "resolve first, so an extension cannot shadow one. It does "
                            "NOT say who runs the command — that is which arm the "
                            "dispatch returns."
                        ),
                    },
                    "flow": {
                        "type": "boolean",
                        "description": (
                            "Whether this command DECLARES what it takes. True means "
                            "`next_step` will step it and `enumerate_domain` will list "
                            "its argument's values, so a host can build a form or a "
                            "completion list for it; false means the command takes one "
                            "opaque line and there is nothing to ask about. Every "
                            "built-in flow is true and the two view commands are false; "
                            "an extension command is true only if it used "
                            "`register_flow` (docs/EXTENSION-FLOWS.md)."
                        ),
                    },
                },
                "required": ["name", "description", "origin", "flow"],
            },
        },
    },
    "required": ["commands"],
}

_GET_TOOLS_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tools": {
            "type": "array",
            "description": "This session's bound AgentTool set, in binding order.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The tool's name, as a tool call addresses it.",
                    },
                    "description": {
                        "type": "string",
                        "description": "What the model is told the tool does.",
                    },
                    "parameters": {
                        "type": "object",
                        "description": (
                            "The tool's own JSON Schema, passed to the provider "
                            "unchanged — this table's supported-keyword rule does not "
                            "govern it."
                        ),
                    },
                },
                "required": ["name", "description", "parameters"],
            },
        },
    },
    "required": ["tools"],
}

_SESSION_LIFECYCLE_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cancelled": {
            "type": "boolean",
            "description": (
                "True if a session_before_switch extension hook vetoed (H2). When "
                "true, `session`/`cursor` are absent — nothing was touched. An "
                "in-flight turn that did not stop in time is a DIFFERENT outcome "
                "and never reaches this shape — see TURN_STILL_RUNNING."
            ),
        },
        "session": {
            "type": "object",
            "description": (
                "F2's session tuple: {store, session_id, lane, cursor, "
                "addressable}. `lane` is always 'primary' in v1 (lanes are "
                "Tier C, not this phase). Present only when cancelled is "
                "false. `addressable` (finding 7 of the Tier B review) is "
                "the field that says whether `session_id` is a value "
                "ANOTHER call can use: true means list_sessions returns this "
                "id and switch_session resolves it; false means this session "
                'exists in memory only — it is `new_session {"persist": '
                "false}`'s product, switch_session answers -32602 for it, "
                "list_sessions never shows it, and the verbs D-7 rule 1 "
                "governs (set_model/set_session_name/compact) refuse on it. "
                "`store` names the store THIS CONNECTION's catalog is on, "
                "which is not a claim that this session is in it: when "
                "addressable is false, nothing was written to that store."
            ),
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "The resulting session_log.cursor, duplicated at top level "
                "(E5/F3 — every mutating response returns the resulting cursor). "
                "Present only when cancelled is false."
            ),
        },
    },
    "required": ["cancelled"],
}

_COMPACT_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "accepted": {
            "type": "boolean",
            "description": (
                "Always true — the compaction was admitted and is now running "
                "in the background. A refusal is an error response instead "
                "(TURN_STILL_RUNNING), never accepted: false."
            ),
        },
        "compaction_id": {
            "type": "string",
            "description": (
                "Correlates this acknowledgement to the compaction_end "
                "notification that reports the outcome. Server-generated; a "
                "host does not supply it."
            ),
        },
    },
    "required": ["accepted", "compaction_id"],
}

_COMPLETE_PATH_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "completion": {
            "type": ["object", "null"],
            "description": (
                "`null` when the cursor is not inside an @reference at all — "
                "the host shows no popup. Otherwise "
                "{start, end, token, matches, total}: `start`/`end` are the "
                "character span of the whole @word, so a host replaces that "
                "span rather than guessing where the token began; `matches` is "
                "a list of {name, detail, is_dir}, `name` being the text that "
                "goes AFTER the @ (directories end in '/'); `total` is how many "
                "entries matched before the list was bounded, so a host can say "
                "'12 of 340' instead of implying it showed everything. "
                "An EMPTY `matches` with a non-null completion is the "
                "'this names no file' warning, not an absence of information."
            ),
        },
    },
    "required": ["completion"],
}

_GET_LAST_ASSISTANT_TEXT_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": ["string", "null"],
            "description": (
                "The last assistant message's concatenated 'text' content "
                "blocks, trimmed. null if no qualifying assistant message "
                "exists YET, or if one exists but it has no text (e.g. a "
                "pure tool-call turn) — the two cases are DELIBERATELY "
                "indistinguishable on the wire, matching pi's own "
                "`getLastAssistantText(): string | undefined` (pi "
                "agent-session.ts:3092) and its RPC verb "
                '(rpc-mode.ts:609-612, docs/rpc.md: \'Returns {"text": '
                "null} if no assistant messages exist' — silent on the "
                "second null-producing case, because on the wire there is "
                "only one representable 'nothing' and pi does not either)."
            ),
        },
    },
    "required": ["text"],
}

_GET_MODELS_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "models": {
            "type": "array",
            "description": (
                "Every config model NAME this child can switch to, sorted, as "
                "[{name, model}]: `name` is the exact string set_model's "
                "`name` param takes, and `model` is the SAME projection "
                "get_state publishes for the active model — {id, provider, "
                "context_window} — obtained by resolving `name` through the "
                "session's bound model resolver, i.e. by asking the one "
                "component set_model itself would ask. Empty only when the "
                "child's config declares no models; a resolver that cannot "
                "be enumerated is an INTERNAL_ERROR, never an empty list."
            ),
        },
    },
    "required": ["models"],
}

_GET_SESSION_STATS_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "context": {
            "type": "object",
            "description": (
                "estimate_context_tokens(session.messages) (compaction.py) "
                "projected as {tokens, usage_tokens, trailing_tokens, "
                "last_usage_index}: tokens is the total estimate the "
                "compaction threshold is checked against; usage_tokens is "
                "the anchored provider-reported count up to the last "
                "assistant Usage, trailing_tokens the heuristic estimate "
                "for messages after it, last_usage_index that message's "
                "index (null if no assistant Usage exists yet, in which "
                "case tokens==trailing_tokens and the whole list was "
                "heuristically estimated)."
            ),
        },
        "context_window": {
            "type": "integer",
            "description": "The active model's context_window (get_model()).",
        },
        "context_headroom": {
            "type": "integer",
            "description": (
                "context_window - context.tokens. Can be negative: an "
                "honest over-budget number, never clamped to zero."
            ),
        },
        "compaction_settings": {
            "type": "object",
            "description": (
                "The session's EFFECTIVE CompactionSettings — {enabled, "
                "reserve_tokens, keep_recent_tokens}, read off "
                "AgentSession.compaction_settings, which hands back a COPY so "
                "a reader cannot retune a turn already in flight. An RPC "
                "session is CONSTRUCTED with enabled=False (backends.py:885) "
                "— that is how a host discovers auto-compaction is off "
                "(§1.1) — and set_auto_compaction (D-4, shipped in this same "
                "tier) is the one thing that changes it, so this reports the "
                "session's LIVE effective setting at call time, never a "
                "constant."
            ),
        },
        "last_compaction": {
            "type": ["object", "null"],
            "description": (
                "{id, timestamp, summary, first_kept_id, tokens_before} for "
                "the most recent type=='compaction' entry in "
                "session_log.entries(), or null if this session has never "
                "compacted — an honest absence, never a fabricated entry."
            ),
        },
        "usage": {
            "type": ["object", "null"],
            "description": "AgentSession.get_usage() — null before the first completion.",
        },
    },
    "required": [
        "context",
        "context_window",
        "context_headroom",
        "compaction_settings",
        "last_compaction",
        "usage",
    ],
}

_LIST_SESSIONS_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sessions": {
            "type": "array",
            "description": (
                "Every session `switch_session` can resolve from this "
                "connection, newest-modified first, as [{session_id, ref, "
                "name, title, message_count, created, modified, parent, "
                "error}]. `session_id` is the exact string switch_session's "
                "`session_id` param takes. `ref` is the STORE's own handle "
                "for that session (SessionCatalog's listing ref — the file "
                "store's absolute .jsonl path, a JMFTS catalog's document "
                "id): it is what names WHICH universe this listing is, since "
                "--mode rpc's default session base is <tmp>/.tau-<uid>/sessions "
                "and the TUI's is ~/.tau/sessions (D-6/H1b). `name` is what "
                "set_session_name set, null if never named; `title` is the "
                "picker's bounded display label (SessionInfo.display_title) "
                "and is the only place message TEXT appears here — "
                "first_message/last_message are deliberately not published, "
                "being unbounded (a 40kB prompt would ride every listing). "
                "`created`/`modified` are ISO-8601; `parent` is the id this "
                "session was forked from, else null; `error` is why this "
                "row's entries could not be read, else null — an unreadable "
                "session stays LISTED and says so (SessionInfo.error) rather "
                "than vanishing from a host's view."
            ),
        },
        "scope": {
            "type": "object",
            "description": (
                "What universe the list above is, as {store, cwd}: `store` "
                "is the same backend label the session tuple of new_session/"
                "fork/switch_session carries, and `cwd` is the working "
                "directory the listing is scoped to — this process's own, "
                "the identical scope switch_session resolves against "
                "(SessionCatalog.resolve_ref is built on list(cwd)), so the "
                "ids here are exactly the ids that verb accepts. Sessions in "
                "OTHER directories are not listed because switch_session "
                "could not reach them either. The BASE DIRECTORY is not a "
                "field: no SessionCatalog declares one (the file store's is "
                "private and `None` means the default), and each entry's "
                "`ref` names it exactly — stated as a limit, not hidden: an "
                "EMPTY list therefore names no location at all."
            ),
        },
    },
    "required": ["sessions", "scope"],
}

_SET_AUTO_COMPACTION_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "enabled": {
            "type": "boolean",
            "description": (
                "The effective state after this call (D-4: 'a plain, "
                "idempotent setter ... returns the effective state') — what "
                "AgentSession.set_auto_compaction returns, read back off the "
                "settings rather than echoed from the request."
            ),
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "session_log.cursor after this call (E5, rule 1 of 'E5 in "
                "Tier B' above). ALWAYS the unchanged tip: this verb mutates "
                "an in-memory CompactionSettings and appends no log entry, so "
                "there is nothing here that could move it. Returned rather "
                "than omitted because absence is not a signal (rule 3) — a "
                "host reads the same field from every mutator and never has "
                "to infer the tip from a missing key (F3)."
            ),
        },
    },
    "required": ["enabled", "cursor"],
}

_SET_MODEL_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "model": {
            "type": "object",
            "description": "AgentSession.get_model() after the switch: {id, provider, context_window}.",
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "session_log.cursor immediately after the model_change entry "
                "this call appended (E5) — that entry's own id, since the "
                "append is the last write this handler makes."
            ),
        },
    },
    "required": ["model", "cursor"],
}

_SET_SESSION_NAME_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The name just persisted (echoes params.name).",
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "The resulting session_log.cursor (E5/F3 — every mutating "
                "response returns the resulting cursor)."
            ),
        },
    },
    "required": ["name", "cursor"],
}

_GET_SESSION_NAME_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": ["string", "null"],
            "description": (
                "The session's durable display name, or null if never set "
                "(extension_types.read_session_name)."
            ),
        },
    },
    "required": ["name"],
}

_COMPLETE_MESSAGE_ID_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "description": (
                "The candidates in tree order (root-most first), as [{entry_id, "
                "preview}]: `entry_id` is the value every message_id argument "
                "takes, and `preview` is the entry's first line — the row the tree "
                "browser draws. Bounded by `limit`."
            ),
        },
        "total": {
            "type": "integer",
            "description": (
                "How many entries matched BEFORE `limit` was applied, so a host is "
                "told it is seeing a prefix rather than shown one silently (G3, the "
                "rule complete_path already follows)."
            ),
        },
    },
    "required": ["matches", "total"],
}

_GET_TREE_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "description": (
                "Every entry in the log, in the order a browser draws them — "
                "preorder over the parent/child tree, roots in load order, children "
                "oldest first. FLAT, with `parent_id` carrying the shape: a nested "
                "projection of a long linear conversation is one nesting level per "
                "message, which is a serializer's recursion limit rather than a tree "
                "anyone wanted. Nothing is filtered out — which rows a browser "
                "declines to draw (a `navigate` with one child) is the reader's rule, "
                "not the log's."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "description": (
                            "The entry's id — the value every `message_id` argument "
                            "takes (navigate, elide_span, commit_branch, "
                            "paste_subtree, summarize_and_navigate)."
                        ),
                    },
                    "parent_id": {
                        "type": ["string", "null"],
                        "description": "The entry this one hangs from; null for a root.",
                    },
                    "kind": {
                        "type": "string",
                        "description": (
                            "The entry's type: 'message', 'customMessage', "
                            "'compaction', 'elide', 'branch_summary', 'navigate', "
                            "'customEntry', 'model_change'. Not a closed enum — a kind "
                            "added later reaches a host as a row it can draw and does "
                            "not recognise, rather than as a gap."
                        ),
                    },
                    "role": {
                        "type": ["string", "null"],
                        "description": (
                            "'user', 'assistant', 'toolResult' or 'system' on a message "
                            "entry; null on every bookkeeping kind."
                        ),
                    },
                    "preview": {
                        "type": "string",
                        "description": (
                            "The entry's first line, uncut — a host elides it to its "
                            "own width. Empty for an entry carrying no text."
                        ),
                    },
                    "is_cursor": {
                        "type": "boolean",
                        "description": (
                            "Whether this entry is the session's cursor — where the "
                            "next submission lands. Exactly one node carries true, or "
                            "none on a session whose cursor names no entry."
                        ),
                    },
                    "timestamp": {
                        "type": ["integer", "null"],
                        "description": (
                            "Epoch milliseconds, or null when no clock applies "
                            "(docs/MESSAGE-TIMESTAMPS.md). This is the key children are "
                            "sorted by, so re-sorting on it reproduces `nodes`."
                        ),
                    },
                    "first_kept_id": {
                        "type": ["string", "null"],
                        "description": (
                            "On a splice anchor ('compaction' or 'elide'), the oldest "
                            "entry the fold keeps; null on every other kind. This is "
                            "the fold's whole boundary: the entries a host paints as "
                            "folded are the ones on the cursor's ancestor chain that "
                            "sit before this id, with a system message carried across "
                            "(docs/SYSTEM-PROMPT-IN-THE-FOLD.md). Sent because a host "
                            "holding only the shape would have to guess at it."
                        ),
                    },
                    "from_id": {
                        "type": ["string", "null"],
                        "description": (
                            "On a 'branch_summary', the head of the branch it "
                            "summarizes — the pair a browser draws as a summary and the "
                            "line it is about. Display metadata, never a splice "
                            "boundary. Null on every other kind."
                        ),
                    },
                    "is_system": {
                        "type": "boolean",
                        "description": (
                            "Whether this entry is the system prompt. A fold carries it "
                            "across rather than dropping it, so a host computing the "
                            "folded span excludes it."
                        ),
                    },
                    "tool_call_ids": {
                        "type": "array",
                        "description": (
                            "The toolCall block ids an assistant message declares; "
                            "empty on every other entry. With `tool_call_id` this is "
                            "the whole of the pairing rule a mark expands over: to "
                            "every provider a call and its result are one unit, and a "
                            "branch carrying one without the other is a prefix the API "
                            "rejects (docs/TREE-EDITOR-MANUAL.md §6)."
                        ),
                        "items": {"type": "string"},
                    },
                    "tool_call_id": {
                        "type": ["string", "null"],
                        "description": "The call a 'toolResult' answers; null elsewhere.",
                    },
                    "copyable": {
                        "type": "boolean",
                        "description": (
                            "Whether paste_subtree will take this entry as a source "
                            "root — conversation_tree.COPYABLE_KINDS, reported per node "
                            "so a host greys the illegal source rather than holding a "
                            "second copy of the tuple."
                        ),
                    },
                    "estimated_tokens": {
                        "type": "integer",
                        "description": (
                            "compaction.estimate_tokens over this entry's message, 0 "
                            "for an entry carrying none. An ESTIMATE — a "
                            "4-chars-per-token heuristic — and the only token figure "
                            "available for an arbitrary set of entries, because the one "
                            "measured figure in a session is usage.input_tokens on a "
                            "finished assistant message and that measures one request. "
                            "A host totalling these says 'estimate' beside the number, "
                            "as the TUI's browser does."
                        ),
                    },
                },
                "required": [
                    "entry_id",
                    "parent_id",
                    "kind",
                    "role",
                    "preview",
                    "is_cursor",
                    "timestamp",
                    "first_kept_id",
                    "from_id",
                    "is_system",
                    "tool_call_ids",
                    "tool_call_id",
                    "copyable",
                    "estimated_tokens",
                ],
            },
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "session_log.cursor at the moment of the read, duplicated out of "
                "`nodes` so a host finds it without scanning. Null on a session whose "
                "cursor names no entry, which is also the one case in which no node "
                "carries is_cursor: true."
            ),
        },
        "count": {
            "type": "integer",
            "description": (
                "len(nodes). Present so a host can check it read a whole tree rather "
                "than a truncated one: this read is UNBOUNDED by design — the shape IS "
                "the answer and a bounded shape is a different tree — which is why it "
                "is a pull and is never pushed (G3)."
            ),
        },
    },
    "required": ["nodes", "cursor", "count"],
}

_GET_PENDING_REQUEST_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "request": {
            "type": ["object", "null"],
            "description": (
                "The extension request AT THE CURSOR, or null when there is none. "
                "The cursor only, never an ancestry walk: the thing a user is "
                "looking at and the thing that refused their submission are one "
                "entry. {entry_id, extension, extension_name, sentence, label, "
                "lock, ask, release} — `label` is τ's own framing of the four "
                "states over `lock` and `ask`, `sentence` is the extension's own "
                "line, `ask` is a validated `ui.form` spec ({title, fields, "
                "actions}) or null, and `release` names a command that clears the "
                "lock (advisory: commands are exempt from a lock by placement, "
                "not by name). A host renders all four states; three of them draw "
                "something and the fourth is this verb answering null."
            ),
        },
    },
    "required": ["request"],
}

_ANSWER_REQUEST_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handled": {
            "type": "boolean",
            "description": (
                "Whether the extension that raised the request was loaded and ran "
                "its action. FALSE still means the response was appended and the "
                "lock released — a lock whose owner cannot answer must not become "
                "a session nobody can continue — so a host reports it as a warning "
                "and carries on, rather than as a failure to retry."
            ),
        },
        "output": {
            "type": ["string", "null"],
            "description": "What the dispatched command produced, or null.",
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "session_log.cursor after the response was appended (E5 rule 1). "
                "The append is what RELEASES the lock — appending moves the cursor "
                "and a lock is read at the cursor — so this value is the evidence "
                "the session is answerable again."
            ),
        },
    },
    "required": ["handled", "output", "cursor"],
}

_GET_ENTRY_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entry": {
            "type": "object",
            "description": (
                "The raw session-log entry, as stored: camelCase `parentId` / "
                "`firstKeptId` / `fromId`, a `type`, and whatever payload that type "
                "carries — a `message` for the message kinds, a `summary` for a "
                "compaction or a branch_summary. Handed over whole rather than "
                "projected, because the caller is a detail pane rendering ONE node and "
                "a projection would be a second message shape to keep in step with "
                "get_messages'. One node per call: get_tree carries a one-line preview "
                "per row precisely so a browser does not pull bodies it is not showing."
            ),
        },
    },
    "required": ["entry"],
}

_LIST_MANAGED_EXTENSIONS_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "extensions": {
            "type": "array",
            "description": (
                "Every file extension under management, in load order, as "
                "[{path, enabled}]. `path` is the exact string every "
                "extension_name argument takes (enable_extension, "
                "disable_extension, reload_extension); `enabled` is false exactly "
                "when the extension is loaded but its bucket has been removed from "
                "the runner, so its hooks, tools and slash commands are not "
                "offered."
            ),
        },
    },
    "required": ["extensions"],
}

_GET_EXTENSION_STATE_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "extensions": {
            "type": "array",
            "description": (
                "Every loaded extension and what it registered, as [{name, path, "
                "tools, commands, shortcuts, hooks, content_hash, subjects}] — "
                "sdk.summarize_extensions of the live registry, which is the same "
                "projection the TUI's /extensions listing draws. Read LIVE, not "
                "from the load-time snapshot, so a reload_extension is reflected "
                "here."
            ),
        },
        "errors": {
            "type": "array",
            "description": (
                "Every discovered file that FAILED to load, as [{path, error}]. "
                "Kept from the last load_extensions call, because a failed import "
                "leaves nothing to recompute from. This is the half that makes "
                "this a read of its own rather than list_managed_extensions with "
                "more fields: a file that cannot import can never be a legal "
                "extension_name, and is exactly what a listing must show."
            ),
        },
    },
    "required": ["extensions", "errors"],
}

_GET_EXTENSION_CONFIG_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "The managed path the token resolved to, not the token sent.",
        },
        "schema": {
            "type": "object",
            "description": (
                "The extension's CONFIG_SCHEMA, normalized at load into {title, "
                "fields} — the same spec shape ui.form takes, so a head that can "
                "render a form can render a settings screen with no new widget. "
                "null for an extension that declares none, which is the answer that "
                "tells a head to offer no screen rather than an empty one."
            ),
        },
        "values": {
            "type": "object",
            "description": (
                "The live slice api.config returns for this extension, keyed by "
                "file stem: config.json's extensions.<stem> with --ext-config "
                "overrides applied, plus any set_extension_config since. {} for an "
                "unconfigured extension — never the schema's defaults, which the "
                "extension itself supplies."
            ),
        },
    },
    "required": ["path", "schema", "values"],
}

_TREE_CONTEXT_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "description": (
                "ConversationTree.context_for(cursor) after the mutation — the "
                "same flat message array get_messages returns, for the path this "
                "call just produced. Returned rather than left for a follow-up "
                "get_messages because the mutation's whole product is a different "
                "context, and a host that had to fetch it separately could render "
                "the old one in between."
            ),
        },
        "cursor": {
            "type": ["string", "null"],
            "description": "session_log.cursor after the mutation (E5 rule 1).",
        },
    },
    "required": ["messages", "cursor"],
}

_PASTE_SUBTREE_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "minted_ids": {
            "type": "array",
            "description": (
                "The ids minted, in the order they were appended. The first is the "
                "copy of `source_id` itself. Ids rather than messages because a "
                "paste edits the TREE and never moves the leaf: the current "
                "context is unchanged, so there is nothing to re-render until "
                "someone navigates onto the copy."
            ),
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "session_log.cursor after the paste — E5 rule 1, and here it is "
                "the UNCHANGED tip, present because absence is never a signal "
                "(rule 3), not because anything moved."
            ),
        },
    },
    "required": ["minted_ids", "cursor"],
}

_EXTENSION_ACTION_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["enable", "disable", "reload", "configure"],
            "description": "Which action ran — echoes the verb.",
        },
        "path": {
            "type": "string",
            "description": (
                "The managed path the action resolved to. NOT always what was "
                "sent: `path` accepts a file stem as well as a full path, and this "
                "is the full path it matched. On a failed resolution it is the "
                "unresolved string, so a host can quote back what it asked for."
            ),
        },
        "ok": {
            "type": "boolean",
            "description": (
                "Whether the action changed anything. false is a reportable no-op, "
                "never an error: an unknown target, an already-enabled extension, "
                "an already-disabled one. A hard failure — a file that no longer "
                "imports, which only reload can hit — RAISES instead and reaches "
                "the host as INTERNAL_ERROR, with the extension left torn down."
            ),
        },
        "message": {
            "type": "string",
            "description": "The human-readable line, the same one the TUI listing shows.",
        },
        "cursor": {
            "type": ["string", "null"],
            "description": (
                "session_log.cursor — E5 rule 1 on a mutator whose whole product "
                "is runtime state. It is the live tip reported as a READ, not a "
                "claim that this call wrote anything; the same reading "
                "set_auto_compaction's cursor already has."
            ),
        },
    },
    "required": ["action", "path", "ok", "message", "cursor"],
}


CAPABILITIES: dict[str, Capability] = {
    capability.name: capability
    for capability in (
        Capability(
            "get_state",
            "read",
            "What is running: session id, status, whether a turn is streaming, the "
            "model, the last usage, the message count and the cursor.",
            on_wire=True,
            returns=_GET_STATE_RETURNS,
        ),
        Capability(
            "get_messages",
            "read",
            "The active path's messages, as the model would be sent them.",
            on_wire=True,
            returns=_GET_MESSAGES_RETURNS,
        ),
        Capability(
            "get_last_assistant_text",
            "read",
            "The text of the most recent assistant message.",
            on_wire=True,
            returns=_GET_LAST_ASSISTANT_TEXT_RETURNS,
        ),
        Capability(
            "get_session_stats",
            "read",
            "Token accounting for this session, and the compaction settings in force.",
            on_wire=True,
            returns=_GET_SESSION_STATS_RETURNS,
        ),
        Capability(
            "get_session_name",
            "read",
            "This session's human-readable name, or none if it was never named.",
            on_wire=True,
            returns=_GET_SESSION_NAME_RETURNS,
        ),
        Capability(
            "get_tools",
            "read",
            "The tools bound to this session, each with its parameter schema.",
            on_wire=True,
            returns=_GET_TOOLS_RETURNS,
        ),
        Capability(
            "get_commands",
            "read",
            "Every slash command that resolves: the built-in vocabulary plus whatever "
            "the loaded extensions registered. Enumerated at call time, because the "
            "second half of that list changes when an extension loads.",
            on_wire=True,
            returns=_GET_COMMANDS_RETURNS,
        ),
        Capability(
            "get_models",
            "read",
            "The configured models.",
            on_wire=True,
            returns=_GET_MODELS_RETURNS,
        ),
        Capability(
            "list_sessions",
            "read",
            "The sessions on disk.",
            on_wire=True,
            returns=_LIST_SESSIONS_RETURNS,
        ),
        Capability(
            "complete_path",
            "read",
            "Paths matching a typed prefix.",
            on_wire=True,
            arguments=(
                Argument("text", "text", "The line being completed."),
                Argument("cursor", "integer", "The caret's offset into that line."),
            ),
            returns=_COMPLETE_PATH_RETURNS,
        ),
        Capability(
            "complete_message_id",
            "read",
            "Entry ids in a scope, with the text of each.",
            on_wire=True,
            arguments=(
                Argument(
                    "scope",
                    "message_id_scope",
                    "Which entries are candidates.",
                    required=False,
                ),
                Argument(
                    "cursor",
                    "message_id",
                    "The entry a scoped enumeration is relative to.",
                    required=False,
                ),
                Argument("query", "text", "Filter text.", required=False),
                Argument("limit", "integer", "How many to return at most.", required=False),
            ),
            returns=_COMPLETE_MESSAGE_ID_RETURNS,
        ),
        Capability(
            "get_tree",
            "read",
            "The whole conversation tree: every entry, its parent, and the facts a "
            "browser colours a row with. The read the tree-editing mutations were "
            "uncallable without — a head could navigate, elide, branch and paste, "
            "and had no way to show anyone what it would be doing it to.",
            on_wire=True,
            returns=_GET_TREE_RETURNS,
        ),
        Capability(
            "get_entry",
            "read",
            "One entry's full body, by id. What a detail pane draws beside the tree, "
            "and the reason get_tree carries previews rather than messages.",
            on_wire=True,
            arguments=(Argument("entry_id", "message_id", "Which entry to read."),),
            returns=_GET_ENTRY_RETURNS,
        ),
        Capability(
            "get_pending_request",
            "read",
            "The extension request at the cursor — a lock, an ask, or both — or "
            "nothing. The read a head polls at every cursor move to decide what to "
            "draw, and the one that tells an out-of-process head WHY submit refused.",
            on_wire=True,
            returns=_GET_PENDING_REQUEST_RETURNS,
        ),
        Capability(
            "answer_request",
            "mutation",
            "Answer an extension's ask: append the response, which releases the "
            "lock, then dispatch the pressed action. In that order, so the handler "
            "runs on a session that is already unlocked and may submit a turn of "
            "its own.",
            on_wire=True,
            arguments=None,
            returns=_ANSWER_REQUEST_RETURNS,
        ),
        Capability(
            "list_managed_extensions",
            "read",
            "Every managed file extension, and whether it is enabled.",
            on_wire=True,
            returns=_LIST_MANAGED_EXTENSIONS_RETURNS,
        ),
        Capability(
            "get_extension_state",
            "read",
            "Every managed extension with what it registered, and every file that "
            "failed to load. Not a domain: a file that failed to import can never "
            "be a legal argument, and is exactly what the listing must show.",
            on_wire=True,
            returns=_GET_EXTENSION_STATE_RETURNS,
        ),
        Capability(
            "get_extension_config",
            "read",
            "One extension's declared config schema and its current values — the "
            "read a settings screen is built from.",
            on_wire=True,
            arguments=(Argument("path", "extension_name", "Which extension."),),
            returns=_GET_EXTENSION_CONFIG_RETURNS,
        ),
        Capability(
            "set_extension_config",
            "mutation",
            "Replace an extension's config slice and reload it so the values take "
            "effect. Values are checked against the declared schema; an undeclared "
            "key raises. Applies for this session only — the core does not own "
            "~/.tau/config.json, so persisting is head-local.",
            on_wire=True,
            arguments=None,
            returns=_EXTENSION_ACTION_RETURNS,
        ),
        Capability(
            "submit",
            "mutation",
            "Append a message to the tree and run a turn on it. Idempotently puts the "
            "session in the running state — a submission arriving mid-turn is queued "
            "or steered, never a second loop.",
            on_wire=True,
            arguments=None,
            returns=_SUBMIT_RETURNS,
        ),
        Capability(
            "abort",
            "mutation",
            "Stop the in-flight turn, and any compaction it started. A running session "
            "becomes a stopped one, which is why this is a mutation and not a signal "
            "with a category of its own.",
            on_wire=True,
            returns=_ABORT_RETURNS,
        ),
        Capability(
            "compact",
            "mutation",
            "Summarize the older messages into a checkpoint entry.",
            on_wire=True,
            arguments=(
                Argument(
                    "custom_instructions",
                    "text",
                    "Extra focus for the generated summary.",
                    required=False,
                ),
            ),
            returns=_COMPACT_RETURNS,
        ),
        Capability(
            "set_auto_compaction",
            "mutation",
            "Whether this session compacts itself when the context fills.",
            on_wire=True,
            arguments=(Argument("enabled", "boolean", "The state to put it in."),),
            returns=_SET_AUTO_COMPACTION_RETURNS,
        ),
        Capability(
            "new_session",
            "mutation",
            "Start a fresh session and make it the live one.",
            on_wire=True,
            arguments=(
                Argument(
                    "persist",
                    "boolean",
                    "Whether the new session is written to disk.",
                    required=False,
                ),
            ),
            returns=_SESSION_LIFECYCLE_RETURNS,
        ),
        Capability(
            "switch_session",
            "mutation",
            "Make another session the live one.",
            on_wire=True,
            arguments=(Argument("session_id", "session_id", "Which session to resume."),),
            returns=_SESSION_LIFECYCLE_RETURNS,
        ),
        Capability(
            "set_model",
            "mutation",
            "Switch the active model by config name, effective next turn.",
            on_wire=True,
            arguments=(Argument("name", "model_name", "Which configured model."),),
            returns=_SET_MODEL_RETURNS,
        ),
        Capability(
            "set_session_name",
            "mutation",
            "Give this session a human-readable name.",
            on_wire=True,
            arguments=(Argument("name", "text", "The name to give it."),),
            returns=_SET_SESSION_NAME_RETURNS,
        ),
        Capability(
            "fork",
            "mutation",
            "Branch this session's active path into a new addressable session.",
            on_wire=True,
            returns=_SESSION_LIFECYCLE_RETURNS,
        ),
        Capability(
            "enable_extension",
            "mutation",
            "Runtime-enable a disabled extension.",
            on_wire=True,
            arguments=(Argument("path", "extension_name", "Which extension."),),
            returns=_EXTENSION_ACTION_RETURNS,
        ),
        Capability(
            "disable_extension",
            "mutation",
            "Runtime-disable a loaded extension.",
            on_wire=True,
            arguments=(Argument("path", "extension_name", "Which extension."),),
            returns=_EXTENSION_ACTION_RETURNS,
        ),
        Capability(
            "reload_extension",
            "mutation",
            "Re-import an extension from disk.",
            on_wire=True,
            arguments=(Argument("path", "extension_name", "Which extension."),),
            returns=_EXTENSION_ACTION_RETURNS,
        ),
        Capability(
            "navigate",
            "mutation",
            "Move the session cursor to an entry.",
            on_wire=True,
            arguments=(Argument("target_id", "message_id", "The entry to move onto."),),
            returns=_TREE_CONTEXT_RETURNS,
        ),
        Capability(
            "summarize_and_navigate",
            "mutation",
            "Summarize a subtree and splice the summary onto the path.",
            on_wire=True,
            arguments=(
                Argument("target_id", "message_id", "The entry to move onto."),
                Argument(
                    "custom_instructions",
                    "text",
                    "Extra focus for the generated summary.",
                    required=False,
                ),
            ),
            returns=_TREE_CONTEXT_RETURNS,
        ),
        Capability(
            "elide_span",
            "mutation",
            "Fold a span out of the active context.",
            on_wire=True,
            arguments=(
                Argument("anchor_id", "message_id", "The entry the fold hangs from."),
                Argument("first_kept_id", "message_id", "The first entry still shown."),
            ),
            returns=_TREE_CONTEXT_RETURNS,
        ),
        Capability(
            "commit_branch",
            "mutation",
            "Branch from a set of marked entries.",
            on_wire=True,
            arguments=(
                Argument("ids", "message_id", "The marked entries.", cardinality="many"),
                Argument(
                    "drop_context",
                    "boolean",
                    "Whether the branch keeps only the selection.",
                ),
            ),
            returns=_TREE_CONTEXT_RETURNS,
        ),
        Capability(
            "paste_subtree",
            "mutation",
            "Copy a subtree under another entry.",
            on_wire=True,
            arguments=(
                Argument("source_id", "message_id", "The copied subtree's root."),
                Argument("target_id", "message_id", "The entry the copy hangs from."),
            ),
            returns=_PASTE_SUBTREE_RETURNS,
        ),
    )
}


FLOWS: tuple[Flow, ...] = (
    Flow(
        name="compact",
        description="compact the conversation and re-render the transcript",
        mutation="compact",
        arguments=(
            Argument(
                "custom_instructions",
                "text",
                "Extra focus for the generated summary.",
                required=False,
            ),
        ),
    ),
    Flow(
        name="fork",
        description="branch this session's history into a new one and continue there",
        mutation="fork",
    ),
    Flow(
        name="model",
        description="switch the active model, effective on the next turn",
        mutation="set_model",
        arguments=(Argument("name", "model_name", "Which configured model."),),
    ),
    Flow(
        name="resume",
        description="open the session picker, or resume the session named by <ref>",
        mutation="switch_session",
        arguments=(Argument("session_id", "session_id", "Which session to resume."),),
    ),
    Flow(
        name="name",
        description="give this session a display name the picker will show",
        mutation="set_session_name",
        arguments=(Argument("name", "text", "The name to give it."),),
    ),
    Flow(
        name="autocompact",
        description="turn automatic compaction on or off for this session",
        mutation="set_auto_compaction",
        arguments=(Argument("enabled", "boolean", "The state to put it in."),),
    ),
    Flow(
        name="enable_extension",
        description="re-bind a disabled extension by re-running its register()",
        mutation="enable_extension",
        arguments=(Argument("path", "extension_name", "Which extension."),),
    ),
    Flow(
        name="disable_extension",
        description="tear down a loaded extension so its hooks stop firing",
        mutation="disable_extension",
        arguments=(Argument("path", "extension_name", "Which extension."),),
    ),
    Flow(
        name="reload_extension",
        description="re-import an extension from disk and re-register it",
        mutation="reload_extension",
        arguments=(Argument("path", "extension_name", "Which extension."),),
    ),
)


VIEW_COMMANDS: dict[str, str] = {
    "tree": "open the session-tree browser",
    "extensions": "list loaded extensions and what each registered",
}
"""The slash names that open a view, mapped to what the view is for.

A view is head code and this table says nothing about what it composes, what it holds
selected, or how it is laid out. What it declares is the NAME, and it declares it for
the reason ``commands.py`` gives for keeping the built-in vocabulary in the core: a
name a head does not resolve is sent to the model as prose, so a head that lacked
``/tree`` would silently prompt the model with the word "tree".

Which views a head actually HAS is head-local, and it says so twice: by what it offers
in completion and the palette, and by raising ``UnsupportedCommandError`` for a name it
cannot perform. Those two are the whole of "a web head should not advertise /tree" —
not resolving it is the one thing that must not be head-local.
"""


def _check_registry(
    domains: Mapping[str, Domain],
    capabilities: Mapping[str, Capability],
    flows: Sequence[Flow],
    views: Mapping[str, str],
    enumerators: Mapping[str, Any] = MappingProxyType({}),
) -> None:
    """Cross-check the four tables, so a typo is not a silent empty list.

    Every enumerator names a declared read, every flow ends in a declared mutation,
    every argument names a declared domain, and every flow argument is an argument of
    the mutation it ends in. Run at import for the built-in tables and again for every
    overlay :meth:`Vocabulary.extended_with` builds, rather than by a test, because the
    failure mode without it is a step that offers no candidates and looks like a scope
    that happened to be empty.

    The last of those is what stops the wire and the flow table from drifting. Both
    used to name ``set_model``'s argument ``name`` in two hand-written places with
    nothing comparing them, so renaming one left the other saying something a head
    would send and the verb would reject.

    Args:
        domains: Every domain a flow or capability argument may name.
        capabilities: Every read and mutation, by name.
        flows: The flows to check.
        views: The view command names, checked for clashes with flow names.
        enumerators: Domain name to the callable listing its values. A domain named
            here is satisfied by that callable; one that is not must name a declared
            read capability. Both are the same check — that something will actually
            produce the values — asked of the two places values can come from.

    Raises:
        ValueError: Any of the cross-references above does not hold.
    """
    for domain in domains.values():
        if domain.enumerator is None or domain.name in enumerators:
            continue
        found_read = capabilities.get(domain.enumerator)
        if found_read is None or found_read.kind != "read":
            raise ValueError(
                f"domain {domain.name!r} names enumerator {domain.enumerator!r}, which is "
                "neither a declared read capability nor a callable passed as `enumerators`"
            )
    for capability in capabilities.values():
        if capability.returns is None:
            raise ValueError(
                f"capability {capability.name!r} declares no `returns`. A capability whose "
                "result shape is stated nowhere leaves every head to discover it by calling "
                "and looking (docs/REMOTE-CONTROL.md §6, 'the result half is generated too')."
            )
        for argument in capability.arguments or ():
            if argument.domain not in domains:
                raise ValueError(
                    f"capability {capability.name!r} argument {argument.name!r} names "
                    f"unknown domain {argument.domain!r}"
                )
    for flow in flows:
        found = capabilities.get(flow.mutation)
        if found is None or found.kind != "mutation":
            raise ValueError(
                f"flow {flow.name!r} ends in {flow.mutation!r}, "
                "which is not a declared mutation capability"
            )
        for argument in flow.arguments:
            if argument.domain not in domains:
                raise ValueError(
                    f"flow {flow.name!r} argument {argument.name!r} names unknown "
                    f"domain {argument.domain!r}"
                )
        _check_flow_matches_mutation(flow, found)
    clash = set(views) & {flow.name for flow in flows}
    if clash:
        raise ValueError(f"view commands and flows share the names {sorted(clash)}")
    declared_scopes = domains["message_id_scope"].values or ()
    if set(declared_scopes) != set(get_args(MessageIdScope)):
        raise ValueError(
            f"the 'message_id_scope' domain offers {sorted(declared_scopes)}, but "
            f"ConversationTree accepts {sorted(get_args(MessageIdScope))}. A head would "
            "render a scope the tree refuses, or never offer one it accepts."
        )


def _check_flow_matches_mutation(flow: Flow, mutation: Capability) -> None:
    """A flow's arguments must be its mutation's arguments, or a prefix of them.

    Three rules, and each names a way a head would be misled. A flow argument the
    mutation does not take is a value the head collects and the performer discards. A
    domain or cardinality that differs is a field rendered from the wrong value set. A
    required mutation argument the flow never asks for is a gesture that reaches
    :class:`~tau_agent_core.flows.Ready` and then fails.

    A flow may leave out an OPTIONAL argument — that is how a gesture stays short —
    and it may order what it does ask for however reads best for a person, since the
    performer takes them by name.
    """
    if mutation.arguments is None:
        raise ValueError(
            f"flow {flow.name!r} ends in {mutation.name!r}, whose arguments are not "
            "expressible as domains. A flow cannot collect what no head can render."
        )
    declared = {argument.name: argument for argument in mutation.arguments}
    for argument in flow.arguments:
        found = declared.get(argument.name)
        if found is None:
            raise ValueError(
                f"flow {flow.name!r} asks for {argument.name!r}, which "
                f"{mutation.name!r} does not take"
            )
        if (found.domain, found.cardinality) != (argument.domain, argument.cardinality):
            raise ValueError(
                f"flow {flow.name!r} declares {argument.name!r} as "
                f"{argument.domain}/{argument.cardinality}, but {mutation.name!r} takes it "
                f"as {found.domain}/{found.cardinality}"
            )
    asked = {argument.name for argument in flow.arguments}
    missing = [name for name, arg in declared.items() if arg.required and name not in asked]
    if missing:
        raise ValueError(
            f"flow {flow.name!r} never asks for {sorted(missing)}, which {mutation.name!r} requires"
        )


_check_registry(DOMAINS, CAPABILITIES, FLOWS, VIEW_COMMANDS)


@agent_facing(topic="sessions")
def slash_vocabulary() -> dict[str, str]:
    """Every built-in slash command, mapped to what it does.

    The projection :data:`tau_agent_core.commands.FRONTEND_COMMANDS` is built from,
    so the flow table and the slash vocabulary cannot drift apart. Built-ins only —
    what an extension added is :meth:`Vocabulary.extension_vocabulary`, kept apart
    because ``resolve_command`` resolves the two halves in that order.

    Ordering is ``compact``, the view commands, then the remaining flows, which is
    the order the vocabulary already had — completion lists and the RPC
    ``get_commands`` listing are both observably ordered, so the projection preserves
    what callers already see rather than re-sorting on a new principle.

    Returns:
        A fresh dict of command name to one-line description. Fresh rather than
        shared, because it goes to callers that hold it.
    """
    flows = {flow.name: flow.description for flow in FLOWS}
    return {
        "compact": flows["compact"],
        **VIEW_COMMANDS,
        **{name: text for name, text in flows.items() if name != "compact"},
    }


@dataclass(frozen=True)
class Vocabulary:
    """The registry a caller reads, which is τ's own plus whatever this session added.

    Reference: docs/EXTENSION-FLOWS.md.

    Every pure function over the registry takes one of these, defaulting to
    :data:`BUILTIN`. That is the whole mechanism: the tables stay frozen module
    constants and an extension's declarations arrive as a LAYER, so two sessions in
    one process — a fork, a sub-agent, a ``switch_session`` — cannot see each other's
    flows. A mutable global would have made ``next_step`` answer differently
    depending on which session last loaded an extension, which is exactly the
    property ``resolve_command``'s docstring calls being "pure and total".

    ``enumerators`` is where an extension's live values come from, and it is separate
    from :attr:`domains` on purpose: a :class:`Domain` is a declarative record that
    goes over the wire, and a Python callable cannot. A head reads the domain and
    calls ``enumerate_domain``; only the process holding the extension runs the
    callable.

    Attributes:
        domains: Every domain, by name. Built-ins plus any an extension declared.
        capabilities: Every read and mutation, by name.
        flows: The flows, built-ins first, in the order a vocabulary lists them.
        views: View command names mapped to what the view is for.
        enumerators: Domain name to the callable that lists its values. Empty for
            :data:`BUILTIN` — τ's own enumerable domains are dispatched by name
            inside :func:`~tau_agent_core.flows.enumerate_domain`, which needs the
            session and runtime objects a callable here does not get.
        extension_flows: The names in :attr:`flows` an extension declared, so
            dispatch can tell a flow it performs itself from one whose mutation is an
            extension's own handler.
    """

    domains: Mapping[str, Domain]
    capabilities: Mapping[str, Capability]
    flows: tuple[Flow, ...]
    views: Mapping[str, str]
    enumerators: Mapping[str, Any] = field(default_factory=dict)
    extension_flows: frozenset[str] = frozenset()

    def flow(self, name: str) -> Flow | None:
        """The flow of that name, or ``None``."""
        for declared in self.flows:
            if declared.name == name:
                return declared
        return None

    def extension_vocabulary(self) -> dict[str, str]:
        """What extensions added, as command name to description.

        Returns:
            A fresh dict, in declaration order. Empty for :data:`BUILTIN`.
        """
        return {
            flow.name: flow.description for flow in self.flows if flow.name in self.extension_flows
        }

    def extended_with(
        self,
        flows: Sequence[Flow],
        *,
        domains: Mapping[str, Domain] | None = None,
        enumerators: Mapping[str, Any] | None = None,
    ) -> Vocabulary:
        """This vocabulary plus what an extension declared, cross-checked as one.

        Each added flow's mutation is synthesised as a private
        :class:`Capability` naming the flow itself, because an extension's mutation
        IS its registered handler and there is no wire verb behind it. That is what
        lets :func:`_check_registry` validate the overlay with the same rules it
        applies to τ's own tables rather than a second, laxer set.

        Args:
            flows: The flows to add. A name already in this vocabulary is refused.
            domains: Domains the added flows name, beyond the ones already here.
            enumerators: Domain name to the callable listing its values, for the
                added domains that compute them.

        Returns:
            A new :class:`Vocabulary`. This one is unchanged.

        Raises:
            ValueError: An added flow's name collides with one already declared, an
                added flow declares more than one argument, or the combined tables
                fail :func:`_check_registry`.
        """
        if not flows:
            return self
        taken = {flow.name for flow in self.flows} | set(self.views)
        merged_domains = {**self.domains, **(domains or {})}
        merged_capabilities = dict(self.capabilities)

        for flow in flows:
            if flow.name in taken:
                raise ValueError(
                    f"flow {flow.name!r} is already declared. An extension cannot shadow a "
                    "built-in gesture: resolve_command gives the built-in to a collision, so "
                    "the added flow would be unreachable by name."
                )
            if len(flow.arguments) > 1:
                raise ValueError(
                    f"flow {flow.name!r} declares {len(flow.arguments)} arguments. An "
                    "extension flow ends in a handler taking one typed line, and there is no "
                    "rule for splitting one line across two arguments — the same refusal "
                    "bind_command_args makes. Drive a multi-field gesture with ui.form."
                )
            taken.add(flow.name)
            merged_capabilities[flow.mutation] = Capability(
                name=flow.mutation,
                kind="mutation",
                description=flow.description,
                on_wire=False,
                arguments=flow.arguments,
                returns=_EXTENSION_FLOW_RETURNS,
            )

        combined = (*self.flows, *flows)
        merged_enumerators = {**self.enumerators, **(enumerators or {})}
        _check_registry(
            merged_domains, merged_capabilities, combined, self.views, merged_enumerators
        )
        return Vocabulary(
            domains=merged_domains,
            capabilities=merged_capabilities,
            flows=combined,
            views=self.views,
            enumerators=merged_enumerators,
            extension_flows=self.extension_flows | {flow.name for flow in flows},
        )


_EXTENSION_FLOW_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output": {
            "type": ["string", "null"],
            "description": (
                "What the extension's handler returned, coerced to display text. "
                "The same channel a command registered without a flow reports on."
            ),
        }
    },
    "required": ["output"],
}
"""What an extension flow's synthesised mutation gives back.

One shape for all of them, because the performer is always
``AgentSession.run_extension_command`` and its answer is always the handler's return
value as text.
"""

BUILTIN = Vocabulary(
    domains=DOMAINS,
    capabilities=CAPABILITIES,
    flows=FLOWS,
    views=VIEW_COMMANDS,
)
"""τ's own registry, with nothing layered on it.

The default of every function that takes a :class:`Vocabulary`, so a caller with no
extensions — a test, a headless run, a head peeking before a backend exists — reads
exactly the tables this module declares.
"""
