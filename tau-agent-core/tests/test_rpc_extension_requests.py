"""`get_pending_request` and `answer_request`: the lock, on the wire.

0.10.0 replaced `ui.confirm` / `ui.select` / `ui.input` with one persisted
`extension_request` entry and recorded that every head renders all four of its
states. Three heads could: the TUI, the REPL and print mode all reach
`AgentSession.pending_request` and `AgentSession.answer_request` in process. The
RPC wire could not — a host learned about a lock by being REFUSED by one, in
`SUBMISSION_REJECTED` data, and had no verb to release it. A locked session was
therefore a session an out-of-process head could never continue.

What this file owns is the wiring: that the read reports the cursor's request and
null otherwise, that the write appends before it dispatches, and that each
refusal `AgentSession.answer_request` raises reaches a host as INVALID_PARAMS
with nothing appended. The lock ALGEBRA is tested in `test_extension_locks.py`.

Reference: docs/EXTENSION-LOCKS.md §2, §3, §9.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.capabilities import CAPABILITIES
from tau_agent_core.extension_locks import (
    REQUEST_ENTRY_TYPE,
    RESPONSE_ENTRY_TYPE,
    build_request_data,
)
from tau_agent_core.extension_types import validate_ask_spec
from tau_agent_core.rpc import RPCHandler, commands
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model

# Normalized the way `api.request_user_action` normalizes it: `build_request_data`
# takes an ALREADY-validated spec, and a hand-built one would be a shape no
# extension can actually produce.
ASK = validate_ask_spec(
    {
        "title": "Approve the deploy?",
        "fields": [{"name": "ticket", "kind": "text", "label": "Change ticket"}],
        "actions": [
            {"label": "Approve", "command": "gate-approve"},
            {"label": "Reject", "command": "gate-reject"},
        ],
    }
)
NO_FIELDS_ASK = validate_ask_spec({"title": "Go?", "actions": [{"label": "Yes", "command": "go"}]})


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
    path = Path("/tmp/does-not-need-to-exist.jsonl")


@pytest.fixture
def log() -> _DurableLog:
    return _DurableLog()


@pytest.fixture
def handler(log: _DurableLog) -> RPCHandler:
    return RPCHandler(AgentSession(session_log=log, model=_model(), tools=[]))


def _raise(
    log: _DurableLog, *, lock: bool = True, ask: dict | None = None, release: str | None = None
) -> str:
    return log.append_custom_entry(
        REQUEST_ENTRY_TYPE,
        build_request_data(
            "/x/gate.py",
            "A deploy needs sign-off.",
            lock=lock,
            ask=ask,
            **({} if release is None else {"release": release}),
        ),
    )


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


def test_the_read_is_a_read_and_the_write_is_a_mutation() -> None:
    assert CAPABILITIES["get_pending_request"].kind == "read"
    assert CAPABILITIES["answer_request"].kind == "mutation"
    for verb in ("get_pending_request", "answer_request"):
        assert commands.COMMAND_TABLE[verb].handler is not None
        assert commands.COMMAND_TABLE[verb].since == "0.10.1"


# ── get_pending_request ──────────────────────────────────────────────────────


async def test_no_request_answers_null_rather_than_an_error(handler: RPCHandler) -> None:
    """Null is the ordinary answer. An error here would make the poll unaffordable."""
    assert (await _call(handler, "get_pending_request"))["result"]["request"] is None


async def test_a_request_at_the_cursor_is_reported_whole(
    handler: RPCHandler, log: _DurableLog
) -> None:
    """`label` rides along rather than being recomputed.

    Mutation this kills: sending `lock` and `ask` and leaving a host to derive
    tau's four-state framing line, which is a second copy of the one table
    docs/EXTENSION-LOCKS.md §9 owns.
    """
    entry_id = _raise(log, ask=ASK, release="gate-clear")
    request = (await _call(handler, "get_pending_request"))["result"]["request"]
    assert request["entry_id"] == entry_id
    assert request["extension"] == "/x/gate.py"
    assert request["extension_name"] == "gate"
    assert request["sentence"] == "A deploy needs sign-off."
    assert request["label"] == "Extension gate requires a response"
    assert request["lock"] is True
    assert request["release"] == "gate-clear"
    assert request["ask"]["actions"][0]["label"] == "Approve"


async def test_a_bare_lock_carries_no_ask(handler: RPCHandler, log: _DurableLog) -> None:
    """The state a host renders as a refusal with a way out, not as a form."""
    _raise(log, lock=True, ask=None, release="gate-clear")
    request = (await _call(handler, "get_pending_request"))["result"]["request"]
    assert request["ask"] is None
    assert request["label"] == "Extension gate requires intervention"


async def test_the_read_is_the_cursor_and_not_a_walk(handler: RPCHandler, log: _DurableLog) -> None:
    """Moving past a request clears it. That is what makes branching a way out."""
    _raise(log, ask=ASK)
    log.append_message({"role": "user", "content": [{"type": "text", "text": "moved on"}]})
    assert (await _call(handler, "get_pending_request"))["result"]["request"] is None


# ── answer_request ───────────────────────────────────────────────────────────


async def test_answering_appends_the_response_and_releases_the_lock(
    handler: RPCHandler, log: _DurableLog
) -> None:
    """The append IS the release: it moves the cursor, and a lock is read at the cursor."""
    entry_id = _raise(log, ask=ASK)
    answer = await _call(
        handler,
        "answer_request",
        {"request_id": entry_id, "action": "Approve", "values": {"ticket": "OPS-1"}},
    )
    result = answer["result"]
    assert result["cursor"] == log.cursor
    assert result["cursor"] != entry_id
    responses = [e for e in log.entries() if e.get("customType") == RESPONSE_ENTRY_TYPE]
    assert len(responses) == 1
    assert responses[0]["data"]["values"] == {"ticket": "OPS-1"}
    assert responses[0]["data"]["action"] == "Approve"
    assert (await _call(handler, "get_pending_request"))["result"]["request"] is None


async def test_an_absent_extension_warns_and_still_releases(
    handler: RPCHandler, log: _DurableLog
) -> None:
    """`handled: false` is a warning, not a failure.

    A lock whose owner cannot answer must not become a session nobody can
    continue, so the response is appended and the lock is gone either way.
    """
    entry_id = _raise(log, ask=ASK)
    result = (
        await _call(
            handler,
            "answer_request",
            {"request_id": entry_id, "action": "Approve", "values": {"ticket": "OPS-1"}},
        )
    )["result"]
    assert result["handled"] is False
    assert (await _call(handler, "get_pending_request"))["result"]["request"] is None


async def test_an_ask_with_no_fields_takes_no_values(handler: RPCHandler, log: _DurableLog) -> None:
    entry_id = _raise(log, ask=NO_FIELDS_ASK)
    answer = await _call(handler, "answer_request", {"request_id": entry_id, "action": "Yes"})
    assert answer["result"]["cursor"] is not None


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"request_id": "nope", "action": "Approve"}, "no extension request"),
        ({"request_id": "@bare", "action": "Approve"}, "carries no ask"),
        ({"request_id": "@ask", "action": "Shrug"}, "is not one of this ask's actions"),
        ({"request_id": "@ask", "action": "Approve"}, "missing value(s) for declared field"),
    ],
    ids=[
        "unknown-id",
        "a-bare-lock-is-not-answered",
        "an-action-the-ask-never-declared",
        "an-answer-missing-a-declared-field",
    ],
)
async def test_each_refusal_reaches_the_host_as_invalid_params_with_nothing_appended(
    handler: RPCHandler, log: _DurableLog, params: dict, expected: str
) -> None:
    """Fail-Early, and TOTAL: a refused answer leaves the log byte-identical."""
    bare = _raise(log, lock=True, ask=None)
    asked = _raise(log, ask=ASK)
    resolved = dict(params)
    resolved["request_id"] = {"@bare": bare, "@ask": asked}.get(
        params["request_id"], params["request_id"]
    )
    before = len(log.entries())

    answer = await _call(handler, "answer_request", resolved)
    assert "result" not in answer
    assert answer["error"]["code"] == -32602
    assert expected in answer["error"]["message"]
    assert len(log.entries()) == before


async def test_a_missing_action_is_refused_by_the_schema(
    handler: RPCHandler, log: _DurableLog
) -> None:
    entry_id = _raise(log, ask=ASK)
    answer = await _call(handler, "answer_request", {"request_id": entry_id})
    assert answer["error"]["code"] == -32602
    assert "action" in answer["error"]["message"]
