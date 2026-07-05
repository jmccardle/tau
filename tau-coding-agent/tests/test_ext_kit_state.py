"""Tests for ``examples/ext_kit/state.py`` — the S56 *backplane* primitive.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S56.

Two stores, two persistence stories:

* **TreeStore** — typed records over the durable ``customEntry`` node (S39): the
  append→load round-trip, custom-type isolation, the "latest snapshot" read, typed
  (dataclass) records via ``encode``/``decode``, active-path reconstruction that
  drops records off an abandoned branch, the backplane guarantee (records never
  reach ``convert_to_llm``), and — the headline — a RELOAD-INVARIANCE proof against
  a real on-disk ``Session`` reload (à la S29/S39).
* **FileStore** — atomic cross-session JSON: save/load round-trip and cross-session
  reopen, the atomic temp-file+replace mechanism (no leftover temp, existing state
  survives a failed serialization), the missing-file Fail-Early (raise vs supplied
  default), corrupt-file raise, and name-traversal rejection.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.messages import convert_to_llm
from tau_ai.types import Model
from tau_coding_agent.session_store import Session

# ── import the kit as a top-level package (examples/ on the path) ────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = str(_REPO_ROOT / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

from ext_kit import state  # noqa: E402  (path insertion must precede the import)


# ── fixtures: a real (on-disk) session + a bound ExtensionAPI ─────────────────


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


def _session(tmp_path: Path) -> Session:
    return Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)


def _api_for(store: Session) -> ExtensionAPI:
    session = AgentSession(session_log=store, model=_model(), extensions=[])
    return ExtensionAPI(session=session)


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


# ── TreeStore: the append → load round-trip ──────────────────────────────────


def test_treestore_append_then_load(tmp_path):
    api = _api_for(_session(tmp_path))
    store: state.TreeStore = state.TreeStore(api, "todo")

    store.append({"text": "buy milk", "done": False})
    store.append({"text": "walk dog", "done": True})

    # A FRESH store over the same log reconstructs both records in tree order.
    fresh: state.TreeStore = state.TreeStore(api, "todo")
    records = fresh.load()
    assert records == [
        {"text": "buy milk", "done": False},
        {"text": "walk dog", "done": True},
    ]
    assert len(fresh) == 2
    assert list(fresh) == records


def test_treestore_requires_custom_type(tmp_path):
    api = _api_for(_session(tmp_path))
    with pytest.raises(ValueError, match="custom_type is required"):
        state.TreeStore(api, "")


def test_treestore_latest_snapshot(tmp_path):
    """``latest()`` is the "full snapshot wins" read (the todo/list pattern)."""
    api = _api_for(_session(tmp_path))
    store: state.TreeStore = state.TreeStore(api, "todos")
    assert store.latest() is None

    store.append({"items": ["a"]})
    store.append({"items": ["a", "b"]})
    assert store.latest() == {"items": ["a", "b"]}

    reloaded: state.TreeStore = state.TreeStore(api, "todos")
    reloaded.load()
    assert reloaded.latest() == {"items": ["a", "b"]}


def test_treestore_isolates_custom_types(tmp_path):
    """Two stores of different types over one log never cross-contaminate."""
    api = _api_for(_session(tmp_path))
    todos: state.TreeStore = state.TreeStore(api, "todo")
    marks: state.TreeStore = state.TreeStore(api, "bookmark")

    todos.append({"text": "task"})
    marks.append({"label": "here", "entry": "abc"})
    todos.append({"text": "task2"})

    assert state.TreeStore(api, "todo").load() == [{"text": "task"}, {"text": "task2"}]
    assert state.TreeStore(api, "bookmark").load() == [{"label": "here", "entry": "abc"}]


def test_treestore_append_rejects_non_dict_record(tmp_path):
    """A record that does not encode to a dict raises (Fail-Early, not dropped)."""
    api = _api_for(_session(tmp_path))
    store: state.TreeStore = state.TreeStore(api, "notes")
    with pytest.raises(TypeError, match="must encode to a dict"):
        store.append("just a string")  # type: ignore[arg-type]


# ── TreeStore: typed records via encode/decode ───────────────────────────────


@dataclass
class Bookmark:
    label: str
    entry_id: str


def test_treestore_typed_records_roundtrip(tmp_path):
    api = _api_for(_session(tmp_path))
    store: state.TreeStore[Bookmark] = state.TreeStore(
        api,
        "bookmark",
        encode=lambda b: {"label": b.label, "entry_id": b.entry_id},
        decode=lambda d: Bookmark(label=d["label"], entry_id=d["entry_id"]),
    )
    store.append(Bookmark("start", "aaa"))
    store.append(Bookmark("fix", "bbb"))

    reloaded: state.TreeStore[Bookmark] = state.TreeStore(
        api,
        "bookmark",
        decode=lambda d: Bookmark(label=d["label"], entry_id=d["entry_id"]),
    )
    assert reloaded.load() == [Bookmark("start", "aaa"), Bookmark("fix", "bbb")]

    # The persisted form on the tree is the plain dict (JSON-shaped).
    raw = [e for e in api.context.entries() if e.get("type") == "customEntry"]
    assert raw[0]["data"] == {"label": "start", "entry_id": "aaa"}


# ── TreeStore: active-path reconstruction (tree-as-truth) ────────────────────


def test_treestore_excludes_abandoned_branch(tmp_path):
    """Records on a branch the cursor navigated away from are not reconstructed.

    Reconstructing from *all* entries would resurrect an abandoned branch's
    records — a silent divergence from what the session shows. TreeStore walks the
    active ``parentId`` chain, so a record appended, then navigated-past, drops out.
    """
    store = _session(tmp_path)
    api = _api_for(store)
    ts: state.TreeStore = state.TreeStore(api, "note")

    # A message to branch from, then a record on the current tip.
    root_id = store.append_message({"role": "user", "content": "root"})
    ts.append({"n": "on-main"})

    # Navigate the cursor back to the root, then append a record on the new branch.
    store.append_navigate(root_id)
    ts.append({"n": "on-branch"})

    # Active path = root → on-branch; the on-main record is off the active branch.
    records = state.TreeStore(api, "note").load()
    assert records == [{"n": "on-branch"}]


def test_treestore_records_never_reach_the_model(tmp_path):
    """The backplane guarantee: a TreeStore record is excluded from the LLM wire."""
    store = _session(tmp_path)
    api = _api_for(store)
    store.append_message({"role": "user", "content": "hello"})
    ts: state.TreeStore = state.TreeStore(api, "secret")
    ts.append({"payload": "MODEL MUST NOT SEE THIS"})

    context = ConversationTree(store.entries(), store.cursor).context_for()
    assert "hello" in _text_blob(context)
    assert "MODEL MUST NOT SEE THIS" not in _text_blob(context)
    assert "MODEL MUST NOT SEE THIS" not in _text_blob(convert_to_llm(context))


# ── TreeStore: RELOAD-INVARIANCE (real on-disk Session reload) ───────────────


def test_treestore_survives_ondisk_reload(tmp_path):
    """append → flush → Session.load: a fresh TreeStore reconstructs the records.

    The S56 reload-invariance proof, à la S39's on-disk test: the durable
    ``customEntry`` nodes the store appends are flushed to the real ``.jsonl`` and
    reconstructed byte-identically after an actual process-restart-shaped reload.
    """
    store = _session(tmp_path)
    api = _api_for(store)
    ts: state.TreeStore = state.TreeStore(api, "todo")
    ts.append({"text": "buy milk", "done": False, "n": 1})
    ts.append({"text": "walk dog", "done": True, "n": 2})

    # A real reload from the persisted JSONL bytes, then a fresh store over it.
    reloaded = Session.load(store.path)
    reloaded_api = _api_for(reloaded)
    survived = state.TreeStore(reloaded_api, "todo").load()
    assert survived == [
        {"text": "buy milk", "done": False, "n": 1},
        {"text": "walk dog", "done": True, "n": 2},
    ]


# ── FileStore: save / load round-trip + cross-session ────────────────────────


def test_filestore_save_load_roundtrip(tmp_path):
    fs = state.FileStore("ledger", base_dir=tmp_path)
    assert fs.exists() is False
    fs.save({"total": 1.25, "runs": ["a", "b"]})
    assert fs.exists() is True
    assert fs.load() == {"total": 1.25, "runs": ["a", "b"]}
    assert fs.path == tmp_path / "ledger.json"


def test_filestore_cross_session_reopen(tmp_path):
    """A second FileStore over the same path sees the first's writes (cross-session)."""
    state.FileStore("corpus", base_dir=tmp_path).save([{"finding": "sql-injection"}])
    reopened = state.FileStore("corpus", base_dir=tmp_path)
    assert reopened.load() == [{"finding": "sql-injection"}]


