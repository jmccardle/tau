"""`Permissions` (D24 section 7): rules that refuse, allow or ask about a tool call, and the
question's `yes | always | no`."""

import json
import os
import re
import sys
from pathlib import Path
from typing import cast

import pytest
from fakes import FAKE_MODEL, FakeProvider, FakeTool, Reply
from harness import Harness, say, scenario

from tau import (
    Body,
    ConfigError,
    Ending,
    FindTool,
    GrepTool,
    Outcome,
    Part,
    Permissions,
    Stop,
    UserConfig,
    model_config,
    read_models,
)

REFUSED = "the user refused this call"
PATH_PATTERNS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows: permission rules with a path pattern are not ported; they are refused there "
    "(README, docs/plan.md Phase 9)",
)


def call(id: str, tool: str, **args: object) -> Part.Call:
    return Part.Call(id, tool, json.dumps(args))


def calls(*made: Part.Call) -> Reply:
    return Reply(list(made), Stop.Tools)


def bash(*commands: str) -> Reply:
    return calls(*(call("abc"[i], "bash", command=c) for i, c in enumerate(commands)))


def rules(**options: list[str]) -> dict[str, object]:
    """The `cli` layer: the fake model and permissions' options."""
    models = {FAKE_MODEL: {"model": FAKE_MODEL, "provider": "fake"}}
    return {"models": models, "extensions": {"permissions": options}}


async def start(
    tmp_path: Path,
    script: list[Reply],
    options: dict[str, list[str]] | None = None,
    tools: list[FakeTool] | None = None,
) -> tuple[Harness, list[FakeTool]]:
    """A session whose root config carries `options`, after one `go`."""
    made = tools or [FakeTool("bash", "output")]
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script),
        tools=made,
        extensions=[Permissions(tmp_path)],
        config=model_config(FAKE_MODEL, {"permissions": options or {}}),
        user_config=UserConfig(tmp_path / "tau-home", cli=rules(**(options or {}))),
    )
    await h.send("go")
    await h.settle()
    return h, made


def results(h: Harness) -> list[tuple[str, Outcome]]:
    body = next(b for b in reversed(h.bodies()) if isinstance(b, Body.Results))
    return [(c, outcome) for c, outcome, _ in body.outcomes]


def refusal(h: Harness) -> str:
    ((_, outcome),) = results(h)
    assert isinstance(outcome, Outcome.Err)
    return outcome.error


# Nothing configured: nothing asked


@scenario
async def test_with_no_rules_nothing_is_asked(tmp_path: Path) -> None:
    h, (tool,) = await start(tmp_path, [bash("rm -rf /", "ls"), say("ok")])
    assert (await h.view()).questions == ()
    assert tool.calls == [{"command": "rm -rf /"}, {"command": "ls"}]
    await h.close()


@scenario
async def test_the_one_config_line_that_turns_asking_on(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider([bash("ls"), say("ok")]),
        tools=[FakeTool("bash", "output")],
        extensions=[Permissions(tmp_path)],
    )
    await h.configure("permissions", {"ask": ["bash"]})
    await h.send("go")
    await h.settle()
    (question,) = (await h.view()).questions
    assert question.extension == "permissions"
    await h.close()


# What a question shows and offers


@scenario
async def test_a_question_shows_the_tool_its_argument_and_the_rule(tmp_path: Path) -> None:
    h, (tool,) = await start(
        tmp_path, [bash("git push origin master"), say("ok")], {"ask": ["bash"]}
    )
    (question,) = (await h.view()).questions
    assert question.text == "bash: git push origin master\n(ask rule: bash)"
    assert question.schema == {"type": "string", "enum": ["yes", "always", "no"]}
    assert question.to is None
    assert tool.calls == [], "the tool waits for the answer"
    await h.respond("yes")
    await h.settle()
    assert tool.calls == [{"command": "git push origin master"}]
    ((_, outcome),) = results(h)
    assert outcome == Outcome.Ok("output")
    await h.close()


@scenario
async def test_a_path_tool_shows_its_path_and_a_tool_without_one_its_arguments(
    tmp_path: Path,
) -> None:
    path = os.path.realpath(tmp_path / "x")  # a real path: shown as it is
    reply = calls(call("a", "write", path=path, content="text"), call("b", "other", n=1))
    tools = [FakeTool("write", "w"), FakeTool("other", "o")]
    h, _ = await start(tmp_path, [reply, say("ok")], {"ask": ["write", "other"]}, tools)
    first, second = (await h.view()).questions
    assert first.text == f"write: {path}\n(ask rule: write)"
    assert second.text == 'other: {"n": 1}\n(ask rule: other)'
    await h.close()


# no, decline, abort


@scenario
async def test_no_gives_the_call_an_error_the_model_reads(tmp_path: Path) -> None:
    h, (tool,) = await start(tmp_path, [bash("git push"), say("ok")], {"ask": ["bash"]})
    await h.respond("no")
    await h.settle()
    assert tool.calls == []
    assert refusal(h) == REFUSED
    await h.close()


@scenario
async def test_a_declined_question_is_a_refusal(tmp_path: Path) -> None:
    h, (tool,) = await start(tmp_path, [bash("git push"), say("ok")], {"ask": ["bash"]})
    await h.respond(None)
    await h.settle()
    assert tool.calls == []
    assert refusal(h) == REFUSED
    await h.close()


