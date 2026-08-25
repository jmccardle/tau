"""RPC Tier B — `list_sessions` (finding 8), and the `addressable` field on
the session tuple (finding 7). One file, because they are one property seen
from two ends.

Finding 8: `switch_session` takes "an exact session id, or a unique id
prefix" and NOTHING on the command table produced one. A host could only
switch to a session it had made in this process (`new_session`/`fork`) or
whose id it had recorded from an earlier run's `get_state`; sessions made by
the TUI, by `tau -p`, or by a previous RPC child were unreachable — the same
G1 hole `get_models` closed for `set_model`'s config NAME one round earlier,
and, being absent from `declined` as well, a C1 violation rather than a
deferral.

Finding 7: `new_session {"persist": false}` handed back a tuple whose schema
called itself "F2's addressable tuple" and whose `store` said `"file"`, for
a session `switch_session` answered `-32602` for and no file ever held.

What this file pins, beyond "the list comes back":

* the equivalence that IS both findings — `session.addressable` is true
  exactly when `list_sessions` returns that id, and `switch_session` accepts
  exactly the ids `list_sessions` returned. Asserted as an equivalence over
  both `persist` modes rather than as two hand-written expectations, so a
  verb that listed ids `switch_session` rejects (or a flag that disagreed
  with the listing) cannot pass.
* scope honesty (unit S / D-6): the rows say WHICH universe they are, since
  `--mode rpc` and the TUI now default to different session directories.
  The end-to-end half of this is
  `tau-coding-agent/tests/test_rpc_conformance.py`; here it is the `ref`
  projection and the `scope` object.
* the projection is a projection: `first_message`/`last_message` are
  unbounded message text and are deliberately not published.
* read-ness: no D-1 `turn_safety_guard` (it answers with a turn in flight)
  and no `cursor` (E5 rule 2).

A new file per unit (docs/RPC-TIER-B.md §3 B1-B6 bullet: "a new file, so no
unit contends on a test file with any other").

A real `AgentSession` + a real `AgentSessionRuntime` over a test-only
`SessionCatalog` (the idiom `test_agent_session_runtime.py` established, not
imported from it — cross-test-file imports have no precedent in this suite):
this verb reads the runtime's own catalog and cwd, and the equivalence above
is only meaningful against the real `resolve_ref`.

Reference: docs/RPC-TIER-B.md §6 "Every test must be able to fail", D-9; the
Tier B review, findings 7 and 8.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.agent_session_runtime import AgentSessionRuntime
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.rpc import commands, dialect
from tau_agent_core.rpc.dialect import SESSION_NOT_PERSISTED
from tau_agent_core.rpc.handler import RPCHandler
from tau_agent_core.session_catalog import ConversationSession, SessionCatalog, SessionInfo
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model

_CWD = "/work"
_OTHER_CWD = "/elsewhere"

#: The fake store's base directory. Deliberately NOT under a home directory:
#: the property the `ref` projection exists for is "which universe is this",
#: and unit S made `<tmp>/.tau/sessions` vs `~/.tau/sessions` the live
#: instance of that question (D-6/H1b).
_BASE = "/tmp-fake/.tau/sessions"


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


class _FakeConversationSession:
    """A RAM-only `ConversationSession` that declares a durable location the
    way the FILE store does — `path`, one of `commands._DURABLE_LOCATION_
    ATTRS`, set on the persisted product and `None` on the ephemeral one
    (`session_store.py`'s `create_in_memory`, whose `_persist_*` are then
    no-ops).

    That one attribute is the whole reason this fake is not
    `test_agent_session_runtime.py`'s: its session declares NEITHER durable
    name, which is the JMFTS ephemeral shape and would make every product
    here — persisted or not — report `addressable: false`, i.e. would make
    the equivalence this file tests unfalsifiable.
    """

    def __init__(
        self, cwd: str, model: str, backend: str, name: str | None = None, *, path: str | None
    ) -> None:
        self._log = InMemorySessionLog()
        self._cwd = cwd
        self._model = model
        self._backend = backend
        self._name = name
        self.path = path
        now = datetime.now(timezone.utc)
        self._created = now
        self._modified = now
        #: What `SessionInfo` would carry for this session. Kept here so a
        #: test can make a row unreadable, or give it a 40kB first message,
        #: without reaching into the catalog's listing code.
        self._first_message = ""
        self._last_message = ""
        self._parent: str | None = None
        self._error: str | None = None

    @property
    def id(self) -> str:
        return self._log.id

    @property
    def cursor(self) -> str | None:
        return self._log.cursor

    def entries(self) -> list[dict[str, Any]]:
        return self._log.entries()

    def append_message(self, message: dict[str, Any]) -> str:
        return self._log.append_message(message)

    def append_custom_message(self, message: dict[str, Any], custom_type: str) -> str:
        return self._log.append_custom_message(message, custom_type)

    def append_custom_entry(self, custom_type: str, data: dict[str, Any]) -> str:
        return self._log.append_custom_entry(custom_type, data)

    def append_compaction(
        self,
        summary: str,
        first_kept_id: str,
        tokens_before: int,
        *,
        summarizer_model_id: str,
        summary_usage: dict[str, int],
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        return self._log.append_compaction(
            summary,
            first_kept_id,
            tokens_before,
            summarizer_model_id=summarizer_model_id,
            summary_usage=summary_usage,
            covered_entries=covered_entries,
            covered_tokens=covered_tokens,
            agent_spec_id=agent_spec_id,
        )

    def append_elide(
        self,
        first_kept_id: str,
        *,
        covered_entries: int,
        covered_tokens: int,
        agent_spec_id: str | None,
    ) -> str:
        return self._log.append_elide(
            first_kept_id,
            covered_entries=covered_entries,
            covered_tokens=covered_tokens,
            agent_spec_id=agent_spec_id,
        )

    def append_navigate(self, target_id: str | None) -> str:
        return self._log.append_navigate(target_id)

    def append_branch_summary(self, summary: str, from_id: str | None) -> str:
        return self._log.append_branch_summary(summary, from_id)

    def append_at(self, parent_id, entry_type, payload) -> str:
        return self._log.append_at(parent_id, entry_type, payload)

    @property
    def header(self) -> dict[str, Any]:
        return {"type": "session", "id": self.id, "cwd": self._cwd}

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [e["message"] for e in self._log.entries() if e.get("type") == "message"]

    @property
    def context(self) -> list[dict[str, Any]]:
        return ConversationTree(self.entries(), self.cursor).context_for()

    @property
    def model(self) -> str:
        return self._model

    @property
    def backend(self) -> str:
        return self._backend

    def display_title(self) -> str:
        if self._error:
            return f"⚠ unreadable session ({self.path}) — {self._error}"
        return self._name or f"Session ({self.id[:8]})"

    def append_model_change(self, model: str, backend: str) -> str:
        self._model, self._backend = model, backend
        return "model-change"

    def append_session_info(self, name: str) -> str:
        self._name = name
        return "session-info"


class _FakeCatalog(SessionCatalog):
    """The five abstract primitives, RAM-only, with the file store's two
    load-bearing behaviours kept: `create` gives the session a location under
    a base directory (so `ref` names a universe) and registers it for
    listing; `create_ephemeral` does neither.

    `resolve_ref`/`most_recent` are NOT overridden — the base class builds
    them out of `list` + `load`, which is exactly the coupling
    `list_sessions` claims ("the listing and switch_session's resolution are
    the SAME set by construction").
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _FakeConversationSession] = {}

    def _path_for(self, cwd: str, session_id: str) -> str:
        return f"{_BASE}/{cwd.strip('/').replace('/', '-')}/{session_id}.jsonl"

    def create(
        self, cwd, model, backend, *, system_prompt: str | None = None, name: str | None = None
    ) -> ConversationSession:
        session = _FakeConversationSession(cwd, model, backend, name, path=None)
        session.path = self._path_for(cwd, session.id)
        if system_prompt:
            session.append_message({"role": "system", "content": system_prompt})
        self._sessions[session.id] = session
        return session

    def create_ephemeral(
        self, cwd, model, backend, *, system_prompt: str | None = None, name: str | None = None
    ) -> ConversationSession:
        # Mirrors FileSessionCatalog: same construction, no path, never
        # registered — so it is never listed and never resolvable.
        session = _FakeConversationSession(cwd, model, backend, name, path=None)
        if system_prompt:
            session.append_message({"role": "system", "content": system_prompt})
        return session

    def load(self, ref: str) -> ConversationSession:
        for session in self._sessions.values():
            if session.path == ref:
                return session
        raise FileNotFoundError(f"no session at {ref!r}")

    def fork(self, source: ConversationSession, cwd: str) -> ConversationSession:
        assert isinstance(source, _FakeConversationSession)
        forked = _FakeConversationSession(cwd, source.model, source.backend, path=None)
        forked.path = self._path_for(cwd, forked.id)
        forked._parent = source.id
        for entry in source.entries():
            if entry.get("type") == "message":
                forked.append_message(entry["message"])
        self._sessions[forked.id] = forked
        return forked

    def list(self, cwd: str | None = None) -> list[SessionInfo]:
        rows = [
            SessionInfo(
                ref=str(session.path),
                id=session.id,
                cwd=session._cwd,
                name=session._name,
                created=session._created,
                modified=session._modified,
                message_count=len(session.messages),
                first_message=session._first_message,
                last_message=session._last_message,
                parent=session._parent,
                error=session._error,
            )
            for session in self._sessions.values()
            if cwd is None or session._cwd == cwd
        ]
        # Newest-modified first — SessionCatalog.list's documented contract,
        # and what `most_recent` depends on.
        return sorted(rows, key=lambda info: info.modified, reverse=True)


