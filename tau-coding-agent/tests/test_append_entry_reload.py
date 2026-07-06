"""S39 (E6 §2) — ``customEntry`` round-trips a real on-disk ``.jsonl`` reload.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S39 (G4).

The strongest reload-invariance proof (à la S29's on-disk test): the durable
``customEntry`` node ``api.append_entry`` writes is flushed to the real ``.jsonl``
on append and reconstructed byte-identically by ``Session.load`` — surviving an
actual process-restart-shaped reload, not just an in-RAM re-fold. It also stays a
NON-message backplane node across the reload: excluded from the reconstructed
context and the LLM wire.
"""

from __future__ import annotations

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.messages import convert_to_llm
from tau_ai.types import Model
from tau_coding_agent.session_store import Session


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


def test_custom_entry_survives_ondisk_reload(tmp_path) -> None:
    """append_entry → flush → Session.load: the entry round-trips through bytes."""
    store = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    session = AgentSession(session_log=store, model=_model(), extensions=[])

    session._append_custom_entry("todo", {"text": "buy milk", "done": False, "n": 3})

    before = store.entries()
    assert sum(1 for e in before if e.get("type") == "customEntry") == 1

    # A real reload from the persisted JSONL bytes.
    reloaded = Session.load(store.path)
    assert reloaded.entries() == before  # byte-identical round-trip
    entries = [e for e in reloaded.entries() if e.get("type") == "customEntry"]
    assert len(entries) == 1
    assert entries[0]["customType"] == "todo"
    assert entries[0]["data"] == {"text": "buy milk", "done": False, "n": 3}


def test_custom_entry_off_the_wire_after_reload(tmp_path) -> None:
    """The reloaded backplane node stays out of the context and the LLM wire."""
    store = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    session = AgentSession(session_log=store, model=_model(), extensions=[])
    session._session_log.append_message({"role": "user", "content": "hello"})
    session._append_custom_entry("secret", {"payload": "MODEL MUST NOT SEE THIS"})

    reloaded = Session.load(store.path)
    context = ConversationTree(reloaded.entries(), reloaded.cursor).context_for()
    # The real turn folds back…
    assert "hello" in _text_blob(context)
    # …but the customEntry never becomes a message, so it never reaches the wire.
    assert "MODEL MUST NOT SEE THIS" not in _text_blob(context)
    assert "MODEL MUST NOT SEE THIS" not in _text_blob(convert_to_llm(context))
