"""What a frame record says in a tree browser: a config entry, or a legacy ``agent_spec``.

τ writes config entries now (docs/CURSORS.md §5); logs written before that carry
``agent_spec`` records, which still render with a delta against their nearest
ancestor. Both are rows a reader scans for "which agent spoke here".

W2 writes the node for one stated reason — *"Turns 1-5 from a read-only reviewer
and 6-10 from a full-tool builder are indistinguishable … the loss you feel the
first time you debug why the agent did not run the tests"* — and the browser
rendered it as ``customEntry: agent_spec``, which is that loss with a label on it.
``ConversationTree._preview_of`` now names the frame, and names the DELTA when a
second spec appears on the same path, because a swap is what a reader scanning a
transcript is looking for.

The prohibition this file also pins: decision 3 says ``agent_spec`` is a RECORD,
never a contract, and "must not grow a reader that reconstructs from it". A preview
string is rendering, not reconstruction — so the preview must never be the thing a
session is rebuilt from, and the node must still contribute nothing to context.

Reference: NODE-ADDRESSABLE-AGENTS.md W2, decision 3 (and T4 for the fold).
"""

from __future__ import annotations

from typing import Any

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.cursor import Cursor
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools.base import AgentTool, ToolDefinition

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000


def _model(model_id: str = "model-a") -> Model:
    return Model(
        id=model_id,
        name=model_id,
        api="openai-completions",
        provider="openai",
        base_url="http://localhost",
        context_window=128000,
        max_tokens=4096,
    )


def _tool(name: str) -> AgentTool:
    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name,
            description=name,
            parameters={"type": "object", "properties": {}, "required": []},
            execute=lambda ctx: "ok",
        )
    )


def _previews(cursor: Cursor, custom_type: str = "agent_spec") -> list[str]:
    """Every ``custom_type`` row's preview, in log order."""
    tree = cursor.tree()
    nodes = {}

    def _collect(node) -> None:
        nodes[node.id] = node
        for child in node.children:
            _collect(child)

    for root in tree.tree():
        _collect(root)
    return [
        nodes[e["id"]].preview
        for e in cursor.entries()
        if e.get("type") == "customEntry" and e.get("customType") == custom_type
    ]


async def _spec(cursor: Cursor, **overrides: Any) -> str:
    """Append a hand-built ``agent_spec`` payload; returns its entry id."""
    data: dict[str, Any] = {
        "model": {"id": "model-a", "provider": "openai", "context_window": 128000},
        "system_prompt_digest": "digest-a",
        "tools": ["read", "grep"],
        "extensions": [],
        "cwd": "/repo",
    }
    data.update(overrides)
    return await cursor.append_custom_entry("agent_spec", data)


# --- the first spec on a path: say what the frame IS -------------------------


async def test_a_sessions_first_config_names_the_model_and_the_tool_set():
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model("gpt-4o"),
        tools=[_tool("read"), _tool("grep")],
    )
    await session.start()
    (preview,) = _previews(session.cursor, "config")
    assert preview.startswith("config: model gpt-4o; thinking off; 2 tools: read, grep")


async def test_a_tool_less_config_says_so_rather_than_showing_an_empty_list():
    session = AgentSession(session_log=InMemorySessionLog(), model=_model("gpt-4o"), tools=[])
    await session.start()
    (preview,) = _previews(session.cursor, "config")
    assert "no tools" in preview


async def test_a_long_tool_set_is_truncated_behind_its_count():
    cursor = Cursor.newest(InMemorySessionLog())
    await _spec(cursor, tools=["read", "write", "edit", "bash", "ls", "grep"])
    assert _previews(cursor) == ["agent_spec: model-a · 6 tools: read, write, edit, bash +2 more"]


# --- a second spec on the same path: say what CHANGED ------------------------


async def test_a_model_swap_reads_as_a_model_swap():
    """A config entry carries only what changed, so the swap's row names only the model."""
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model("model-a"),
        tools=[_tool("read")],
        model_resolver=lambda name: _model(name),
    )
    await session.start()
    session.set_model("model-b")
    await session.start()
    previews = _previews(session.cursor, "config")
    assert previews[0].startswith("config: model model-a")
    assert previews[1] == "config: model model-b"


async def test_a_tool_set_change_is_reported_as_a_delta():
    cursor = Cursor.newest(InMemorySessionLog())
    await _spec(cursor, tools=["read", "grep"])
    await _spec(cursor, tools=["read", "write", "bash"])
    assert _previews(cursor)[1] == "agent_spec: tools +write +bash -grep"


async def test_model_and_tools_changing_together_are_both_named():
    cursor = Cursor.newest(InMemorySessionLog())
    await _spec(cursor)
    await _spec(cursor, model={"id": "model-b"}, tools=["read", "grep", "bash"])
    assert _previews(cursor)[1] == "agent_spec: model model-a → model-b; tools +bash"


