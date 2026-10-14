"""A Monty sandbox whose state is a fold over the τ tree: rollback, fork, resume, deltas.

Runs against the real file ``Session``, ``AgentSession``, ``Cursor`` and, in one
test, a real turn with a scripted provider. Run explicitly:
``pytest experiments/stateful-sandbox`` (``experiments/`` is outside ``testpaths``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pydantic_monty as monty
import pytest
from sandbox_state import (
    CUSTOM_TYPE,
    BlobStore,
    MontySandbox,
    SandboxError,
    decode_state,
    make_python_tool,
    sandbox_delta_text,
    script_bindings,
    state_entry_at,
    summarize_branch_with_sandbox,
)
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.submission import Submission
from tau_coding_agent.session_store import Session
from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

HERE = Path(__file__).parent
_TS = 1_700_000_000_000


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


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}], "timestamp": _TS}


def _session(tmp_path: Path, tools: list[Any] | None = None) -> tuple[AgentSession, Path]:
    log = Session.create(str(tmp_path), "m", "openai", base_dir=tmp_path / "sessions")
    assert log.path is not None
    return AgentSession(session_log=log, model=_model(), tools=tools or []), log.path


FETCHED: list[str] = []


def fetch(url: str) -> dict[str, Any]:
    """A host tool with a visible side effect, so a replayed call would show."""
    FETCHED.append(url)
    return {"url": url, "status": 200, "body": f"<html>{url}</html>"}


@pytest.fixture
async def pool():
    async with monty.AsyncMonty(min_processes=2) as p:
        yield p


@pytest.fixture
async def box_factory(pool):
    made: list[MontySandbox] = []

    def make(path: Path | None, **kw: Any) -> MontySandbox:
        box = MontySandbox(
            pool, blobs=None if path is None else BlobStore.beside(path), tools={"fetch": fetch}, **kw
        )
        made.append(box)
        return box

    yield make
    for box in made:
        await box.close()


async def _value(box: MontySandbox, cursor: Any, expr: str) -> Any:
    """Evaluate ``expr`` in a fresh interpreter restored at ``cursor``'s leaf; writes nothing."""
    live, _ = await box.restore(cursor.entries(), cursor.leaf)
    try:
        return await live.feed_run(expr)
    except monty.MontyRuntimeError as exc:
        return f"<{str(exc).split(':')[0]}>"
    finally:
        await live.__aexit__(None, None, None)


def test_script_bindings_finds_module_level_names_only():
    names, digests = script_bindings(
        "import random as r\nx = 1\nfor i in range(3):\n    pass\n"
        "def f(a):\n    local = a\n    global g\n    g = a\n    return a\n"
        "class C:\n    pass\nsq = [k * k for k in range(3)]\n(a, b) = (1, 2)\n"
    )
    assert names == ["r", "x", "i", "f", "g", "C", "sq", "a", "b"]
    assert set(digests) == {"f", "C"}


async def test_rollback_restores_the_earlier_variables(tmp_path, box_factory):
    session, path = _session(tmp_path)
    box = box_factory(path)
    cur = session.cursor
    await cur.append_message(_msg("user", "start"))
    first = await box.run(cur, "x = 1\nitems = [1]")
    after_first = await cur.append_message(_msg("assistant", "ran first"))
    second = await box.run(cur, "x = 2\nitems.append(2)\ny = 'only later'")
    assert second.delta["added"].keys() == {"y"}
    assert set(second.delta["changed"]) == {"x", "items"}

    cur.move(after_first)
    rolled = await box.run(cur, "(x, items)")
    assert rolled.value == (1, [1])
    assert "y" not in rolled.listing
    assert await _value(box, cur, "y") == "<NameError>"

    entries = cur.entries()
    assert state_entry_at(entries, after_first)["id"] == first.entry_id
    assert state_entry_at(entries, rolled.entry_id)["data"]["parent_state"] == first.entry_id
    assert state_entry_at(entries, second.entry_id)["data"]["generation"] == 2
    assert state_entry_at(entries, rolled.entry_id)["data"]["generation"] == 2, "per-path, not per-tree"


async def test_two_cursors_from_one_entry_diverge(tmp_path, box_factory):
    session, path = _session(tmp_path)
    box = box_factory(path)
    base = await box.run(session.cursor, "shared = 'base'\nn = 0")
    left = await session.open_cursor(base.entry_id, label="left")
    right = await session.open_cursor(base.entry_id, label="right")

    await box.run(left, "n = n + 1\nleft_only = 'L'")
    await box.run(right, "n = n + 100\nright_only = 'R'")
    await box.run(left, "n = n + 1")
    await box.run(right, "n = n + 100")

    assert (await box.run(left, "(shared, n)")).value == ("base", 2)
    assert (await box.run(right, "(shared, n)")).value == ("base", 200)
    assert await _value(box, left, "right_only") == "<NameError>"
    assert await _value(box, right, "left_only") == "<NameError>"
    untouched = await session.open_cursor(base.entry_id, label="third")
    assert await _value(box, untouched, "n") == 0


