"""The head-agnostic reading of a completion the output cap cut off.

Reference: docs/TRUNCATED-TOOL-CALLS.md §3. The TUI's box, print mode's stderr
lines and an RPC host's ``message_end`` fields all describe the same fact; this
is where that fact is decided, so all three agree by construction.
"""

from __future__ import annotations

from tau_agent_core.truncation import (
    Truncation,
    completion_truncation,
    dropped_tool_calls,
    truncation_from_messages,
    truncation_notice,
)


def _assistant(stop_reason: str | None, dropped: int | None = None) -> dict:
    usage: dict = {"total_tokens": 100, "output_tokens": 90}
    if dropped is not None:
        usage["extra"] = {"dropped_partial_tool_calls": dropped}
    message: dict = {"role": "assistant", "content": [], "usage": usage}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return message


class TestDroppedToolCalls:
    def test_the_count_is_read_off_usage_extra(self):
        assert dropped_tool_calls({"extra": {"dropped_partial_tool_calls": 2}}) == 2

    def test_a_completion_that_dropped_none_wrote_no_key(self):
        assert dropped_tool_calls({"extra": {}}) == 0
        assert dropped_tool_calls({}) == 0

    def test_a_non_integer_count_reads_as_none_dropped(self):
        """A count τ did not write is not evidence of a loss."""
        assert dropped_tool_calls({"extra": {"dropped_partial_tool_calls": "two"}}) == 0
        assert dropped_tool_calls({"extra": {"dropped_partial_tool_calls": True}}) == 0
        assert dropped_tool_calls({"extra": {"dropped_partial_tool_calls": -1}}) == 0


class TestOneCompletion:
    def test_a_length_stop_is_one_truncated_completion(self):
        assert completion_truncation(_assistant("length")) == Truncation(1, 0)

    def test_it_carries_the_calls_that_completion_lost(self):
        assert completion_truncation(_assistant("length", 3)) == Truncation(1, 3)

    def test_every_other_stop_reason_is_nothing(self):
        for reason in ("stop", "toolUse", "error", None):
            assert completion_truncation(_assistant(reason)) == Truncation(0, 0)

    def test_an_aborted_completions_drops_are_not_the_caps_doing(self):
        """An abort drops calls too. This function says what the CAP did, and an
        Esc the user pressed is not a cap to raise."""
        assert completion_truncation(_assistant("aborted", 2)) == Truncation(0, 0)

    def test_a_message_with_no_usage_still_reports_the_stop(self):
        assert completion_truncation({"role": "assistant", "stop_reason": "length"}) == Truncation(
            1, 0
        )


class TestAWholeTurn:
    def test_two_truncated_completions_are_two_losses(self):
        """Unlike a prompt size, these SUM: each cut is its own missing answer."""
        messages = [_assistant("length", 1), _assistant("toolUse"), _assistant("length", 2)]
        assert truncation_from_messages(messages) == Truncation(2, 3)

    def test_a_clean_turn_is_nothing(self):
        assert truncation_from_messages([_assistant("stop")]) == Truncation(0, 0)
        assert truncation_from_messages([]) == Truncation(0, 0)

    def test_a_user_message_is_not_read(self):
        messages = [{"role": "user", "content": "hi", "stop_reason": "length"}]
        assert truncation_from_messages(messages) == Truncation(0, 0)


class TestTheNotice:
    def test_nothing_truncated_says_nothing(self):
        assert truncation_notice(Truncation(0, 0), max_tokens=4096) is None

    def test_it_quotes_the_cap_it_was_given(self):
        notice = truncation_notice(Truncation(1, 0), max_tokens=900)
        assert notice is not None
        assert "max_tokens = 900" in notice
        assert "stop_reason: length" in notice

    def test_an_unknown_cap_says_unknown_rather_than_a_default(self):
        """Fail Early: quoting 4096 at an operator whose real cap is something
        else sends them to change a number that was already right."""
        notice = truncation_notice(Truncation(1, 0), max_tokens=None)
        assert notice is not None
        assert "max_tokens = unknown" in notice

    def test_a_dropped_call_is_named_and_counted(self):
        notice = truncation_notice(Truncation(1, 1), max_tokens=4096)
        assert notice is not None
        assert "1 tool call was dropped" in notice

    def test_several_dropped_calls_read_as_plural(self):
        notice = truncation_notice(Truncation(2, 3), max_tokens=4096)
        assert notice is not None
        assert "2 of this turn's completions" in notice
        assert "3 tool calls were dropped" in notice

    def test_no_dropped_call_adds_no_second_sentence(self):
        notice = truncation_notice(Truncation(1, 0), max_tokens=4096)
        assert notice is not None
        assert "dropped" not in notice
