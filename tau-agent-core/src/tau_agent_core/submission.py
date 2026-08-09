"""The submission lifecycle's data model: one shape for every input source.

Reference: docs/SUBMISSION-LIFECYCLE.md, "The dataclasses" section (phase 1, part 1).

This module defines the submission types plus the machinery that belongs to the record rather
than to the session: :data:`DRIVING_SUBMISSION_DEPTH` / :func:`next_submission_depth`, which is
how `Submission.depth` actually gets incremented (decision 3), and
:data:`SUBMISSION_ALLOWS_USER_INPUT` / :func:`user_input_permitted`, which is how
`Submission.allow_user_input` actually gates a dialog. Both are ContextVars for the same reason:
the question each answers is "what is driving the code executing right now", which is a property
of the call, not of the session. Everything else —
``AgentSession.submit()`` (phase 1, part 2), the `input` hook relocation, command dispatch, the
`prompt()` compatibility wrapper — lives in ``agent_session.py``. Importing this module still
changes the behaviour of nothing: the context var is unset until a `submit()` sets it.

Why this exists (docs/SUBMISSION-LIFECYCLE.md "The problem" / "Prior art"): today a human
typing into the TUI, `AgentSession.prompt()`, and `ctx.prompt()` each define "what does
submitting a turn mean" independently, and `prompt()` has no concurrency guard at all — two
concurrent callers interleave and corrupt history. pi's answer is one seam,
`AgentSession.prompt(text, options?)` (`agent-session.ts:988`), that every frontend funnels
through. `Submission` is τ's version of pi's `PromptOptions` plus `InputSource`
(`types.ts:781`), widened with the mechanisms the spec's "Five mechanisms" section names:
per-request capability declaration (Jupyter `allow_stdin`), a named concurrency policy
(LangGraph `multitask_strategy`), and typed in-band refusal (LSP
`ApplyWorkspaceEditResult`).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from tau_agent_core.commands import CommandOutcome

#: Self-submission depth cap (decision 3). `submit()` (not this module) raises past this —
#: Neovim's shape: non-reentrant by default, opt-in nesting via `depth`, hard cap rather than
#: an advisory flag (AutoGen's `stop_hook_active` is the counter-example this rejects: a flag
#: that "does not prevent loops automatically" is not a guard). Not a constructor parameter:
#: nothing yet needs a different bound, and `AgentSession.__init__` already takes fifteen.
MAX_SUBMISSION_DEPTH = 10

#: The depth of the submission driving the turn the *currently executing code* belongs to, or
#: ``None`` outside any submission-driven turn. `AgentSession.submit()` sets it for the lifetime
#: of the turn it admits and resets it in the same `finally`; `next_submission_depth` below reads
#: it. This is what makes `MAX_SUBMISSION_DEPTH` reachable instead of decorative: until it
#: existed, no call site anywhere incremented `Submission.depth`, so the counter was structurally
#: always zero and decision 3's cap could never fire.
#:
#: A ContextVar rather than an attribute on `AgentSession` because the question decision 3 asks is
#: CAUSAL — "was this submission made from inside a turn?" — not temporal — "was a turn running
#: when this submission arrived?". `asyncio.Task` copies the ambient context at CREATION, so a
#: task spawned by a hook running inside a turn inherits that turn's depth even if it does not
#: reach `submit()` until after the turn has ended, while a long-lived task created before the
#: turn (an extension's bus subscription loop, a timer) inherits nothing and stays at depth 0 no
#: matter how much traffic it delivers mid-turn. A session attribute gets both of those backwards:
#: it loses the self-continuation loop that submits a beat too late, and it accumulates depth on
#: an unrelated busy bus until a legitimate workload is locked out at ten messages.
#:
#: Known escape (documented, not worked around): a self-submission scheduled through something
#: that does NOT copy the context — `loop.call_later`, a thread, or a pre-existing task merely
#: *signalled* from inside the turn — is not counted. Nothing in τ originates a submission that
#: way today; a mechanism that wants to must thread the depth itself, via `Submission.depth`,
#: which `next_submission_depth` treats as a floor.
DRIVING_SUBMISSION_DEPTH: ContextVar[int | None] = ContextVar(
    "tau_driving_submission_depth", default=None
)


def next_submission_depth(declared: int = 0) -> int:
    """The depth a submission entering `submit()` right now actually has (decision 3).

    ``declared`` is the depth the submitter put on the record. Outside any turn it stands
    unchanged — that is the ordinary case, and a human typing while an unrelated turn runs must
    NOT climb toward the cap. Inside a turn (see :data:`DRIVING_SUBMISSION_DEPTH`) the submission
    is by definition self-submission, so it sits one below the turn that caused it.

    ``max`` rather than a plain override because the two inputs describe two different chains and
    neither may be dropped: ``declared`` is a chain relayed from outside this process (an RPC
    client forwarding a depth it was given), the context var is the in-process one. Taking the
    larger can only ever tighten the bound, which is the safe direction for a guard.
    """
    driving = DRIVING_SUBMISSION_DEPTH.get()
    if driving is None:
        return declared
    return max(declared, driving + 1)


#: Whether the submission driving the *currently executing code* permits a blocking dialog to
#: reach a human, or ``None`` outside any submission-driven turn. `AgentSession.submit()` sets it
#: from `Submission.allow_user_input` for the lifetime of the turn it admits and resets it in the
#: same `finally`; :func:`user_input_permitted` below reads it, and `ExtensionUI`'s blocking
#: dialogs (``confirm``/``select``/``input``/``form``) consult that. Enforcement is the pre-existing
#: `HeadlessDialogError` path, exactly as the spec requires ("Enforcement stays HeadlessDialogError"):
#: a dialog under a submission that forbids user input takes the headless-answer route regardless of
#: the process having a TUI delegate, so it returns the explicitly configured ``--ui-defaults``
#: answer or RAISES. Nothing is auto-answered.
#:
#: A ContextVar rather than an attribute on `AgentSession`, for the same reason as
#: :data:`DRIVING_SUBMISSION_DEPTH` (and it is the reason the field is per-submission at all —
#: docs/SUBMISSION-LIFECYCLE.md "The dataclasses": "an embedded τ can serve an interactive session
#: and a cron-triggered submission simultaneously"): the question `ui.confirm()` asks is CAUSAL —
#: "is the code asking running UNDER a submission that permits it?" — not temporal — "was such a
#: submission in flight when it asked?". `asyncio.Task` copies the ambient context at CREATION, so
#: a task a cron-driven turn's hook spawned still cannot open a dialog even if it gets round to
#: trying after that turn ended, while an extension's long-lived subscription loop — which predates
#: the turn and belongs to no submission — is unaffected by a cron turn happening to be in flight.
#: A session attribute gets both of those backwards.
#:
#: ``None`` (nothing published) means "not running under a submission" and leaves dialog behaviour
#: exactly as it was before this existed — `continue_conversation()`, a TUI slash-command handler,
#: an extension's `session_start`. It is deliberately NOT the same as ``True``: ``True`` is a
#: submission that positively declared the capability.
SUBMISSION_ALLOWS_USER_INPUT: ContextVar[bool | None] = ContextVar(
    "tau_submission_allows_user_input", default=None
)


def user_input_permitted() -> bool:
    """May the code running right now open a blocking dialog to a human?

    ``False`` only when a submission that declared ``allow_user_input=False`` is driving this
    code (see :data:`SUBMISSION_ALLOWS_USER_INPUT`). Outside any submission-driven turn the
    answer is ``True`` — the capability is a per-submission RESTRICTION, and code that belongs
    to no submission has not been restricted by one.
    """
    return SUBMISSION_ALLOWS_USER_INPUT.get() is not False


#: Who originated a submission. Carried on every `Submission` and (phase 2) every `AgentEvent`,
#: so a renderer can decide *how* to show a turn without the core knowing any renderer exists —
#: Jupyter's `parent_header` argument from the spec's "Prior art" section. τ previously had no
#: analogue of pi's `InputSource` (`types.ts:781`) at all.
SubmissionSource = Literal[
    "interactive",  # a human at a frontend
    "rpc",  # a programmatic client over a transport
    "extension",  # an extension's own logic
    "bus",  # NATS / message bus
    "timer",  # schedule / cron
    "webhook",  # inbound HTTP
    "voice",  # speech front end
    "agent",  # τ driving itself (sub-agent, self-continuation)
]

#: Concurrency policy against an in-flight turn — LangGraph Platform's `multitask_strategy`,
#: named on the submission rather than the submitter (the spec's "Five mechanisms" point 4:
#: the same parameter appears on the timer-driven `crons.create`, which proves it belongs here).
#:   reject   — refuse if a turn is in flight; returns accepted=False
#:   enqueue  — run after the current turn finishes (τ's existing followUp/nextTurn)
#:   steer    — deliver after the current turn's tool calls, before the next LLM call (pi's steer)
#:   rollback — abort the in-flight turn and discard its progress, then run
#:   fork     — branch at a node and run a second agent concurrently; the in-flight
#:              turn is untouched. Requires NODE-ADDRESSABLE-AGENTS.md I1.
#
# rollback and fork both select a parent and proceed in another direction. They differ in what
# happens to the children already there: rollback makes them stop being the path, fork leaves
# them as a second live pointer. On an append-only log that asymmetry is the whole cost —
# fork is purely additive and rollback is not (see the spec's open question 2).
MultitaskStrategy = Literal["reject", "enqueue", "steer", "rollback", "fork"]

#: JSON scalar types admissible inside `correlation` (plus `list`/`dict`, checked recursively by
#: `_require_json_safe`). Deliberately excludes tuple, set, bytes, and anything else — those
#: round-trip through `json.dumps`/`asdict()` differently or not at all, which is exactly the
#: "live object riding in correlation" failure decision 4 exists to catch at construction.
_JSON_SCALAR_TYPES = (type(None), bool, int, float, str)


def _require_json_safe(value: Any, path: str) -> None:
    """Raise ``ValueError`` if `value` (found at `path` within `correlation`) is not JSON-safe.

    Recurses into list/dict so a live object smuggled two levels deep — e.g.
    ``correlation={"bus": {"msg": <nats.Msg>}}`` — is caught here, at construction, where the
    traceback names `path` and the offending type. The alternative is letting it ride until a
    JSON renderer three hops downstream calls ``json.dumps`` on it and fails with no context
    about which submission or which key produced it (decision 4).
    """
    if isinstance(value, _JSON_SCALAR_TYPES):
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _require_json_safe(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"{path} has a non-string key {key!r} ({type(key).__name__}); "
                    "JSON object keys must be strings"
                )
            _require_json_safe(item, f"{path}[{key!r}]")
        return
    raise ValueError(
        f"{path} = {value!r} is a {type(value).__name__}, not a JSON scalar/list/dict. "
        "correlation must be free of live objects (e.g. a bus message) — it rides onto "
        "emitted events and is rendered as JSON several hops downstream (decision 4)."
    )


@dataclass(frozen=True)
class Submission:
    """One admission request to `AgentSession.submit()` — the single door every input source
    (interactive, headless, RPC, extension, bus, timer, webhook, voice, self-driven) uses to
    start or steer a turn. See docs/SUBMISSION-LIFECYCLE.md "The one door".

    Frozen because a `Submission` is carried across an await boundary into `submit()` and
    (via `correlation`) onto emitted events; nothing downstream may mutate the record a
    renderer is attributing an event to.
    """

    text: str
    source: SubmissionSource
    submitter: str  # extension name, "human", channel id — WHO, not what kind
    submission_id: str  # uuid4; the parent_header analogue
    images: list[dict[str, Any]] | None = None

    multitask_strategy: MultitaskStrategy = "reject"
    # Fail-Early default. pi throws when streaming and no behaviour is named; "reject" is that,
    # as a value instead of an exception. A submitter that wants queueing says so.

    expand_commands: bool = False
    # Whether leading "/" is command-dispatched and templates/skills expanded. FALSE by default:
    # pi's sendUserMessage sets expandPromptTemplates: false, so injected text can never smuggle
    # a "/compact" through a bus payload. An extension that wants to compact calls ctx.compact()
    # — the typed API — not a string.
    #
    # LIVE since B2-b: `submit()` step 3 resolves the text through
    # `tau_agent_core.commands.resolve_command` when this is True and reports the decision on
    # `SubmissionResult.command`. The default staying False is the SECURITY property, not a
    # phasing artefact — a bus/timer/webhook payload beginning with "/compact" is literal text
    # that goes to the model, and only a submitter that positively declared itself an
    # interactive frontend gets dispatch.

    allow_user_input: bool = False
    # Jupyter allow_stdin. Whether code running under THIS submission may prompt a human.
    # Per-submission, not per-process: an embedded τ can serve an interactive session and a
    # cron-triggered submission simultaneously. Enforcement stays HeadlessDialogError, via
    # SUBMISSION_ALLOWS_USER_INPUT above: submit() publishes this for the turn it admits and
    # ExtensionUI's blocking dialogs consult it.

    store_history: bool = True  # does this enter the durable session log

    silent: bool = False  # suppress renderer-visible output; forces store_history=False
    # NOT YET IMPLEMENTED at the session seam: the store_history fold below is real (and is what
    # a caller wanting a non-persisted turn should ask for directly), but nothing suppresses
    # renderer-visible output, which is what `silent` names. `submit()` RAISES NotImplementedError
    # on True — see its docstring for why the core cannot honour it before Block 3: the event bus
    # is one multiplexed channel whose subscribers are not all renderers, so suppression at the
    # core would blind measurement and RPC capture too, and the spec's own Jupyter rule forbids
    # the core deciding what a renderer shows.

    correlation: dict[str, Any] = field(default_factory=dict)
    # Free-form origin detail: bus subject + binding_id, cron id, HTTP request id. Carried onto
    # emitted events so a renderer can fan out to the right consumer. Free-form, but NOT
    # unchecked: __post_init__ raises on a value that is not a JSON scalar/list/dict
    # (decision 4). The failure mode being prevented is a live NATS message object riding
    # in `correlation` and detonating in the JSON renderer three hops downstream.

    depth: int = 0
    # Self-submission depth. Incremented when a submission is made from inside a turn that
    # is itself driven by a submission; submit() raises past MAX_SUBMISSION_DEPTH (decision 3).
    # Neovim's shape: non-reentrant by default, opt-in nesting, hard cap.
    #
    # A submitter normally leaves this at 0 and lets submit() derive it (next_submission_depth
    # above): the value on the record as constructed is only a FLOOR, the depth of a chain the
    # submitter already knows about from outside this process. The number the cap is actually
    # checked against — and the number this field carries once admitted, because submit()
    # `replace()`s the record with it — is that floor combined with the in-process context.

    def __post_init__(self) -> None:
        # silent ⇒ store_history=False is resolved ONCE, here, rather than leaving every
        # renderer/persistence call site to re-derive the interaction (the spec's admission
        # step 5 names exactly this: "the interaction resolved once here"). `object.__setattr__`
        # is the escape hatch for a frozen dataclass's own __post_init__ — it is the only place
        # this record is ever mutated, and only to normalize a redundant combination the caller
        # supplied, not to add new information. Restructuring `store_history` into a derived
        # property instead would mean it could no longer be a plain field on the frozen record
        # that round-trips through asdict()/reconstruct (the parity test this work item adds),
        # so the assignment, not a property, is the clearer fix.
        if self.silent and self.store_history:
            object.__setattr__(self, "store_history", False)

        # correlation is free-form BUT must be JSON-safe recursively (decision 4) — see
        # _require_json_safe's docstring for the failure this prevents.
        for key, value in self.correlation.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"correlation has a non-string key {key!r} ({type(key).__name__}); "
                    "JSON object keys must be strings"
                )
            _require_json_safe(value, f"correlation[{key!r}]")


@dataclass(frozen=True)
class SubmissionResult:
    """The outcome of a `Submission` — LSP's `ApplyWorkspaceEditResult` shape: a refusal is a
    **result**, not an exception (decision-adjacent to the spec's "Five mechanisms" point 5).
    """

    accepted: bool  # LSP ApplyWorkspaceEditResult.applied
    submission_id: str
    rejection_reason: str | None = None  # LSP failureReason — a RESULT, not an exception
    messages: list[dict[str, Any]] = field(default_factory=list)

    command: CommandOutcome | None = None
    # Set when `expand_commands` was True and the text resolved to a command
    # (docs/SUBMISSION-LIFECYCLE.md submit() step 3). The submission was ACCEPTED and no turn
    # ran, so `messages` is empty — a caller that reads only `messages` sees an empty turn,
    # which is why this is a field a caller must look at rather than an out-of-band signal.
    #
    # `performer="core"` means the core already ran it (an extension-registered command) and
    # `output` is its text; `performer="frontend"` means the core decided WHAT it is and the
    # caller must perform it — or raise `UnsupportedCommandError`. See commands.py for why
    # that split exists and why silently ignoring the second shape is the failure mode the
    # whole lifecycle is about.