@pytest.fixture
def catalog() -> _FakeCatalog:
    return _FakeCatalog()


@pytest.fixture
def session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])


@pytest.fixture
def runtime(session: AgentSession, catalog: _FakeCatalog) -> AgentSessionRuntime:
    return AgentSessionRuntime(session, catalog, _CWD, "m", "openai", "file")


@pytest.fixture
def handler(session: AgentSession, runtime: AgentSessionRuntime) -> RPCHandler:
    return RPCHandler(session, runtime=runtime)


async def _call(handler: RPCHandler, method: str = "list_sessions", **params) -> dict:
    """Dispatch and return the whole wire response (envelope included), so a
    test can assert on `error` as readily as on `result`."""
    request: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        request["params"] = params
    await handler._handle_request(request)
    return await handler._output_queue.get()


async def _ids(handler: RPCHandler) -> list[str]:
    return [row["session_id"] for row in (await _call(handler))["result"]["sessions"]]


# ── table wiring ─────────────────────────────────────────────────────────


def test_list_sessions_is_a_tier_b_read_with_schemas() -> None:
    entry = commands.COMMAND_TABLE["list_sessions"]
    assert entry.tier == "B"
    assert entry.since == "tier-b"
    assert entry.handler is not None
    assert entry.declined_because is None
    assert entry.result_schema is not None
    assert entry.result_schema["required"] == ["sessions", "scope"]
    # A read takes no params at all (the same NO_PARAMS_SCHEMA object every
    # other read on the table uses, so an unexpected param is refused by
    # `validate_params` before the handler runs).
    assert entry.params_schema is commands.NO_PARAMS_SCHEMA