@scenario
async def test_an_abort_while_asking_refuses_every_open_call(tmp_path: Path) -> None:
    h, (tool,) = await start(tmp_path, [bash("one", "two")], {"ask": ["bash"]})
    assert len((await h.view()).questions) == 2
    await h.abort()
    await h.settle()
    view = await h.view()
    assert view.questions == ()
    assert view.ending == Ending.Aborted()
    assert tool.calls == []
    assert [o for _, o in results(h)] == [Outcome.Err(REFUSED)] * 2
    await h.close()


@scenario
async def test_a_batch_asks_at_once_and_clients_race_to_answer(tmp_path: Path) -> None:
    h, (tool,) = await start(tmp_path, [bash("one", "two", "three"), say("ok")], {"ask": ["bash"]})
    alice = await h.connect("alice@laptop:tui")
    bob = await h.connect("bob@phone:web")
    first, second, third = (await h.view()).questions
    assert [q.text.splitlines()[0] for q in (first, second, third)] == [
        "bash: one",
        "bash: two",
        "bash: three",
    ]
    await bob.answer(first.id, "yes")
    with pytest.raises(LookupError):
        await alice.answer(first.id, "no")  # the first good answer won
    await alice.answer(second.id, "no")
    await bob.answer(third.id, "yes")
    await h.settle()
    assert [c for c, _ in results(h)] == ["a", "b", "c"], "results are in call order"
    assert tool.calls == [{"command": "one"}, {"command": "three"}]
    assert [o for _, o in results(h)][1] == Outcome.Err(REFUSED)
    await h.close()


@scenario
async def test_a_wrong_answer_is_refused_and_the_question_stays_open(tmp_path: Path) -> None:
    h, (tool,) = await start(tmp_path, [bash("ls"), say("ok")], {"ask": ["bash"]})
    with pytest.raises(ValueError, match="must be one of"):
        await h.respond("maybe")
    assert len((await h.view()).questions) == 1
    assert tool.calls == []
    await h.close()


# Rules: order and matching


@scenario
async def test_deny_is_checked_first_then_allow_then_ask(tmp_path: Path) -> None:
    options = {"ask": ["read", "write"], "allow": ["write", "edit"], "deny": ["edit"]}
    reply = calls(
        call("a", "edit", path="x"), call("b", "write", path="y"), call("c", "read", path="z")
    )
    tools = [FakeTool(name, name) for name in ("edit", "write", "read")]
    h, (edit, write, read) = await start(tmp_path, [reply, say("ok")], options, tools)
    (question,) = (await h.view()).questions
    assert question.text.startswith("read: "), "only the unmatched-by-allow call asks"
    await h.respond("yes")
    await h.settle()
    assert (edit.calls, len(write.calls), len(read.calls)) == ([], 1, 1)
    first, second, third = (o for _, o in results(h))
    assert isinstance(first, Outcome.Err)
    assert "deny rule 'edit'" in first.error
    assert second == Outcome.Ok("write")
    assert third == Outcome.Ok("read")
    await h.close()


@PATH_PATTERNS
@scenario
async def test_a_deny_refuses_without_asking(tmp_path: Path) -> None:
    reply = calls(call("a", "write", path="/etc/ssh/sshd_config", content="x"))
    h, (tool,) = await start(
        tmp_path,
        [reply, say("ok")],
        {"deny": ["write:/etc/*"], "ask": ["write"]},
        [FakeTool("write", "w")],
    )
    assert (await h.view()).questions == ()
    assert tool.calls == []
    assert "deny rule 'write:/etc/*'" in refusal(h)
    await h.close()


@scenario
async def test_nothing_matching_runs_without_a_question(tmp_path: Path) -> None:
    h, (tool,) = await start(
        tmp_path, [bash("git status"), say("ok")], {"ask": ["read"], "allow": ["bash:git push"]}
    )
    assert (await h.view()).questions == ()
    assert tool.calls == [{"command": "git status"}]
    await h.close()


EXACT = [
    # (allow rule, command, runs without asking)
    ("bash:git push origin master", "git push origin master", True),
    ("bash:git push origin master", "git push origin master ", False),
    ("bash:git push origin master", "git push origin master && rm -rf x", False),
    ("bash:git push origin master", "Git push origin master", False),  # case matters
    ('bash:git commit -m "wip"', 'git commit -m "wip"', True),  # quotes are ordinary text
    ("bash:ls [*].py", "ls *.py", True),  # [*] is a literal star, as always writes it
    ("bash:ls [*].py", "ls x.py", False),
    ("bash:a\\b", "a\\b", True),  # a backslash is itself
    ("bash", "anything; at all", True),
]


@pytest.mark.parametrize(("rule", "command", "runs"), EXACT)
@scenario
async def test_a_bash_allow_names_one_exact_command(
    tmp_path: Path, rule: str, command: str, runs: bool
) -> None:
    options = {"ask": ["bash"], "allow": [rule]}
    h, (tool,) = await start(tmp_path, [bash(command), say("ok")], options)
    assert bool((await h.view()).questions) is not runs
    assert (tool.calls == []) is not runs
    await h.close()


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        ({"deny": ["bash:rm *"]}, "rule 'bash:rm *' in deny: there a bash rule is the bare name"),
        ({"ask": ["bash:git push"]}, "rule 'bash:git push' in ask: there a bash rule is the bare"),
        ({"allow": ["bash:git status*"]}, "names one exact command, without wildcards"),
        ({"allow": ["bash:ls [ab]"]}, "names one exact command, without wildcards"),
        ({"allow": ["bash:git ?ull"]}, "names one exact command, without wildcards"),
    ],
)
@scenario
async def test_a_bash_rule_with_a_pattern_is_refused_rather_than_trusted(
    tmp_path: Path, options: dict[str, list[str]], problem: str
) -> None:
    """A pattern over a command's text is easy to step around (`ls; rm x`, `/bin/rm`, `sudo rm`,
    `bash -c "rm x"`), so it is no rule at all: the call is refused, naming the problem."""
    h, (tool,) = await start(tmp_path, [bash("ls"), say("ok")], options)
    assert tool.calls == []
    assert problem in refusal(h)
    await h.close()


