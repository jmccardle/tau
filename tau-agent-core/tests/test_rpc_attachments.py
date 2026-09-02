"""Protocol 1.4 — `complete_path`, and `submit`/`prompt`'s `expand_attachments`.

The two halves of a usable chat editor for a head that is not the TUI. Before
1.4 an `@notes.txt` sent over this wire reached the model as those eleven
literal characters: expansion was a frontend job (docs/FILE-ATTACHMENTS.md §2)
and this wire had no frontend to do it. These tests pin both halves, and pin
the thing that makes them one feature rather than two: `complete_path` lists
the directory that `expand_attachments` then reads, so a path the popup offers
is a path the expansion resolves.

A new file per unit, following the Tier B convention (docs/RPC-TIER-B.md §3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.rpc import commands
from tau_agent_core.rpc.commands import _submission_from_params
from tau_agent_core.rpc.handler import RPCHandler
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


@pytest.fixture
def handler() -> RPCHandler:
    return RPCHandler(AgentSession(session_log=InMemorySessionLog(), tools=[], model=_model()))


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory with a known shape, made the PROCESS working directory.

    `complete_path` and the expansion both read `Path.cwd()` — that identity is
    the point of the verb (a host cannot list the right filesystem for itself),
    so a test that passed a cwd in would be testing a seam neither has.
    """
    (tmp_path / "notes.txt").write_text("the note body\n", encoding="utf-8")
    (tmp_path / "nouns.md").write_text("# nouns\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".hidden").write_text("secret\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def _call(handler: RPCHandler, method: str, params: dict) -> dict:
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return await handler._output_queue.get()


async def _call_for_response(handler: RPCHandler, method: str, params: dict) -> dict:
    """Like `_call`, but skips the turn's own events to find the RESPONSE.

    `prompt` has two completions (C3) and its turn starts emitting immediately,
    so the acknowledgement is not reliably the only thing on the queue by the
    time this returns.
    """
    await handler._handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    for _ in range(50):
        message = await handler._output_queue.get()
        if "id" in message:
            return message
    raise AssertionError(f"no response to {method} in the first 50 outbound messages")


# ── complete_path ────────────────────────────────────────────────────────


async def test_lists_the_working_directory_for_a_bare_at(handler, workspace) -> None:
    """`@` with an empty prefix is how the whole directory becomes browsable."""
    result = (await _call(handler, "complete_path", {"text": "look at @", "cursor": 9}))["result"]
    names = {m["name"] for m in result["completion"]["matches"]}
    assert names == {"notes.txt", "nouns.md", "other.py", "sub/"}


async def test_hidden_entries_need_a_dot_prefix(handler, workspace) -> None:
    """`.hidden` is absent above and present here — the shell rule, not a filter
    that would make the file unreachable."""
    result = (await _call(handler, "complete_path", {"text": "@.", "cursor": 2}))["result"]
    assert [m["name"] for m in result["completion"]["matches"]] == [".hidden"]


async def test_prefix_narrows_and_directories_are_marked(handler, workspace) -> None:
    result = (await _call(handler, "complete_path", {"text": "@no", "cursor": 3}))["result"]
    matches = result["completion"]["matches"]
    assert {m["name"] for m in matches} == {"notes.txt", "nouns.md"}
    assert all(m["is_dir"] is False for m in matches)


async def test_span_covers_the_whole_token_not_just_what_precedes_the_cursor(
    handler, workspace
) -> None:
    """A host replaces `start:end`. With the cursor mid-token, a span ending at
    the cursor would leave the tail behind and produce `@notes.txtes.txt`."""
    text = "see @notes.txt please"
    result = (await _call(handler, "complete_path", {"text": text, "cursor": 7}))["result"]
    completion = result["completion"]
    assert text[completion["start"] : completion["end"]] == "@notes.txt"


async def test_a_cursor_outside_any_reference_is_null(handler, workspace) -> None:
    """`null` means "show no popup" — distinct from a completion with no
    matches, which means "this names no file"."""
    result = (await _call(handler, "complete_path", {"text": "plain words", "cursor": 5}))["result"]
    assert result["completion"] is None


async def test_a_token_naming_nothing_is_an_empty_match_list_not_null(handler, workspace) -> None:
    result = (await _call(handler, "complete_path", {"text": "@zzz", "cursor": 4}))["result"]
    assert result["completion"] is not None
    assert result["completion"]["matches"] == []
    assert result["completion"]["total"] == 0


async def test_total_is_the_true_count_so_a_bounded_list_says_so(
    handler, tmp_path, monkeypatch
) -> None:
    """G3: `matches` is bounded, and `total` reports what the bound hid. A host
    told only the bounded list would imply it had shown everything."""
    from tau_agent_core.attachments import _COMPLETION_LIMIT

    for i in range(_COMPLETION_LIMIT + 7):
        (tmp_path / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = (await _call(handler, "complete_path", {"text": "@f", "cursor": 2}))["result"]
    assert len(result["completion"]["matches"]) == _COMPLETION_LIMIT
    assert result["completion"]["total"] == _COMPLETION_LIMIT + 7


async def test_cursor_is_required(handler, workspace) -> None:
    """Not defaulted: which reference is being completed is decided from it, so
    a missing cursor is a mistake rather than a request for position 0."""
    message = await _call(handler, "complete_path", {"text": "@no"})
    assert message["error"]["code"] == commands.INVALID_PARAMS


# ── expand_attachments ───────────────────────────────────────────────────


def test_without_the_flag_an_at_word_stays_literal_text(workspace) -> None:
    """The 1.3 behaviour, pinned: expansion is opt-in, and a host that does not
    ask for it gets byte-identical text."""
    sub, report = _submission_from_params({"text": "read @notes.txt"})
    assert sub.text == "read @notes.txt"
    assert report is None


def test_the_flag_prepends_the_block_and_keeps_the_at_word(workspace) -> None:
    """docs/FILE-ATTACHMENTS.md §2: blocks go in FRONT and the `@word` stays
    where the human typed it — the model reads the material first and the
    instruction last, and the instruction still names the file as they did."""
    sub, report = _submission_from_params({"text": "read @notes.txt", "expand_attachments": True})
    assert (
        sub.text
        == '<attachment filename="notes.txt">\nthe note body\n</attachment>\nread @notes.txt'
    )
    assert report == {"expanded": 1, "images": 0, "unresolved": [], "failures": []}


def test_an_at_word_naming_nothing_is_reported_as_unresolved(workspace) -> None:
    """It is left in the text as prose, which is correct — and SAID, which is
    the difference between a deliberate fallthrough and a silent one."""
    sub, report = _submission_from_params({"text": "read @nope.txt", "expand_attachments": True})
    assert sub.text == "read @nope.txt"
    assert report == {"expanded": 0, "images": 0, "unresolved": ["nope.txt"], "failures": []}


def test_expand_attachments_is_not_passed_to_the_submission(workspace) -> None:
    """It is a wire-level instruction to the handler, not a Submission field.
    A Submission that carried it would raise on a field it has never had."""
    sub, _ = _submission_from_params({"text": "hi", "expand_attachments": True})
    assert not hasattr(sub, "expand_attachments")


def test_the_hosts_own_images_survive_expansion(workspace) -> None:
    """A host may send images AND ask for expansion. Letting either win loses
    one of them silently."""
    image = {"type": "image", "data": "aGk=", "mime_type": "image/png"}
    sub, report = _submission_from_params(
        {"text": "read @notes.txt", "images": [image], "expand_attachments": True}
    )
    assert sub.images == [image]
    assert report["expanded"] == 1


async def test_the_ack_carries_the_report_only_when_expansion_ran(workspace) -> None:
    """Absent means "expansion did not run", which is a different statement
    from an empty summary claiming it ran and found nothing.

    A handler each, because a turn is in flight the moment the first `prompt`
    is acknowledged and the second would be refused by admission.
    """
    plain_handler = RPCHandler(
        AgentSession(session_log=InMemorySessionLog(), tools=[], model=_model())
    )
    plain = (await _call_for_response(plain_handler, "prompt", {"text": "hello"}))["result"]
    assert "attachments" not in plain

    expanding_handler = RPCHandler(
        AgentSession(session_log=InMemorySessionLog(), tools=[], model=_model())
    )
    expanded = (
        await _call_for_response(
            expanding_handler,
            "prompt",
            {"text": "read @notes.txt", "expand_attachments": True},
        )
    )["result"]
    assert expanded["attachments"] == {
        "expanded": 1,
        "images": 0,
        "unresolved": [],
        "failures": [],
    }


async def test_the_schema_accepts_the_flag_on_both_verbs() -> None:
    for verb in ("submit", "prompt"):
        properties = commands.COMMAND_TABLE[verb].params_schema["properties"]
        assert "expand_attachments" in properties, verb


# ── the two halves agree ─────────────────────────────────────────────────


async def test_a_path_the_popup_offers_is_a_path_the_expansion_resolves(handler, workspace) -> None:
    """The identity the whole feature rests on. Both read `Path.cwd()`, so a
    completion cannot offer a file the expansion would then call unresolved —
    which is exactly what a host listing its OWN filesystem would do under
    Remote SSH."""
    listing = (await _call(handler, "complete_path", {"text": "@", "cursor": 1}))["result"]
    offered = [m["name"] for m in listing["completion"]["matches"] if not m["is_dir"]]

    for name in offered:
        _, report = _submission_from_params({"text": f"@{name}", "expand_attachments": True})
        assert report["unresolved"] == [], f"{name} was offered but did not resolve"
        assert report["expanded"] == 1, name
