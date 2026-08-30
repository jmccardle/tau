"""ctx.spawn_branch — the C2/W14 branch sub-agent (JMFTS-INTEGRATION-PLAN.md §9.2).

The live end-to-end path (a real sub-agent, a real tool call, a real verdict) was
verified against the llama.cpp box during W14. What is pinned here is the behaviour
that must not SILENTLY regress: tool scoping, failure containment, and the structural
isolation of a branch's work.
"""

from __future__ import annotations

import asyncio

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools.base import AgentToolResult
from tau_llm.types import Model


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


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.label = name
        self.description = f"the {name} tool"
        self.parameters = {"type": "object", "properties": {}}
        self.calls: list[dict] = []

    async def execute(self, tool_call_id, args, signal=None, on_update=None):
        self.calls.append(args)
        return AgentToolResult(
            tool_name=self.name,
            tool_call_id=tool_call_id,
            content=[{"type": "text", "text": "ok"}],
        ).model_dump()


def _session(tools: list) -> tuple[AgentSession, InMemorySessionLog]:
    log = InMemorySessionLog()
    session = AgentSession(
        session_log=log, model=_model(), system_prompt="", tools=tools, api_key="k"
    )
    log.append_message({"role": "user", "content": [{"type": "text", "text": "shared prefix"}]})
    return session, log


async def test_asking_for_an_unavailable_tool_raises_before_any_model_call():
    """Fail-Early, and BEFORE the model runs. A sub-agent silently missing a tool it was
    told to use does not error — it returns a confident wrong answer ("I couldn't find
    it"), which reads exactly like a real verdict."""
    session, log = _session([_Tool("lookup")])
    before = [e["id"] for e in log.entries()]

    with pytest.raises(ValueError, match="not available on this session"):
        await session._extension_api.context.spawn_branch(
            log.cursor, "go", tools=["lookup", "bash"]
        )

    assert [e["id"] for e in log.entries()] == before, "nothing was written"


async def test_the_allowlist_is_a_hard_filter(monkeypatch):
    """Sub-agents share the process and cwd, so 'inherit the parent's tools' would hand a
    retrieval evaluator `write` and `bash`. Only the named tools reach the sub-agent."""
    lookup, danger = _Tool("lookup"), _Tool("write")
    session, log = _session([lookup, danger])

    captured: dict = {}

    async def _fake_prompt(self, text, images=None, context=None):
        captured["tools"] = [t.name for t in self._tools]
        return []

    monkeypatch.setattr(AgentSession, "prompt", _fake_prompt)
    await session._extension_api.context.spawn_branch(log.cursor, "go", tools=["lookup"])

    assert captured["tools"] == ["lookup"], "write must not be handed to the sub-agent"


def _extension_session() -> tuple[AgentSession, InMemorySessionLog]:
    """A session in the shape a host uses when it owns every tool it offers.

    ``tools=[]`` plus ``no_tools="builtin"`` suppresses the built-ins and leaves
    extension registrations alone — the supported way to hand a model a small,
    purpose-built vocabulary and nothing else. It is also the shape under which
    ``session._tools`` is empty while the model is being offered two tools.
    """
    log = InMemorySessionLog()
    session = AgentSession(
        session_log=log,
        model=_model(),
        system_prompt="",
        tools=[],
        no_tools="builtin",
        api_key="k",
    )

    async def _execute(tool_call_id, params, signal, on_update, ctx):
        return {"content": [{"type": "text", "text": "ok"}]}

    for name in ("say", "remember"):
        session._extension_api.register_tool(
            {
                "name": name,
                "description": f"the {name} tool",
                "parameters": {"type": "object", "properties": {}},
                "execute": _execute,
            }
        )
    log.append_message({"role": "user", "content": [{"type": "text", "text": "shared prefix"}]})
    return session, log