PATHS = [
    ("write:/etc/*", "/etc/passwd", True),
    ("write:/etc/*", "/etc/ssh/sshd_config", True),
    ("write:/etc/*", "/etcetera/x", False),
    ("write:/etc/*", "etc/passwd", False),
    ("write:/etc/*", "/tmp/../etc/passwd", True),  # the path it names, not the one it spells
    ("write:/etc/*", "//etc/x", True),
    ("write:/etc/*", "/./etc/x", True),
    ("write:/etc/*", "../" * 40 + "etc/x", True),  # relative to the session's directory
    ("write:/etc/*", "/ETC/x", sys.platform == "darwin"),  # case matters, but macOS folds it
    ("write:*.env", "/home/me/prod.env", True),
]


@PATH_PATTERNS
@pytest.mark.parametrize(("rule", "path", "matches"), PATHS)
@scenario
async def test_a_path_pattern_matches_the_path_argument(
    tmp_path: Path, rule: str, path: str, matches: bool
) -> None:
    reply = calls(call("a", "write", path=path, content="x"))
    h, _ = await start(tmp_path, [reply, say("ok")], {"ask": [rule]}, [FakeTool("write", "w")])
    assert bool((await h.view()).questions) is matches
    await h.close()


@PATH_PATTERNS
@scenario
async def test_a_relative_deny_matches_the_path_as_written_and_as_resolved(
    tmp_path: Path,
) -> None:
    """`secrets/*` names the path the model wrote; `*/secrets/*` the one it lands on."""
    for rule, path in [
        ("write:secrets/*", "secrets/key"),
        ("write:*/secrets/*", "sub/../secrets/key"),
    ]:
        reply = calls(call("a", "write", path=path, content="x"))
        h, (tool,) = await start(
            tmp_path / rule[6:8],
            [reply, say("ok")],
            {"deny": [rule]},
            [FakeTool("write", "w")],
        )
        assert tool.calls == [], rule
        assert f"deny rule {rule!r}" in refusal(h)
        await h.close()


@PATH_PATTERNS
@scenario
async def test_a_deny_on_grep_looks_at_every_file_it_is_given(tmp_path: Path) -> None:
    reply = calls(call("a", "grep", pattern="root", files=["ok.txt", "/etc/shadow"]))
    h, (tool,) = await start(
        tmp_path, [reply, say("ok")], {"deny": ["grep:/etc/*"]}, [FakeTool("grep", "g")]
    )
    assert tool.calls == []
    assert "deny rule" in refusal(h)
    await h.close()


@PATH_PATTERNS
@scenario
async def test_a_grep_with_no_path_searches_the_working_directory(tmp_path: Path) -> None:
    reply = calls(call("a", "grep", pattern="x"))
    here = os.path.realpath(tmp_path)
    h, (tool,) = await start(
        tmp_path, [reply, say("ok")], {"deny": [f"grep:{here}"]}, [FakeTool("grep", "g")]
    )
    assert tool.calls == []
    assert "deny rule" in refusal(h)
    await h.close()


@PATH_PATTERNS
@pytest.mark.parametrize("tool", ["grep", "find"])
@pytest.mark.parametrize("root", ["/etc/ssh", "/etc/ssh/", "/tmp/../etc/ssh", "link/ssh"])
@scenario
async def test_a_deny_on_a_search_refuses_a_root_inside_the_denied_directory(
    tmp_path: Path, tool: str, root: str
) -> None:
    """`deny: grep:/etc/*` would be no guard if `path=/etc/ssh` or a link into `/etc` could
    search it."""
    (tmp_path / "link").symlink_to("/etc")
    reply = calls(call("a", tool, pattern="root", path=root))
    h, (fake,) = await start(
        tmp_path, [reply, say("ok")], {"deny": [f"{tool}:/etc/*"]}, [FakeTool(tool, "t")]
    )
    assert fake.calls == []
    assert "deny rule" in refusal(h)
    await h.close()


@PATH_PATTERNS
@pytest.mark.parametrize("tool", ["grep", "find"])
@pytest.mark.parametrize("root", ["/etc", "/", "/tmp/../etc", "link", "link/", ".", "/etcetera"])
@scenario
async def test_a_search_whose_root_is_not_denied_runs_without_the_denied_files(
    tmp_path: Path, tool: str, root: str
) -> None:
    """The root is not itself denied, so the search runs, told to leave out the denied files. A
    root that cannot hold them (`/etcetera`) is told too: a link under it might lead there."""
    (tmp_path / "link").symlink_to("/etc")
    here = os.path.realpath(tmp_path)
    rule = f"{tool}:/etc/*" if root != "." else f"{tool}:{here}/secret/*"
    reply = calls(call("a", tool, pattern="root", path=root))
    h, (fake,) = await start(tmp_path, [reply, say("ok")], {"deny": [rule]}, [FakeTool(tool, "t")])
    (sent,) = fake.calls
    patterns = cast("list[str]", sent["exclude"])
    denied = os.path.realpath("/etc/passwd") if root != "." else f"{here}/secret/key"
    assert any(re.fullmatch(p, denied, re.DOTALL) for p in patterns)
    assert not any(re.fullmatch(p, f"{here}/open/key", re.DOTALL) for p in patterns)
    await h.close()


