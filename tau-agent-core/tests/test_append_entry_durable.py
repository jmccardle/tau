"""S39 (E6 §2) — ``api.append_entry`` persists a durable ``customEntry`` node.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S39 (G4; the tree-as-backplane
foundation S56's ``TreeStore`` builds on).

Before S39 ``api.append_entry`` wrote to the registry's RAM-only ``_entry_store``,
lost on restart. It now APPENDs a ``{customType, data}`` node of its own tree entry
KIND (``customEntry``) to the authoritative session log, proving:

* the entry lands on the durable path (persisted), readable through ``ctx.entries()``;
* it is a NON-message node — ``ConversationTree`` never folds it into context and
  ``convert_to_llm`` never sees it, so it stays excluded from model input;
* it survives a RELOAD — a fresh fold / raw-entry read over the persisted entries
  still carries it, ``{customType, data}`` round-tripping intact (à la S29);
* Fail-Early: no session bound, an empty ``custom_type``, or non-dict ``data`` all
  raise rather than silently dropping the entry.
"""

from __future__ import annotations

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.messages import convert_to_llm
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.types import Model


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _make_session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), extensions=[])


def _text_blob(messages: list) -> str:
    out: list[str] = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for block in content:
                out.append(str(block.get("text", "")) if isinstance(block, dict) else "")
    return "\n".join(out)


def test_append_entry_persists_a_custom_entry_node() -> None:
    """The entry is a persisted ``customEntry`` on the log, carrying customType+data."""
    session = _make_session()
    api = ExtensionAPI(session=session)

    api.append_entry("todo", {"text": "buy milk", "done": False})

    entries = session._session_log.entries()
    custom = [e for e in entries if e.get("type") == "customEntry"]
    assert len(custom) == 1
    assert custom[0]["customType"] == "todo"
    assert custom[0]["data"] == {"text": "buy milk", "done": False}
    # A real tree node: it advanced the leaf and sits on the parentId chain.
    assert session._session_log.cursor == custom[0]["id"]


def test_append_entry_excluded_from_context_and_wire() -> None:
    """A ``customEntry`` is a non-message node: never rendered, never on the wire."""
    session = _make_session()
    api = ExtensionAPI(session=session)

    api.append_entry("secret", {"payload": "MODEL MUST NOT SEE THIS"})

    tree = ConversationTree(session._session_log.entries(), session._session_log.cursor)
    # It emits NO loop message (context_for skips the non-message kind)…
    context = tree.context_for()
    assert "MODEL MUST NOT SEE THIS" not in _text_blob(context)
    # …so convert_to_llm never even sees it — the model input stays clean.
    wire = convert_to_llm(context)
    assert "MODEL MUST NOT SEE THIS" not in _text_blob(wire)


def test_append_entry_readable_through_ctx_entries() -> None:
    """The durable entry is read back through ``ctx.entries()`` (S56 reconstruction)."""
    session = _make_session()
    api = ExtensionAPI(session=session)

    api.append_entry("bookmark", {"label": "start"})
    api.append_entry("bookmark", {"label": "mid"})

    # ``ctx`` is the ExtensionContext ExtensionAPI bound the session onto.
    entries = api._context.entries()
    bookmarks = [e for e in entries if e.get("type") == "customEntry"]
    assert [e["data"]["label"] for e in bookmarks] == ["start", "mid"]
    assert all(e["customType"] == "bookmark" for e in bookmarks)


def test_append_entry_survives_reload() -> None:
    """Reload-invariance: a fresh read over the persisted entries keeps the entry."""
    session = _make_session()
    api = ExtensionAPI(session=session)
    api.append_entry("counter", {"value": 42})

    # Simulate a reload: read the persisted entries alone, rebuild the tree.
    persisted = session._session_log.entries()
    reloaded = [e for e in persisted if e.get("type") == "customEntry"]
    assert len(reloaded) == 1
    assert reloaded[0]["customType"] == "counter"
    assert reloaded[0]["data"] == {"value": 42}
    # Still excluded from a post-reload fold (no message leaks).
    context = ConversationTree(persisted, session._session_log.cursor).context_for()
    assert "42" not in _text_blob(context)


def test_append_entry_interleaves_with_messages_without_polluting_context() -> None:
    """Backplane entries between real turns don't enter the model context."""
    session = _make_session()
    api = ExtensionAPI(session=session)

    session._session_log.append_message({"role": "user", "content": "hello"})
    api.append_entry("trace", {"step": 1})
    session._session_log.append_message({"role": "assistant", "content": "hi there"})

    context = ConversationTree(
        session._session_log.entries(), session._session_log.cursor
    ).context_for()
    blob = _text_blob(context)
    # The real turns fold onto the path…
    assert "hello" in blob and "hi there" in blob
    # …but the interleaved customEntry does not.
    assert "step" not in blob
    assert "customEntry" not in blob


def test_append_entry_raises_without_session() -> None:
    """Fail-Early: no session bound → raise, not a RAM store that evaporates (G4)."""
    api = ExtensionAPI()
    with pytest.raises(RuntimeError, match="append_entry"):
        api.append_entry("todo", {"text": "x"})


def test_append_entry_rejects_empty_custom_type() -> None:
    """Fail-Early: the extension-origin identity is required, never fabricated."""
    session = _make_session()
    api = ExtensionAPI(session=session)
    with pytest.raises(ValueError, match="custom_type"):
        api.append_entry("", {"text": "x"})


def test_append_entry_rejects_non_dict_data() -> None:
    """Fail-Early: data must be a dict (structured record), not a bare value."""
    session = _make_session()
    api = ExtensionAPI(session=session)
    with pytest.raises(ValueError, match="data"):
        api.append_entry("todo", "not a dict")  # type: ignore[arg-type]