async def test_a_branch_may_hold_a_tool_that_came_from_an_extension(monkeypatch):
    """The allowlist must be checked against what the SESSION has, not against `_tools`.

    `_tools` is only the constructor's list; an extension's registrations are merged in
    by `_build_turn_tools`, which is what every turn's loop is built from. Reading
    `_tools` here meant that on a session whose tools all arrive that way — `tools=[]`
    plus `no_tools="builtin"` — every non-empty allowlist raised "not available on this
    session" naming a tool the model had just successfully called, and `tools=[]` (a
    sub-agent that can think and do nothing) was the only value that did not.
    """
    session, log = _extension_session()
    assert [t.name for t in session._tools] == [], "the shape the bug needs"

    captured: dict = {}

    async def _fake_prompt(self, text, images=None, context=None):
        captured["tools"] = [t.name for t in self._tools]
        return []

    monkeypatch.setattr(AgentSession, "prompt", _fake_prompt)
    result = await session._extension_api.context.spawn_branch(log.cursor, "go", tools=["remember"])

    assert result.ok
    assert captured["tools"] == ["remember"], "scoping still applies — `say` must not cross"


async def test_the_refusal_still_fires_and_now_names_the_extension_tools():
    """Fail-Early is unchanged; only the list it is checked against is corrected.

    The `available:` list is the assertion that matters. It used to read `[]` on this
    session, which is what made the error unactionable — it said "not available on this
    session" about a session where two tools were.
    """
    session, log = _extension_session()

    with pytest.raises(ValueError, match=r"\['bash'\].*available: \['remember', 'say'\]"):
        await session._extension_api.context.spawn_branch(
            log.cursor, "go", tools=["remember", "bash"]
        )


async def test_a_failing_sub_agent_is_contained_and_marks_its_branch(monkeypatch):
    """§9.2/5. A raise here would mean one bad evaluator in a fan-out kills the whole
    primary turn. The failure comes back as a RESULT, and the branch records it."""
    session, log = _session([])
    tip = log.cursor

    async def _boom(self, text, images=None, context=None):
        raise RuntimeError("the sub-agent exploded")

    monkeypatch.setattr(AgentSession, "prompt", _boom)
    result = await session._extension_api.context.spawn_branch(tip, "go", tools=[])

    assert result.ok is False
    assert "exploded" in (result.error or "")
    assert log.cursor == tip, "a failed branch must not move the primary cursor"

    marks = [e for e in log.entries() if e.get("customType") == "branch_error"]
    assert len(marks) == 1, "the branch is marked, so the failure is visible in the tree"
    # The lane is in the mark's PAYLOAD (what the branch was), not a marker on the
    # entry (who wrote it) — docs/LANE-REMOVAL.md §4.
    assert marks[0]["data"]["lane"] == result.lane
    assert "branchOf" not in marks[0]


async def test_the_sub_agents_work_never_reaches_the_spawners_context(
    monkeypatch,
):
    session, log = _session([])
    tip = log.cursor

    async def _work(self, text, images=None, context=None):
        # the sub-agent writes through its OWN log, which is the BranchView
        self._session_log.append_message(
            {"role": "assistant", "content": [{"type": "text", "text": "SUB-AGENT ONLY"}]}
        )
        return []

    monkeypatch.setattr(AgentSession, "prompt", _work)
    result = await session._extension_api.context.spawn_branch(tip, "go", tools=[])

    assert result.ok is True
    assert all("branchOf" not in e for e in log.entries()), "no durable branch marker"

    assert log.cursor == tip, "the primary cursor did not move"
    primary = ConversationTree(log.entries(), log.cursor).context_for()
    assert "SUB-AGENT ONLY" not in str(primary)

    # ...and the spawner can still read the verdict back, via the branch's leaf.
    assert result.leaf is not None
    assert "SUB-AGENT ONLY" in str(
        ConversationTree(log.entries(), result.leaf).context_for(result.leaf)
    )


