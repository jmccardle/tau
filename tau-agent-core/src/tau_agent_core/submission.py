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

from tau_agent_core.extension_locks import ExtensionRequest
from tau_agent_core.flows import Dispatched

MAX_SUBMISSION_DEPTH = 10

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

MultitaskStrategy = Literal["reject", "enqueue", "steer", "rollback", "fork"]

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

    expand_commands: bool = False

    allow_user_input: bool = False

    store_history: bool = True  # does this enter the durable session log

    silent: bool = False  # suppress renderer-visible output; forces store_history=False

    correlation: dict[str, Any] = field(default_factory=dict)

    depth: int = 0

    def __post_init__(self) -> None:
        if self.silent and self.store_history:
            object.__setattr__(self, "store_history", False)

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

    ``lock`` is the extension request that refused this submission
    (docs/EXTENSION-LOCKS.md §5). Set only alongside ``accepted=False``, and
    always beside a ``rejection_reason`` carrying the same facts as prose — so a
    head renders the record and a log reads the sentence.
    """

    accepted: bool  # LSP ApplyWorkspaceEditResult.applied
    submission_id: str
    rejection_reason: str | None = None  # LSP failureReason — a RESULT, not an exception
    messages: list[dict[str, Any]] = field(default_factory=list)

    command: Dispatched | None = None

    lock: ExtensionRequest | None = None