async def test_a_fresh_process_resumes_the_exact_state(tmp_path, box_factory):
    session, path = _session(tmp_path)
    box = box_factory(path)
    cur = session.cursor
    await box.run(cur, "a = {'k': [1, 2, 3]}\ndef twice(v):\n    return v * 2")
    early = cur.leaf
    await box.run(cur, "page = fetch('https://example.invalid/x')\nb = twice(21)")
    big = await box.run(
        cur,
        "import random\nrandom.seed(7)\nnoise = ''.join([chr(97 + random.randint(0, 25)) for _ in range(200000)])",
    )
    assert "blob" in state_entry_at(cur.entries(), big.entry_id)["data"]["state"], "over the inline limit"
    fetched_before = len(FETCHED)

    def child(leaf: str) -> dict[str, Any]:
        proc = subprocess.run(
            [sys.executable, str(HERE / "resume_probe.py"), str(path), leaf, "a", "b", "page", "len(noise)", "twice(5)"],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    latest = child("default")
    assert latest["state_id"] == big.entry_id
    assert latest["values"] == {
        "a": {"k": [1, 2, 3]},
        "b": 42,
        "page": {"url": "https://example.invalid/x", "status": 200, "body": "<html>https://example.invalid/x</html>"},
        "len(noise)": 200000,
        "twice(5)": 10,
    }
    assert len(FETCHED) == fetched_before, "restore re-ran no tool"
    back = child(early)
    assert back["values"]["a"] == {"k": [1, 2, 3]}
    assert back["values"]["b"] == "<missing>"


async def test_a_real_turn_records_state_between_the_call_and_its_result(tmp_path, pool):
    box = MontySandbox(pool, blobs=None, tools={"fetch": fetch})
    session, _ = _session(tmp_path, tools=[make_python_tool(box)])
    replies = [
        AssistantMessage(
            content=[ToolCall(id="c1", name="python", arguments={"code": "r = fetch('u')\nprint(r['status'])"})],
            api="openai-completions", provider="openai", model="m", stop_reason="toolUse",
            timestamp=_TS, usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        ),
        AssistantMessage(
            content=[TextContent(text="done")],
            api="openai-completions", provider="openai", model="m", stop_reason="stop",
            timestamp=_TS, usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        ),
    ]

    class _Stream:
        def __init__(self, message: AssistantMessage) -> None:
            self._m = message

        def __aiter__(self):
            async def gen():
                yield DoneEvent(final=self._m, usage=self._m.usage)

            return gen()

        async def result(self) -> AssistantMessage:
            return self._m

        def abort(self) -> None:
            pass

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=[_Stream(m) for m in replies]):
        await session.submit(
            Submission(text="go", source="interactive", submitter="human",
                       submission_id="s1", multitask_strategy="enqueue")
        )
    await box.close()

    path = session.cursor.tree().path()
    kinds = [(e["type"], e.get("customType") or (e.get("message") or {}).get("role")) for e in path]
    i = kinds.index(("customEntry", CUSTOM_TYPE))
    assert kinds[i - 1] == ("message", "assistant") and kinds[i + 1] == ("message", "toolResult")
    context = session.cursor.context()
    assert all(m.get("customType") != CUSTOM_TYPE for m in context)
    assert "script_sha256" not in json.dumps(context), "the model never sees the entry's payload"
    tool_text = json.dumps([m for m in context if m.get("role") == "toolResult"])
    assert "200" in tool_text and "[sandbox] +r:dict" in tool_text


async def test_a_failed_script_records_its_calls_and_changes_nothing(tmp_path, box_factory):
    session, path = _session(tmp_path)
    box = box_factory(path)
    cur = session.cursor
    await box.run(cur, "p = 1")
    failed = await box.run(cur, "p = 2\nq = fetch('side-effect')\nboom = 1 / 0")
    assert not failed.ok and "ZeroDivisionError" in (failed.error or "")
    entry = state_entry_at(cur.entries(), cur.leaf)
    assert entry is not None and entry["id"] != failed.entry_id
    recorded = [e for e in cur.entries() if e["id"] == failed.entry_id][0]["data"]
    assert recorded["state"] is None and recorded["tool_calls"][0]["name"] == "fetch"
    assert (await box.run(cur, "p")).value == 1, "Monty kept p = 2 in the heap; the tree did not"
    assert await _value(box, cur, "q") == "<NameError>"


