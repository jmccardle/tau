"""docs/EXTENSION-LOCKS.md — an extension stops the session, and asks.

The core half. One reserved ``customEntry`` carries a lock, an ask, or both;
``submit`` refuses at the cursor and only there; the four escapes are ordinary
tree operations. The TUI half (the bounce, the transcript row, the modal) is
``tau-coding-agent/tests/test_extension_locks_tui.py``.
"""

from __future__ import annotations

import copy

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_locks import (
    REQUEST_ENTRY_TYPE,
    RESPONSE_ENTRY_TYPE,
    build_request_data,
    read_request,
    refusal_reason,
    request_at_cursor,
)
from tau_agent_core.extension_types import ExtensionAPI, validate_ask_spec
from tau_agent_core.messages import convert_to_llm
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.session_log import InMemorySessionLog, resolve_cursor
from tau_agent_core.submission import Submission
from tau_llm.types import Model

_ASK = {
    "title": "Voice",
    "text": "Pick one before continuing.",
    "fields": [{"name": "voice", "kind": "select", "options": ["alto", "bass"]}],
    "actions": [{"label": "Use it", "command": "gate-release"}],
}


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


def _gate(api):
    """An extension that arms a locked ask at ``session_start``."""

    async def release(args, ctx):
        return f"released {args}"

    async def arm(event, ctx):
        api.request_user_action("Pick a voice.", lock=True, ask=_ASK, release="gate-release")

    api.register_command("gate-release", {"description": "release", "handler": release})
    api.on("session_start", arm)


async def _session(log: InMemorySessionLog | None = None, extensions=(_gate,)) -> AgentSession:
    """A started session. ``session_start`` runs, so an arming hook has fired.

    The request is armed from a hook rather than from ``register`` because
    ``AgentSession.__init__`` appends its ``agent_spec`` node AFTER binding
    extensions — an entry appended after a request moves the cursor off it, and
    the cursor is the whole read (§2).
    """
    session = AgentSession(
        session_log=log or InMemorySessionLog(),
        model=_model(),
        extensions=list(extensions),
    )
    await session.emit_session_start()
    return session


_FILE_GATE = '''
ASK = {
    "title": "Voice",
    "text": "Pick one before continuing.",
    "fields": [{"name": "voice", "kind": "select", "options": ["alto", "bass"]}],
    "actions": [{"label": "Use it", "command": "gate-release"}],
}


def register(api):
    async def arm(event, ctx):
        api.request_user_action("Pick a voice.", lock=True, ask=ASK, release="gate-release")

    api.on("session_start", arm)
'''


async def _file_gate_session(tmp_path) -> AgentSession:
    """The same gate, loaded from a FILE, because only a file can be disabled.

    ``disable_extension`` resolves a path against the loaded-extension registry;
    an inline factory has a ``module:qualname`` label and no entry there.
    """
    gate = tmp_path / "gate.py"
    gate.write_text(_FILE_GATE)
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), extensions=[])
    await session.load_extensions([str(gate)])
    await session.emit_session_start()
    return session


def _reloaded_log(entries: list[dict]) -> InMemorySessionLog:
    """An in-memory log seeded from ``entries``, cursor resolved as a load does.

    :class:`InMemorySessionLog` has no load path of its own, so this performs the
    one every real store performs: keep the entries, and set the leaf to
    :func:`resolve_cursor` over them.
    """
    log = InMemorySessionLog()
    log._entries = copy.deepcopy(entries)
    log._ids = {str(e["id"]) for e in entries}
    log._leaf_id = resolve_cursor(entries)
    return log


def _submission(text: str, **kwargs) -> Submission:
    return Submission(
        text=text,
        source="interactive",
        submitter="human",
        submission_id=f"s-{text}",
        **kwargs,
    )


# ── the entry ────────────────────────────────────────────────────────────────


def test_a_request_neither_locking_nor_asking_is_refused() -> None:
    """§4: that entry is plain bookkeeping, and ``api.append_entry`` already spells it."""
    with pytest.raises(ValueError, match="append_entry"):
        build_request_data("/x/gate.py", "nothing to see")


async def test_the_entry_stores_the_extension_rather_than_looking_it_up() -> None:
    """§4: the case this exists for is a reload where the owner never loaded."""
    session = await _session()
    entry = next(
        e for e in session._session_log.entries() if e.get("customType") == REQUEST_ENTRY_TYPE
    )
    assert entry["data"]["extension"].endswith("_gate")
    assert read_request(entry).extension_name.endswith("_gate")


