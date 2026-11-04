"""`Permissions` (D24 section 7): rules that refuse, allow or ask about a tool call, and the
question's `yes | always | no`."""

import json
import os
from pathlib import Path

import pytest
from fakes import FAKE_MODEL, FakeProvider, FakeTool, Reply
from harness import Harness, say, scenario

from tau import (
    Body,
    Ending,
    Outcome,
    Part,
    Permissions,
    Stop,
    UserConfig,
    model_config,
    read_models,
)

REFUSED = "the user refused this call"


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
        tmp_path, [bash("git push origin master"), say("ok")], {"ask": ["bash:git push*"]}
    )
    (question,) = (await h.view()).questions
    assert question.text == "bash: git push origin master\n(ask rule: bash:git push*)"
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
    reply = calls(call("a", "write", path="/tmp/x", content="text"), call("b", "other", n=1))
    tools = [FakeTool("write", "w"), FakeTool("other", "o")]
    h, _ = await start(tmp_path, [reply, say("ok")], {"ask": ["write", "other"]}, tools)
    first, second = (await h.view()).questions
    assert first.text == "write: /tmp/x\n(ask rule: write)"
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
    options = {"ask": ["bash"], "allow": ["bash:ls*", "bash:rm*"], "deny": ["bash:rm*"]}
    h, (tool,) = await start(tmp_path, [bash("rm x", "ls y", "git z"), say("ok")], options)
    (question,) = (await h.view()).questions
    assert question.text.startswith("bash: git z"), "only the unmatched-by-allow call asks"
    await h.respond("yes")
    await h.settle()
    assert tool.calls == [{"command": "ls y"}, {"command": "git z"}]
    first, second, third = (o for _, o in results(h))
    assert isinstance(first, Outcome.Err)
    assert "deny rule 'bash:rm*'" in first.error
    assert second == Outcome.Ok("output")
    assert third == Outcome.Ok("output")
    await h.close()


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
        tmp_path, [bash("git status"), say("ok")], {"ask": ["bash:git push*", "read"]}
    )
    assert (await h.view()).questions == ()
    assert tool.calls == [{"command": "git status"}]
    await h.close()


GLOBS = [
    # (rule, command, matches)
    ("bash:git push origin master", "git push origin master", True),
    ("bash:git push origin master", "git push origin master ", False),
    ("bash:git push*", "git push", True),
    ("bash:git push*", "git pus", False),
    ("bash:git push*", "git push origin master && rm -rf x", True),  # a chain is one text
    ("bash:git*", "Git status", False),  # case matters
    ("bash:echo*", "echo a\necho b", True),  # * crosses a newline
    ("bash:cat /tmp/*", "cat /tmp/a/b/c", True),  # and a slash
    ("bash:git ?ull", "git pull", True),
    ("bash:git ?ull", "git ppull", False),
    ('bash:git commit -m "wip"', 'git commit -m "wip"', True),  # quotes are ordinary text
    ("bash:git commit -m 'a b'", "git commit -m 'a b'", True),
    ("bash:ls [*].py", "ls *.py", True),  # [*] is a literal star
    ("bash:ls [*].py", "ls x.py", False),
    ("bash:ls [ab]", "ls a", True),
    ("bash:a\\b", "a\\b", True),  # a backslash is itself
]


@pytest.mark.parametrize(("rule", "command", "matches"), GLOBS)
@scenario
async def test_a_bash_pattern_matches_the_whole_command_by_fnmatch(
    tmp_path: Path, rule: str, command: str, matches: bool
) -> None:
    h, (tool,) = await start(tmp_path, [bash(command), say("ok")], {"ask": [rule]})
    assert bool((await h.view()).questions) is matches
    assert (tool.calls == []) is matches
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
    ("write:/etc/*", "/ETC/x", False),
    ("write:*.env", "/home/me/prod.env", True),
]


@pytest.mark.parametrize(("rule", "path", "matches"), PATHS)
@scenario
async def test_a_path_pattern_matches_the_path_argument(
    tmp_path: Path, rule: str, path: str, matches: bool
) -> None:
    reply = calls(call("a", "write", path=path, content="x"))
    h, _ = await start(tmp_path, [reply, say("ok")], {"ask": [rule]}, [FakeTool("write", "w")])
    assert bool((await h.view()).questions) is matches
    await h.close()


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


@scenario
async def test_a_deny_on_grep_looks_at_every_file_it_is_given(tmp_path: Path) -> None:
    reply = calls(call("a", "grep", pattern="root", files=["ok.txt", "/etc/shadow"]))
    h, (tool,) = await start(
        tmp_path, [reply, say("ok")], {"deny": ["grep:/etc/*"]}, [FakeTool("grep", "g")]
    )
    assert tool.calls == []
    assert "deny rule" in refusal(h)
    await h.close()


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


