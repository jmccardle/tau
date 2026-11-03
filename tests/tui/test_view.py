"""`SessionView`: events in, state out (D23 sections 2 and 9). No screen."""

from pathlib import Path

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
from tau.tui import SessionView


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
    assert seen.notices == ["careful"]
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


def test_a_call_forms_from_its_name_and_a_stray_argument_is_ignored() -> None:
    seen = SessionView()
    seen.hear(view(0, 1, busy=True))
    seen.hear(Live(0, CallArgs(0, '{"stray"')))
    assert seen.live[0].calls == {}
    seen.hear(Live(0, CallNamed(0, "c", "read")))
    seen.hear(Live(0, CallArgs(0, '{"path"')))
    seen.hear(Live(0, CallArgs(0, ': "x"}')))
    (call,) = seen.live[0].calls.values()
    assert (call.id, call.tool, call.args) == ("c", "read", '{"path": "x"}')


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
