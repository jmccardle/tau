"""`tau sessions` and `tau export` (D18): the catalog's listing, names, refs and Markdown export,
through `tau.cli.main` on session directories laid out as the catalog writes them."""

import json
from pathlib import Path

from world import World, tau

from tau import Author, Body, Outcome, Part, Session, Stop, model_config, to_markdown

OLD = "20261030-091502-1c2d3e"
NEW = "20261031-142233-7f3a9c"
OTHER = "20261031-150000-aaaaaa"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "export"
FIELDS = ["id", "path", "cwd", "name", "forked_from", "updated", "first", "error"]


def project(world: World, name: str) -> Path:
    path = world.home / name
    path.mkdir()
    return path


def rows(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.splitlines()]


# -- tau sessions


def test_sessions_lists_this_directory_newest_first_as_id_age_and_title(world: World) -> None:
    here, there = project(world, "here"), project(world, "there")
    world.session(OLD, cwd=here, says=["fix the parser\nplease"], age=7200)
    world.session(NEW, cwd=here, says=["hello"], age=30)
    world.session(OTHER, cwd=there, says=["not here"])
    result = tau(world, "sessions", cwd=here)
    assert result.code == 0, result.err
    assert result.out.splitlines() == [
        f"{NEW}   now  hello",
        f"{OLD}    2h  fix the parser please",
    ]


def test_all_adds_the_cwd_before_the_title_so_awk_and_cut_work(world: World) -> None:
    here, there = project(world, "here"), project(world, "there")
    world.session(OLD, cwd=here, says=["a"], age=120)
    world.session(OTHER, cwd=there, says=["b"], age=0)
    result = tau(world, "sessions", "--all", cwd=here)
    assert result.out.splitlines() == [f"{OTHER}   now  {there}  b", f"{OLD}    2m  {here}  a"]


def test_a_name_is_the_title_and_a_session_with_no_messages_says_so(world: World) -> None:
    here = project(world, "here")
    world.session(OLD, cwd=here, name="the store rewrite", says=["hello"], age=10)
    world.session(NEW, cwd=here, age=5)
    lines = tau(world, "sessions", cwd=here).out.splitlines()
    assert lines == [f"{NEW}   now  (no messages)", f"{OLD}   now  the store rewrite"]


def test_a_long_first_message_is_cut_to_one_line(world: World) -> None:
    world.session(OLD, says=["word " * 40])
    title = tau(world, "sessions").out.split("  ", 2)[2].strip()
    assert len(title) == 80
    assert title.endswith("…")


def test_json_is_one_object_per_line_with_the_fields_of_a_row(world: World) -> None:
    here = project(world, "here")
    world.session(OLD, cwd=here, says=["first words"])
    world.session(
        NEW, cwd=here, name="n", forked_from={"session": OLD, "node": 2}, says=["ignored"]
    )
    result = tau(world, "sessions", "--json", cwd=here)
    parsed = rows(result.out)
    assert [list(r) for r in parsed] == [FIELDS, FIELDS]
    by_id = {r["id"]: r for r in parsed}
    assert by_id[OLD]["first"] == "first words"
    assert by_id[OLD]["name"] is None
    assert by_id[OLD]["cwd"] == str(here)
    assert by_id[OLD]["path"] == str(world.tau / "sessions" / OLD)
    assert isinstance(by_id[OLD]["updated"], float)
    assert by_id[NEW]["name"] == "n"
    assert by_id[NEW]["first"] is None, "a named session's log is not read"
    assert by_id[NEW]["forked_from"] == {"session": OLD, "node": 2}


