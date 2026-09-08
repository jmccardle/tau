"""Tier C — `next_step` and `enumerate_domain`, the flow loop on the wire.

The pure half is pinned in `test_flows.py`; these pin the wire contract: the two
mutually-exclusive result shapes, the dataclass fields a host actually receives, and
the Fail-Early refusals reaching the host as errors rather than as empty listings.

A new file per unit, following the Tier B convention (docs/RPC-TIER-B.md §3).
"""

from __future__ import annotations

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.rpc import commands
from tau_agent_core.rpc.handler import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog
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


@pytest.fixture
def handler() -> RPCHandler:
    return RPCHandler(AgentSession(session_log=InMemorySessionLog(), tools=[], model=_model()))


async def _call(handler: RPCHandler, method: str, params: dict) -> dict:
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return await handler._output_queue.get()


class TestNextStepOnTheWire:
    async def test_a_step_carries_the_argument_and_its_resolved_domain(self, handler):
        got = await _call(handler, "next_step", {"flow": "resume"})
        result = got["result"]
        assert result["status"] == "step"
        assert result["ready"] is None
        assert result["step"]["argument"]["name"] == "session_id"
        assert result["step"]["domain"]["enumerator"] == "list_sessions"
        assert result["step"]["domain"]["values"] is None

    async def test_a_ready_names_the_mutation_and_its_arguments(self, handler):
        got = await _call(
            handler,
            "next_step",
            {"flow": "reload_extension", "bound": {"path": "/x"}},
        )
        result = got["result"]
        assert result["status"] == "ready"
        assert result["step"] is None
        assert result["ready"] == {
            "flow": "reload_extension",
            "mutation": "reload_extension",
            "arguments": {"path": "/x"},
        }

    async def test_an_enumerable_domain_arrives_needing_a_second_call(self, handler):
        """`values` null plus an `enumerator` is the host's instruction to enumerate."""
        got = await _call(handler, "next_step", {"flow": "disable_extension"})
        domain = got["result"]["step"]["domain"]
        assert domain["values"] is None
        assert domain["enumerator"] == "list_managed_extensions"

    async def test_the_cursor_is_echoed_back_for_the_host_to_hand_on(self, handler):
        got = await _call(handler, "next_step", {"flow": "resume", "cursor": "e7"})
        assert got["result"]["step"]["cursor"] == "e7"

    async def test_an_unknown_flow_is_an_error_not_an_empty_answer(self, handler):
        got = await _call(handler, "next_step", {"flow": "tree"})
        assert "error" in got
        assert "no flow named 'tree'" in got["error"]["message"]

    async def test_it_is_a_read_and_carries_no_cursor_key(self, handler):
        """E5 rule 2: a read never carries `cursor`, and absence is never a signal."""
        got = await _call(handler, "next_step", {"flow": "compact"})
        assert "cursor" not in got["result"]


class TestEnumerateDomainOnTheWire:
    async def test_a_fixed_domain_lists_value_and_label_pairs(self, handler):
        got = await _call(handler, "enumerate_domain", {"domain": "boolean"})
        assert got["result"]["domain"] == "boolean"
        assert got["result"]["values"] == [
            {"value": "true", "label": "true"},
            {"value": "false", "label": "false"},
        ]
        assert got["result"]["total"] == 2

    async def test_a_free_domain_answers_with_none_and_says_so(self, handler):
        got = await _call(handler, "enumerate_domain", {"domain": "text"})
        assert got["result"]["values"] == []
        assert got["result"]["total"] == 0

    async def test_message_ids_come_from_the_live_session_with_their_text(self, handler):
        """Every entry the session holds, the agent_spec record included."""
        log = handler.session.session_log
        first = log.append_message({"role": "user", "content": "run the tests"})
        second = log.append_message({"role": "assistant", "content": "they pass"})
        got = await _call(handler, "enumerate_domain", {"domain": "message_id"})
        found = {v["value"]: v["label"] for v in got["result"]["values"]}
        assert found[first] == "run the tests"
        assert found[second] == "they pass"
        assert got["result"]["total"] == len(log.entries())

    async def test_the_query_filters_by_text(self, handler):
        log = handler.session.session_log
        log.append_message({"role": "user", "content": "run the tests"})
        second = log.append_message({"role": "assistant", "content": "they pass"})
        got = await _call(handler, "enumerate_domain", {"domain": "message_id", "query": "PASS"})
        assert [v["value"] for v in got["result"]["values"]] == [second]

    async def test_a_domain_needing_a_runtime_this_process_lacks_is_an_error(self, handler):
        """Fail-Early: an empty list would say 'no sessions exist'."""
        got = await _call(handler, "enumerate_domain", {"domain": "session_id"})
        assert "error" in got
        assert "needs a runtime" in got["error"]["message"]

    async def test_it_is_a_read_and_carries_no_cursor_key(self, handler):
        got = await _call(handler, "enumerate_domain", {"domain": "text"})
        assert "cursor" not in got["result"]


class TestTheTableRows:
    @pytest.mark.parametrize("verb", ["next_step", "enumerate_domain"])
    def test_both_are_live_tier_c_rows_with_both_schemas(self, verb):
        entry = commands.COMMAND_TABLE[verb]
        assert entry.tier == "C"
        assert entry.declined_because is None
        assert entry.params_schema and entry.result_schema

    @pytest.mark.parametrize("verb", ["next_step", "enumerate_domain"])
    def test_neither_schema_declares_a_cursor(self, verb):
        """The E5 read rule, asserted rather than only written in the notes."""
        entry = commands.COMMAND_TABLE[verb]
        assert "cursor" not in entry.result_schema["properties"]
