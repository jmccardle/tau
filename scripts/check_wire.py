"""The wire's coding holds its own promises (D21 sections 4, 5), checked on one value of each kind.

Every event, delta and core value goes through JSON text and comes back equal; every row of the
error table is sent for the exceptions it names and rebuilt as the class it names; a call's
params are refused when they do not fit. The behavior suite run against a remote client
(`tests/serve`) is the test of the whole wire; this holds the coding alone, which the public
API does not expose. Needs a build of tau (`maturin develop`).
"""

import json
import sys
from collections.abc import Callable

import tau
from tau import _protocol as wire
from tau.commandline import UnknownCommand
from tau.events import (
    Appended,
    CallArgs,
    CallNamed,
    CursorGone,
    CursorView,
    CutOff,
    HookFailure,
    Labels,
    Live,
    Notice,
    OpenQuestion,
    TextDelta,
    ThinkingDelta,
    ToolProgress,
)

problems: list[str] = []


def check(ok: bool, what: str) -> None:
    if not ok:
        problems.append(what)


def through_text(data: object) -> object:
    return json.loads(json.dumps(data))


def core_values() -> list[object]:
    session = tau.Session.create(tau.Config('{"model": "m"}'), "system", 1)
    cursor = session.open(1, tau.Author.Frontend("c"), 2)
    session.input(cursor, tau.Author.Frontend("c"), "hi")
    replay = tau.Replay("fake", '{"sig": 1}')
    parts = [tau.Part.Text("t"), tau.Part.Thinking("th", replay), tau.Part.Call("1", "tool", "{}")]
    outcome = tau.Outcome.Ok("fine"), tau.Outcome.Err("bad"), tau.Outcome.Interrupted()
    return [
        *[session.tree.node(i) for i in range(len(session.tree))],
        *session.cursor(cursor).items,
        tau.Body.System("s"),
        tau.Body.User("u"),
        tau.Body.Assistant(parts, tau.Stop.Tools, "tool_use", '{"usage": 1}'),
        tau.Body.Results([("1", o, tau.Author.Tools()) for o in outcome]),
        tau.Body.Config(tau.Config("{}")),
        tau.Body.Custom("kind", "data"),
        tau.Author.Provider(),
        tau.Author.Core(),
        tau.Author.Extension("e"),
        tau.ItemData.Extension("e", "d"),
        tau.Ending.Done(),
        tau.Ending.Halted("why"),
        tau.Ending.Failed("E: x"),
        tau.Ending.Aborted(),
    ]


def events(values: list[object]) -> list[tau.Event]:
    nodes = tuple(v for v in values if isinstance(v, tau.Node))
    item = next(v for v in values if isinstance(v, tau.Item))
    view = CursorView(
        0,
        1,
        False,
        True,
        (OpenQuestion(3, "c", "q?", {"type": "string"}), OpenQuestion(4, None, "r?", {})),
        (item,),
        tau.Ending.Failed("E: x"),
        HookFailure("e", "pre_step", "E: x"),
        "fake/m",
        (("e", "text"),),
    )
    deltas = [
        TextDelta("a"),
        ThinkingDelta("b"),
        CallNamed(1, "c", "tool"),
        CallArgs(1, "{"),
        ToolProgress(2, "c", "out"),
    ]
    bare = CursorView(0, 1, True, False, (), (), None, None, None, ())
    return [
        view,
        bare,
        CursorGone(2, 5),
        Labels((("main", 0),)),
        Appended(nodes),
        Notice("hello"),
        *[Live(0, d) for d in deltas],
    ]


def round_trips(values: list[object]) -> None:
    for value in values:
        kind = next(cls for cls in wire.CORE if isinstance(value, cls))
        back = wire.decode(kind, through_text(wire.encode(value)))
        check(back == value, f"{value!r} came back as {back!r}")
    for event in events(values):
        data = through_text(wire.encode_event(event))
        check(isinstance(data, dict) and "kind" in data, f"{event!r} has no kind")
        check(wire.decode_event(data) == event, f"{event!r} came back unequal")


def errors() -> None:
    made: list[BaseException] = [
        tau.Busy("b"),
        tau.NoSuchCursor("n"),
        tau.CursorClosed("c"),
        tau.NoSuchNode("n"),
        tau.NoSuchLabel("l"),
        wire.ProtocolMismatch("speaks 1", 1),
        KeyError("q"),
        UnknownCommand("zz"),
        ValueError("v"),
        PermissionError("p"),
        tau.CommandFailed("E: x"),
        tau.ConfigError("c"),
        ConnectionError("the driver is stopped"),
        CutOff("behind"),
    ]
    for error in made:
        found = wire.exception_for(**wire_fields(wire.error_object(error)))
        row = wire.error_of(error)
        check(type(found) is row.raises, f"{error!r} comes back as {found!r}, not {row.raises}")
        check(isinstance(error, row.raises), f"{error!r} is not a {row.raises.__name__}")
        check(str(found).endswith(str(error)), f"{error!r} lost its message: {found!r}")
    sent = {wire.error_of(e).code for e in made}
    named = {row.code for row in wire.ERRORS if row.sent}
    check(
        sent == named, f"rows never sent: {sorted(named - sent)}; unnamed: {sorted(sent - named)}"
    )
    internal = wire.error_object(OSError("disk"))
    check(internal == {"code": -32603, "message": "OSError: disk"}, f"internal: {internal}")
    check(isinstance(wire.exception_for(-1, "x"), wire.ProtocolError), "an unknown code")
    check(wire.end_error({"error": None}) is None, "end with no error")
    ended = wire.end_error(wire.end_params(CutOff("behind")))
    check(type(ended) is CutOff, f"end CutOff came back as {ended!r}")
    stopped = wire.end_error(wire.end_params(ConnectionError("E: x")))
    check(type(stopped) is ConnectionError, f"end Stopped came back as {stopped!r}")


def wire_fields(error: dict[str, object]) -> dict[str, object]:
    return {
        "code": error["code"],
        "message": error["message"],
        "data": error.get("data"),
    }


def refuses(call: Callable[[], object], what: str) -> None:
    try:
        call()
    except TypeError:
        return
    problems.append(f"{what} was not refused with TypeError")


def params() -> None:
    check(
        wire.decode_params("configure", {"cursor": 0, "extension": "e", "values": {"a": 1}})
        == {"cursor": 0, "extension": "e", "values": {"a": 1}},
        "configure params",
    )
    check(wire.decode_params("watch", {"cursors": [1, 2]}) == {"cursors": [1, 2]}, "watch params")
    refuses(lambda: wire.decode_params("input", {"cursor": 0}), "a missing param")
    refuses(lambda: wire.decode_params("input", {"cursor": 0, "text": "x", "by": "y"}), "extra")
    refuses(lambda: wire.decode_params("input", {"cursor": "0", "text": "x"}), "a string cursor")
    refuses(lambda: wire.decode_params("input", {"cursor": True, "text": "x"}), "a bool cursor")
    refuses(lambda: wire.encode({"path": object()}), "a value that is not JSON")
    check(wire.encode_result("step", None) is None, "a call that returns nothing")


def main() -> int:
    values = core_values()
    round_trips(values)
    errors()
    params()
    for problem in problems:
        print(f"check_wire.py: {problem}", file=sys.stderr)
    if not problems:
        print(f"wire: {len(values)} core values, {len(wire.ERRORS)} errors, ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