def test_a_directory_that_is_not_a_readable_session_is_a_row_with_an_error(world: World) -> None:
    here = project(world, "here")
    sessions = world.tau / "sessions"
    world.session(OLD, cwd=here, says=["fine"])
    (sessions / "no-info").mkdir()
    (sessions / "bad-info").mkdir()
    (sessions / "bad-info" / "info.json").write_text("{oops", encoding="utf-8")
    (sessions / "no-cwd").mkdir()
    (sessions / "no-cwd" / "info.json").write_text("{}", encoding="utf-8")
    (sessions / "a-file").write_text("not a directory", encoding="utf-8")
    result = tau(world, "sessions", "--json", cwd=here)
    by_id = {r["id"]: r for r in rows(result.out)}
    assert sorted(str(k) for k in by_id) == [OLD, "bad-info", "no-cwd", "no-info"]
    assert by_id[OLD]["error"] is None
    assert "info.json is missing" in str(by_id["no-info"]["error"])
    assert "not JSON" in str(by_id["bad-info"]["error"])
    assert "no 'cwd'" in str(by_id["no-cwd"]["error"])
    text = tau(world, "sessions", cwd=here).out
    assert "no-info" in text
    assert "error: " in text


def test_a_log_that_is_not_a_log_is_a_row_with_an_error(world: World) -> None:
    here = project(world, "here")
    path = world.session(OLD, cwd=here)
    (path / "log").write_bytes(b"this is not a tau log at all")
    (row,) = rows(tau(world, "sessions", "--json", cwd=here).out)
    assert "not a τ session file" in str(row["error"])


def test_no_sessions_directory_is_an_empty_list(world: World) -> None:
    assert tau(world, "sessions").out == ""
    assert tau(world, "sessions", "--json").code == 0


def test_session_dir_is_taken_against_the_directory_it_was_typed_in(world: World) -> None:
    here = project(world, "here")
    world.session(OLD, cwd=here, says=["in the other directory"], sessions=here / "mine")
    listed = tau(world, "sessions", "--session-dir", "mine", cwd=here)
    assert listed.out.splitlines()[0].endswith("in the other directory")
    assert tau(world, "sessions", cwd=here).out == "", "the default directory has none"


# -- names and refs


def test_name_prints_sets_and_removes_a_name(world: World) -> None:
    here = project(world, "here")
    world.session(OLD, cwd=here, says=["hello"])
    assert tau(world, "sessions", "name", OLD).code == 1, "no name: nothing to print"
    assert tau(world, "sessions", "name", OLD).out == ""
    result = tau(world, "sessions", "name", OLD, "the store rewrite")
    assert result.out == f"named {OLD}: the store rewrite\n"
    assert tau(world, "sessions", "name", OLD).out == "the store rewrite\n"
    assert tau(world, "sessions", cwd=here).out.endswith("  the store rewrite\n")
    assert tau(world, "sessions", "name", OLD, "").out == f"removed the name of {OLD}\n"
    assert tau(world, "sessions", "name", OLD).code == 1
    info = json.loads((world.tau / "sessions" / OLD / "info.json").read_text(encoding="utf-8"))
    assert info == {"cwd": str(here)}, "the name is gone, the rest is as it was"


def test_a_rename_keeps_cwd_and_forked_from(world: World) -> None:
    source = {"session": OLD, "node": 3}
    world.session(NEW, forked_from=source)
    tau(world, "sessions", "name", NEW, "x")
    info = json.loads((world.tau / "sessions" / NEW / "info.json").read_text(encoding="utf-8"))
    assert info == {"cwd": str(world.home), "name": "x", "forked_from": source}


def test_renaming_a_session_another_process_holds_works_and_the_last_wins(world: World) -> None:
    path = world.session(OLD)
    held = Session.load(path)
    try:
        assert tau(world, "sessions", "name", OLD, "first").code == 0
        assert tau(world, "sessions", "name", OLD, "second").code == 0
        assert tau(world, "sessions", "name", OLD).out == "second\n"
    finally:
        held.close()


def test_a_ref_is_a_path_an_id_a_name_or_a_unique_prefix(world: World) -> None:
    world.session(OLD, name="rewrite")
    world.session(NEW, name="other")
    path = world.tau / "sessions" / OLD
    for ref in (str(path), OLD, "rewrite", "20261030"):
        assert tau(world, "sessions", "name", ref).out == "rewrite\n", ref
    assert tau(world, "sessions", "name", "other").out == "other\n"