async def test_an_unexpected_param_is_refused(handler: RPCHandler) -> None:
    """Known gap 2 in the notes, enforced rather than merely stated: there is
    no `cwd` param, so a host cannot widen the scope to sessions
    `switch_session` would then refuse."""
    response = await _call(handler, cwd="/elsewhere")
    assert "result" not in response
    assert response["error"]["code"] == dialect.INVALID_PARAMS


# ── the listing ──────────────────────────────────────────────────────────


async def test_lists_this_cwds_sessions_newest_first_with_the_published_projection(
    handler: RPCHandler, catalog: _FakeCatalog
) -> None:
    """The whole result, asserted as one literal: the row fields the schema
    names, no more, and newest-modified first."""
    older = catalog.create(_CWD, "m", "openai", name="the older one")
    newer = catalog.create(_CWD, "m", "openai")
    newer.append_message({"role": "user", "content": "hello"})
    older._modified = newer._modified - timedelta(minutes=5)
    older._parent = "parent-id"

    response = await _call(handler)

    assert "error" not in response
    assert response["result"]["sessions"] == [
        {
            "session_id": newer.id,
            "ref": f"{_BASE}/work/{newer.id}.jsonl",
            "name": None,
            "title": f"Session ({newer.id[:8]})",
            "message_count": 1,
            "created": newer._created.isoformat(),
            "modified": newer._modified.isoformat(),
            "parent": None,
            "error": None,
        },
        {
            "session_id": older.id,
            "ref": f"{_BASE}/work/{older.id}.jsonl",
            "name": "the older one",
            "title": "the older one",
            "message_count": 0,
            "created": older._created.isoformat(),
            "modified": older._modified.isoformat(),
            "parent": "parent-id",
            "error": None,
        },
    ]


