"""B6 — the `get_last_assistant_text` Tier B verb (docs/RPC-TIER-B.md §3).

Read-only, no D-1 turn_safety_guard, no §1.1 append guard — the smallest unit
in the tier, and per the unit brief the two questions that ARE the unit:

1. What does it return when there is no assistant message yet? -> `{"text":
   null}`.
2. What counts as "text" when the last assistant message carries tool calls
   or thinking blocks alongside — or instead of — text? -> only `type ==
   "text"` content blocks, concatenated in order and trimmed; a message with
   no text block after that filter also reads back as `{"text": null}`,
   indistinguishable from "no assistant message at all" (documented, not
   hidden — see `commands._last_assistant_text`'s docstring and this verb's
   `notes`).

This file owns two layers, per B6's own contract: `commands._last_assistant_
text` is where the actual decision logic lives (block filtering, the
aborted-with-empty-content skip, trim-to-None) and is tested exhaustively as
a pure function; `_handle_get_last_assistant_text` is a one-line pass-
through, tested at the wire level just enough to prove it is registered,
schema-checked, and dispatches to that function.

Every test below is written to be MUTATION-KILLABLE: each docstring names a
concrete implementation change and the assertion that would catch it. See
this module's bottom comment for the mutate/red/restore/green log kept for
the StructuredOutput report (house rule: "every test must be able to fail").
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tau_agent_core.rpc import RPCHandler, commands
from tau_agent_core.session import SessionState

# ─────────────────────────────────────────────────────────────────────────
# Layer 1 — `commands._last_assistant_text`, the pure decision function.
# ─────────────────────────────────────────────────────────────────────────


def test_no_messages_at_all_returns_none():
    """Fresh session, nothing yet — the ordinary case, not an error.

    Mutation this kills: replacing the `for ... return None` fallthrough
    with a `raise` (treating "no assistant message" as malformed input,
    which the unit brief explicitly says it is not)."""
    assert commands._last_assistant_text([]) is None


def test_only_user_messages_returns_none():
    """No assistant message anywhere in history.

    Mutation this kills: dropping the `role != "assistant"` guard, which
    would make a `role="user"` dict's absent `.get("content")` blow up or
    (worse) silently match."""
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert commands._last_assistant_text(messages) is None


def test_plain_text_assistant_message_returns_its_text():
    """The base case: one assistant message, one text block.

    Mutation this kills: returning the block dict instead of its `.text`,
    or forgetting to return anything on the matching branch."""
    messages = [{"role": "assistant", "content": [{"type": "text", "text": "hello there"}]}]
    assert commands._last_assistant_text(messages) == "hello there"


def test_multiple_text_blocks_concatenate_in_order_with_no_separator():
    """pi: `text += content.text` inside one loop, no join character.

    Mutation this kills: joining with `" "` or `"\\n"` instead of `""`, or
    reversing block order."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "part one, "},
                {"type": "text", "text": "part two."},
            ],
        }
    ]
    assert commands._last_assistant_text(messages) == "part one, part two."


def test_toolcall_blocks_contribute_nothing():
    """A tool-call-only assistant message has a real `role="assistant"`
    entry but no `text` field to read from its `toolCall` block.

    This alone does not distinguish a `type == "text"` whitelist from a
    `type != "thinking"` blacklist (a `toolCall` block has no `text` key
    either way, so both filters read `""` from it) — see the dedicated
    `test_unknown_block_type_with_a_text_key_is_still_excluded` below for
    the test that actually pins down whitelist-not-blacklist. Dropping the
    `type` check ENTIRELY is likewise killed THERE and not here, for the
    same reason (measured while fixing phase-5 finding 8: with the filter
    clause deleted, this test still passes and
    `test_unknown_block_type_with_a_text_key_is_still_excluded` is the one
    that goes red).

    Mutation this test itself kills: `return text` in place of `return
    text or None` — a real assistant message whose blocks all filter out
    must read back as `None`, not `""`. `""` is schema-legal (`"type":
    ["string", "null"]`) but contract-wrong: pi's `text.trim() ||
    undefined`, and `{"text": ""}` would tell a host "the assistant said
    nothing, out loud" rather than "nothing to report"."""
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {}}],
        }
    ]
    assert commands._last_assistant_text(messages) is None


