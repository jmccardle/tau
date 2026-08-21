"""Tests for tau_agent_core.event_projection — the cumulative-message -> delta
projector extracted from tau-coding-agent's TauBackend (REMOTE-CONTROL.md E1).

R-T6 (REMOTE-CONTROL.md §9): concatenating every emitted delta across a turn
must reproduce the final assistant text EXACTLY, byte-exact. "Concatenating"
means applying each delta the way a consumer is contractually required to:
append when ``replace`` is False, RESET to ``delta`` when ``replace`` is True
(the defensive provider-replaced-rather-than-extended case).
"""

from __future__ import annotations

from tau_llm.types import ThinkingContent, ToolCall

from tau_agent_core.event_projection import BlockDelta, MessageDeltaProjector


def _apply(acc: str, delta: BlockDelta) -> str:
    """The consumer-side reconstruction rule a delta contract demands."""
    assert delta.delta is not None
    return delta.delta if delta.replace else acc + delta.delta


def _text_message(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


class TestNormalGrowth:
    def test_suffix_deltas_reconstruct_final_text(self):
        projector = MessageDeltaProjector()
        snapshots = ["Hi", "Hi there", "Hi there, ", "Hi there, friend."]

        acc = ""
        for snap in snapshots:
            for delta in projector.project(_text_message(snap)):
                assert delta.type == "text"
                assert delta.replace is False
                acc = _apply(acc, delta)

        assert acc == snapshots[-1]

    def test_each_delta_is_the_incremental_suffix(self):
        projector = MessageDeltaProjector()
        deltas = []
        for snap in ["Sure, ", "Sure, let me look."]:
            deltas.extend(projector.project(_text_message(snap)))

        assert [d.delta for d in deltas] == ["Sure, ", "let me look."]


class TestNoChangeRepeat:
    def test_identical_resend_emits_nothing(self):
        projector = MessageDeltaProjector()
        first = projector.project(_text_message("hello"))
        assert [d.delta for d in first] == ["hello"]

        second = projector.project(_text_message("hello"))
        assert second == []

    def test_empty_text_block_emits_nothing(self):
        projector = MessageDeltaProjector()
        assert projector.project(_text_message("")) == []


class TestReplaceNotExtend:
    def test_non_prefix_resend_flags_replace_with_whole_value(self):
        projector = MessageDeltaProjector()
        first = projector.project(_text_message("Hello"))
        assert first[0].delta == "Hello"
        assert first[0].replace is False

        # Does NOT start with "Hello" -> defensive replace, not a suffix.
        second = projector.project(_text_message("Goodbye"))
        assert len(second) == 1
        assert second[0].replace is True
        assert second[0].delta == "Goodbye"

    def test_replace_reconstructs_final_text_exactly(self):
        projector = MessageDeltaProjector()
        acc = ""
        for snap in ["Hello", "Goodbye", "Goodbye, friend"]:
            for delta in projector.project(_text_message(snap)):
                acc = _apply(acc, delta)
        assert acc == "Goodbye, friend"


class TestMultiBlockMessage:
    def test_text_and_thinking_diffed_independently(self):
        projector = MessageDeltaProjector()

        def msg(thinking: str, text: str) -> dict:
            content = []
            if thinking:
                content.append({"type": "thinking", "thinking": thinking})
            if text:
                content.append({"type": "text", "text": text})
            return {"role": "assistant", "content": content}

        sequence = [
            msg("Let me think. ", ""),
            msg("Let me think. 2+2=4.", ""),
            msg("Let me think. 2+2=4.", "The answer is 4."),
        ]

        text_acc = ""
        thinking_acc = ""
        for m in sequence:
            for delta in projector.project(m):
                if delta.type == "text":
                    text_acc = _apply(text_acc, delta)
                elif delta.type == "thinking":
                    thinking_acc = _apply(thinking_acc, delta)

        assert text_acc == "The answer is 4."
        assert thinking_acc == "Let me think. 2+2=4."

    def test_interleaved_single_block_updates_do_not_cross_contaminate(self):
        """Mirrors the real agent-loop shape: a ThinkingDeltaEvent message is
        `[{"type": "thinking", ...}]` and a TextDeltaEvent message is
        `[{"type": "text", ...}]` — both single-block, both at index 0. A
        position-keyed (rather than type-keyed) accumulator would misattribute
        the first text delta as a "replace" of the thinking block.
        """
        projector = MessageDeltaProjector()

        thinking_deltas = []
        text_deltas = []
        for delta in projector.project(
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "Hmm"}]}
        ):
            thinking_deltas.append(delta)
        for delta in projector.project(
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "Hmm, ok."}]}
        ):
            thinking_deltas.append(delta)
        for delta in projector.project(
            {"role": "assistant", "content": [{"type": "text", "text": "Answer"}]}
        ):
            text_deltas.append(delta)

        assert [d.delta for d in thinking_deltas] == ["Hmm", ", ok."]
        assert all(d.replace is False for d in thinking_deltas)
        # The text block's first appearance is ordinary growth from nothing,
        # NOT a replace of the (differently-typed) thinking block that
        # happened to occupy the same content-list position.
        assert [d.delta for d in text_deltas] == ["Answer"]
        assert text_deltas[0].replace is False