async def search_outputs(
    root: Path, replies: list[Part.Call], options: dict[str, list[str]], tool: FakeTool
) -> list[str]:
    """What the real `tool` answered, call by call, in a session of `options`."""
    h, _ = await start(root, [calls(*replies), say("ok")], options, [tool])
    body = next(b for b in reversed(h.bodies()) if isinstance(b, Body.Results))
    await h.close()
    return [o.value if isinstance(o, Outcome.Ok) else str(o) for _, o, _ in body.outcomes]


def secret_tree(tmp_path: Path) -> Path:
    real = tmp_path.resolve()
    (real / "open").mkdir()
    (real / "secret").mkdir()
    (real / "open" / "a.txt").write_text("needle\n")
    (real / "secret" / "b.txt").write_text("needle\n")
    return real


@PATH_PATTERNS
@pytest.mark.parametrize("exclude", [None, [], ["nothing-matches-this"]])
@scenario
async def test_a_model_cannot_switch_the_filter_off_with_its_own_exclude(
    tmp_path: Path, exclude: list[str] | None
) -> None:
    """`exclude: null` is no `exclude`; a list the model gives only adds to the deny patterns."""
    real = secret_tree(tmp_path)
    call_ = call("a", "grep", pattern="needle", exclude=exclude)
    (text,) = await search_outputs(
        real, [call_], {"deny": [f"grep:{real}/secret/*"]}, cast("FakeTool", GrepTool(real))
    )
    assert "a.txt" in text
    assert "b.txt" not in text


@PATH_PATTERNS
@pytest.mark.parametrize("args_raw", ['{"pattern": "x", "exclude": "all"}', "not json", "[]"])
@scenario
async def test_a_search_whose_arguments_cannot_take_the_deny_patterns_is_refused(
    tmp_path: Path, args_raw: str
) -> None:
    reply = calls(Part.Call("a", "grep", args_raw))
    h, (fake,) = await start(
        tmp_path, [reply, say("ok")], {"deny": ["grep:/etc/*"]}, [FakeTool("grep", "g")]
    )
    assert fake.calls == []
    assert "deny rules cannot be applied" in refusal(h)
    await h.close()


@PATH_PATTERNS
@pytest.mark.parametrize("tool", ["grep", "find"])
@pytest.mark.parametrize("root", ["/etc", "/", "link"])
@scenario
async def test_an_ask_on_a_search_still_asks_when_the_root_holds_the_pattern(
    tmp_path: Path, tool: str, root: str
) -> None:
    (tmp_path / "link").symlink_to("/etc")
    reply = calls(
        call("a", tool, pattern="x", path=root), call("b", tool, pattern="x", path="/etcetera")
    )
    h, (fake,) = await start(
        tmp_path, [reply, say("ok")], {"ask": [f"{tool}:/etc/*"]}, [FakeTool(tool, "t")]
    )
    (question,) = (await h.view()).questions
    assert f"ask rule: {tool}:/etc/*" in question.text
    assert [c["path"] for c in fake.calls] == ["/etcetera"]
    await h.close()


@PATH_PATTERNS
@pytest.mark.parametrize(("tool", "pattern"), [("grep", "needle"), ("find", "*")])
@scenario
async def test_an_unreadable_denied_directory_is_not_entered_or_named(
    tmp_path: Path, tool: str, pattern: str
) -> None:
    real = secret_tree(tmp_path)
    (real / "secret" / "private").mkdir()
    (real / "secret" / "private" / "key").write_text("needle\n")
    (real / "secret" / "private").chmod(0)
    try:
        made = GrepTool(real) if tool == "grep" else FindTool(real)
        (text,) = await search_outputs(
            real,
            [call("a", tool, pattern=pattern)],
            {"deny": [f"{tool}:{real}/secret/*"]},
            cast("FakeTool", made),
        )
    finally:
        (real / "secret" / "private").chmod(0o700)
    assert "a.txt" in text
    assert "secret" not in text
    assert "private" not in text
    assert "could not" not in text


@PATH_PATTERNS
@scenario
async def test_the_tool_reads_a_set_and_a_newline_as_the_check_does(tmp_path: Path) -> None:
    """`--`, `&&` and `~~` in a set are no operators, and `*` crosses a newline, in the tool
    exactly as in the check."""
    real = tmp_path.resolve()
    (real / "open").mkdir()
    (real / "keep").mkdir()
    for name in ["+", ",", "-", "a", "b", "&", "~", "x\ny", "kept"]:
        (real / "open" / name).write_text("needle\n")
    rules = [f"grep:{real}/open/[+--]", f"grep:{real}/open/[&&]", f"grep:{real}/open/[a-a~~]"]
    rules.append(f"grep:{real}/open/x*y")
    (text,) = await search_outputs(
        real,
        [call("a", "grep", pattern="needle")],
        {"deny": rules},
        cast("FakeTool", GrepTool(real)),
    )
    shown = {line.split(":")[0] for line in text.splitlines() if ":1:needle" in line}
    assert shown == {"open/b", "open/kept"}