async def test_a_new_system_prompt_is_named_but_never_quoted():
    """The record carries a DIGEST, deliberately (the prompt routinely holds a
    repo's project instructions); the preview can only report that it changed."""
    cursor = Cursor.newest(InMemorySessionLog())
    await _spec(cursor, system_prompt_digest="digest-a")
    await _spec(cursor, system_prompt_digest="digest-b")
    preview = _previews(cursor)[1]
    assert preview == "agent_spec: new system prompt"
    assert "digest-b" not in preview


async def test_extensions_are_a_count_not_a_wall_of_paths():
    cursor = Cursor.newest(InMemorySessionLog())
    await _spec(cursor, extensions=[])
    await _spec(cursor, extensions=["/home/u/.tau/extensions/a.py", "/home/u/.tau/extensions/b.py"])
    preview = _previews(cursor)[1]
    assert preview == "agent_spec: extensions 0 → 2"
    assert ".py" not in preview


async def test_a_changed_cwd_is_named():
    """§5 "The filesystem is frame, not path": which directory a span of turns ran
    against is exactly what W2 says the record is for."""
    cursor = Cursor.newest(InMemorySessionLog())
    await _spec(cursor, cwd="/repo")
    await _spec(cursor, cwd="/repo/worktrees/fix")
    assert _previews(cursor)[1] == "agent_spec: cwd /repo → /repo/worktrees/fix"


async def test_an_unchanged_re_record_says_unchanged():
    """Informative for the reader hunting a swap: this node is not the one."""
    cursor = Cursor.newest(InMemorySessionLog())
    await _spec(cursor)
    await _spec(cursor)
    assert _previews(cursor)[1] == "agent_spec: model-a · 2 tools: read, grep (unchanged)"


# --- the delta is against the ANCESTOR, not against load order ---------------


async def test_the_delta_is_computed_against_the_nearest_ancestor_spec():
    """I1: a leaf's context is its ancestor chain and nothing else, so a spec on a
    sibling branch never governed these turns and must not be the baseline."""
    cursor = Cursor.newest(InMemorySessionLog())
    root = await _spec(cursor, model={"id": "model-a"})
    await cursor.append_message({"role": "user", "content": "hi"})

    # A sibling branch off the root with a completely different frame…
    await cursor.log.append_at(
        root,
        "customEntry",
        {"customType": "agent_spec", "data": {"model": {"id": "sideshow"}, "tools": []}},
    )

    # …and a swap on the cursor's own path. The delta must read against model-a.
    await _spec(cursor, model={"id": "model-b"}, tools=["read", "grep"])

    assert _previews(cursor)[-1] == "agent_spec: model model-a → model-b"


async def test_a_root_spec_has_no_previous_and_states_the_frame():
    cursor = Cursor.newest(InMemorySessionLog())
    await _spec(cursor, model={"id": "model-a"}, tools=["read"])
    assert _previews(cursor)[0] == "agent_spec: model-a · 1 tool: read"


# --- hand-written / future logs are reported, not guessed at -----------------


async def test_a_payload_with_no_frame_says_so():
    cursor = Cursor.newest(InMemorySessionLog())
    await cursor.append_custom_entry("agent_spec", {})
    assert _previews(cursor) == ["agent_spec: (no model recorded) · no tools"]


def test_a_non_dict_payload_is_reported_rather_than_crashing_the_browser():
    """Same policy as ``_splice_span_phrase``'s unreachable boundary: this flow cannot
    write such a node, a hand-written log can, and a browser that raises on one is
    a browser that cannot show the log you are debugging."""
    entries = [
        {
            "id": "e1",
            "parentId": None,
            "type": "customEntry",
            "customType": "agent_spec",
            "data": "not a dict",
            "timestamp": _TS,
        }
    ]
    assert ConversationTree(entries, "e1").tree()[0].preview == "agent_spec: no frame recorded"


# --- decision 3: still a record, still not model input -----------------------


async def test_the_preview_changes_nothing_about_the_fold():
    """Rendering a row must not make the node reachable by the model."""
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model("gpt-4o"),
        tools=[_tool("read")],
        model_resolver=lambda name: _model(name),
    )
    await session.start()
    session.set_model("model-b")
    await session.start()
    await session.cursor.append_message({"role": "user", "content": "hi"})
    assert [m["content"] for m in session.messages] == ["hi"]
    assert len(_previews(session.cursor, "config")) == 2


async def test_other_custom_entries_name_their_type_and_summarize_their_payload():
    """The agent_spec row stays agent_spec-specific; the rest say what they hold.

    Until docs/EXTENSION-MESSAGES.md §3 this asserted ``customEntry: todo_state`` —
    the kind with a label on it, which is the loss ``_agent_spec_preview`` was
    written to fix for one kind and left in place for every other.
    """
    cursor = Cursor.newest(InMemorySessionLog())
    await cursor.append_custom_entry("todo_state", {"items": []})
    assert cursor.tree().tree()[0].preview == "todo_state — items=[0]"
