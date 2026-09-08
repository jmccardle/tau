"""0.9.8's eleven verbs: the session tree and the extension system, on the wire.

Before them, `docs/VSCODE-HEAD.md` §6 measured the gap this file closes: 20 live
verbs, none of which read or wrote tree structure, so τ's differentiating feature
was reachable only from inside the Textual head. Eight mutations and three reads
later, every declared capability has a verb.

What this file owns, and what it deliberately does not. The OPERATIONS are tested
where they live — `tau_agent_core.tree_ops` and `AgentSession`'s extension methods
each have their own suites, and re-asserting "an elide refuses a resume point off
the anchor's path" here would pin the same behaviour in two places and let them
disagree. What is only true at this layer is the wiring: that each verb is
registered with a derived params schema, that E5 and D-7 are answered the same way
Tier B answers them, that a core `ValueError` reaches a host as INVALID_PARAMS
rather than as INTERNAL_ERROR, and that the result actually carries what the
schema promises.

Reference: docs/REMOTE-CONTROL.md §6; docs/VSCODE-HEAD.md §6; the E5 and
DURABILITY blocks in `rpc/commands.py`.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.capabilities import CAPABILITIES
from tau_agent_core.rpc import RPCHandler, commands, dialect
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model

TREE_MUTATIONS = (
    "navigate",
    "summarize_and_navigate",
    "elide_span",
    "commit_branch",
    "paste_subtree",
)
EXTENSION_MUTATIONS = ("enable_extension", "disable_extension", "reload_extension")
NEW_READS = ("complete_message_id", "list_managed_extensions", "get_extension_state")
NEW_VERBS = TREE_MUTATIONS + EXTENSION_MUTATIONS + NEW_READS


def _model() -> Model:
    return Model(
        id="m1",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m1",
        context_window=8192,
        max_tokens=256,
    )


class _DurableLog(InMemorySessionLog):
    """`InMemorySessionLog` that declares where it lives.

    `path` is not decoration: D-7 rule 1 asks every appending verb's log to
    declare a durable location, so a fake that wants the success path has to
    answer the same question the file-backed `Session` does. The bare
    `InMemorySessionLog` declares nothing, which is what `_ephemeral_handler`
    below uses to reach the refusal.
    """

    path = Path("/tmp/does-not-need-to-exist.jsonl")


def _session(log: InMemorySessionLog) -> AgentSession:
    return AgentSession(session_log=log, model=_model(), tools=[])


@pytest.fixture
def log() -> _DurableLog:
    return _DurableLog()


@pytest.fixture
def entries(log: _DurableLog) -> list[str]:
    """Three messages, so a navigate has somewhere to go and a paste something to copy."""
    return [
        log.append_message({"role": "user", "content": [{"type": "text", "text": "one"}]}),
        log.append_message({"role": "assistant", "content": [{"type": "text", "text": "two"}]}),
        log.append_message({"role": "user", "content": [{"type": "text", "text": "three"}]}),
    ]


@pytest.fixture
def handler(log: _DurableLog) -> RPCHandler:
    return RPCHandler(_session(log))


@pytest.fixture
def ephemeral_handler() -> RPCHandler:
    """A session on a log that declares no durable location at all."""
    return RPCHandler(_session(InMemorySessionLog()))


def _body(verb: str) -> str:
    """A verb's handler source with the `@command(...)` decorator sliced off.

    `inspect.getsource` of a decorated function returns the decorator too, and
    these decorators carry `notes` prose that NAMES the guards — so an unsliced
    search finds "turn_safety_guard" in a read that says it takes none.
    """
    handler = commands.COMMAND_TABLE[verb].handler
    assert handler is not None
    source = inspect.getsource(handler)
    return source[source.index("async def ") :]


async def _call(handler: RPCHandler, method: str, params: dict | None = None) -> dict:
    request: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    await handler._handle_request(request)
    out = []
    while not handler._output_queue.empty():
        out.append(await handler._output_queue.get())
    return out[-1]


# ── table wiring ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("verb", NEW_VERBS)
def test_each_verb_is_live_tier_c_and_schema_bearing(verb: str) -> None:
    entry = commands.COMMAND_TABLE[verb]
    assert entry.tier == "C"
    assert entry.since == "0.9.8"
    assert entry.handler is not None
    assert entry.declined_because is None
    assert entry.result_schema is not None


@pytest.mark.parametrize("verb", NEW_VERBS)
def test_each_verb_performs_a_declared_capability(verb: str) -> None:
    """The verb name IS the capability name — that is what makes the wire 1:1.

    Mutation this kills: registering a verb under a friendlier name than the
    capability it performs, which is how the two tables drifted before the
    registry existed.
    """
    assert verb in CAPABILITIES
    assert CAPABILITIES[verb].on_wire is True


@pytest.mark.parametrize("verb", TREE_MUTATIONS + EXTENSION_MUTATIONS)
def test_every_new_mutation_carries_the_cursor(verb: str) -> None:
    """E5 rule 1, for the eight. Required in the schema, not merely permitted.

    The three extension mutations append nothing, and their `cursor` is the live
    tip reported as a read — the same reading `set_auto_compaction`'s already has.
    Rule 3 is why they carry it anyway: absence is never a signal.
    """
    schema = commands.COMMAND_TABLE[verb].result_schema
    assert schema is not None
    assert "cursor" in schema["required"]


@pytest.mark.parametrize("verb", NEW_READS)
def test_no_new_read_carries_a_cursor(verb: str) -> None:
    """E5 rule 2. A host that wants the tip calls get_state."""
    schema = commands.COMMAND_TABLE[verb].result_schema
    assert schema is not None
    assert "cursor" not in schema["properties"]


def test_d7_is_answered_by_who_appends_and_nothing_else() -> None:
    """D-7 rule 1 over the new verbs, read out of the shipped handler sources.

    The five tree mutations append, so all five must reach
    `require_durable_session` — they do it through `tree_mutation_guard`, which is
    the one place that call appears for them. The three extension mutations append
    nothing, so none of them may call it: refusing an extension reload because the
    session is ephemeral would deny a working capability over a promise it never
    made, which is exactly the line `set_auto_compaction` drew in Tier B.

    Mutation this reddens: give `enable_extension` the tree guard, or open a tree
    verb's body with a bare `turn_safety_guard`.
    """
    for verb in TREE_MUTATIONS:
        assert "tree_mutation_guard" in _body(verb), verb
    for verb in EXTENSION_MUTATIONS:
        source = _body(verb)
        assert "turn_safety_guard" in source, verb
        assert "require_durable_session" not in source, verb
        assert "tree_mutation_guard" not in source, verb
    assert "require_durable_session" in inspect.getsource(commands.tree_mutation_guard)


@pytest.mark.parametrize("verb", NEW_READS)
def test_no_new_read_takes_a_guard(verb: str) -> None:
    """D-1 binds mutators. A read that took the turn lock would refuse a host
    asking what the tree contains while a turn is running, which is when it most
    wants to know."""
    source = _body(verb)
    assert "turn_safety_guard" not in source
    assert "require_durable_session" not in source


# ── the tree mutations ───────────────────────────────────────────────────────


async def test_navigate_moves_the_cursor_and_returns_the_new_context(
    handler: RPCHandler, log: _DurableLog, entries: list[str]
) -> None:
    """The result is the context the move PRODUCED, not the one it left.

    Mutation this kills: returning `session.messages` read before the append, or
    dropping `messages` and leaving the host to call get_messages — which would
    let it render the pre-navigate transcript in between.
    """
    response = await _call(handler, "navigate", {"target_id": entries[0]})

    assert response["result"]["cursor"] == entries[0]
    assert log.cursor == entries[0]
    assert [m["content"][0]["text"] for m in response["result"]["messages"]] == ["one"]


async def test_navigate_to_an_unknown_entry_is_invalid_params(handler: RPCHandler) -> None:
    """A core `ValueError` is a CALLER error here, not a crash.

    Mutation this kills: dropping the `except ValueError` from
    `tree_mutation_guard`, which would surface -32603 INTERNAL_ERROR and tell a
    host that τ broke rather than that it asked for an id that does not exist.
    """
    response = await _call(handler, "navigate", {"target_id": "no-such-entry"})

    assert response["error"]["code"] == dialect.INVALID_PARAMS
    assert "no-such-entry" in response["error"]["message"]


async def test_a_tree_mutation_refuses_an_unpersisted_session(
    ephemeral_handler: RPCHandler,
) -> None:
    """D-7 rule 1, reached rather than read off the source.

    A tree edit that dies with the process leaves a host holding a conversation it
    can never load again — a worse version of the promise `set_model` already
    refuses to make.
    """
    entry_id = ephemeral_handler.session.session_log.append_message(
        {"role": "user", "content": [{"type": "text", "text": "one"}]}
    )
    response = await _call(ephemeral_handler, "navigate", {"target_id": entry_id})

    assert response["error"]["code"] == dialect.SESSION_NOT_PERSISTED


async def test_paste_subtree_returns_minted_ids_and_leaves_the_cursor_alone(
    handler: RPCHandler, log: _DurableLog, entries: list[str]
) -> None:
    """The one tree mutation whose result is not a message list.

    A paste edits the TREE; what the model sees changes only when someone
    navigates onto the copy. So the cursor is where it was, and the ids are what
    a host needs to navigate there.

    Mutation this kills: giving `paste_subtree` the shared context result schema,
    which would report a re-render that did not happen.
    """
    before = log.cursor

    response = await _call(
        handler, "paste_subtree", {"source_id": entries[1], "target_id": entries[0]}
    )

    minted = response["result"]["minted_ids"]
    assert len(minted) == 2  # the assistant message and the user message under it
    assert response["result"]["cursor"] == before
    assert log.cursor == before
    assert {e["id"] for e in log.entries()} >= set(minted)


async def test_commit_branch_refuses_an_empty_selection(handler: RPCHandler) -> None:
    response = await _call(handler, "commit_branch", {"ids": [], "drop_context": False})

    assert response["error"]["code"] == dialect.INVALID_PARAMS


async def test_elide_span_refuses_an_unreachable_resume_point(
    handler: RPCHandler, log: _DurableLog, entries: list[str]
) -> None:
    """The refusal that matters most: an unreachable resume point empties the
    context silently rather than erroring, which is why `tree_ops` checks it and
    why the check has to survive the trip through this layer."""
    response = await _call(
        handler, "elide_span", {"anchor_id": entries[2], "first_kept_id": "not-an-entry"}
    )

    assert response["error"]["code"] == dialect.INVALID_PARAMS
    assert log.entries()[-1]["id"] == entries[2]


async def test_summarize_and_navigate_refuses_an_unknown_target_before_spending_a_completion(
    handler: RPCHandler,
) -> None:
    """`subtree_text` answers "" for an unknown id, so without the check in
    `tree_ops.summarize_and_navigate` this verb would spend a model call
    summarizing nothing and append the result.

    The refusal arrives with no provider configured to reach, which is the proof:
    a version that called the summarizer first would fail on the connection, not
    on the argument."""
    response = await _call(handler, "summarize_and_navigate", {"target_id": "no-such-entry"})

    assert response["error"]["code"] == dialect.INVALID_PARAMS
    assert "no-such-entry" in response["error"]["message"]


# ── the extension verbs ──────────────────────────────────────────────────────


async def test_an_extension_action_on_an_unknown_target_is_a_reportable_no_op(
    handler: RPCHandler,
) -> None:
    """`ok=false`, not an error. The distinction is the core's, and it survives:
    a target that is not loaded is something a host can show a person, where a
    file that no longer imports is a failure that raises."""
    response = await _call(handler, "enable_extension", {"path": "nothing.py"})

    assert response["result"]["ok"] is False
    assert response["result"]["action"] == "enable"
    assert response["result"]["path"] == "nothing.py"
    assert response["result"]["message"]


async def test_an_extension_action_runs_on_an_unpersisted_session(
    ephemeral_handler: RPCHandler,
) -> None:
    """D-7 rule 2, reached rather than read off the source. Extension state is
    runtime state and is never written to the log, so requiring durability here
    would deny a working capability over a promise the verb never made."""
    response = await _call(ephemeral_handler, "disable_extension", {"path": "nothing.py"})

    assert "error" not in response
    assert response["result"]["ok"] is False


async def test_get_extension_state_reports_extensions_and_errors_separately(
    handler: RPCHandler,
) -> None:
    """Both keys are always present. An empty `errors` list is a session where
    nothing failed to import; a missing one would be a host unable to tell that
    from a τ that does not report failures."""
    response = await _call(handler, "get_extension_state")

    assert response["result"]["extensions"] == []
    assert response["result"]["errors"] == []


async def test_list_managed_extensions_hands_back_the_boolean(handler: RPCHandler) -> None:
    """The whole reason this verb exists beside `enumerate_domain`: the domain has
    only value/label, so it renders enabled-ness into English inside the label."""
    schema = commands.COMMAND_TABLE["list_managed_extensions"].result_schema
    assert schema is not None
    assert "enabled" in schema["properties"]["extensions"]["description"]

    response = await _call(handler, "list_managed_extensions")
    assert response["result"]["extensions"] == []


# ── complete_message_id ──────────────────────────────────────────────────────


async def test_complete_message_id_returns_pairs_and_the_true_total(
    handler: RPCHandler, entries: list[str]
) -> None:
    """`total` is the count BEFORE the limit, so a host is told it is seeing a
    prefix rather than shown one silently (G3).

    Mutation this kills: reporting `len(matches)` as `total`, which reads as "the
    scope held exactly this much".
    """
    response = await _call(handler, "complete_message_id", {"limit": 2})

    result = response["result"]
    assert len(result["matches"]) == 2
    assert result["total"] > 2
    assert set(result["matches"][0]) == {"entry_id", "preview"}


async def test_complete_message_id_refuses_a_cursor_naming_no_entry(handler: RPCHandler) -> None:
    """Fail-Early: the alternative is an empty match list, which reads as "the
    scope held nothing" rather than "the scope does not exist"."""
    response = await _call(
        handler,
        "complete_message_id",
        {"scope": "ancestors_of_cursor", "cursor": "no-such-entry"},
    )

    assert response["error"]["code"] == dialect.INVALID_PARAMS
    assert "no-such-entry" in response["error"]["message"]