@PATH_PATTERNS
@pytest.mark.parametrize(
    ("rule", "path", "denied"),
    [
        ("write:/etc/[^b]ar", "/etc/^ar", True),  # `^` is no negation, as in fnmatch
        ("write:/etc/[^b]ar", "/etc/xar", False),
        ("write:/etc/[!b]ar", "/etc/xar", True),
        ("write:/etc/[ab", "/etc/[ab", True),  # an unclosed `[` is itself
        ("write:/etc/{a,b}", "/etc/{a,b}", True),
        ("write:/tmp/../etc/*", "/etc/passwd", True),
        ("write:/a/b/../c/*", "/a/c/x", True),
    ],
)
@scenario
async def test_a_rule_pattern_means_what_fnmatch_says(
    tmp_path: Path, rule: str, path: str, denied: bool
) -> None:
    reply = calls(call("a", "write", path=path, content="x"))
    h, (tool,) = await start(
        tmp_path, [reply, say("ok")], {"deny": [rule]}, [FakeTool("write", "w")]
    )
    assert (tool.calls == []) is denied
    await h.close()


@PATH_PATTERNS
@scenario
async def test_a_search_excludes_what_an_unclosed_bracket_or_a_caret_rule_names(
    tmp_path: Path,
) -> None:
    """The tool is given the same grammar as the check: a `[` in a rule breaks no search."""
    real = secret_tree(tmp_path)
    (real / "open" / "[x").write_text("needle\n")
    (text,) = await search_outputs(
        real,
        [call("a", "grep", pattern="needle")],
        {"deny": [f"grep:{real}/open/[x", f"grep:{real}/secret/[!q]*"]},
        cast("FakeTool", GrepTool(real)),
    )
    assert "a.txt" in text
    assert "[x" not in text
    assert "b.txt" not in text


@PATH_PATTERNS
@scenario
async def test_a_read_deny_also_guards_what_grep_would_read(tmp_path: Path) -> None:
    reply = calls(
        call("a", "grep", pattern="x", files=["/etc/shadow"]),
        call("b", "grep", pattern="x", path="/"),
        call("c", "find", pattern="x", path="/"),
    )
    h, (fake, find) = await start(
        tmp_path,
        [reply, say("ok")],
        {"deny": ["read:/etc/*", "read"]},
        [FakeTool("grep", "g"), FakeTool("find", "f")],
    )
    (patterns,) = [cast("list[str]", c["exclude"]) for c in fake.calls]
    assert any(re.fullmatch(p, os.path.realpath("/etc/shadow"), re.DOTALL) for p in patterns)
    assert find.calls == [{"pattern": "x", "path": "/"}]  # names, not content: read does not cover
    await h.close()


@PATH_PATTERNS
@pytest.mark.parametrize("tool", ["grep", "find"])
@scenario
async def test_a_search_with_no_path_cannot_return_what_a_deny_hides(
    tmp_path: Path, tool: str
) -> None:
    """End to end with the real tools, in a tmp dir: the denied files, and the link to one, are
    left out of the answer, which says that files were."""
    real = tmp_path.resolve()
    (real / "open").mkdir()
    (real / "secret").mkdir()
    (real / "open" / "a.txt").write_text("needle\n")
    (real / "secret" / "b.txt").write_text("needle\n")
    (real / "open" / "link.txt").symlink_to(real / "secret" / "b.txt")
    pattern = "needle" if tool == "grep" else "*.txt"
    reply = calls(call("a", tool, pattern=pattern), call("b", tool, pattern=pattern, path="open"))
    real_tool = cast("FakeTool", GrepTool(real) if tool == "grep" else FindTool(real))
    h, _ = await start(real, [reply, say("ok")], {"deny": [f"{tool}:{real}/secret/*"]}, [real_tool])
    body = next(b for b in reversed(h.bodies()) if isinstance(b, Body.Results))
    texts = [o.value for _, o, _ in body.outcomes if isinstance(o, Outcome.Ok)]
    assert len(texts) == 2
    for text in texts:
        assert "a.txt" in text
        assert "b.txt" not in text
        assert "link.txt" not in text
        assert "left out of this search" in text
    await h.close()


@PATH_PATTERNS
@scenario
async def test_an_allow_on_grep_needs_every_file_to_match(tmp_path: Path) -> None:
    ok = os.path.realpath(tmp_path / "ok")
    reply = calls(
        call("a", "grep", pattern="x", files=[f"{ok}/a", f"{ok}/b"]),
        call("b", "grep", pattern="x", files=[f"{ok}/a", "/etc/shadow"]),
        call("c", "grep", pattern="x", path=f"{ok}/d", files=["/etc/shadow"]),
    )
    options = {"ask": ["grep"], "allow": [f"grep:{ok}/*"]}
    h, (tool,) = await start(tmp_path, [reply, say("done")], options, [FakeTool("grep", "g")])
    assert len((await h.view()).questions) == 2
    assert tool.calls == [{"pattern": "x", "files": [f"{ok}/a", f"{ok}/b"]}]
    await h.close()


@PATH_PATTERNS
@pytest.mark.parametrize("tool", ["read", "write", "edit", "grep", "find"])
@scenario
async def test_every_path_tool_is_denied_by_the_path_it_lands_on(tmp_path: Path, tool: str) -> None:
    reply = calls(call("a", tool, path="/tmp/../etc/passwd", pattern="x"))
    h, (fake,) = await start(
        tmp_path, [reply, say("ok")], {"deny": [f"{tool}:/etc/*"]}, [FakeTool(tool, "t")]
    )
    assert fake.calls == []
    assert "deny rule" in refusal(h)
    await h.close()