async def test_the_scope_names_the_store_and_the_cwd_the_ids_resolve_against(
    handler: RPCHandler,
) -> None:
    """`scope` is what makes the rows mean something: the same store label
    the session tuple carries, and the cwd `switch_session` resolves
    against."""
    scope = (await _call(handler))["result"]["scope"]
    assert scope == {"store": "file", "cwd": _CWD}


async def test_a_session_in_another_directory_is_neither_listed_nor_switchable(
    handler: RPCHandler, catalog: _FakeCatalog
) -> None:
    """Known gap 2, from the other side: the scope is not a filter this verb
    chose for tidiness — `switch_session` resolves against THIS cwd only, so
    a wider listing would advertise ids the next call refuses. Both halves
    asserted, so shrinking the listing (or widening it) breaks this."""
    mine = catalog.create(_CWD, "m", "openai")
    theirs = catalog.create(_OTHER_CWD, "m", "openai")

    assert await _ids(handler) == [mine.id]

    refused = await _call(handler, method="switch_session", session_id=theirs.id)
    assert "result" not in refused
    assert refused["error"]["code"] == dialect.INVALID_PARAMS


async def test_the_ref_names_where_each_session_lives(
    handler: RPCHandler, catalog: _FakeCatalog
) -> None:
    """Unit S/D-6 made "which universe am I looking at" a real question: an
    RPC child defaults to `<tmp>/.tau/sessions` while the TUI and `--print`
    use `~/.tau/sessions`. The `ref` is the store's own handle and is what
    tells them apart — the end-to-end proof against a real child's real
    directory is `test_rpc_conformance.py`."""
    catalog.create(_CWD, "m", "openai")

    rows = (await _call(handler))["result"]["sessions"]

    assert rows
    for row in rows:
        assert row["ref"].startswith(_BASE)


async def test_an_unreadable_session_stays_listed_and_says_why(
    handler: RPCHandler, catalog: _FakeCatalog
) -> None:
    """Known gap 3 / `SessionInfo.error`'s own reasoning: a session whose
    entries cannot be read stays in the listing, marked. Dropping the row
    would make a conversation vanish from a host's view with no signal —
    the silent failure persisting a session exists to prevent."""
    broken = catalog.create(_CWD, "m", "openai")
    broken._error = "integrity check failed"

    (row,) = (await _call(handler))["result"]["sessions"]

    assert row["session_id"] == broken.id
    assert row["error"] == "integrity check failed"
    assert "unreadable session" in row["title"]