def test_the_scope_enum_on_the_wire_is_the_domains_own_values() -> None:
    """Derived, not retyped. The three scopes are `ConversationTree`'s, and the
    schema gets them from the `message_id_scope` domain rather than from a literal
    that could fall behind it."""
    from tau_agent_core.capabilities import DOMAINS

    schema = commands.COMMAND_TABLE["complete_message_id"].params_schema
    assert schema["properties"]["scope"]["enum"] == list(DOMAINS["message_id_scope"].values)


# ── the extension-config pair ────────────────────────────────────────────────

CONFIG_VERBS = ("get_extension_config", "set_extension_config")

_SCHEMA_EXT = """
CONFIG_SCHEMA = {"fields": [{"name": "ceiling", "kind": "number", "default": 1.0}]}


def register(api):
    pass
"""


@pytest.fixture
async def configured_handler(log: _DurableLog, tmp_path: Path) -> RPCHandler:
    """A handler whose session has one loaded extension declaring a CONFIG_SCHEMA."""
    ext = tmp_path / "budget.py"
    ext.write_text(_SCHEMA_EXT)
    session = _session(log)
    await session.load_extensions([str(ext)], discover=False)
    return RPCHandler(session)


@pytest.mark.parametrize("verb", CONFIG_VERBS)
def test_each_config_verb_is_live_tier_c_and_schema_bearing(verb: str) -> None:
    entry = commands.COMMAND_TABLE[verb]
    assert entry.handler is not None
    assert entry.tier == "C"
    assert entry.params_schema is not None
    assert entry.result_schema is not None
    assert verb in CAPABILITIES