async def test_system_prompt_defaults_to_the_parents_but_can_be_overridden(monkeypatch):
    """W1 (NODE-ADDRESSABLE-AGENTS.md §5/W1): ``system_prompt`` was hardcoded to
    ``session._system_prompt`` with no override, which was the one concrete blocker on
    'fork at a node with a different spec'. Default behaviour must be unchanged; passing
    a string must reach the sub-agent's ``AgentSession`` instead."""
    session, log = _session([])
    captured: dict = {}

    async def _fake_prompt(self, text, images=None, context=None):
        captured["system_prompt"] = self._system_prompt
        return []

    monkeypatch.setattr(AgentSession, "prompt", _fake_prompt)

    await session._extension_api.context.spawn_branch(log.cursor, "go", tools=[])
    assert captured["system_prompt"] == "", "unchanged default: inherits the parent's prompt"

    await session._extension_api.context.spawn_branch(
        log.cursor, "go", tools=[], system_prompt="you are a critic"
    )
    assert captured["system_prompt"] == "you are a critic"


async def test_max_turns_bounds_the_sub_agent(monkeypatch):
    """A looping sub-agent must not be able to burn the primary run's budget."""
    session, log = _session([])
    captured: dict = {}

    async def _fake_prompt(self, text, images=None, context=None):
        captured["max_turns"] = self._max_turns
        return []

    monkeypatch.setattr(AgentSession, "prompt", _fake_prompt)
    await session._extension_api.context.spawn_branch(log.cursor, "go", tools=[], max_turns=3)

    assert captured["max_turns"] == 3


# ---------------------------------------------------------------------------
# The branch's terminal bracket: ``branch_end``, emitted from a ``finally``.
# ---------------------------------------------------------------------------


def _branch_ends(session: AgentSession) -> list[dict]:
    """Record every ``branch_end`` this session's bus publishes."""
    seen: list[dict] = []
    session._events.on("branch_end", lambda **kw: seen.append(kw))
    return seen


async def test_a_finished_branch_announces_its_end_exactly_once(monkeypatch):
    session, log = _session([])
    ends = _branch_ends(session)

    async def _work(self, text, images=None, context=None):
        return []

    monkeypatch.setattr(AgentSession, "prompt", _work)
    result = await session._extension_api.context.spawn_branch(log.cursor, "go", tools=[])

    assert ends == [{"lane": result.lane, "label": result.label, "error": None}]


async def test_a_failing_branch_still_announces_its_end(monkeypatch):
    """The bracket has to survive the failure it is meant to report. A consumer
    that opened a span on the branch's first event (the TUI opens a render lane)
    closes it here — the sub-agent's own ``agent_end`` never arrives, because
    ``AgentLoop.run`` emits it after the while loop rather than from a ``finally``."""
    session, log = _session([])
    ends = _branch_ends(session)

    async def _boom(self, text, images=None, context=None):
        raise RuntimeError("the provider dropped the connection")

    monkeypatch.setattr(AgentSession, "prompt", _boom)
    result = await session._extension_api.context.spawn_branch(log.cursor, "go", tools=[])

    assert result.ok is False
    assert ends == [
        {"lane": result.lane, "label": result.label, "error": "the provider dropped the connection"}
    ]


async def test_a_cancelled_branch_still_announces_its_end(monkeypatch):
    """``abort()`` cancels every forked task and ``CancelledError`` is not an
    ``Exception``, so the containment handler never sees it — the ``finally``
    does. A bare cancel stringifies to "", so the error names the TYPE rather
    than reporting an empty reason."""
    session, log = _session([])
    ends = _branch_ends(session)
    running = asyncio.Event()

    async def _hang(self, text, images=None, context=None):
        running.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(AgentSession, "prompt", _hang)
    task = asyncio.get_running_loop().create_task(
        session._extension_api.context.spawn_branch(log.cursor, "go", tools=[])
    )
    await running.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(ends) == 1
    assert ends[0]["error"] == "CancelledError"


async def test_a_branch_that_never_started_announces_nothing(monkeypatch):
    """The bracket is only owed for a branch that actually ran. The allowlist
    check raises BEFORE the sub-agent exists, so there is no span to close and no
    ``branch_end`` claiming one ended."""
    session, log = _session([_Tool("lookup")])
    ends = _branch_ends(session)

    with pytest.raises(ValueError, match="not available on this session"):
        await session._extension_api.context.spawn_branch(log.cursor, "go", tools=["bash"])

    assert ends == []