async def test_message_text_is_not_published(handler: RPCHandler, catalog: _FakeCatalog) -> None:
    """`first_message`/`last_message` are whole message texts — unbounded —
    and a listing carrying two per row would put an arbitrary multiple of
    the transcript on the wire every time a host asked what it may switch
    to. `title` is the same data BOUNDED (`SessionInfo.display_title`: 50
    characters and an ellipsis), which is why it is the only place message
    text appears here at all."""
    session = catalog.create(_CWD, "m", "openai")
    session._first_message = "PAD" + "x" * 40_000
    session._last_message = "SECRET-TAIL"

    (row,) = (await _call(handler))["result"]["sessions"]

    assert set(row) == {
        "session_id",
        "ref",
        "name",
        "title",
        "message_count",
        "created",
        "modified",
        "parent",
        "error",
    }
    # The 40kB body did not ride along — only display_title's 50-char head.
    assert len(row["title"]) == 53
    assert "x" * 60 not in repr(row)
    # The LAST message is not published in any form.
    assert "SECRET-TAIL" not in repr(row)


async def test_an_empty_directory_lists_nothing_and_is_not_an_error(handler: RPCHandler) -> None:
    """ "Nothing has been persisted here yet" is a real answer with a real
    (empty) list — unlike `get_models`, there is no "nobody can be asked"
    case to conflate it with, since `SessionCatalog.list` is one of the five
    abstract methods a catalog cannot be constructed without."""
    response = await _call(handler)

    assert "error" not in response
    assert response["result"]["sessions"] == []
    assert response["result"]["scope"]["cwd"] == _CWD


# ── the round trip that IS finding 8 ─────────────────────────────────────


async def test_a_listed_id_is_one_switch_session_accepts(
    handler: RPCHandler, catalog: _FakeCatalog
) -> None:
    """The G1 round trip, driven entirely over the wire: `list_sessions` for
    an id, `switch_session` with that exact string, `get_state` for the
    result — no out-of-band read of the session directory anywhere in the
    loop, which is the whole claim. The target is a session made BEFORE this
    connection existed (the case a host could not reach at all), not one
    `new_session`/`fork` handed back."""
    planted = catalog.create(_CWD, "m", "openai", name="made by the TUI")

    (row,) = (await _call(handler))["result"]["sessions"]
    assert row["session_id"] == planted.id

    switched = await _call(handler, method="switch_session", session_id=row["session_id"])
    assert "error" not in switched, switched
    assert switched["result"]["session"]["session_id"] == planted.id

    state = await _call(handler, method="get_state")
    assert state["result"]["session_id"] == planted.id


async def test_a_unique_prefix_of_a_listed_id_also_resolves(
    handler: RPCHandler, catalog: _FakeCatalog
) -> None:
    """`switch_session`'s param takes "an exact session id, or a unique id
    prefix", so the ids this verb publishes have to be usable that way
    too — a listing of, say, truncated display ids would satisfy every
    shape assertion above and still be unusable."""
    planted = catalog.create(_CWD, "m", "openai")

    (row,) = (await _call(handler))["result"]["sessions"]
    switched = await _call(handler, method="switch_session", session_id=row["session_id"][:8])

    assert "error" not in switched, switched
    assert switched["result"]["session"]["session_id"] == planted.id


async def test_a_session_the_listing_omits_is_one_switch_session_refuses(
    handler: RPCHandler,
) -> None:
    """The negative half of the same equivalence, and finding 7's measured
    trace: `new_session {"persist": false}` produces an id, the listing does
    not contain it, and `switch_session` answers -32602 for it."""
    fresh = await _call(handler, method="new_session", persist=False)
    unpersisted_id = fresh["result"]["session"]["session_id"]

    assert unpersisted_id not in await _ids(handler)

    refused = await _call(handler, method="switch_session", session_id=unpersisted_id)
    assert "result" not in refused
    assert refused["error"]["code"] == dialect.INVALID_PARAMS
    assert "no session matches" in refused["error"]["message"]