def test_an_id_is_tried_before_a_name(world: World) -> None:
    world.session(OLD, name=NEW)
    world.session(NEW, name="the real one")
    assert tau(world, "sessions", "name", NEW).out == "the real one\n"


def test_a_relative_path_ref_is_taken_against_the_directory_it_was_typed_in(world: World) -> None:
    here = project(world, "here")
    world.session(OLD, name="rewrite", sessions=here / "kept")
    result = tau(world, "sessions", "name", f"kept/{OLD}", cwd=here)
    assert result.out == "rewrite\n"


def test_a_ref_searches_every_directory_not_just_this_one(world: World) -> None:
    world.session(OLD, cwd=project(world, "elsewhere"), name="far")
    assert tau(world, "sessions", "name", "far", cwd=world.home).out == "far\n"


def test_an_unknown_ref_is_an_error_naming_the_directory(world: World) -> None:
    world.session(OLD)
    result = tau(world, "sessions", "name", "nothing")
    assert result.code == 1
    assert result.err.startswith("tau: no session 'nothing' in ")
    assert str(world.tau / "sessions") in result.err


def test_a_prefix_or_name_of_several_sessions_lists_the_candidates(world: World) -> None:
    here, there = project(world, "here"), project(world, "there")
    world.session(OLD, cwd=here, name="twin", age=130)
    world.session(NEW, cwd=there, name="twin", age=10)
    for ref, what in (("twin", "the name 'twin'"), ("202610", "the prefix '202610'")):
        result = tau(world, "sessions", "name", ref)
        assert result.code == 1
        assert f"tau: {what} names 2 sessions" in result.err
        lines = result.err.splitlines()
        assert lines[1].split()[:3] == [NEW, "now", str(there)], "newest first, with their cwd"
        assert lines[2].split()[:3] == [OLD, "2m", str(here)]


def test_a_name_may_not_be_asked_about_a_node(world: World) -> None:
    world.session(OLD)
    result = tau(world, "sessions", "name", f"{OLD}:2")
    assert result.code == 1
    assert "not about a node" in result.err


def test_a_session_with_a_bad_info_file_is_not_named(world: World) -> None:
    path = world.session(OLD)
    (path / "info.json").write_text("{}", encoding="utf-8")
    result = tau(world, "sessions", "name", OLD, "x")
    assert result.code == 1
    assert "no 'cwd'" in result.err


# -- tau export


def tool_use(session: Session, tip: int) -> int:
    """A conversation that uses every kind of node the export lays out (node ids: user 2, a reply
    with a call 3, its results 4, a custom node 5, the answer 6, an extension's message 7, two
    calls 8, their results 9 and a reply cut off by its length 10)."""
    frontend, tools, model = Author.Frontend("t"), Author.Tools(), Author.Provider()
    user = session.append(tip, Body.User("list the files"), frontend, 1)
    reply = session.append(
        user,
        Body.Assistant(
            [
                Part.Thinking("The user wants the files."),
                Part.Text("I'll look."),
                Part.Call("call_1", "bash", '{"command": "ls"}'),
            ],
            Stop.Tools,
            "tool_calls",
        ),
        model,
        2,
    )
    output = "a.txt\nb.txt\n```\nfenced\n```"
    results = session.append(reply, Body.Results([("call_1", Outcome.Ok(output), tools)]), tools, 3)
    session.append(results, Body.Custom("sample_error", "never reaches a model"), model, 3)
    answer = session.append(
        results,
        Body.Assistant([Part.Text("Two files: `a.txt` and `b.txt`.")], Stop.End, "stop"),
        model,
        4,
    )
    nudge = session.append(answer, Body.User("now read x"), Author.Extension("gamemaster"), 5)
    tried = session.append(
        nudge,
        Body.Assistant(
            [Part.Call("call_2", "read", '{"path": "x"}'), Part.Call("call_3", "bash", "{}")],
            Stop.Tools,
            "tool_calls",
        ),
        model,
        6,
    )
    failed = session.append(
        tried,
        Body.Results(
            [
                ("call_2", Outcome.Err("no such file"), tools),
                ("call_3", Outcome.Interrupted(), Author.Extension("guard")),
            ]
        ),
        tools,
        7,
    )
    return session.append(
        failed, Body.Assistant([Part.Text("I gave u")], Stop.Length, "length"), model, 8
    )