def test_only_the_write_takes_the_turn_guard() -> None:
    """The read touches no registry, so guarding it would deny a listing mid-turn
    for nothing. The write reloads the extension, which rewrites the tool table."""
    assert "turn_safety_guard" not in _body("get_extension_config")
    assert "turn_safety_guard" in _body("set_extension_config")


def test_the_writes_params_are_hand_written_and_say_why() -> None:
    """`values` keys are whatever THIS extension declared, which no Domain can
    say — the same case `submit` is, and the capability records it as
    arguments=None rather than as an omission."""
    assert CAPABILITIES["set_extension_config"].arguments is None
    schema = commands.COMMAND_TABLE["set_extension_config"].params_schema
    assert sorted(schema["properties"]) == ["path", "values"]
    assert schema["required"] == ["path", "values"]


async def test_the_read_carries_the_schema_and_the_live_values(
    configured_handler: RPCHandler,
) -> None:
    response = await _call(configured_handler, "get_extension_config", {"path": "budget"})

    result = response["result"]
    assert [f["name"] for f in result["schema"]["fields"]] == ["ceiling"]
    assert result["values"] == {}
    assert result["path"].endswith("budget.py")


async def test_the_read_refuses_an_unknown_target(configured_handler: RPCHandler) -> None:
    """A read has no ok=false channel, so Fail-Early is the only honest answer."""
    response = await _call(configured_handler, "get_extension_config", {"path": "nope"})

    assert response["error"]["code"] == dialect.INVALID_PARAMS


async def test_the_write_applies_and_the_read_sees_it(configured_handler: RPCHandler) -> None:
    written = await _call(
        configured_handler, "set_extension_config", {"path": "budget", "values": {"ceiling": 7.5}}
    )
    assert written["result"]["action"] == "configure"
    assert written["result"]["ok"] is True
    assert "cursor" in written["result"]

    read_back = await _call(configured_handler, "get_extension_config", {"path": "budget"})
    assert read_back["result"]["values"] == {"ceiling": 7.5}


async def test_an_undeclared_key_reaches_the_host_as_invalid_params(
    configured_handler: RPCHandler,
) -> None:
    """A core `ValueError` is a host's malformed request, not τ's internal error."""
    response = await _call(
        configured_handler,
        "set_extension_config",
        {"path": "budget", "values": {"ceiling": 1.0, "typo": 2}},
    )

    assert response["error"]["code"] == dialect.INVALID_PARAMS
    assert "typo" in response["error"]["message"]