def test_filestore_overwrite_replaces(tmp_path):
    fs = state.FileStore("s", base_dir=tmp_path)
    fs.save({"v": 1})
    fs.save({"v": 2})
    assert fs.load() == {"v": 2}


# ── FileStore: the atomic-write mechanism ────────────────────────────────────


def test_filestore_save_leaves_no_temp_file(tmp_path):
    fs = state.FileStore("s", base_dir=tmp_path)
    fs.save({"ok": True})
    # Exactly the store file — the temp file was renamed into place, not left behind.
    present = sorted(p.name for p in tmp_path.iterdir())
    assert present == ["s.json"]


def test_filestore_failed_save_preserves_existing(tmp_path):
    """A serialization error must not truncate the existing store or leak a temp.

    The atomic temp-file+replace protects good state: writing a non-JSON-encodable
    value raises, but the previously-saved blob is intact and no temp file lingers.
    """
    fs = state.FileStore("s", base_dir=tmp_path)
    fs.save({"good": 1})

    with pytest.raises(TypeError):
        fs.save({"bad": object()})  # not JSON-serializable

    assert fs.load() == {"good": 1}  # existing state survived
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.json"]  # no temp leftover


# ── FileStore: Fail-Early on missing / corrupt / traversal ───────────────────


def test_filestore_missing_without_default_raises(tmp_path):
    fs = state.FileStore("absent", base_dir=tmp_path)
    with pytest.raises(FileNotFoundError, match="no store at"):
        fs.load()


def test_filestore_missing_with_default_returns_it(tmp_path):
    fs = state.FileStore("absent", base_dir=tmp_path)
    assert fs.load(default=[]) == []
    assert fs.load(default={"seed": 1}) == {"seed": 1}
    # An explicit default of None is honored (not treated as "unset").
    assert fs.load(default=None) is None


def test_filestore_corrupt_file_raises(tmp_path):
    fs = state.FileStore("bad", base_dir=tmp_path)
    fs.path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        fs.load()  # no silent reset of real (if corrupt) state


@pytest.mark.parametrize("bad", ["../escape", "a/b", "sub/../x", ".", "..", ""])
def test_filestore_rejects_unsafe_names(bad):
    with pytest.raises(ValueError):
        state.FileStore(bad, base_dir="/tmp")


def test_filestore_default_dir_under_tau(monkeypatch, tmp_path):
    """With no base_dir, the store lands under ``$HOME/.tau/ext-state/``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    fs = state.FileStore("l")
    assert fs.path == tmp_path / ".tau" / "ext-state" / "l.json"