class TestNonDiffableBlockPassthrough:
    """Non-text-like block kinds (e.g. a streaming toolCall) are not diffed,
    but must not be silently dropped — the whole current block is surfaced
    via BlockDelta.block whenever it changes.
    """

    def test_tool_call_block_surfaced_on_change_and_suppressed_when_unchanged(self):
        projector = MessageDeltaProjector()

        def msg(arguments: str) -> dict:
            return {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "c1", "name": "ls", "arguments": arguments}],
            }

        first = projector.project(msg("{}"))
        assert len(first) == 1
        assert first[0].type == "toolCall"
        assert first[0].delta is None
        assert first[0].block == {"type": "toolCall", "id": "c1", "name": "ls", "arguments": "{}"}

        # Unchanged resend -> nothing (same "no actual change" contract as text).
        assert projector.project(msg("{}")) == []

        # Changed arguments -> surfaced again, whole new block.
        grown = projector.project(msg('{"p": "."}'))
        assert len(grown) == 1
        assert grown[0].block == {
            "type": "toolCall",
            "id": "c1",
            "name": "ls",
            "arguments": '{"p": "."}',
        }

    def test_toolcall_and_text_blocks_together_only_text_is_diffed_as_delta(self):
        projector = MessageDeltaProjector()
        message = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Running ls"},
                {"type": "toolCall", "id": "c1", "name": "ls", "arguments": "{}"},
            ],
        }
        deltas = projector.project(message)
        by_type = {d.type: d for d in deltas}
        assert by_type["text"].delta == "Running ls"
        assert by_type["toolCall"].delta is None
        assert by_type["toolCall"].block is not None

    def test_mutating_callers_block_in_place_after_project_is_not_silently_dropped(self):
        """A producer that reuses and mutates the same block dict object
        (rather than building a fresh one) must not have its next real change
        silently swallowed by the projector comparing the dict to itself.
        """
        projector = MessageDeltaProjector()
        block = {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {"p": "."}}
        message = {"role": "assistant", "content": [block]}

        first = projector.project(message)
        assert len(first) == 1
        assert first[0].block == {
            "type": "toolCall",
            "id": "c1",
            "name": "ls",
            "arguments": {"p": "."},
        }

        # No change -> nothing, as usual.
        assert projector.project(message) == []

        # Mutate the SAME dict object in place (as opposed to the producer
        # building a fresh dict, which is what agent_loop.py does today via
        # model_dump() — this simulates a different, equally legal producer).
        block["arguments"] = {"p": "/etc"}
        second = projector.project(message)
        assert len(second) == 1, "a real change must still be reported, not swallowed"
        assert second[0].block == {
            "type": "toolCall",
            "id": "c1",
            "name": "ls",
            "arguments": {"p": "/etc"},
        }

    def test_emitted_block_is_independent_of_later_caller_mutation(self):
        """The BlockDelta.block payload handed back must be a snapshot, not a
        live reference to the caller's dict — mutating the caller's dict after
        the fact must not retroactively change what was already emitted.
        """
        projector = MessageDeltaProjector()
        block = {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {"p": "."}}
        emitted = projector.project({"role": "assistant", "content": [block]})[0]

        block["arguments"]["p"] = "/etc"  # mutate the producer's own dict after emission

        assert emitted.block == {
            "type": "toolCall",
            "id": "c1",
            "name": "ls",
            "arguments": {"p": "."},
        }

    def test_tool_call_shifted_from_index_0_to_1_by_arriving_text_is_not_re_emitted_whole(self):
        """Mirrors _consolidate_text_and_thinking's real ordering: a tool call
        streams alone at index 0, then once any text accumulates the content
        becomes [text, toolCall] and the SAME call sits at index 1. Keyed by
        id, this must be recognized as the same block (not re-emitted whole a
        second time with no actual change).
        """
        projector = MessageDeltaProjector()
        tool_call = {"type": "toolCall", "id": "c1", "name": "ls", "arguments": {}}

        alone = projector.project({"role": "assistant", "content": [tool_call]})
        assert len(alone) == 1

        shifted = projector.project(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Running ls"}, tool_call],
            }
        )
        shifted_by_type = {d.type: d for d in shifted}
        assert "toolCall" not in shifted_by_type, (
            "the unchanged tool call must not be re-emitted just because it "
            "moved to a new content-list index"
        )
        assert shifted_by_type["text"].delta == "Running ls"

    def test_two_distinct_tool_call_ids_at_the_same_index_are_not_conflated(self):
        """Two different tool calls that happen to land at the same index in
        successive snapshots (e.g. the first call finishes and is replaced in
        the content list by a second, distinct call) are different blocks —
        keying on id (not index) must not treat the second as a change to the
        first.
        """
        projector = MessageDeltaProjector()

        first = projector.project(
            {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "A", "name": "ls", "arguments": {}}],
            }
        )
        assert len(first) == 1
        assert first[0].block["id"] == "A"

        second = projector.project(
            {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "B", "name": "cat", "arguments": {}}],
            }
        )
        assert len(second) == 1
        assert second[0].block["id"] == "B"
        assert second[0].block["name"] == "cat"

        # Re-sending A's original (unchanged) snapshot must still be
        # recognized as unchanged -- A's identity was not overwritten by B
        # having occupied the same index=0.
        third = projector.project(
            {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "A", "name": "ls", "arguments": {}}],
            }
        )
        assert third == []