# ── finding 7: the tuple tells the truth about itself ────────────────────


@pytest.mark.parametrize("persist", [True, False])
async def test_addressable_is_exactly_membership_of_the_listing(
    handler: RPCHandler, persist: bool
) -> None:
    """The one property both findings are about, asserted as an EQUIVALENCE
    over both modes rather than as two hand-written expectations: the flag
    on the tuple and the contents of the listing cannot disagree, whichever
    of them is wrong.

    A hardcoded `addressable: True` passes the persist=True case and fails
    here; a listing that included unpersisted sessions fails the other."""
    fresh = await _call(handler, method="new_session", persist=persist)
    tuple_ = fresh["result"]["session"]

    assert tuple_["addressable"] is (tuple_["session_id"] in await _ids(handler))
    assert tuple_["addressable"] is persist


async def test_an_unpersisted_tuple_still_reports_its_store_and_cursor(
    handler: RPCHandler,
) -> None:
    """`addressable: false` is the field that was missing; nothing else is
    removed. `store` still names the store THIS CONNECTION is on (the schema
    says so — it is not a claim the session is in it), and `cursor` is the
    in-memory tip E5 requires of every lifecycle response."""
    fresh = await _call(handler, method="new_session", persist=False)
    result = fresh["result"]

    assert result["session"]["addressable"] is False
    assert result["session"]["store"] == "file"
    assert result["session"]["lane"] == "primary"
    # E5: `cursor` is still reported (this fake's fresh log has no entries
    # yet, so it is null here — the KEY is what E5 requires, and the two
    # copies must agree).
    assert "cursor" in result
    assert result["cursor"] == result["session"]["cursor"]


async def test_fork_and_switch_session_report_addressable_too(
    handler: RPCHandler, catalog: _FakeCatalog
) -> None:
    """All three lifecycle verbs share `SESSION_LIFECYCLE_RESULT_SCHEMA`, so
    a host reads ONE contract: the field is present on every one of them,
    not only on the verb whose param made it necessary."""
    planted = catalog.create(_CWD, "m", "openai")
    # `fork` needs the connection to be ON a catalog-produced session first
    # (AgentSessionRuntime.fork refuses the bare scratch log an AgentSession
    # is constructed with) — that is rpc_mode's startup invariant, restated
    # here in one call.
    await _call(handler, method="new_session")

    forked = await _call(handler, method="fork")
    assert forked["result"]["session"]["addressable"] is True

    switched = await _call(handler, method="switch_session", session_id=planted.id)
    assert switched["result"]["session"]["addressable"] is True


async def test_the_lifecycle_schema_describes_every_field_the_tuple_carries(
    handler: RPCHandler,
) -> None:
    """The schema is what a second implementation reads (G1), and it is
    published verbatim into docs/RPC-PROTOCOL.md — a field the wire carries
    and the schema does not describe is invisible to every host that builds
    from the document rather than from a packet capture.

    Asserted against a REAL response's keys rather than a hand-copied list,
    so the next field added to this tuple (or a rename of this one) has to
    be described too."""
    fresh = await _call(handler, method="new_session")
    described = commands.SESSION_LIFECYCLE_RESULT_SCHEMA["properties"]["session"]["description"]

    for field in fresh["result"]["session"]:
        assert field in described, f"the session tuple carries {field!r}, undescribed"

    # The unconditional claim finding 7 named, gone: the tuple is not
    # "addressable" by definition any more, it is addressable when it says so.
    assert "F2's addressable tuple" not in described


# ── the predicate itself ─────────────────────────────────────────────────


class _DeclaresNothing:
    """The JMFTS ephemeral shape (`tau_jmfts.catalog._EphemeralConversation
    Session`): neither `path` nor `root_doc_id`."""


class _DeclaresNone:
    """The file store's in-memory product: declares `path`, set to None."""

    path = None