@PATH_PATTERNS
@scenario
async def test_a_pattern_needs_the_argument_and_a_bare_rule_does_not(tmp_path: Path) -> None:
    """A call whose arguments are not an object or whose path is not a string matches no pattern
    (`grep` with no path searches the working directory); the bare rule matches them."""
    reply = calls(
        call("a", "grep", pattern="x"),
        Part.Call("b", "grep", "not json"),
        Part.Call("c", "grep", "[1]"),
        call("d", "grep", path=3),
    )
    grep = FakeTool("grep", "g")
    h, _ = await start(tmp_path, [reply, say("ok")], {"ask": ["grep:/nowhere/src/*"]}, [grep])
    assert (await h.view()).questions == ()
    await h.close()

    h, _ = await start(
        tmp_path / "bare", [reply, say("ok")], {"ask": ["grep"]}, [FakeTool("grep", "g")]
    )
    assert len((await h.view()).questions) == 4
    await h.close()


@scenario
async def test_a_tool_name_may_hold_a_colon_and_only_the_pattern_tools_split_at_one(
    tmp_path: Path,
) -> None:
    tool = FakeTool("srv:tool", "o")
    h, _ = await start(
        tmp_path, [calls(call("a", "srv:tool", n=1)), say("ok")], {"ask": ["srv:tool"]}, [tool]
    )
    (question,) = (await h.view()).questions
    assert question.text == 'srv:tool: {"n": 1}\n(ask rule: srv:tool)'
    await h.close()


@pytest.mark.parametrize(
    ("rule", "problem"),
    [
        ("bash:", "empty pattern"),
        ("", "names no tool"),
        ("read:[z-a]", "rule 'read:[z-a]' has an invalid pattern"),
    ],
)
@scenario
async def test_a_bad_rule_refuses_the_call_rather_than_letting_it_run(
    tmp_path: Path, rule: str, problem: str
) -> None:
    h, (tool,) = await start(tmp_path, [bash("ls"), say("ok")], {"deny": [rule]})
    assert tool.calls == []
    assert problem in refusal(h)
    await h.close()


# /config checks the tool names as the start does


@scenario
async def test_config_refuses_a_rule_naming_no_tool_and_saves_nothing(tmp_path: Path) -> None:
    h, _ = await start(tmp_path, [bash("ls"), say("ok")])
    saved = tmp_path / "tau-home" / "config.json"
    before = saved.read_text(encoding="utf-8") if saved.exists() else None
    for values in ({"ask": ["Bash"]}, {"deny": ["bash", "wirte:/etc/*"]}):
        with pytest.raises(ConfigError, match="names the tool"):
            await h.configure("permissions", values, save=True)
    with pytest.raises(ConfigError, match="there a bash rule is the bare name"):
        await h.configure("permissions", {"deny": ["bash:rm *"]}, save=True)
    assert (saved.read_text(encoding="utf-8") if saved.exists() else None) == before
    await h.configure("permissions", {"deny": ["bash"]}, save=True)
    assert json.loads(saved.read_text(encoding="utf-8"))["extensions"]["permissions"] == {
        "deny": ["bash"]
    }
    await h.close()


@scenario
async def test_a_rule_for_a_built_in_tool_that_is_not_loaded_is_no_error(tmp_path: Path) -> None:
    """`agent` is a tool of `agents`, which is off unless asked for; a deny for it is a guard
    for the sessions that load it. A name no extension provides is still refused."""
    h, _ = await start(tmp_path, [bash("ls"), say("ok")])
    await h.configure("permissions", {"deny": ["agent"]})
    await h.settle()
    with pytest.raises(ConfigError, match="names the tool 'agnet'"):
        await h.configure("permissions", {"deny": ["agnet"]})
    await h.close()


# always


@scenario
async def test_always_runs_the_call_and_saves_its_exact_rule_for_every_session(
    tmp_path: Path,
) -> None:
    script = [bash("git push origin master"), bash("git push origin master"), say("ok")]
    h, (tool,) = await start(tmp_path, script, {"ask": ["bash"]})
    await h.respond("always")
    await h.settle()
    saved = tmp_path / "tau-home" / "config.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == {
        "extensions": {"permissions": {"allow": ["bash:git push origin master"]}}
    }
    configs = [b.config.data for b in h.bodies() if isinstance(b, Body.Config)]
    assert not any("allow" in str(data) for data in configs[1:]), "no node: the file is global"
    await h.step()  # the model asks for the same again: no question
    await h.settle()
    assert tool.calls == [{"command": "git push origin master"}] * 2
    await h.close()

    # A new session reads the file.
    again = FakeTool("bash", "output")
    h = await Harness.load(
        tmp_path,
        provider=FakeProvider(
            [bash("git push origin master"), bash("git push origin dev"), say("ok")]
        ),
        tools=[again],
        extensions=[Permissions(tmp_path)],
        catalog=read_models(saved, cli=rules(ask=["bash"])),
    )
    await h.send("and now?")
    await h.settle()
    assert again.calls == [{"command": "git push origin master"}]
    (question,) = (await h.view()).questions
    assert question.text.startswith("bash: git push origin dev"), "the rule was exact"
    await h.close()


@pytest.mark.parametrize("tool", ["read", "write", "edit", "bash"])
@scenario
async def test_always_on_a_malformed_call_saves_nothing_and_says_so(
    tmp_path: Path, tool: str
) -> None:
    """The bare tool name would allow every call of the tool, so the call runs once."""
    reply = calls(call("a", tool, file_path="src/a.py"))
    h, (fake,) = await start(tmp_path, [reply, say("ok")], {"ask": [tool]}, [FakeTool(tool, "t")])
    await h.respond("always")
    await h.settle()
    assert len(fake.calls) == 1
    assert not (tmp_path / "tau-home" / "config.json").exists()
    assert any("no rule was saved" in n for n in h.notices())
    await h.close()