def test_an_unparseable_request_does_not_lock() -> None:
    """A hand-edited log is read, not enforced against: no sentence, no lock."""
    assert read_request({"type": "customEntry", "customType": REQUEST_ENTRY_TYPE, "id": "x"}) is None


async def test_the_request_is_not_model_input() -> None:
    """A ``customEntry`` is tree-as-backplane state; ``convert_to_llm`` never sees it."""
    session = await _session()
    entries = session._session_log.entries()
    tree = ConversationTree(entries, resolve_cursor(entries))
    blob = str(convert_to_llm(tree.context_for(resolve_cursor(entries))))
    assert "Pick a voice" not in blob


# ── the label, over the two keys (§9) ────────────────────────────────────────


@pytest.mark.parametrize(
    "lock,ask,expected",
    [
        (True, _ASK, "requires a response"),
        (True, None, "requires intervention"),
        (False, _ASK, "requests a response"),
    ],
)
def test_the_label_is_a_function_of_the_two_keys(lock, ask, expected) -> None:
    data = build_request_data(
        "/x/tectum.py", "s", lock=lock, ask=None if ask is None else validate_ask_spec(ask)
    )
    request = read_request(
        {"type": "customEntry", "customType": REQUEST_ENTRY_TYPE, "id": "e1", "data": data}
    )
    assert request.label == f"Extension tectum {expected}"


# ── the check, at the cursor and only there (§2, §5) ─────────────────────────


async def test_a_lock_refuses_a_prompt_and_names_the_way_out() -> None:
    session = await _session()
    result = await session.submit(_submission("hello"))
    assert result.accepted is False
    assert result.lock is not None
    assert result.lock.entry_id == session.pending_request.entry_id
    assert "gate-release" in result.rejection_reason


async def test_a_command_is_exempt_by_placement() -> None:
    """§5: command resolution runs inside the pipeline, so the release is admitted."""
    session = await _session()
    result = await session.submit(_submission("/gate-release", expand_commands=True))
    assert result.accepted is True
    assert result.command is not None


async def test_an_ask_without_a_lock_lets_a_prompt_through() -> None:
    """§3, row 1: ignoring a request is allowed."""

    def ask_only(api):
        async def arm(event, ctx):
            api.request_user_action("Answer whenever.", ask=_ASK)

        api.on("session_start", arm)

    session = await _session(extensions=[ask_only])
    assert session.pending_request is not None
    assert session.pending_request.lock is False
    # No model is reachable, so the turn's failure is the proof it was ADMITTED.
    with pytest.raises(Exception) as excinfo:
        await session.submit(_submission("hello"))
    assert "requires" not in str(excinfo.value)


async def test_a_lock_off_the_cursor_is_inert() -> None:
    """§7: nothing walks the path, so navigating away releases and back re-locks."""
    session = await _session()
    request_id = session.pending_request.entry_id
    parent = next(
        e["parentId"] for e in session._session_log.entries() if str(e["id"]) == request_id
    )
    session._session_log.append_navigate(str(parent))
    assert session.pending_request is None

    session._session_log.append_navigate(request_id)
    assert session.pending_request is not None


async def test_a_reload_still_refuses_with_no_extension_loaded() -> None:
    """§5: loading resolves the cursor, and the cursor is the lock. Nothing else runs."""
    first = await _session()
    reloaded = await _session(log=_reloaded_log(first._session_log.entries()), extensions=[])
    result = await reloaded.submit(_submission("hello"))
    assert result.accepted is False
    assert result.lock.extension_name.endswith("_gate")


# ── answering (§3, §8) ───────────────────────────────────────────────────────


async def test_answering_appends_the_response_and_dispatches_the_action() -> None:
    session = await _session()
    request_id = session.pending_request.entry_id

    result = await session.answer_request(request_id, "Use it", {"voice": "bass"})

    assert result.handled is True
    assert result.output == f"released {request_id}"
    response = next(
        e for e in session._session_log.entries() if e.get("customType") == RESPONSE_ENTRY_TYPE
    )
    assert response["data"] == {
        "requestId": request_id,
        "extension": session._session_log.entries()[1]["data"]["extension"],
        "action": "Use it",
        "values": {"voice": "bass"},
    }


async def test_answering_releases_the_lock_by_moving_the_cursor() -> None:
    session = await _session()
    await session.answer_request(session.pending_request.entry_id, "Use it", {"voice": "alto"})
    assert session.pending_request is None
    accepted = await session.submit(_submission("/gate-release", expand_commands=True))
    assert accepted.accepted is True


