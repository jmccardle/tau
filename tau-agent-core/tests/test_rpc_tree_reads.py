"""`get_tree` and `get_entry`: the shape half of the tree, on the wire.

0.9.8 put five tree MUTATIONS on the wire and no read of the structure they act
on, so an out-of-process head could navigate, elide, branch and paste, and had no
way to show anyone what it would be doing it to — `docs/VSCODE-HEAD.md` §6 named
that as the one remaining structural gap and named this pair as what closes it.

What this file owns is the WIRING and the PROJECTION: that each node carries the
facts a browser colours a row with, that the order is the order a browser draws,
and that a bad id refuses rather than answering null. The tree ALGEBRA is tested
where it lives (`test_conversation_tree.py`), and re-asserting the fold's rules
here would pin them in two places and let them disagree.

Reference: docs/VSCODE-HEAD.md §6; docs/TREE-EDITOR-MANUAL.md §6 (the pairing
rule `tool_call_ids` / `tool_call_id` serve).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.capabilities import CAPABILITIES
from tau_agent_core.conversation_tree import COPYABLE_KINDS
from tau_agent_core.rpc import RPCHandler, commands
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model

TREE_READS = ("get_tree", "get_entry")


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
    """`InMemorySessionLog` that declares where it lives."""

    path = Path("/tmp/does-not-need-to-exist.jsonl")


@pytest.fixture
def log() -> _DurableLog:
    return _DurableLog()


@pytest.fixture
def handler(log: _DurableLog) -> RPCHandler:
    return RPCHandler(AgentSession(session_log=log, model=_model(), tools=[]))


@pytest.fixture
def entries(handler: RPCHandler, log: _DurableLog) -> dict[str, str]:
    """A user turn with a tool call and its result, appended AFTER the agent_spec.

    Depends on `handler` for the ordering, not for the handler: constructing an
    `AgentSession` appends an `agent_spec` entry, so a fixture that appended
    first would put the session's own root in the middle of the conversation.
    """
    user = log.append_message({"role": "user", "content": [{"type": "text", "text": "one"}]})
    called = log.append_message(
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "call-1", "name": "ls", "arguments": {}}],
        }
    )
    answered = log.append_message(
        {
            "role": "toolResult",
            "tool_call_id": "call-1",
            "content": [{"type": "text", "text": "a.txt"}],
        }
    )
    return {"user": user, "called": called, "answered": answered}


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


@pytest.mark.parametrize("verb", TREE_READS)
def test_each_read_is_live_tier_c_and_schema_bearing(verb: str) -> None:
    entry = commands.COMMAND_TABLE[verb]
    assert entry.tier == "C"
    assert entry.since == "0.10.1"
    assert entry.handler is not None
    assert entry.declined_because is None
    assert entry.result_schema is not None


@pytest.mark.parametrize("verb", TREE_READS)
def test_each_read_performs_a_declared_read_capability(verb: str) -> None:
    assert verb in CAPABILITIES
    assert CAPABILITIES[verb].on_wire is True
    assert CAPABILITIES[verb].kind == "read"


# ── get_tree ─────────────────────────────────────────────────────────────────


async def test_get_tree_answers_one_node_per_entry_in_draw_order(
    handler: RPCHandler, entries: dict[str, str]
) -> None:
    """Preorder over the tree, which for a linear log is append order.

    The `agent_spec` `AgentSession.__init__` appends is the first row and IS
    drawn: it records what the model was told, which is a real change to the
    conversation and one a reader browsing history wants to see.
    """
    answer = await _call(handler, "get_tree")
    result = answer["result"]
    assert result["count"] == len(result["nodes"]) == 4
    assert result["nodes"][0]["kind"] == "customEntry"
    assert [node["entry_id"] for node in result["nodes"][1:]] == [
        entries["user"],
        entries["called"],
        entries["answered"],
    ]


async def test_a_fork_draws_its_branches_under_their_shared_parent(
    handler: RPCHandler, log: _DurableLog, entries: dict[str, str]
) -> None:
    """A second child of the user message is a sibling, and both are its children.

    Mutation this kills: projecting the log in APPEND order and calling it a
    tree. Append order puts the second branch last; draw order puts it directly
    under the parent it hangs from, ahead of nothing, and the parent links are
    what a host rebuilds the shape from.
    """
    log.append_navigate(entries["user"])
    second = log.append_message(
        {"role": "assistant", "content": [{"type": "text", "text": "other"}]}
    )
    result = (await _call(handler, "get_tree"))["result"]
    nodes = {node["entry_id"]: node for node in result["nodes"]}
    assert nodes[second]["parent_id"] == entries["user"]
    assert nodes[entries["called"]]["parent_id"] == entries["user"]
    order = [node["entry_id"] for node in result["nodes"]]
    # Preorder, so a subtree is contiguous: the first branch is finished before
    # the second one starts. Append order would interleave them.
    assert order.index(entries["called"]) < order.index(entries["answered"]) < order.index(second)


async def test_the_pairing_facts_ride_along(handler: RPCHandler, entries: dict[str, str]) -> None:
    """A mark expands over a tool group, and this is what a head expands it from.

    Without these two keys an out-of-process head would have to pull every
    message body to discover which assistant declared which call — the pull
    `preview` exists to avoid.
    """
    nodes = {
        node["entry_id"]: node for node in (await _call(handler, "get_tree"))["result"]["nodes"]
    }
    assert nodes[entries["called"]]["tool_call_ids"] == ["call-1"]
    assert nodes[entries["called"]]["tool_call_id"] is None
    assert nodes[entries["answered"]]["tool_call_id"] == "call-1"
    assert nodes[entries["answered"]]["tool_call_ids"] == []


async def test_the_cursor_is_named_once_in_the_nodes_and_once_beside_them(
    handler: RPCHandler, log: _DurableLog, entries: dict[str, str]
) -> None:
    result = (await _call(handler, "get_tree"))["result"]
    assert result["cursor"] == log.cursor
    flagged = [node["entry_id"] for node in result["nodes"] if node["is_cursor"]]
    assert flagged == [log.cursor]


async def test_a_folds_boundary_is_on_the_node_that_folds(
    handler: RPCHandler, log: _DurableLog, entries: dict[str, str]
) -> None:
    """`first_kept_id` is the whole of what a head paints `folded` from.

    Mutation this kills: dropping the key and leaving a head to infer the span
    from the anchor's position, which is the guess this field exists to remove.
    """
    await _call(
        handler,
        "elide_span",
        {"anchor_id": entries["answered"], "first_kept_id": entries["called"]},
    )
    nodes = {
        node["entry_id"]: node for node in (await _call(handler, "get_tree"))["result"]["nodes"]
    }
    anchors = [node for node in nodes.values() if node["kind"] == "elide"]
    assert len(anchors) == 1
    assert anchors[0]["first_kept_id"] == entries["called"]
    assert nodes[entries["user"]]["first_kept_id"] is None


async def test_copyable_reports_the_paste_source_rule(
    handler: RPCHandler, log: _DurableLog, entries: dict[str, str]
) -> None:
    """A host greys an illegal paste source from this rather than from its own tuple."""
    await _call(
        handler,
        "elide_span",
        {"anchor_id": entries["answered"], "first_kept_id": entries["called"]},
    )
    nodes = (await _call(handler, "get_tree"))["result"]["nodes"]
    for node in nodes:
        assert node["copyable"] == (node["kind"] in COPYABLE_KINDS), node["kind"]
    assert any(node["copyable"] is False for node in nodes)


async def test_a_session_with_no_conversation_answers_its_one_bookkeeping_row(
    handler: RPCHandler,
) -> None:
    """Not an error, and not empty either.

    `AgentSession.__init__` appends an `agent_spec`, so the smallest real tree is
    one node. A head opening a browser on a fresh session draws that row rather
    than an empty panel it would read as a failure.
    """
    result = (await _call(handler, "get_tree"))["result"]
    assert result["count"] == 1
    assert result["nodes"][0]["kind"] == "customEntry"
    assert result["nodes"][0]["parent_id"] is None
    assert result["cursor"] == result["nodes"][0]["entry_id"]


async def test_the_read_answers_while_a_turn_is_running(handler: RPCHandler) -> None:
    """No D-1 turn_safety_guard: a browser opened mid-turn shows the tree as it stands."""
    body = commands.COMMAND_TABLE["get_tree"].handler
    assert body is not None
    import inspect

    source = inspect.getsource(body)
    assert "turn_safety_guard" not in source[source.index("async def ") :]


# ── get_entry ────────────────────────────────────────────────────────────────


async def test_get_entry_hands_back_the_raw_stored_entry(
    handler: RPCHandler, entries: dict[str, str]
) -> None:
    """Raw, in the stored camelCase shape — a detail pane renders it, nothing else does."""
    result = (await _call(handler, "get_entry", {"entry_id": entries["called"]}))["result"]
    entry = result["entry"]
    assert entry["id"] == entries["called"]
    assert entry["parentId"] == entries["user"]
    assert entry["type"] == "message"
    assert entry["message"]["content"][0]["name"] == "ls"


async def test_get_entry_reaches_a_node_off_the_active_path(
    handler: RPCHandler, log: _DurableLog, entries: dict[str, str]
) -> None:
    """The reason this is not `get_messages`.

    A browser's cursor is very often on a node the active path does not contain,
    and `get_messages` answers only for the path.
    """
    log.append_navigate(entries["user"])
    abandoned = entries["answered"]
    result = (await _call(handler, "get_entry", {"entry_id": abandoned}))["result"]
    assert result["entry"]["id"] == abandoned


async def test_an_unknown_id_refuses_rather_than_answering_null(handler: RPCHandler) -> None:
    """Fail-Early: a null entry reads as 'this node is empty', which is a different fact."""
    answer = await _call(handler, "get_entry", {"entry_id": "no-such-entry"})
    assert "result" not in answer
    assert answer["error"]["code"] == -32602
    assert "no-such-entry" in answer["error"]["message"]


async def test_a_missing_id_is_refused_by_the_schema(handler: RPCHandler) -> None:
    answer = await _call(handler, "get_entry", {})
    assert answer["error"]["code"] == -32602
    assert "entry_id" in answer["error"]["message"]