@PATH_PATTERNS
@scenario
async def test_a_relative_allow_rule_starts_in_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    reply = calls(
        call("a", "read", path="src/a.py"),
        call("b", "read", path="./src/../src/b.py"),
        call("c", "read", path="other/c.py"),
    )
    options = {"ask": ["read"], "allow": ["read:src/*"]}
    h, (tool,) = await start(tmp_path, [reply, say("done")], options, [FakeTool("read", "r")])
    (question,) = (await h.view()).questions
    assert question.text.startswith(f"read: {os.path.realpath(tmp_path / 'other')}/c.py\n")
    assert [c["path"] for c in tool.calls] == ["src/a.py", "./src/../src/b.py"]
    await h.close()


@scenario
async def test_always_on_a_command_with_glob_characters_allows_only_that_command(
    tmp_path: Path,
) -> None:
    script = [bash("ls *.py"), bash("ls x.py"), bash("ls *.py"), say("ok")]
    h, (tool,) = await start(tmp_path, script, {"ask": ["bash"]})
    await h.respond("always")
    await h.settle()
    saved = tmp_path / "tau-home" / "config.json"
    assert json.loads(saved.read_text(encoding="utf-8"))["extensions"]["permissions"]["allow"] == [
        "bash:ls [*].py"
    ]
    (question,) = (await h.view()).questions
    assert question.text.startswith("bash: ls x.py")
    await h.respond("no")
    await h.settle()
    assert tool.calls == [{"command": "ls *.py"}] * 2
    await h.close()


@scenario
async def test_always_on_a_bare_rule_for_an_extension_tool_allows_the_tool(
    tmp_path: Path,
) -> None:
    script = [calls(call("a", "mytool", n=1)), calls(call("a", "mytool", n=2)), say("ok")]
    h, (tool,) = await start(tmp_path, script, {"ask": ["mytool"]}, [FakeTool("mytool", "o")])
    await h.respond("always")
    await h.settle()
    saved = tmp_path / "tau-home" / "config.json"
    assert json.loads(saved.read_text(encoding="utf-8"))["extensions"]["permissions"]["allow"] == [
        "mytool"
    ]
    assert tool.calls == [{"n": 1}, {"n": 2}]
    await h.close()


@scenario
async def test_a_deny_still_refuses_what_always_allowed(tmp_path: Path) -> None:
    h, (tool,) = await start(
        tmp_path, [bash("rm x"), say("ok")], {"allow": ["bash:rm x"], "deny": ["bash"]}
    )
    assert tool.calls == []
    assert "deny rule" in refusal(h)
    await h.close()


# Paths: the path a call names, not the one it spells


@PATH_PATTERNS
@scenario
async def test_a_deny_sees_through_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "etc-link").symlink_to("/etc")
    reply = calls(call("a", "write", path="etc-link/x", content="x"))
    h, (tool,) = await start(
        tmp_path, [reply, say("ok")], {"deny": ["write:/etc/*"]}, [FakeTool("write", "w")]
    )
    assert tool.calls == []
    assert "deny rule" in refusal(h)
    await h.close()


@PATH_PATTERNS
@scenario
async def test_a_rule_about_a_linked_directory_denies_the_path_it_lands_on(tmp_path: Path) -> None:
    """macOS's `/etc` is a link to `/private/etc`: `write:/etc/*` must refuse what lands there,
    however it is spelled. Here `link` is the link and `real` the directory."""
    real = tmp_path.resolve() / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    for n, path in enumerate([f"{real}/x", f"{tmp_path}/real/../link/x", f"{real}/sub/../x"]):
        reply = calls(call("a", "write", path=path, content="x"))
        h, (tool,) = await start(
            tmp_path / f"s{n}",
            [reply, say("ok")],
            {"deny": [f"write:{tmp_path}/link/*"]},
            [FakeTool("write", "w")],
        )
        assert tool.calls == [], path
        assert "deny rule" in refusal(h)
        await h.close()


@PATH_PATTERNS
@scenario
async def test_an_allow_about_a_linked_directory_is_not_widened_by_the_link(
    tmp_path: Path,
) -> None:
    """An allow is read as written: a link where the rule names a directory cannot make it allow
    the directory the link leads to."""
    real = tmp_path.resolve() / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    reply = calls(call("a", "write", path=f"{real}/x", content="x"))
    options = {"ask": ["write"], "allow": [f"write:{tmp_path.resolve()}/link/*"]}
    h, (tool,) = await start(tmp_path, [reply, say("ok")], options, [FakeTool("write", "w")])
    (question,) = (await h.view()).questions
    assert question.text.startswith(f"write: {real}/x\n")
    assert tool.calls == []
    await h.close()