async def test_an_answer_the_owner_cannot_run_still_unlocks() -> None:
    """A lock whose owner is gone must not become a session nobody can continue."""
    first = await _session()
    reloaded = await _session(log=_reloaded_log(first._session_log.entries()), extensions=[])
    result = await reloaded.answer_request(
        reloaded.pending_request.entry_id, "Use it", {"voice": "alto"}
    )
    assert result.handled is False
    assert reloaded.pending_request is None


@pytest.mark.parametrize(
    "action,values,match",
    [
        ("Nope", {"voice": "alto"}, "not one of this ask"),
        ("Use it", {"voice": "tenor"}, "not one of"),
        ("Use it", {}, "missing value"),
    ],
)
async def test_a_bad_answer_raises_rather_than_persisting_a_partial_one(
    action, values, match
) -> None:
    session = await _session()
    with pytest.raises(ValueError, match=match):
        await session.answer_request(session.pending_request.entry_id, action, values)
    assert not [
        e for e in session._session_log.entries() if e.get("customType") == RESPONSE_ENTRY_TYPE
    ]


# ── the escapes (§6) ─────────────────────────────────────────────────────────


async def test_disabling_the_owner_moves_the_cursor_back_one(tmp_path) -> None:
    session = await _file_gate_session(tmp_path)
    path = session.pending_request.extension

    outcome = await session.disable_extension(path)

    assert outcome.ok is True
    assert "releasing its lock" in outcome.message
    assert session.pending_request is None


async def test_navigating_back_onto_a_disabled_extensions_lock_re_locks(tmp_path) -> None:
    """§7, degradation 1: the tree does not care what is loaded."""
    session = await _file_gate_session(tmp_path)
    request_id = session.pending_request.entry_id
    await session.disable_extension(session.pending_request.extension)
    assert session.pending_request is None

    session._session_log.append_navigate(request_id)
    assert session.pending_request is not None


async def test_disabling_an_unrelated_extension_moves_nothing(tmp_path) -> None:
    session = await _file_gate_session(tmp_path)
    bystander = tmp_path / "bystander.py"
    bystander.write_text("def register(api):\n    pass\n")
    await session.load_extensions([str(bystander)])
    before = session._session_log.cursor

    outcome = await session.disable_extension(str(bystander))

    assert outcome.ok is True
    assert "releasing" not in outcome.message
    assert session._session_log.cursor == before
    assert session.pending_request is not None


# ── the ask spec (§8) ────────────────────────────────────────────────────────


def test_an_ask_merges_the_panel_body_with_form_fields() -> None:
    spec = validate_ask_spec(_ASK)
    assert spec["body"] == {"kind": "text", "text": "Pick one before continuing."}
    assert [f["kind"] for f in spec["fields"]] == ["select"]
    assert spec["actions"] == [{"label": "Use it", "command": "gate-release"}]


def test_an_ask_may_carry_no_body_and_no_fields() -> None:
    spec = validate_ask_spec({"actions": [{"label": "OK", "command": "c"}]})
    assert spec["body"] is None
    assert spec["fields"] == []


@pytest.mark.parametrize(
    "spec,match",
    [
        ({"text": "x"}, "non-empty list"),
        ({"text": "x", "list": ["y"], "actions": [{"label": "a", "command": "c"}]}, "at most one"),
        ({"actions": [{"label": "a", "command": "c", "args": "1"}]}, "one argument is the request"),
        ({"fields": [{"name": "n", "kind": "slider"}], "actions": []}, "unknown kind"),
    ],
)
def test_a_malformed_ask_raises(spec, match) -> None:
    with pytest.raises(ValueError, match=match):
        validate_ask_spec(spec)


async def test_request_user_action_needs_an_extension_identity() -> None:
    """Fail-Early: the identity is STORED, so a bare api has nothing to store."""
    session = await _session(extensions=[])
    with pytest.raises(RuntimeError, match="no extension identity"):
        ExtensionAPI(session=session).request_user_action("x", lock=True)


def test_refusal_reason_names_the_owner_and_an_escape() -> None:
    request = read_request(
        {
            "type": "customEntry",
            "customType": REQUEST_ENTRY_TYPE,
            "id": "e1",
            "data": build_request_data("/x/tectum.py", "Nothing may proceed", lock=True),
        }
    )
    reason = refusal_reason(request)
    assert "tectum requires intervention" in reason
    assert "branch to the parent node" in reason


def test_request_at_cursor_is_none_pre_root() -> None:
    assert request_at_cursor([], None) is None