@scenario
async def test_a_pattern_needs_the_argument_and_a_bare_rule_does_not(tmp_path: Path) -> None:
    """`grep` with no path, and a call whose arguments are not an object, match no pattern;
    the bare rule matches them."""
    reply = calls(
        call("a", "grep", pattern="x"),
        Part.Call("b", "grep", "not json"),
        Part.Call("c", "grep", "[1]"),
        call("d", "grep", path=3),
    )
    grep = FakeTool("grep", "g")
    h, _ = await start(tmp_path, [reply, say("ok")], {"ask": ["grep:src/*"]}, [grep])
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
    [("bash:", "empty pattern"), ("", "names no tool")],
)
@scenario
async def test_a_bad_rule_refuses_the_call_rather_than_letting_it_run(
    tmp_path: Path, rule: str, problem: str
) -> None:
    h, (tool,) = await start(tmp_path, [bash("ls"), say("ok")], {"deny": [rule]})
    assert tool.calls == []
    assert problem in refusal(h)
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
        tmp_path, [bash("rm x"), say("ok")], {"ask": ["bash"], "deny": ["bash:rm*"]}
    )
    assert tool.calls == []
    assert "deny rule" in refusal(h)
    await h.close()


# Paths: the path a call names, not the one it spells


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


# Commands: an allow never vouches for a chain, a deny looks at each piece

CHAINS = [
    "git status; rm -rf x",
    "git status && rm -rf x",
    "git status | sh",
    "git status\nrm -rf x",
    "git status $(rm -rf x)",
    "git status `rm x`",
    "git status > ~/.bashrc",
    "git status < /etc/passwd",
    "git status & rm x",
]


@pytest.mark.parametrize("command", CHAINS)
@scenario
async def test_an_allow_does_not_run_a_command_that_chains_or_redirects(
    tmp_path: Path, command: str
) -> None:
    options = {"ask": ["bash"], "allow": ["bash:git status*"]}
    h, (tool,) = await start(tmp_path, [bash(command), say("ok")], options)
    assert len((await h.view()).questions) == 1, "it falls to ask"
    assert tool.calls == []
    await h.close()


@pytest.mark.parametrize("command", ["git status", "git status -s", "git status --short"])
@scenario
async def test_an_allow_runs_a_plain_command(tmp_path: Path, command: str) -> None:
    options = {"ask": ["bash"], "allow": ["bash:git status*"]}
    h, (tool,) = await start(tmp_path, [bash(command), say("ok")], options)
    assert (await h.view()).questions == ()
    assert tool.calls == [{"command": command}]
    await h.close()


@scenario
async def test_a_pattern_that_holds_the_operator_allows_a_command_that_has_it(
    tmp_path: Path,
) -> None:
    options = {"ask": ["bash"], "allow": ["bash:git status && git diff*"]}
    h, (tool,) = await start(
        tmp_path, [bash("git status && git diff -s", "git status && rm x"), say("ok")], options
    )
    (question,) = (await h.view()).questions
    assert question.text.startswith("bash: git status && rm x")
    assert tool.calls == [{"command": "git status && git diff -s"}]
    await h.close()


REFUSED_BY_RM = [
    "rm -rf x",
    "ls; rm -rf x",
    "ls && rm x",
    "ls | rm x",
    " rm -rf x",
    "\trm x",
    "/bin/rm -rf x",
    "command rm x",
    "sudo rm x",
    "FOO=1 rm x",
    "A=1 B=2 /usr/bin/rm x",
    "echo $(rm x)",
    "echo `rm x`",
    "echo a\nrm x",
    "(rm x)",
    "{ rm x; }",
    "true || rm x",
    "rm\t-rf x",  # a tab is a space
    "ls;\trm x",
    "\\rm x",  # a backslash before the first word
    "ls; \\/bin/rm x",
    "if true; then rm x; fi",
    "for i in 1; do rm $i; done",
    "if false; then ls; else rm x; fi",
    "if false; then ls; elif true; then rm x; fi",
    "sudo -u root rm x",
    "sudo -n -- rm x",
    "env -i rm x",
    "env -u HOME rm x",
    "nice -n 10 rm x",
    "nice rm x",
    "timeout 5 rm x",
    "timeout -s KILL 5 rm x",
    "ls | xargs rm -f",
    "xargs -n1 rm x",
    "nohup nice -n 5 sudo rm x",
]


@pytest.mark.parametrize("command", REFUSED_BY_RM)
@scenario
async def test_a_bash_deny_refuses_a_command_with_a_matching_piece(
    tmp_path: Path, command: str
) -> None:
    h, (tool,) = await start(tmp_path, [bash(command), say("ok")], {"deny": ["bash:rm *"]})
    assert tool.calls == []
    assert "deny rule 'bash:rm *'" in refusal(h)
    await h.close()


@pytest.mark.parametrize("command", ["ls", "echo rm", "grep rm file", "firm x", "echo rm -rf"])
@scenario
async def test_a_bash_deny_leaves_other_commands_alone(tmp_path: Path, command: str) -> None:
    h, (tool,) = await start(tmp_path, [bash(command), say("ok")], {"deny": ["bash:rm *"]})
    assert tool.calls == [{"command": command}]
    await h.close()


@scenario
async def test_an_ask_rule_also_sees_the_pieces_of_a_chain(tmp_path: Path) -> None:
    h, (tool,) = await start(
        tmp_path, [bash("ls && git push origin master"), say("ok")], {"ask": ["bash:git push*"]}
    )
    assert len((await h.view()).questions) == 1
    assert tool.calls == []
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