async def test_a_memory_limit_trip_leaves_the_last_good_state(tmp_path, box_factory):
    session, path = _session(tmp_path)
    box = box_factory(path, limits={"max_memory": 4_000_000})
    cur = session.cursor
    await box.run(cur, "keep = 'ok'")
    tripped = await box.run(cur, "huge = 'y' * 50_000_000")
    assert "MemoryError" in (tripped.error or "")
    assert (await box.run(cur, "keep")).value == "ok"


async def test_host_functions_and_files_do_not_travel(tmp_path, pool):
    session, path = _session(tmp_path)
    with_tools = MontySandbox(pool, blobs=BlobStore.beside(path), tools={"fetch": fetch})
    cur = session.cursor
    await with_tools.run(cur, "getter = fetch")
    opened = await with_tools.run(cur, "fh = open('/etc/hostname')")
    assert "PermissionError" in (opened.error or ""), "no filesystem unless mounted"
    await with_tools.close()

    bare = MontySandbox(pool, blobs=BlobStore.beside(path), tools={})
    calls = await bare.run(cur, "getter('x')")
    assert "NameError" in (calls.error or ""), "a stored host function is a name, bound per run"
    await bare.close()


async def test_a_missing_side_file_fails_rather_than_starting_empty(tmp_path, box_factory):
    session, path = _session(tmp_path)
    box = box_factory(path)
    cur = session.cursor
    big = await box.run(cur, "import random\nz = ''.join([chr(97 + random.randint(0, 25)) for _ in range(200000)])")
    ref = state_entry_at(cur.entries(), big.entry_id)["data"]["state"]
    (BlobStore.beside(path).root / f"{ref['blob']}.zlib").unlink()
    await box.close()
    with pytest.raises(SandboxError, match="missing"):
        await box.run(cur, "len(z)")
    with pytest.raises(SandboxError, match="monty"):
        decode_state({**ref, "engine_version": "0.0.0"}, BlobStore.beside(path))


async def test_branch_summary_carries_an_exact_sandbox_delta(tmp_path, box_factory):
    session, path = _session(tmp_path)
    box = box_factory(path)
    cur = session.cursor
    await cur.append_message(_msg("user", "load the data"))
    await box.run(cur, "rows = [1, 2, 3]\nthreshold = 2")
    branch_point = await cur.append_message(_msg("assistant", "loaded"))
    await cur.append_message(_msg("user", "try a filter"))
    await box.run(cur, "kept = [r for r in rows if r >= threshold]\nthreshold = 3")
    await box.run(cur, "page = fetch('a')\nrows = rows + [4]")
    await box.run(cur, "oops = 1 / 0")
    abandoned = cur.leaf

    rolled_back = sandbox_delta_text(cur.entries(), branch_point, abandoned, kept=False)
    assert rolled_back.splitlines()[0] == "Sandbox: 3 script(s) ran (1 failed, no effect); nested calls fetch×1."
    assert "NOT in your sandbox now" in rolled_back
    assert "  + kept: list = [2, 3]" in rolled_back
    assert "  ~ threshold: int 2 -> int 3" in rolled_back
    assert "  ~ rows: list [1, 2, 3] -> list [1, 2, 3, 4]" in rolled_back

    async def fake_summary(text: str, model: Any, **_: Any) -> tuple[str, dict[str, int]]:
        assert "kept" not in text, "subtree_text skips customEntry: the summarizer is blind to the sandbox"
        return "The user tried a filter.", {"input": 1, "output": 1}

    with patch("tau_agent_core.session_manager.summarize_branch", side_effect=fake_summary):
        await summarize_branch_with_sandbox(cur, branch_point, _model(), carry=True)
    summary = [m for m in cur.context() if "Branch summary" in json.dumps(m)]
    assert summary and "Variables now in your sandbox" in json.dumps(summary)
    assert (await box.run(cur, "(kept, threshold, len(rows))")).value == ([2, 3], 3, 4), "carried"


async def test_compaction_keeps_state_and_its_summary_can_say_how_it_changed(tmp_path, box_factory):
    session, path = _session(tmp_path)
    box = box_factory(path)
    cur = session.cursor
    start = await cur.append_message(_msg("user", "begin"))
    for i in range(4):
        await box.run(cur, f"acc_{i} = {i}\ntotal = {sum(range(i + 1))}")
        await cur.append_message(_msg("assistant", f"step {i}"))
    first_kept = await cur.append_message(_msg("user", "keep from here"))
    note = sandbox_delta_text(cur.entries(), start, first_kept, kept=True)
    await cur.append_compaction(
        "Summary of steps 0-3.\n\n" + note,
        first_kept,
        1000,
        summarizer_model_id="mock",
        summary_usage={"input": 0, "output": 0},
        covered_entries=8,
        covered_tokens=1000,
        config_id=None,
    )
    context = json.dumps(cur.context())
    assert "step 0" not in context and "+ acc_3: int = 3" in context
    assert (await box.run(cur, "(acc_0, total)")).value == (0, 6), "ancestry, not context"
