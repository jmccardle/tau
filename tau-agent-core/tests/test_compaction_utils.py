"""Tests for compaction_utils — file-op tracking + conversation serialization.

Port-parity checks against pi's utils.ts behavior, exercised on τ message dicts.
"""

from __future__ import annotations

from tau_agent_core.compaction_utils import (
    TOOL_RESULT_MAX_CHARS,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)


def _assistant(*blocks: dict) -> dict:
    return {"role": "assistant", "content": list(blocks)}


def _tool_call(name: str, **args) -> dict:
    return {"type": "toolCall", "id": f"c_{name}", "name": name, "arguments": args}


# ── file-op extraction ────────────────────────────────────────────────────


def test_extract_records_read_write_edit_by_tool_name():
    ops = create_file_ops()
    extract_file_ops_from_message(
        _assistant(
            _tool_call("read", path="a.py"),
            _tool_call("write", path="b.py"),
            _tool_call("edit", path="c.py"),
        ),
        ops,
    )
    assert ops.read == {"a.py"}
    assert ops.written == {"b.py"}
    assert ops.edited == {"c.py"}


def test_extract_ignores_non_assistant_and_pathless_calls():
    ops = create_file_ops()
    # Non-assistant messages carry no tool calls.
    extract_file_ops_from_message({"role": "user", "content": "hi"}, ops)
    # A tool call without a string path is skipped.
    extract_file_ops_from_message(_assistant(_tool_call("read")), ops)
    extract_file_ops_from_message(_assistant(_tool_call("bash", command="ls")), ops)
    assert ops.read == set()
    assert ops.written == set()
    assert ops.edited == set()


def test_compute_file_lists_dedups_and_prefers_modified():
    ops = create_file_ops()
    ops.read.update({"shared.py", "only_read.py"})
    ops.edited.add("shared.py")  # read AND edited -> counts only as modified
    ops.written.add("new.py")
    read_files, modified_files = compute_file_lists(ops)
    assert read_files == ["only_read.py"]
    assert modified_files == ["new.py", "shared.py"]  # sorted


def test_format_file_operations_tags_and_empty():
    assert format_file_operations([], []) == ""
    out = format_file_operations(["r.py"], ["m.py"])
    assert out == "\n\n<read-files>\nr.py\n</read-files>\n\n<modified-files>\nm.py\n</modified-files>"


# ── conversation serialization ────────────────────────────────────────────


def test_serialize_labels_each_role():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "do the thing"}]},
        _assistant(
            {"type": "thinking", "thinking": "let me plan"},
            {"type": "text", "text": "on it"},
            _tool_call("read", path="x.py"),
        ),
        {
            "role": "toolResult",
            "content": [{"type": "text", "text": "file contents"}],
        },
    ]
    out = serialize_conversation(messages)
    assert "[User]: do the thing" in out
    assert "[Assistant thinking]: let me plan" in out
    assert "[Assistant]: on it" in out
    assert "[Assistant tool calls]: read(path=\"x.py\")" in out
    assert "[Tool result]: file contents" in out


def test_serialize_truncates_long_tool_results():
    big = "x" * (TOOL_RESULT_MAX_CHARS + 500)
    out = serialize_conversation(
        [{"role": "toolResult", "content": [{"type": "text", "text": big}]}]
    )
    assert "more characters truncated" in out
    assert len(out) < len(big) + 200  # not the full payload


def test_serialize_handles_plain_string_user_content():
    out = serialize_conversation([{"role": "user", "content": "plain string"}])
    assert out == "[User]: plain string"
