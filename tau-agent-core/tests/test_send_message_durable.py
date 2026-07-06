"""S38 (E6 §2) — ``api.send_message`` appends a durable ``customMessage`` node.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S38 (G5 / D-E6-1).

Proves the formerly-inert ``api.send_message`` is now honest:

* it APPENDs a ``role: "custom"`` node to the authoritative session log, so the
  message lands on the active path (persisted + rendered), like a
  ``before_agent_start`` injection (S29);
* the node survives a RELOAD — a fresh ``ConversationTree`` fold over the persisted
  entries still carries it (the reload-invariance proof, à la S29);
* it is **display-only by default** (D-E6-1): ``convert_to_llm`` DROPS it, so the
  model never sees it — unless ``options={"visible_to_model": True}`` opts in, in
  which case it is remapped custom→user on the wire.
"""

from __future__ import annotations

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.messages import convert_to_llm
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.types import Model


def _make_session() -> AgentSession:
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    return AgentSession(session_log=InMemorySessionLog(), model=model, extensions=[])


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


def test_send_message_appends_durable_custom_message_node() -> None:
    """The node is a persisted ``customMessage`` entry on the active path."""
    session = _make_session()
    api = ExtensionAPI(session=session)

    api.send_message({"customType": "gate-note", "content": "policy applied"})

    entries = session._session_log.entries()
    custom = [e for e in entries if e.get("type") == "customMessage"]
    assert len(custom) == 1
    assert custom[0]["customType"] == "gate-note"
    assert custom[0]["message"]["role"] == "custom"
    # Rendered on the active path (persisted == rendered).
    path = ConversationTree(entries, session._session_log.cursor).context_for()
    assert "policy applied" in _text_blob(path)


def test_send_message_display_only_by_default_off_the_wire() -> None:
    """Default is display-only: on the path, dropped by convert_to_llm (D-E6-1)."""
    session = _make_session()
    api = ExtensionAPI(session=session)

    api.send_message({"customType": "gate-note", "content": "secret to the model"})

    path = ConversationTree(session._session_log.entries(), session._session_log.cursor).context_for()
    # On the rendered path…
    assert "secret to the model" in _text_blob(path)
    # …but NOT on the wire — the display-only custom node is dropped.
    wire = convert_to_llm(path)
    assert "secret to the model" not in _text_blob(wire)
    assert all((not isinstance(m, dict)) or m.get("role") != "custom" for m in wire)


def test_send_message_visible_to_model_opt_in_reaches_the_wire() -> None:
    """visible_to_model=True remaps custom→user so the model sees it."""
    session = _make_session()
    api = ExtensionAPI(session=session)

    api.send_message(
        {"customType": "gate-note", "content": "the model should read this"},
        {"visible_to_model": True},
    )

    path = ConversationTree(session._session_log.entries(), session._session_log.cursor).context_for()
    wire = convert_to_llm(path)
    assert "the model should read this" in _text_blob(wire)
    # Remapped to a real user message the provider accepts.
    assert any(isinstance(m, dict) and m.get("role") == "user" for m in wire)


def test_send_message_survives_reload() -> None:
    """Reload-invariance: a fresh fold over the persisted entries keeps the node."""
    session = _make_session()
    api = ExtensionAPI(session=session)
    api.send_message({"customType": "gate-note", "content": "durable across reload"})

    # Simulate a reload: rebuild the tree from the persisted entries alone.
    persisted = session._session_log.entries()
    reloaded = ConversationTree(persisted, session._session_log.cursor)
    assert "durable across reload" in _text_blob(reloaded.context_for())
    # The visibleToModel flag also round-trips (still display-only on the wire).
    assert "durable across reload" not in _text_blob(convert_to_llm(reloaded.context_for()))


def test_send_message_requires_content_and_custom_type() -> None:
    """Fail-Early: missing content or customType raises (no fabricated default)."""
    session = _make_session()
    api = ExtensionAPI(session=session)

    import pytest

    with pytest.raises(ValueError):
        api.send_message({"customType": "gate-note"})  # no content
    with pytest.raises(ValueError):
        api.send_message({"content": "hi"})  # no customType