class TestReset:
    def test_reset_starts_a_fresh_message(self):
        projector = MessageDeltaProjector()
        projector.project(_text_message("Hello"))

        projector.reset()

        # Without reset, "Hi" would be flagged as a non-prefix replace of
        # "Hello". After reset it is ordinary growth from nothing.
        fresh = projector.project(_text_message("Hi"))
        assert len(fresh) == 1
        assert fresh[0].delta == "Hi"
        assert fresh[0].replace is False


class TestRT6FullTurnByteExact:
    """R-T6: a realistic sequence covering normal growth, a no-change repeat,
    a multi-block message, and the replace-not-extend case, all in one turn —
    concatenating every emitted delta must reproduce the final assistant text
    exactly.
    """

    def test_realistic_turn_reconstructs_byte_exact(self):
        projector = MessageDeltaProjector()

        def msg(thinking: str, text: str, tool_args: str | None = None) -> dict:
            content: list[dict] = []
            if thinking:
                content.append({"type": "thinking", "thinking": thinking})
            if text:
                content.append({"type": "text", "text": text})
            if tool_args is not None:
                content.append(
                    {"type": "toolCall", "id": "c1", "name": "ls", "arguments": tool_args}
                )
            return {"role": "assistant", "content": content}

        final_text = "Here are the results: a.py, b.py. All good!"
        sequence = [
            msg("Let me check the ", ""),
            msg("Let me check the directory.", ""),
            msg("Let me check the directory.", ""),  # no-change repeat (thinking)
            msg("Let me check the directory.", "", tool_args="{}"),
            msg("Let me check the directory.", "", tool_args='{"path": "."}'),
            msg("Let me check the directory.", "Here are the "),
            msg("Let me check the directory.", "Here are the results: a.py, "),
            msg("Let me check the directory.", "Here are the results: a.py, "),  # no-change
            msg("Let me check the directory.", "Here are the results: a.py, b.py. All good!"),
        ]

        text_acc = ""
        thinking_acc = ""
        tool_blocks_seen = 0
        emitted_total = 0
        for m in sequence:
            for delta in projector.project(m):
                emitted_total += 1
                if delta.type == "text":
                    text_acc = _apply(text_acc, delta)
                elif delta.type == "thinking":
                    thinking_acc = _apply(thinking_acc, delta)
                elif delta.type == "toolCall":
                    tool_blocks_seen += 1

        assert text_acc == final_text
        assert thinking_acc == "Let me check the directory."
        assert tool_blocks_seen == 2  # two DISTINCT arguments payloads only
        # The two verbatim no-change repeats (one thinking-only, one text-only)
        # must not have contributed any BlockDelta at all.
        assert emitted_total == (
            # thinking: "Let me check the " -> "...directory." = 2 changes
            2
            # toolCall: "{}" -> '{"path": "."}' = 2 changes
            + 2
            # text: "" -> "Here are the " -> "...a.py, " -> "...b.py. All good!" = 3 changes
            + 3
        )

    def test_multiple_projectors_are_independent(self):
        """One projector per streamed message (as backends.py does per turn) —
        two independent instances must not share state.
        """
        p1 = MessageDeltaProjector()
        p2 = MessageDeltaProjector()

        p1.project(_text_message("Turn one text"))
        # A fresh projector for turn two sees "Answer" as ordinary growth,
        # not a replace of turn one's unrelated text.
        deltas = p2.project(_text_message("Answer"))
        assert deltas[0].delta == "Answer"
        assert deltas[0].replace is False