@PATH_PATTERNS
@scenario
async def test_a_deny_ignores_case_on_macos_and_an_allow_never_does(tmp_path: Path) -> None:
    """A macOS volume folds case: `SECRET/key` is `secret/key` there, and a deny that missed it
    would be no guard. An allow that folded would only widen."""
    real = tmp_path.resolve()
    reply = calls(
        call("a", "write", path=f"{real}/SECRET/key", content="x"),
        call("b", "write", path=f"{real}/OK/x", content="x"),
    )
    options = {
        "deny": [f"write:{real}/secret/*"],
        "ask": ["write"],
        "allow": [f"write:{real}/ok/*"],
    }
    h, _ = await start(tmp_path, [reply, say("ok")], options, [FakeTool("write", "w")])
    asked = [q.text.split("\n")[0] for q in (await h.view()).questions]
    if sys.platform == "darwin":
        assert asked == [f"write: {real}/OK/x"], "the deny refused SECRET; the allow missed OK"
    else:
        assert asked == [f"write: {real}/SECRET/key", f"write: {real}/OK/x"]
    await h.close()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="path patterns work on this platform; the tests above hold them",
)
@scenario
async def test_on_windows_a_path_pattern_is_refused_and_always_saves_nothing(
    tmp_path: Path,
) -> None:
    """How a pattern meets a Windows path is not decided: a rule with one is refused where it is
    configured (a call under it is refused, naming why), and `always` saves no rule."""
    reply = calls(call("a", "write", path="notes.txt", content="x"))
    h, (tool,) = await start(
        tmp_path, [reply, say("ok")], {"deny": ["write:secret/*"]}, [FakeTool("write", "w")]
    )
    assert tool.calls == []
    assert "path patterns are not available on Windows" in refusal(h)
    await h.close()
    h, (tool,) = await start(
        tmp_path / "always", [reply, say("ok")], {"ask": ["write"]}, [FakeTool("write", "w")]
    )
    await h.respond("always")
    await h.settle()
    assert len(tool.calls) == 1
    assert not (tmp_path / "always" / "tau-home" / "config.json").exists()
    assert any("path patterns are not available on Windows" in n for n in h.notices())
    await h.close()


@PATH_PATTERNS
@scenario
async def test_an_allow_needs_the_resolved_path_to_match(tmp_path: Path) -> None:
    ok = os.path.realpath(tmp_path / "ok")
    reply = calls(
        call("a", "write", path="ok/file", content="x"),  # relative: resolves under ok
        call("b", "write", path=f"{ok}/../bad", content="x"),  # spells ok, names its sibling
    )
    options = {"ask": ["write"], "allow": [f"write:{ok}/*"]}
    h, (tool,) = await start(tmp_path, [reply, say("done")], options, [FakeTool("write", "w")])
    (question,) = (await h.view()).questions
    assert question.text.startswith(f"write: {os.path.realpath(tmp_path / 'bad')}\n")
    assert f"as written: {ok}/../bad" in question.text
    assert tool.calls == [{"path": "ok/file", "content": "x"}], "only the allowed call ran"
    await h.close()


@PATH_PATTERNS
@scenario
async def test_always_saves_the_resolved_absolute_path(tmp_path: Path) -> None:
    reply = calls(call("a", "write", path="sub/../notes.txt", content="x"))
    h, _ = await start(tmp_path, [reply, say("ok")], {"ask": ["write"]}, [FakeTool("write", "w")])
    await h.respond("always")
    await h.settle()
    saved = json.loads((tmp_path / "tau-home" / "config.json").read_text(encoding="utf-8"))
    assert saved["extensions"]["permissions"]["allow"] == [
        f"write:{os.path.realpath(tmp_path / 'notes.txt')}"
    ]
    await h.close()


# always, hidden by a higher layer


@scenario
async def test_always_holds_for_the_run_when_a_node_hides_the_files_list_and_says_so(
    tmp_path: Path,
) -> None:
    script = [bash("git push"), bash("git push"), say("ok")]
    h, (tool,) = await start(tmp_path, script, {"ask": ["bash"], "allow": ["bash:ls"]})
    await h.respond("always")
    await h.settle()
    assert (await h.view()).questions == (), "the second identical call is not asked again"
    assert tool.calls == [{"command": "git push"}] * 2
    (notice,) = [n for n in h.notices() if n.startswith("permissions:")]
    assert "'bash:git push'" in notice
    assert "hides it" in notice
    await h.close()


@scenario
async def test_always_says_it_was_written_when_nothing_hides_it(tmp_path: Path) -> None:
    h, _ = await start(tmp_path, [bash("git push"), say("ok")], {"ask": ["bash"]})
    await h.respond("always")
    await h.settle()
    (notice,) = [n for n in h.notices() if n.startswith("permissions:")]
    assert notice == (
        "permissions: 'bash:git push' is allowed from now on (written to allow in your config file)"
    )
    await h.close()


@scenario
async def test_saving_a_rule_for_a_missing_tool_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    h, _ = await start(tmp_path, [])
    user = UserConfig(tmp_path / "tau-home")
    user_config = tmp_path / "tau-home" / "config.json"
    assert not user_config.exists()
    with pytest.raises(ConfigError, match="names the tool 'Bash'"):
        await h.configure("permissions", {"ask": ["Bash"]}, save=True)
    assert not user_config.exists(), "neither the node nor the file"
    assert user.saved("permissions") == {}


@scenario
async def test_config_checks_only_the_fields_it_sets(tmp_path: Path) -> None:
    h = await Harness.create(
        tmp_path,
        tools=[FakeTool("bash", "output")],
        extensions=[Permissions(tmp_path)],
        user_config=UserConfig(tmp_path / "tau-home", cli=rules(deny=["nosuch"])),
    )
    await h.configure("permissions", {"ask": ["bash"]}, save=True)
    await h.settle()
    with pytest.raises(ConfigError, match="names the tool 'nosuch2'"):
        await h.configure("permissions", {"ask": ["nosuch2"]})