def exported(world: World, *argv: str) -> str:
    result = tau(world, "export", *argv)
    assert result.code == 0, result.err
    return result.out


def long_session(world: World, **kwargs: object) -> Path:
    """The `tool_use` session, in a working directory that does not exist."""
    return world.session(
        NEW,
        cwd=Path("/work/project"),
        system="You are terse.",
        build=tool_use,
        **kwargs,  # type: ignore[arg-type]
    )


def test_an_export_is_the_golden_file(world: World) -> None:
    long_session(world, name="list the files")
    assert exported(world, NEW) == (FIXTURES / "walk.md").read_text(encoding="utf-8")


def test_thinking_appears_only_with_thinking(world: World) -> None:
    long_session(world, name="list the files")
    assert "Thinking" not in exported(world, NEW)
    with_thinking = exported(world, NEW, "--thinking")
    assert with_thinking == (FIXTURES / "walk_thinking.md").read_text(encoding="utf-8")


def test_an_export_is_deterministic_and_works_with_the_working_directory_gone(
    world: World,
) -> None:
    long_session(world)
    assert not Path("/work/project").exists()
    assert exported(world, NEW) == exported(world, NEW)


def test_a_ref_node_exports_that_walk(world: World) -> None:
    long_session(world)
    short = exported(world, f"{NEW}:3")
    assert "- node: 3\n" in short
    assert short.endswith('**Call:** bash (call_1)\n\n```json\n{"command": "ls"}\n```\n')
    assert "Two files" not in short


def test_a_node_that_is_not_there_is_an_error(world: World) -> None:
    long_session(world)
    result = tau(world, "export", f"{NEW}:99")
    assert result.code == 1
    assert "no node 99" in result.err
    assert result.out == ""


def test_export_writes_a_file_against_the_directory_it_was_typed_in(world: World) -> None:
    long_session(world)
    here = project(world, "here")
    result = tau(world, "export", NEW, "-o", "out.md", cwd=here)
    assert (result.code, result.out) == (0, "")
    assert (here / "out.md").read_text(encoding="utf-8") == exported(world, NEW)


def test_a_session_another_process_holds_exports_from_its_last_sync(world: World) -> None:
    path = long_session(world)
    before = exported(world, NEW)
    held = Session.load(path)
    try:
        assert exported(world, NEW) == before
    finally:
        held.close()


def test_the_header_names_a_forks_source(world: World) -> None:
    long_session(world, forked_from={"session": OLD, "node": 41})
    header = exported(world, NEW).split("\n\n")[1]
    assert header.splitlines()[-1] == f"- forked from: {OLD} at node 41"


def test_an_unknown_ref_exports_nothing(world: World) -> None:
    long_session(world)
    result = tau(world, "export", "nothing")
    assert result.code == 1
    assert result.out == ""


def test_to_markdown_is_pure_over_nodes_and_sizes_each_fence_to_its_content() -> None:
    session = Session.create(model_config("m"), "s", 0)
    tip = session.cursor(0).at
    reply = session.append(
        tip,
        Body.Assistant([Part.Call("c", "bash", "{}")], Stop.Tools, "tool_calls"),
        Author.Provider(),
        0,
    )
    output = "x\n``\n`````\ny"
    results = Body.Results([("c", Outcome.Ok(output), Author.Tools())])
    last = session.append(reply, results, Author.Tools(), 0)
    text = to_markdown(session.tree.walk(last))
    assert f"``````\n{output}\n``````\n" in text, "one longer than the longest run (five)"