def test_unknown_block_type_with_a_text_key_is_still_excluded():
    """The test that actually distinguishes a `type == "text"` WHITELIST
    from a `type != "thinking"` BLACKLIST: an invented block type that
    happens to carry a `text` field. Every real block type in
    `tau_llm.types` either IS `"text"` or has no `text` key, so this is the
    only shape that can tell the two filters apart.

    Mutation this kills: `if block.get("type") != "thinking"` in place of
    `if block.get("type") == "text"` — the blacklist form would let this
    block's `text` field through; the whitelist form (the actual
    implementation) excludes anything that is not literally `"text"`."""
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "some_future_block_type", "text": "should not appear"}],
        }
    ]
    assert commands._last_assistant_text(messages) is None


def test_a_thinking_only_turn_stops_the_search_instead_of_falling_back():
    """A last assistant turn made of nothing but thinking reads back as
    `None` — and the search STOPS there rather than walking further back
    to the previous real answer. That is the whole reason "the last
    assistant message has no text" and "there is no assistant message"
    are indistinguishable on the wire
    (`GET_LAST_ASSISTANT_TEXT_RESULT_SCHEMA`'s `text` description, and
    this verb's `notes`); the ONLY message the walk skips is the
    aborted-and-content-less one (contrast:
    `test_aborted_with_empty_content_is_skipped_in_favor_of_the_prior_
    real_answer`, whose last entry differs from this one only in that it
    has no blocks at all).

    Mutation this kills: `return text or None` -> `if text: return text`
    (fall through and keep searching when the filtered text is empty),
    which broadens the aborted skip to "any turn that produced no text"
    and returns "the real answer" here instead of `None`.

    ── why this test exists in this shape (docs/RPC-TIER-B.md §6) ────────
    It replaces `test_thinking_blocks_contribute_nothing`, which named the
    mutation `block.get("text", block.get("thinking", ""))` and could not
    kill it: applied verbatim to `_last_assistant_text`, the whole file
    stayed green (19 passed), because the sibling `type == "text"`
    whitelist gates every block before that `.get` is ever reached. Two
    independent mechanisms keep thinking content out of the answer — the
    whitelist AND the fact that a thinking block stores its content under
    `thinking`, not `text` — so no single-edit mutation can make thinking
    leak, and "thinking content leaks" has no expressible failure to pin.
    The whitelist half is pinned by
    `test_unknown_block_type_with_a_text_key_is_still_excluded`; the exact
    thinking-plus-text mixed shape is pinned by
    `test_text_survives_alongside_toolcall_and_thinking_blocks`. So the
    thinking block below is kept as the realistic no-text turn, but the
    property asserted is the one a real mutation can break."""
    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": "the real answer"}]},
        {"role": "user", "content": [{"type": "text", "text": "and again?"}]},
        {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "let me consider this"}],
        },
    ]
    assert commands._last_assistant_text(messages) is None


def test_text_survives_alongside_toolcall_and_thinking_blocks():
    """The mixed case named in the unit brief: text block PLUS tool calls
    PLUS thinking, in the same message. Only the text block's content
    should appear.

    Mutation this kills: an early `return None` the moment a non-text
    block is seen (treating "has tool calls" as disqualifying instead of
    just filtering blocks)."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "reasoning..."},
                {"type": "text", "text": "the answer is 42"},
                {"type": "toolCall", "id": "c1", "name": "read", "arguments": {}},
            ],
        }
    ]
    assert commands._last_assistant_text(messages) == "the answer is 42"


def test_whitespace_only_text_trims_to_none():
    """pi: `text.trim() || undefined` — an all-whitespace result is treated
    as absent, not as an empty string.

    Mutation this kills: returning `text` (untrimmed / unfiltered) instead
    of `text.strip() or None`."""
    messages = [{"role": "assistant", "content": [{"type": "text", "text": "   \n  "}]}]
    assert commands._last_assistant_text(messages) is None


def test_leading_and_trailing_whitespace_is_stripped_but_interior_kept():
    """Mutation this kills: stripping every block individually (losing the
    interior space between two blocks) instead of stripping the final
    concatenation once."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "  hello "},
                {"type": "text", "text": " world  "},
            ],
        }
    ]
    assert commands._last_assistant_text(messages) == "hello  world"


