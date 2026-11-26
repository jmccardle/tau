"""`SessionView`: events in, state out (D23 sections 2 and 9). No screen."""

from pathlib import Path

import pytest
from fakes import FAKE_CONFIG

from tau import (
    Appended,
    Author,
    Body,
    CallArgs,
    CallNamed,
    CursorGone,
    CursorView,
    Labels,
    Live,
    Notice,
    Outcome,
    Part,
    Session,
    Stop,
    TextDelta,
    ThinkingDelta,
    ToolProgress,
)
from tau.tui import LiveOutOfOrder, SessionView


def view(cursor: int = 0, at: int = 0, *, busy: bool = False) -> CursorView:
    return CursorView(cursor, at, busy, False, (), (), None, None, None, ())


def tree(tmp_path: Path) -> tuple[Session, list[int]]:
    """A session whose walk is system, user, assistant (one call), results, and a branch off the
    user node: ids 0 to 4."""
    session = Session.create(FAKE_CONFIG, "sys", 0, tmp_path / "s")
    user = session.append(0, Body.User("hi"), Author.Frontend("t"), 0)
    reply = Body.Assistant([Part.Call("c", "tool", "{}")], Stop.Tools)
    assistant = session.append(user, reply, Author.Provider(), 0)
    results = Body.Results([("c", Outcome.Ok("ok"), Author.Tools())])
    done = session.append(assistant, results, Author.Tools(), 0)
    side = session.append(user, Body.User("other"), Author.Frontend("t"), 0)
    return session, [0, user, assistant, done, side]


def test_it_holds_what_the_stream_said(tmp_path: Path) -> None:
    session, (root, user, *_) = tree(tmp_path)
    seen = SessionView()
    seen.hear(view(0, user))
    seen.hear(view(1, root))
    seen.hear(Labels((("main", 0), ("side", 1))))
    seen.hear(Appended((session.tree.node(user),)))
    seen.hear(Notice("careful"))
    assert sorted(seen.views) == [0, 1]
    assert seen.labels_of(1) == ("side",)
    assert seen.nodes[user].body == Body.User("hi")
    assert seen.notices == [Notice("careful")]
    seen.hear(CursorGone(1, root))
    assert sorted(seen.views) == [0]
    assert seen.gone == {1: root}
    seen.hear(view(1, user))  # reopened under the same id
    assert seen.gone == {}
    assert sorted(seen.views) == [0, 1]
    session.close()


def test_the_path_to_a_descendant_through_known_nodes(tmp_path: Path) -> None:
    session, (_, user, assistant, done, side) = tree(tmp_path)
    seen = SessionView()
    seen.hear(Appended(tuple(session.tree.node(i) for i in (user, assistant, done, side))))
    assert [n.id for n in seen.path(user, done) or []] == [assistant, done]
    assert seen.path(done, done) == []
    assert seen.path(done, side) is None, "a branch is not an extension"
    assert seen.path(done, user) is None, "going back is not an extension"
    assert seen.path(user, 99) is None, "a node never carried is read, not guessed"
    session.close()


def test_a_reply_is_a_layer_over_the_cursor_until_the_cursor_moves_or_rests(
    tmp_path: Path,
) -> None:
    seen = SessionView()
    seen.hear(view(0, 1, busy=True))
    seen.hear(Live(0, ThinkingDelta("hm")))
    seen.hear(Live(0, TextDelta("he")))
    seen.hear(Live(0, TextDelta("llo")))
    layer = seen.live[0]
    assert (layer.thinking, layer.text, layer.at) == ("hm", "hello", 1)
    seen.hear(view(0, 1, busy=True))  # the same place: the layer stays
    assert seen.live[0].text == "hello"
    seen.hear(view(0, 2, busy=True))  # the node arrived
    assert seen.live[0].empty
    assert seen.live[0].at is None
    seen.hear(Live(0, TextDelta("next")))
    seen.hear(view(0, 2, busy=False))
    assert seen.live[0].empty, "a cursor at rest has no reply in flight"


def test_a_call_forms_from_its_name() -> None:
    seen = SessionView()
    seen.hear(view(0, 1, busy=True))
    seen.hear(Live(0, CallNamed(0, "c", "read")))
    seen.hear(Live(0, CallArgs(0, '{"path"')))
    seen.hear(Live(0, CallArgs(0, ': "x"}')))
    (call,) = seen.live[0].calls.values()
    assert (call.id, call.tool, call.args) == ("c", "read", '{"path": "x"}')


def test_arguments_of_a_call_never_named_break_the_reply_until_it_is_over() -> None:
    """A stream that loses a delta loses the rest of its reply (`Feed`), so `CallArgs` without
    its `CallNamed` is a broken stream: said once, and the reply is not drawn live."""
    seen = SessionView()
    seen.hear(view(0, 1, busy=True))
    seen.hear(Live(0, TextDelta("editing")))
    seen.hear(Live(0, CallNamed(0, "a", "edit")))
    with pytest.raises(LiveOutOfOrder, match="call 3 came before the call was named"):
        seen.hear(Live(0, CallArgs(3, '{"stray"')))
    layer = seen.live[0]
    assert layer.empty, "nothing of the broken reply is drawn"
    assert layer.broken is not None
    seen.hear(Live(0, CallNamed(3, "d", "edit")))  # the rest of it is ignored, and not said again
    seen.hear(Live(0, CallArgs(3, "{}")))
    seen.hear(Live(0, TextDelta("more")))
    assert layer.empty
    seen.hear(view(0, 2, busy=True))  # the reply landed: the next one is drawn
    seen.hear(Live(0, TextDelta("next")))
    assert (layer.broken, layer.text) == (None, "next")


def test_tool_progress_is_kept_by_node_and_call_until_the_results_arrive(tmp_path: Path) -> None:
    session, (_, user, assistant, done, _) = tree(tmp_path)
    seen = SessionView()
    seen.hear(Appended(tuple(session.tree.node(i) for i in (user, assistant))))
    seen.hear(view(0, assistant, busy=True))
    seen.hear(Live(0, ToolProgress(assistant, "c", "one ")))
    seen.hear(Live(0, ToolProgress(assistant, "c", "two")))
    assert seen.live[0].progress == {(assistant, "c"): "one two"}
    seen.hear(view(0, assistant, busy=True))
    assert seen.live[0].progress, "a view in between does not drop it"
    seen.hear(Appended((session.tree.node(done),)))
    assert seen.live[0].progress == {}
    session.close()


def test_the_layers_of_cursors_are_apart() -> None:
    seen = SessionView()
    seen.hear(view(0, 1, busy=True))
    seen.hear(view(1, 1, busy=True))
    seen.hear(Live(0, TextDelta("a")))
    seen.hear(Live(1, TextDelta("b")))
    seen.hear(view(0, 2, busy=True))
    assert seen.live[0].empty
    assert seen.live[1].text == "b"


def test_forgetting_a_cursors_live_layer_starts_the_next_watch_from_nothing() -> None:
    seen = SessionView()
    seen.hear(view(0, 1, busy=True))
    seen.hear(view(1, 1, busy=True))
    seen.hear(Live(0, TextDelta("par")))
    seen.hear(Live(1, TextDelta("other")))
    seen.forget_live(0)
    assert 0 not in seen.live
    assert seen.live[1].text == "other", "only that cursor's layer"
    seen.hear(Live(0, TextDelta("replayed")))  # a later watch sends what the sample has said
    assert seen.live[0].text == "replayed"
