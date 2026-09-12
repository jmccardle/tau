"""``display`` has a reader, and a browser row says what a node holds.

Reference: docs/EXTENSION-MESSAGES.md. Two faults of one shape — a field an
extension sets that nothing downstream read, so the instruction succeeded and did
nothing. §1 is ``display``; §3 is the row that named the kind and nothing else.
"""

from __future__ import annotations

from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.extension_locks import REQUEST_ENTRY_TYPE, build_request_data
from tau_agent_core.messages import create_custom_message, is_displayed
from tau_agent_core.session_log import InMemorySessionLog


def _preview(log: InMemorySessionLog) -> str:
    return ConversationTree(log.entries(), log.cursor).tree()[0].preview


def test_a_hidden_custom_message_is_not_displayed() -> None:
    assert is_displayed(create_custom_message("probe", "x", display=False)) is False


def test_a_custom_message_is_displayed_by_default() -> None:
    assert is_displayed(create_custom_message("probe", "x")) is True


def test_a_node_predating_the_key_is_displayed() -> None:
    """An older node never asked to be hidden, so absence is not a request."""
    assert is_displayed({"role": "custom", "customType": "probe", "content": []}) is True


def test_every_other_role_is_displayed() -> None:
    """Only a custom message carries the key; nothing else may be filtered by it."""
    assert is_displayed({"role": "user", "content": "hi", "display": False}) is True


def test_visible_to_model_is_a_different_question() -> None:
    """``display`` is about the transcript and ``visibleToModel`` about the wire."""
    message = create_custom_message("probe", "x", display=False, visible_to_model=True)
    assert is_displayed(message) is False
    assert message["visibleToModel"] is True


def test_a_hidden_node_still_has_a_row_and_says_so() -> None:
    """The transcript obeys the flag; the tree is where the node stays visible."""
    log = InMemorySessionLog()
    log.append_custom_message(create_custom_message("probe", "the note", display=False), "probe")
    assert _preview(log) == "probe (hidden): the note"


def test_a_displayed_node_names_the_extension_type() -> None:
    log = InMemorySessionLog()
    log.append_custom_message(create_custom_message("tectum_note", "the build failed"), "tectum_note")
    assert _preview(log) == "tectum_note: the build failed"


def test_a_node_with_no_text_summarizes_its_details() -> None:
    """``(customMessage)`` was the whole row when the content held no text block."""
    log = InMemorySessionLog()
    message = create_custom_message("edit", [], details={"path": "/a/b.py", "line": 42})
    log.append_custom_message(message, "edit")
    assert _preview(log) == "edit: path=/a/b.py, line=42"


def test_a_node_with_nothing_at_all_says_so() -> None:
    log = InMemorySessionLog()
    log.append_custom_message(create_custom_message("ni", []), "ni")
    assert _preview(log) == "ni: no content"


def test_a_request_entry_reads_as_its_state_and_its_sentence() -> None:
    """The four states of docs/EXTENSION-LOCKS.md §3, legible from the row."""
    log = InMemorySessionLog()
    log.append_custom_entry(
        REQUEST_ENTRY_TYPE,
        build_request_data("/home/j/tectum.py", "Approve rm -rf build?", lock=True),
    )
    assert _preview(log) == "Extension tectum requires intervention: Approve rm -rf build?"


def test_a_malformed_request_falls_back_rather_than_raising() -> None:
    """A browser reads a hand-edited log; it does not enforce against one."""
    log = InMemorySessionLog()
    log.append_custom_entry(REQUEST_ENTRY_TYPE, {"sentence": "no extension key"})
    assert _preview(log) == "extension_request — sentence=no extension key"


def test_a_payload_is_summarized_and_never_dumped() -> None:
    """Four fields named, the rest counted, and a long value cut."""
    log = InMemorySessionLog()
    log.append_custom_entry("state", {"a": "x" * 90, "b": 2, "c": [1, 2], "d": {}, "e": 5})
    preview = _preview(log)
    assert preview.startswith("state — a=" + "x" * 39 + "…, b=2, c=[2], d={0 keys}")
    assert preview.endswith("+1 more")