class _DeclaresARootDoc:
    """The JMFTS persisted shape: the OTHER name in
    `_DURABLE_LOCATION_ATTRS`, so the predicate cannot be a `path` check."""

    root_doc_id = 41


@pytest.mark.parametrize(
    "log, expected",
    [
        (_DeclaresNothing(), False),
        (_DeclaresNone(), False),
        (_DeclaresARootDoc(), True),
    ],
)
def test_session_log_is_addressable_asks_the_same_question_d7_asks(
    log: object, expected: bool
) -> None:
    """`session_log_is_addressable` and `require_durable_session` read the
    same `_DURABLE_LOCATION_ATTRS` through the same helper, which is the
    point: "addressable" is not a second notion of durability that could
    drift from D-7's, it is D-7's question asked without a raise.

    Both directions of the asymmetry that list documents: unknown means NO
    (a log declaring neither name is not addressable), and a declared-but-
    unset location is not addressable either."""
    assert commands.session_log_is_addressable(log) is expected

    # ... and the same object, run through D-7's guard, agrees.
    guarded = AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])
    guarded.session_log = log  # type: ignore[assignment]
    if expected:
        commands.require_durable_session(guarded, verb="probe")
    else:
        with pytest.raises(commands.RPCError) as refusal:
            commands.require_durable_session(guarded, verb="probe")
        assert refusal.value.code == SESSION_NOT_PERSISTED


# ── E5 / D-1: it is a read ───────────────────────────────────────────────


async def test_carries_no_cursor(handler: RPCHandler) -> None:
    """E5 rule 2 (commands.py "E5 in Tier B"): a read never carries one."""
    assert "cursor" not in commands.COMMAND_TABLE["list_sessions"].result_schema["properties"]
    assert "cursor" not in (await _call(handler))["result"]


async def test_answers_while_a_turn_holds_the_turn_lock(
    session: AgentSession, handler: RPCHandler, catalog: _FakeCatalog
) -> None:
    """D-1 binds the four MUTATING Tier B verbs; this one must NOT take the
    guard. With `turn_lock` held — a turn genuinely in flight — a verb that
    took the guard would sit on it and then answer TURN_STILL_RUNNING.

    No timeout shrinking: if this verb ever grew the guard, this test would
    take the full 5s `DEFAULT_SWAP_TIMEOUT_S` and then fail on the code —
    slow, but the failure is unambiguous."""
    catalog.create(_CWD, "m", "openai")
    await session.turn_lock.acquire()
    try:
        response = await _call(handler)
    finally:
        session.turn_lock.release()

    assert "error" not in response
    assert len(response["result"]["sessions"]) == 1


async def test_it_answers_the_same_on_an_unpersisted_session(
    handler: RPCHandler, catalog: _FakeCatalog
) -> None:
    """D-7 rule 2: this verb appends nothing, so it never asks whether the
    bound session is durable — and the answer it gives from an unpersisted
    session is precisely that the current session is not among the rows,
    which is how a host gets back to a persisted one."""
    planted = catalog.create(_CWD, "m", "openai")
    await _call(handler, method="new_session", persist=False)

    response = await _call(handler)

    assert "error" not in response
    assert [row["session_id"] for row in response["result"]["sessions"]] == [planted.id]


# ── Fail-Early: no runtime, no answer ────────────────────────────────────


async def test_a_handler_with_no_runtime_refuses(session: AgentSession) -> None:
    """A handler constructed without an `AgentSessionRuntime` (the SDK's
    embedded shape, and most of this package's own tests) has no catalog and
    no cwd — there is no set of sessions to enumerate. Refusing names the
    construction gap; answering `[]` would tell a host that this child has
    no sessions, which is a different and false fact."""
    lonely = RPCHandler(session)

    response = await _call(lonely)

    assert "result" not in response
    assert response["error"]["code"] == dialect.INTERNAL_ERROR
    assert "list_sessions" in response["error"]["message"]
