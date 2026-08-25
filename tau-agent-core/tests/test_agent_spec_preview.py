"""B1-e part 2: what an ``agent_spec`` node says in a tree browser (W2).

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
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools.base import AgentTool, ToolDefinition


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


def _previews(log: InMemorySessionLog) -> list[str]:
    """Every ``agent_spec`` row's preview, in log order."""
    tree = ConversationTree(log.entries(), log.cursor)
    nodes = {}

    def _collect(node) -> None:
        nodes[node.id] = node
        for child in node.children:
            _collect(child)

    for root in tree.tree():
        _collect(root)
    return [
        nodes[e["id"]].preview
        for e in log.entries()
        if e.get("type") == "customEntry" and e.get("customType") == "agent_spec"
    ]


def _spec(log: InMemorySessionLog, **overrides: Any) -> str:
    """Append a hand-built ``agent_spec`` payload; returns its entry id."""
    data: dict[str, Any] = {
        "model": {"id": "model-a", "provider": "openai", "context_window": 128000},
        "system_prompt_digest": "digest-a",
        "tools": ["read", "grep"],
        "extensions": [],
        "cwd": "/repo",
    }
    data.update(overrides)
    return log.append_custom_entry("agent_spec", data)


# --- the first spec on a path: say what the frame IS -------------------------


def test_the_first_spec_names_the_model_and_the_tool_set():
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model("gpt-4o"),
        tools=[_tool("read"), _tool("grep")],
    )
    # Written by the real W2 code path, not a hand-built payload.
    assert _previews(session.session_log) == ["agent_spec: gpt-4o · 2 tools: read, grep"]


def test_a_tool_less_spec_says_so_rather_than_showing_an_empty_list():
    session = AgentSession(session_log=InMemorySessionLog(), model=_model("gpt-4o"), tools=[])
    assert _previews(session.session_log) == ["agent_spec: gpt-4o · no tools"]


def test_a_long_tool_set_is_truncated_behind_its_count():
    log = InMemorySessionLog()
    _spec(log, tools=["read", "write", "edit", "bash", "ls", "grep"])
    assert _previews(log) == ["agent_spec: model-a · 6 tools: read, write, edit, bash +2 more"]


# --- a second spec on the same path: say what CHANGED ------------------------


def test_a_model_swap_reads_as_a_model_swap():
    """The real trigger: ``set_model`` is the one runtime spec swap the class has."""
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model("model-a"),
        tools=[_tool("read")],
        model_resolver=lambda name: _model(name),
    )
    session.set_model("model-b")
    previews = _previews(session.session_log)
    assert previews[0] == "agent_spec: model-a · 1 tool: read"
    assert previews[1] == "agent_spec: model model-a → model-b"


def test_a_tool_set_change_is_reported_as_a_delta():
    log = InMemorySessionLog()
    _spec(log, tools=["read", "grep"])
    _spec(log, tools=["read", "write", "bash"])
    assert _previews(log)[1] == "agent_spec: tools +write +bash -grep"


def test_model_and_tools_changing_together_are_both_named():
    log = InMemorySessionLog()
    _spec(log)
    _spec(log, model={"id": "model-b"}, tools=["read", "grep", "bash"])
    assert _previews(log)[1] == "agent_spec: model model-a → model-b; tools +bash"


def test_a_new_system_prompt_is_named_but_never_quoted():
    """The record carries a DIGEST, deliberately (the prompt routinely holds a
    repo's project instructions); the preview can only report that it changed."""
    log = InMemorySessionLog()
    _spec(log, system_prompt_digest="digest-a")
    _spec(log, system_prompt_digest="digest-b")
    preview = _previews(log)[1]
    assert preview == "agent_spec: new system prompt"
    assert "digest-b" not in preview


def test_extensions_are_a_count_not_a_wall_of_paths():
    log = InMemorySessionLog()
    _spec(log, extensions=[])
    _spec(log, extensions=["/home/u/.tau/extensions/a.py", "/home/u/.tau/extensions/b.py"])
    preview = _previews(log)[1]
    assert preview == "agent_spec: extensions 0 → 2"
    assert ".py" not in preview


def test_a_changed_cwd_is_named():
    """§5 "The filesystem is frame, not path": which directory a span of turns ran
    against is exactly what W2 says the record is for."""
    log = InMemorySessionLog()
    _spec(log, cwd="/repo")
    _spec(log, cwd="/repo/worktrees/fix")
    assert _previews(log)[1] == "agent_spec: cwd /repo → /repo/worktrees/fix"


def test_an_unchanged_re_record_says_unchanged():
    """Informative for the reader hunting a swap: this node is not the one."""
    log = InMemorySessionLog()
    _spec(log)
    _spec(log)
    assert _previews(log)[1] == "agent_spec: model-a · 2 tools: read, grep (unchanged)"


# --- the delta is against the ANCESTOR, not against load order ---------------


def test_the_delta_is_computed_against_the_nearest_ancestor_spec():
    """I1: a leaf's context is its ancestor chain and nothing else, so a spec on a
    sibling branch never governed these turns and must not be the baseline."""
    log = InMemorySessionLog()
    root = _spec(log, model={"id": "model-a"})
    log.append_message({"role": "user", "content": "hi"})

    # A sibling branch off the root with a completely different frame…
    log.append_at(
        root,
        "customEntry",
        {"customType": "agent_spec", "data": {"model": {"id": "sideshow"}, "tools": []}},
    )

    # …and a swap on the primary line. The delta must read against model-a.
    _spec(log, model={"id": "model-b"}, tools=["read", "grep"])

    assert _previews(log)[-1] == "agent_spec: model model-a → model-b"


def test_a_root_spec_has_no_previous_and_states_the_frame():
    log = InMemorySessionLog()
    _spec(log, model={"id": "model-a"}, tools=["read"])
    assert _previews(log)[0] == "agent_spec: model-a · 1 tool: read"


# --- hand-written / future logs are reported, not guessed at -----------------


def test_a_payload_with_no_frame_says_so():
    log = InMemorySessionLog()
    log.append_custom_entry("agent_spec", {})
    assert _previews(log) == ["agent_spec: (no model recorded) · no tools"]


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
            "timestamp": 0,
        }
    ]
    assert ConversationTree(entries, "e1").tree()[0].preview == "agent_spec: no frame recorded"


# --- decision 3: still a record, still not model input -----------------------


def test_the_preview_changes_nothing_about_the_fold():
    """Rendering a row must not make the node reachable by the model."""
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model("gpt-4o"),
        tools=[_tool("read")],
        model_resolver=lambda name: _model(name),
    )
    session.set_model("model-b")
    session.session_log.append_message({"role": "user", "content": "hi"})
    assert [m["content"] for m in session.messages] == ["hi"]
    assert len(_previews(session.session_log)) == 2


def test_other_custom_entries_keep_their_old_row():
    """The preview is agent_spec-specific; extension backplane state is untouched."""
    log = InMemorySessionLog()
    log.append_custom_entry("todo_state", {"items": []})
    tree = ConversationTree(log.entries(), log.cursor)
    assert tree.tree()[0].preview == "customEntry: todo_state"