class TestRealisticAgentLoopMessageShapes:
    """Drives the projector with the ACTUAL block shapes agent_loop.py builds
    (agent_loop.py:598-659), not a hand-rolled approximation:

    - toolCall blocks are built via ``tau_llm.types.ToolCall(...).model_dump()``,
      so ``arguments`` is a DICT (as ``parse_streaming_json`` always returns),
      never the string the rest of this file's toolCall tests use for brevity.
    - A ``ToolCallDeltaEvent`` snapshot's content list is
      ``_consolidate_text_and_thinking(accum) + [toolCall, ...]`` — i.e.
      ``[thinking?, text?, toolCall, ...]`` — built fresh (via ``model_dump()``
      on freshly-constructed pydantic objects) on every single event, which is
      exactly the "producer builds a new dict every time" case the passthrough
      identity fix must handle without help from object identity.
    """

    def test_thinking_then_tool_call_streaming_reconstructs_and_dedupes_by_id(self):
        projector = MessageDeltaProjector()

        def snapshot(thinking: str, tool_args: list[dict]) -> dict:
            # Mirrors _build_partial_message: content_blocks =
            # _consolidate_text_and_thinking(accum) + one ToolCall per call.
            blocks: list[dict] = []
            if thinking:
                blocks.append(ThinkingContent(thinking=thinking).model_dump())
            for i, args in enumerate(tool_args):
                blocks.append(ToolCall(id=f"c{i}", name="ls", arguments=args).model_dump())
            return {"role": "assistant", "content": blocks}

        # Pure ThinkingDeltaEvent stretch: single-block message, index 0.
        thinking_acc = ""
        for delta in projector.project(
            {
                "role": "assistant",
                "content": [ThinkingContent(thinking="Let me check").model_dump()],
            }
        ):
            assert delta.type == "thinking"
            thinking_acc = _apply(thinking_acc, delta)
        for delta in projector.project(
            {
                "role": "assistant",
                "content": [ThinkingContent(thinking="Let me check the directory.").model_dump()],
            }
        ):
            thinking_acc = _apply(thinking_acc, delta)
        assert thinking_acc == "Let me check the directory."

        # ToolCallDeltaEvent stretch: content is now [thinking, toolCall],
        # rebuilt from scratch (fresh model_dump() dicts) on every fragment —
        # arguments grows as parse_streaming_json sees more of the raw JSON.
        tool_blocks: list[dict] = []
        arg_fragments = [{}, {"path": "."}, {"path": ".", "recursive": True}]
        for args in arg_fragments:
            for delta in projector.project(snapshot("Let me check the directory.", [args])):
                if delta.type == "thinking":
                    # Same thinking text re-sent alongside the new tool call
                    # block -- no actual change, must emit nothing.
                    raise AssertionError("unchanged thinking block re-emitted")
                assert delta.type == "toolCall"
                assert isinstance(delta.block["arguments"], dict), (
                    "arguments must be a dict, matching what agent_loop.py "
                    "actually emits (model_dump of tau_llm.types.ToolCall) -- "
                    "not the string shape used elsewhere in this file"
                )
                tool_blocks.append(delta.block)

        assert [b["arguments"] for b in tool_blocks] == arg_fragments
        assert all(b["id"] == "c0" for b in tool_blocks), (
            "one call throughout -- must stay one identity"
        )

        # Re-sending the exact same last snapshot again (e.g. a duplicate
        # provider chunk) must suppress both blocks -- no actual change.
        assert projector.project(snapshot("Let me check the directory.", [arg_fragments[-1]])) == []

        # A second, distinct call joins at index 2 -- must be its own
        # identity, not conflated with c0 despite both being "the toolCall at
        # some index in a content list built fresh from scratch this call".
        second_call_deltas = projector.project(
            snapshot("Let me check the directory.", [arg_fragments[-1], {}])
        )
        assert len(second_call_deltas) == 1
        assert second_call_deltas[0].block["id"] == "c1"