def test_returns_the_last_assistant_message_not_the_first():
    """Search direction matters: pi walks from the END.

    Mutation this kills: iterating `messages` forwards instead of
    `reversed(messages)`, which would return "first" here instead of
    "second"."""
    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
        {"role": "user", "content": [{"type": "text", "text": "more please"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "second"}]},
    ]
    assert commands._last_assistant_text(messages) == "second"


def test_toolresult_messages_between_assistant_turns_are_skipped_over():
    """A realistic transcript: assistant -> toolResult -> assistant. The
    search must not stop or misfire on the intervening non-assistant role.

    Mutation this kills: matching on `message.get("role") != "user"`
    (blacklist) instead of `== "assistant"` (whitelist) — a `toolResult`
    role would then wrongly be treated as a candidate and crash on/return
    its shape instead of being skipped."""
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {}}],
        },
        {"role": "toolResult", "content": [{"type": "text", "text": "file contents"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "based on that, X"}]},
    ]
    assert commands._last_assistant_text(messages) == "based on that, X"


def test_aborted_with_empty_content_is_skipped_in_favor_of_the_prior_real_answer():
    """The one skip named in the unit brief: `stop_reason == "aborted"` AND
    `content == []` together mean "cut off before saying anything" — the
    search must fall through to the previous real assistant turn instead
    of stopping there and reporting nothing.

    Mutation this kills: removing the `stop_reason == "aborted"` skip
    entirely, which would make this test return `None` instead of falling
    through to "the real answer"."""
    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": "the real answer"}]},
        {"role": "user", "content": [{"type": "text", "text": "try again"}]},
        {"role": "assistant", "content": [], "stop_reason": "aborted"},
    ]
    assert commands._last_assistant_text(messages) == "the real answer"


def test_aborted_with_nonempty_content_is_not_skipped():
    """The narrower half of the same rule: `stop_reason == "aborted"` alone
    is NOT enough to skip — only paired with empty content. An abort
    mid-stream that already produced text must still surface that text.

    Mutation this kills: skipping on `stop_reason == "aborted"` alone
    (dropping the `and not content` half of the condition), which would
    make this test return `None` instead of the partial text below."""
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "partial before abort"}],
            "stop_reason": "aborted",
        }
    ]
    assert commands._last_assistant_text(messages) == "partial before abort"


def test_all_assistant_messages_aborted_and_empty_returns_none():
    """No REAL answer anywhere in history — every assistant entry is a
    content-less abort. Falls all the way through to `None`, not an
    exception."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [], "stop_reason": "aborted"},
    ]
    assert commands._last_assistant_text(messages) is None


# ─────────────────────────────────────────────────────────────────────────
# Layer 2 — the wire verb: registration, schema, dispatch.
# ─────────────────────────────────────────────────────────────────────────


def _mock_session(**overrides: Any) -> MagicMock:
    """A trimmed-down stand-in for `AgentSession`, exposing only what
    `RPCHandler.__init__` and `get_last_assistant_text`'s own dispatch path
    touch — the same shape `test_rpc.py::_session` builds, kept local so
    this file contends with no other Tier B unit's test file (unit brief:
    "a NEW file, so no unit contends... on a test file")."""
    session = MagicMock()
    session.state = SessionState(session_id="sess-1", status="idle")
    session.is_streaming = False
    session.is_aborted = False
    session.shutdown_requested = False
    session.messages = []
    session.session_log = MagicMock()
    session.session_log.cursor = "leaf-1"
    session.subscribe.return_value = MagicMock()
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


@pytest.fixture
def session() -> MagicMock:
    return _mock_session()


@pytest.fixture
def handler(session: MagicMock) -> RPCHandler:
    return RPCHandler(session)


async def _drain(handler: RPCHandler) -> list[dict]:
    out = []
    while not handler._output_queue.empty():
        out.append(await handler._output_queue.get())
    return out


def test_verb_is_registered_tier_b_read_only():
    """It is in COMMAND_TABLE, tier B, with a live handler (not declined) —
    the shape `test_rpc_capability_audit.py::test_every_exposed_method_
    names_a_live_undeclined_handler` relies on for any EXPOSED entry
    pointing at it, and what `get_capabilities` (K1) would advertise.

    Mutation this kills: registering under the wrong tier, or leaving the
    verb declined instead of handled."""
    entry = commands.COMMAND_TABLE["get_last_assistant_text"]
    assert entry.tier == "B"
    assert entry.handler is not None
    assert entry.declined_because is None


async def test_unexpected_param_is_rejected_with_invalid_params(handler: RPCHandler):
    """`NO_PARAMS_SCHEMA` has `additionalProperties: False` — this verb
    takes no arguments, and a host that sends one gets a structured
    refusal (C2), not a silently ignored extra field.

    Mutation this kills: swapping `params_schema=NO_PARAMS_SCHEMA` for a
    permissive schema, or dropping schema validation from dispatch."""
    from tau_agent_core.rpc import dialect

    await handler._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "get_last_assistant_text", "params": {"bogus": 1}}
    )
    (response,) = await _drain(handler)
    assert response["error"]["code"] == dialect.INVALID_PARAMS


async def test_dispatch_returns_null_text_on_a_fresh_session(handler: RPCHandler):
    """End-to-end through `_handle_request`: no assistant message yet."""
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_last_assistant_text"})
    (response,) = await _drain(handler)
    assert response["result"] == {"text": None, "method": "get_last_assistant_text"}


async def test_dispatch_returns_the_last_assistant_text(handler: RPCHandler, session: MagicMock):
    """End-to-end through `_handle_request`: proves the handler actually
    reads `session.messages` (not a cached/stale copy) and routes through
    `_last_assistant_text` rather than reimplementing the filter inline.

    Mutation this kills: `_handle_get_last_assistant_text` hand-rolling its
    own (buggy) block filter instead of calling `_last_assistant_text`."""
    session.messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "..."},
                {"type": "text", "text": "hello!"},
            ],
        },
    ]
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": "get_last_assistant_text"})
    (response,) = await _drain(handler)
    assert response["result"] == {"text": "hello!", "method": "get_last_assistant_text"}


# ─────────────────────────────────────────────────────────────────────────
# Mutate / red / restore / green log (the record this module's docstring
# points at; docs/RPC-TIER-B.md §6 "every test must be able to fail").
# Every mutation below was applied to a COPY of the four src trees, run
# with PYTHONPATH shadowing the editable installs, and reverted.
#
# Phase-5 finding 8 round (all against `commands._last_assistant_text`):
#   V  `block.get("text", "")` -> `block.get("text", block.get("thinking",
#      ""))` — the mutation the replaced `test_thinking_blocks_contribute_
#      nothing` named. STAYED GREEN, 19 passed: the `type == "text"`
#      whitelist gates it, so it is an equivalent mutant, which is what
#      made that test vacuous. No test was changed to "fix" this; the
#      property was re-aimed instead (see
#      `test_a_thinking_only_turn_stops_the_search_instead_of_falling_back`).
#   M1 `return text or None` -> `if text: return text` (fall through to
#      the next-older assistant turn when nothing filtered through). RED,
#      and red ONLY in `test_a_thinking_only_turn_stops_the_search_
#      instead_of_falling_back` (1 failed, 18 passed) — that test is the
#      sole pin for "the walk stops at the last assistant message".
#   M2 `return text or None` -> `return text` (leak `""` instead of
#      `None`). RED in 4 tests, including `test_toolcall_blocks_
#      contribute_nothing` — the mutation that docstring now names.
#   M3 delete the `if isinstance(block, dict) and block.get("type") ==
#      "text"` filter clause. RED in exactly one test,
#      `test_unknown_block_type_with_a_text_key_is_still_excluded`; the
#      `toolCall` and thinking shapes both stay green under it, because
#      neither carries a `text` key for an unfiltered join to pick up.
