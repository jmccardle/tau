"""`hooks` (D34): shell commands run at τ's hook points, by the convention Claude Code, Codex and
Gemini CLI share. Every test runs real small `sh` scripts that read the event on standard input
and answer with an exit code or JSON, through a session and the files a person writes."""

import asyncio
import json
import os
import shlex
import signal
import stat
import sys
from pathlib import Path

import pytest
from fakes import FakeProvider, FakeTool, Reply
from harness import Harness, say, scenario
from world import World, loaded

from tau import Author, Body, CommandHooks, Outcome, Part, Stop

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows: hooks run through a POSIX shell, which τ does not have there (D34)",
)


def script(world: World, name: str, body: str) -> str:
    """An executable `sh` script under the test's home, as the command that runs it."""
    path = world.home / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return shlex.quote(str(path))


def hooks_file(world: World, **events: list[dict[str, object]]) -> None:
    (world.tau).mkdir(exist_ok=True)
    (world.tau / "hooks.json").write_text(json.dumps({"hooks": events}), encoding="utf-8")


def group(command: str, matcher: str | None = None, **handler: object) -> dict[str, object]:
    made: dict[str, object] = {"hooks": [{"type": "command", "command": command, **handler}]}
    if matcher is not None:
        made["matcher"] = matcher
    return made


def call(id: str, tool: str, **args: object) -> Reply:
    return Reply([Part.Call(id, tool, json.dumps(args))], Stop.Tools)


async def session(
    tmp_path: Path, world: World, script_: list[Reply], tool: FakeTool | None = None
) -> tuple[Harness, FakeTool]:
    made = tool or FakeTool("bash", "output")
    h = await Harness.create(
        tmp_path,
        provider=FakeProvider(script_),
        tools=[made],
        extensions=[CommandHooks.of(loaded(world))],
    )
    return h, made


def result(h: Harness) -> Outcome:
    body = next(b for b in reversed(h.bodies()) if isinstance(b, Body.Results))
    ((_, outcome, _),) = body.outcomes
    return outcome


def errors(h: Harness) -> list[str]:
    return [n.text for n in h.notice_events() if n.level == "error"]


def warnings(h: Harness) -> list[str]:
    return [n.text for n in h.notice_events() if n.level == "warning"]


async def written(path: Path) -> str:
    """What a command wrote to `path`, once it has (a command runs while the test waits)."""
    async with asyncio.timeout(5):
        while not path.exists() or not path.read_text().strip():
            await asyncio.sleep(0.02)
    return path.read_text().strip()


def ended(pid: int) -> bool:
    """Whether process `pid` has exited (gone, or a zombie nobody has reaped yet)."""
    try:
        return "Z" in Path(f"/proc/{pid}/stat").read_text().split(")")[1][:3]
    except FileNotFoundError:
        return True


# -- PreToolUse


@scenario
async def test_exit_2_blocks_the_call_and_standard_error_is_the_reason(
    tmp_path: Path, world: World
) -> None:
    guard = script(world, "guard.sh", 'echo "no pushing to master" >&2; exit 2')
    hooks_file(world, PreToolUse=[group(guard, "bash")])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="git push"), say("ok")])
    await h.send("go")
    await h.settle()
    assert tool.calls == [], "the tool never ran"
    assert result(h) == Outcome.Err("no pushing to master")
    await h.close()


@scenario
async def test_the_command_is_given_the_event_as_json_on_standard_input(
    tmp_path: Path, world: World
) -> None:
    seen = world.home / "seen.json"
    spy = script(world, "spy.sh", f"cat > {shlex.quote(str(seen))}")
    hooks_file(world, PreToolUse=[group(spy)])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="ls -l"), say("ok")])
    await h.send("go")
    await h.settle()
    event = json.loads(seen.read_text())
    assert event["hook_event_name"] == "PreToolUse"
    assert event["tool_name"] == "bash"
    assert event["tool_input"] == {"command": "ls -l"}
    assert event["tool_use_id"] == "a"
    assert event["cwd"]
    await h.close()


@scenario
async def test_a_command_inherits_the_project_variables_and_runs_in_the_session_directory(
    tmp_path: Path, world: World
) -> None:
    out = world.home / "env.txt"
    spy = script(
        world,
        "env.sh",
        'echo "$CLAUDE_PROJECT_DIR|$TAU_PROJECT_DIR|$TAU_HOOK_EVENT|$(pwd)"'
        f" > {shlex.quote(str(out))}",
    )
    hooks_file(world, PreToolUse=[group(spy)])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    claude, own, event, cwd = out.read_text().strip().split("|")
    assert claude == own
    assert event == "PreToolUse"
    assert Path(cwd).resolve() == Path.cwd().resolve()
    await h.close()


@scenario
async def test_json_deny_with_a_reason_blocks(tmp_path: Path, world: World) -> None:
    deny = script(
        world,
        "deny.sh",
        """printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "deny", "permissionDecisionReason": "read-only today"}}'""",
    )
    hooks_file(world, PreToolUse=[group(deny, "bash")])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="rm x"), say("ok")])
    await h.send("go")
    await h.settle()
    assert tool.calls == []
    assert result(h) == Outcome.Err("read-only today")
    await h.close()


@scenario
async def test_json_allow_and_a_silent_exit_0_let_the_call_run(
    tmp_path: Path, world: World
) -> None:
    allow = script(
        world,
        "allow.sh",
        """printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "allow"}, "suppressOutput": true}'""",
    )
    quiet = script(world, "quiet.sh", "cat >/dev/null")
    hooks_file(world, PreToolUse=[group(allow), group(quiet)])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="ls"), say("ok")])
    await h.send("go")
    await h.settle()
    assert tool.calls == [{"command": "ls"}]
    assert errors(h) == []
    await h.close()


@scenario
async def test_updated_input_rewrites_the_arguments_the_tool_runs_with(
    tmp_path: Path, world: World
) -> None:
    rewrite = script(
        world,
        "rewrite.sh",
        """printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "updatedInput": {"command": "echo safe"}}}'""",
    )
    hooks_file(world, PreToolUse=[group(rewrite, "bash")])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="rm -rf /"), say("ok")])
    await h.send("go")
    await h.settle()
    assert tool.calls == [{"command": "echo safe"}]
    assert result(h) == Outcome.Ok(
        '[hooks rewrote the arguments: {"command": "echo safe"}]\noutput'
    )
    await h.close()


@scenario
async def test_a_rewritten_call_that_fails_says_what_ran_too(tmp_path: Path, world: World) -> None:
    rewrite = script(
        world,
        "rewrite.sh",
        """printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "updatedInput": {"command": "make"}}}'""",
    )
    hooks_file(world, PreToolUse=[group(rewrite)])
    boom = FakeTool("bash", Exception("no makefile"))
    h, _ = await session(tmp_path, world, [call("a", "bash", command="mk"), say("ok")], boom)
    await h.send("go")
    await h.settle()
    outcome = result(h)
    assert isinstance(outcome, Outcome.Err)
    assert outcome.error.startswith('[hooks rewrote the arguments: {"command": "make"}]\n')
    assert "no makefile" in outcome.error
    await h.close()


@scenario
async def test_the_commands_of_an_event_run_in_file_order_and_the_first_block_stops_the_rest(
    tmp_path: Path, world: World
) -> None:
    marker = world.home / "second-ran"
    first = script(world, "first.sh", "echo first says no >&2; exit 2")
    second = script(world, "second.sh", f"touch {shlex.quote(str(marker))}")
    hooks_file(world, PreToolUse=[group(first), group(second)])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    assert result(h) == Outcome.Err("first says no")
    assert not marker.exists()
    await h.close()


@scenario
async def test_a_later_command_sees_the_arguments_an_earlier_one_rewrote(
    tmp_path: Path, world: World
) -> None:
    seen = world.home / "second-saw.json"
    rewrite = script(
        world,
        "one.sh",
        """printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "updatedInput": {"command": "changed"}}}'""",
    )
    spy = script(world, "two.sh", f"cat > {shlex.quote(str(seen))}")
    hooks_file(world, PreToolUse=[group(rewrite), group(spy)])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="original"), say("ok")])
    await h.send("go")
    await h.settle()
    assert json.loads(seen.read_text())["tool_input"] == {"command": "changed"}
    await h.close()


ASK = """printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "ask", "permissionDecisionReason": "pushes upstream"}}'"""


@scenario
async def test_ask_puts_a_question_and_yes_runs_the_call(tmp_path: Path, world: World) -> None:
    hooks_file(world, PreToolUse=[group(script(world, "ask.sh", ASK), "bash")])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="git push"), say("ok")])
    await h.send("go")
    await h.settle()
    (question,) = (await h.view()).questions
    assert question.extension == "hooks"
    assert question.text.splitlines()[0] == "bash: git push", "worded as permissions words it"
    assert "pushes upstream" in question.text
    assert question.schema == {"type": "string", "enum": ["yes", "no"]}
    assert tool.calls == []
    await h.respond("yes")
    await h.settle()
    assert tool.calls == [{"command": "git push"}]
    await h.close()


@scenario
async def test_ask_and_no_refuses_the_call(tmp_path: Path, world: World) -> None:
    hooks_file(world, PreToolUse=[group(script(world, "ask.sh", ASK), "bash")])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="git push"), say("ok")])
    await h.send("go")
    await h.settle()
    await h.respond("no")
    await h.settle()
    assert tool.calls == []
    assert result(h) == Outcome.Err("the user refused this call")
    await h.close()


# -- matchers


@scenario
@pytest.mark.parametrize(
    ("matcher", "runs"),
    [
        (None, True),
        ("", True),
        ("*", True),
        ("bash", True),
        ("Bash", True),  # Claude Code's name for the tool
        ("write|bash", True),
        ("Write, Bash", True),
        ("^ba.h$", True),
        ("^(read|write)$", False),
        ("write", False),
        ("Edit|Write", False),
        ("bas", False),  # a list is of exact names
    ],
)
async def test_a_matcher_names_the_tools_a_rule_applies_to(
    tmp_path: Path, world: World, matcher: str | None, runs: bool
) -> None:
    marker = world.home / "ran"
    touch = script(world, "touch.sh", f"touch {shlex.quote(str(marker))}")
    hooks_file(world, PreToolUse=[group(touch, matcher)])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    assert marker.exists() is runs
    await h.close()


@scenario
async def test_a_matcher_that_can_never_match_is_warned_of_once(
    tmp_path: Path, world: World
) -> None:
    hooks_file(
        world,
        PreToolUse=[group("true", "MultiEdit|Bash"), group("true", "Bash(git *)")],
        PostToolUse=[group("true", "mcp__server__tool")],  # an MCP server may list it later
    )
    h, _ = await session(tmp_path, world, [say("one"), say("two")])
    await h.send("go")
    await h.settle()
    await h.send("again")
    await h.settle()
    assert [w.split(" in ")[0] for w in warnings(h)] == [
        "PreToolUse matcher 'Bash|MultiEdit'",
        "PreToolUse matcher 'Bash(git *)'",
    ]
    assert "can never match MultiEdit" in warnings(h)[0]
    await h.close()


# -- failures


@scenario
async def test_a_timeout_kills_the_command_names_it_and_blocks_the_call(
    tmp_path: Path, world: World
) -> None:
    slow = script(world, "slow.sh", "sleep 30")
    hooks_file(world, PreToolUse=[group(slow, "bash", timeout=0.3)])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    assert tool.calls == []
    outcome = result(h)
    assert isinstance(outcome, Outcome.Err)
    assert "slow.sh" in outcome.error
    assert "timed out after 0.3 s" in outcome.error
    (error,) = errors(h)
    assert "slow.sh" in error
    assert {n.extension for n in h.notice_events()} == {"hooks"}
    await h.close()


@scenario
async def test_on_failure_continue_reports_the_failure_and_runs_the_call(
    tmp_path: Path, world: World
) -> None:
    slow = script(world, "slow.sh", "sleep 30")
    hooks_file(world, PreToolUse=[group(slow, "bash", timeout=0.3, onFailure="continue")])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    assert tool.calls == [{"command": "x"}]
    assert "timed out" in errors(h)[0]
    await h.close()


@scenario
async def test_a_timeout_kills_what_the_command_started(tmp_path: Path, world: World) -> None:
    pid = world.home / "child.pid"
    slow = script(world, "slow.sh", f"sleep 30 &\necho $! > {shlex.quote(str(pid))}\nwait")
    hooks_file(world, PreToolUse=[group(slow, timeout=0.5)])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    child = int(pid.read_text())
    proc = Path(f"/proc/{child}")
    assert not proc.exists() or "Z" in (proc / "stat").read_text().split(")")[1][:3]
    await h.close()


@scenario
async def test_an_exit_other_than_0_and_2_is_a_failure_naming_the_command(
    tmp_path: Path, world: World
) -> None:
    broken = script(world, "broken.sh", "echo boom >&2; exit 1")
    hooks_file(world, PreToolUse=[group(broken)])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    assert tool.calls == []
    outcome = result(h)
    assert isinstance(outcome, Outcome.Err)
    assert "broken.sh" in outcome.error
    assert "exited 1: boom" in outcome.error
    assert any("broken.sh" in e for e in errors(h))
    await h.close()


@scenario
async def test_a_command_that_does_not_start_is_a_failure_naming_it(
    tmp_path: Path, world: World
) -> None:
    hooks_file(
        world,
        PreToolUse=[
            {"hooks": [{"type": "command", "command": "/nonexistent/hook", "args": ["a"]}]}
        ],
    )
    h, tool = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    assert tool.calls == []
    assert "/nonexistent/hook" in errors(h)[0]
    assert "did not start" in errors(h)[0]
    await h.close()


@scenario
async def test_json_that_does_not_parse_or_that_is_not_an_object_fails(
    tmp_path: Path, world: World
) -> None:
    bad = script(world, "bad.sh", "echo '{not json'")
    hooks_file(world, PreToolUse=[group(bad)])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    assert tool.calls == []
    assert "JSON that does not parse" in errors(h)[0]
    await h.close()


@scenario
@pytest.mark.parametrize(
    ("output", "named"),
    [
        ('{"continue": false, "stopReason": "x"}', "continue"),
        ('{"decision": "approve", "reason": "r", "unknownField": 1}', "unknownField"),
        (
            '{"hookSpecificOutput": {"hookEventName": "PreToolUse",'
            ' "permissionDecision": "defer"}}',
            "defer",
        ),
        (
            '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "x"}}',
            "additionalContext",
        ),
        ('{"hookSpecificOutput": {"hookEventName": "Stop"}}', "hookSpecificOutput for 'Stop'"),
    ],
)
async def test_a_decision_field_tau_does_not_support_fails_and_names_it(
    tmp_path: Path, world: World, output: str, named: str
) -> None:
    hooks_file(world, PreToolUse=[group(script(world, "odd.sh", f"echo '{output}'"))])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    assert tool.calls == [], "an answer τ cannot honor never lets the call through"
    assert named in errors(h)[0]
    assert "odd.sh" in errors(h)[0]
    await h.close()


@scenario
async def test_a_child_left_in_the_background_does_not_hold_the_call(
    tmp_path: Path, world: World
) -> None:
    """Once the command exits, τ goes on: the child keeps running, as `&` asked, and holds
    nothing up, though it has the command's standard output and error."""
    pid = world.home / "child.pid"
    notify = script(
        world, "notify.sh", f"(sleep 20; echo late) &\necho $! > {shlex.quote(str(pid))}"
    )
    hooks_file(world, PreToolUse=[group(notify, timeout=5)])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    async with asyncio.timeout(2):
        await h.settle()
    assert tool.calls == [{"command": "x"}]
    assert errors(h) == []
    child = int(await written(pid))
    assert not ended(child), "a hook's background child is left alone"
    os.killpg(os.getpgid(child), signal.SIGKILL)
    await h.close()


@scenario
async def test_stopping_the_session_ends_a_running_command_and_what_it_started(
    tmp_path: Path, world: World
) -> None:
    pid = world.home / "child.pid"
    slow = script(world, "slow.sh", f"sleep 30 &\necho $! > {shlex.quote(str(pid))}\nwait")
    hooks_file(world, PreToolUse=[group(slow)])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    child = int(await written(pid))
    async with asyncio.timeout(5):
        await h.driver.stop()
    assert ended(child)
    assert tool.calls == []


@scenario
@pytest.mark.xfail(
    strict=True,
    reason="an abort never cancels a hook (model.md section 8, Q34), and a hook has no way to "
    "learn of one: ending its command needs the driver (held for John's ruling)",
)
async def test_an_abort_ends_a_running_command_and_what_it_started(
    tmp_path: Path, world: World
) -> None:
    pid = world.home / "child.pid"
    slow = script(world, "slow.sh", f"sleep 30 &\necho $! > {shlex.quote(str(pid))}\nwait")
    hooks_file(world, PreToolUse=[group(slow, timeout=4)])
    h, tool = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    child = int(await written(pid))
    await h.abort()
    try:
        async with asyncio.timeout(1.5):
            await h.settle()
        assert ended(child)
        assert tool.calls == []
    finally:
        await h.settle()
        await h.close()


# -- PostToolUse and PostToolUseFailure


@scenario
async def test_post_tool_use_gets_the_result_and_its_feedback_reaches_the_model(
    tmp_path: Path, world: World
) -> None:
    seen = world.home / "post.json"
    lint = script(
        world, "lint.sh", f"cat > {shlex.quote(str(seen))}; echo 'lint: 2 warnings' >&2; exit 2"
    )
    hooks_file(world, PostToolUse=[group(lint, "bash")])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="make"), say("ok")])
    await h.send("go")
    await h.settle()
    event = json.loads(seen.read_text())
    assert event["hook_event_name"] == "PostToolUse"
    assert event["tool_response"] == "output"
    assert event["tool_input"] == {"command": "make"}
    assert result(h) == Outcome.Ok("output\n\nhook feedback: lint: 2 warnings")
    await h.close()


@scenario
async def test_feedback_after_a_result_that_ends_in_a_newline_has_one_blank_line_before_it(
    tmp_path: Path, world: World
) -> None:
    lint = script(world, "lint.sh", "echo 'lint: 2 warnings' >&2; exit 2")
    hooks_file(world, PostToolUse=[group(lint)])
    tool = FakeTool("bash", "output\n")
    h, _ = await session(tmp_path, world, [call("a", "bash", command="make"), say("ok")], tool)
    await h.send("go")
    await h.settle()
    assert result(h) == Outcome.Ok("output\n\nhook feedback: lint: 2 warnings")
    await h.close()


@scenario
async def test_a_call_id_repeated_in_a_later_reply_is_its_own_call(
    tmp_path: Path, world: World
) -> None:
    """Call ids repeat from one reply to the next: a refusal of the first call `a` says nothing
    of the second, whose result the `PostToolUse` command sees."""
    seen = world.home / "post.json"
    refuse_rm = script(world, "refuse.sh", 'grep -q rm && { echo "no rm" >&2; exit 2; }; exit 0')
    spy = script(world, "spy.sh", f"cat > {shlex.quote(str(seen))}")
    hooks_file(world, PreToolUse=[group(refuse_rm)], PostToolUse=[group(spy)])
    replies = [call("a", "bash", command="rm x"), call("a", "bash", command="ls"), say("ok")]
    h, tool = await session(tmp_path, world, replies)
    await h.send("go")
    await h.settle()
    assert tool.calls == [{"command": "ls"}]
    assert json.loads(seen.read_text())["tool_input"] == {"command": "ls"}
    await h.close()


@scenario
async def test_post_tool_use_json_decision_block_and_context_are_feedback(
    tmp_path: Path, world: World
) -> None:
    both = script(
        world,
        "both.sh",
        """printf '%s' '{"decision": "block", "reason": "tests fail", "hookSpecificOutput":
        {"hookEventName": "PostToolUse", "additionalContext": "see log"}}'""",
    )
    hooks_file(world, PostToolUse=[group(both)])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="make"), say("ok")])
    await h.send("go")
    await h.settle()
    assert result(h) == Outcome.Ok("output\n\nhook feedback: tests fail\n\nhook feedback: see log")
    await h.close()


@scenario
async def test_updated_tool_output_replaces_the_result_text(tmp_path: Path, world: World) -> None:
    redact = script(
        world,
        "redact.sh",
        """printf '%s' '{"hookSpecificOutput": {"hookEventName": "PostToolUse",
        "updatedToolOutput": "[redacted]"}}'""",
    )
    hooks_file(world, PostToolUse=[group(redact)])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="env"), say("ok")])
    await h.send("go")
    await h.settle()
    assert result(h) == Outcome.Ok("[redacted]")
    await h.close()


@scenario
async def test_post_tool_use_failure_runs_for_an_error_and_post_tool_use_does_not(
    tmp_path: Path, world: World
) -> None:
    ok = world.home / "post-ok"
    failed = world.home / "post-failure"
    after_ok = script(world, "ok.sh", f"touch {shlex.quote(str(ok))}")
    after_failed = script(
        world, "failed.sh", f"cat > {shlex.quote(str(failed))}; echo 'was it flaky?' >&2; exit 2"
    )
    hooks_file(world, PostToolUse=[group(after_ok)], PostToolUseFailure=[group(after_failed)])
    boom = FakeTool("bash", Exception("disk full"))
    h, _ = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")], boom)
    await h.send("go")
    await h.settle()
    assert not ok.exists()
    event = json.loads(failed.read_text())
    assert event["hook_event_name"] == "PostToolUseFailure"
    assert "disk full" in event["error"]
    outcome = result(h)
    assert isinstance(outcome, Outcome.Err)
    assert outcome.error.endswith("hook feedback: was it flaky?")
    await h.close()


@scenario
async def test_a_call_a_hook_refused_is_not_reported_as_a_result(
    tmp_path: Path, world: World
) -> None:
    marker = world.home / "post-ran"
    refuse = script(world, "refuse.sh", "echo no >&2; exit 2")
    after = script(world, "after.sh", f"touch {shlex.quote(str(marker))}")
    hooks_file(world, PreToolUse=[group(refuse)], PostToolUseFailure=[group(after)])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="x"), say("ok")])
    await h.send("go")
    await h.settle()
    assert result(h) == Outcome.Err("no")
    assert not marker.exists()
    await h.close()


# -- UserPromptSubmit


@scenario
async def test_exit_2_refuses_a_prompt_with_the_reason_in_a_notice(
    tmp_path: Path, world: World
) -> None:
    seen = world.home / "prompt.json"
    gate = script(
        world, "gate.sh", f"cat > {shlex.quote(str(seen))}; echo 'no secrets' >&2; exit 2"
    )
    hooks_file(world, UserPromptSubmit=[group(gate)])
    h, _ = await session(tmp_path, world, [say("never")])
    await h.send("my password is hunter2")
    await h.settle()
    assert json.loads(seen.read_text())["prompt"] == "my password is hunter2"
    assert not any(isinstance(b, Body.User) for b in h.bodies()), "nothing was written"
    notice = next(n for n in h.notice_events() if "no secrets" in n.text)
    assert notice.extension == "hooks"
    assert h.provider.requests == []
    await h.close()


@scenario
async def test_plain_standard_output_and_additional_context_are_added_to_the_prompt(
    tmp_path: Path, world: World
) -> None:
    plain = script(world, "plain.sh", "cat >/dev/null; echo 'branch: feature/x'")
    json_ = script(
        world,
        "json.sh",
        """cat >/dev/null; printf '%s' '{"hookSpecificOutput": {"hookEventName":
        "UserPromptSubmit", "additionalContext": "tests: 41 pass"}}'""",
    )
    hooks_file(world, UserPromptSubmit=[group(plain), group(json_)])
    h, _ = await session(tmp_path, world, [say("ok")])
    await h.send("fix it")
    await h.settle()
    user = next(b for b in h.bodies() if isinstance(b, Body.User))
    assert user.text == "fix it\n\nbranch: feature/x\n\ntests: 41 pass"
    await h.close()


# -- Stop


@scenario
async def test_a_stop_hook_that_exits_2_keeps_the_turn_going_with_its_reason(
    tmp_path: Path, world: World
) -> None:
    runs = world.home / "stops"
    check = script(
        world,
        "check.sh",
        f"""cat > {shlex.quote(str(world.home / "last-stop.json"))}
        echo x >> {shlex.quote(str(runs))}
        if [ "$(wc -l < {shlex.quote(str(runs))})" -lt 2 ]
        then echo "run the tests first" >&2; exit 2; fi""",
    )
    hooks_file(world, Stop=[group(check)])
    h, _ = await session(tmp_path, world, [say("done"), say("really done")])
    await h.send("go")
    await h.settle()
    users = [n for n in h.nodes() if isinstance(n.body, Body.User)]
    assert [getattr(n.body, "text", None) for n in users] == [
        "go",
        "Stop hook feedback: run the tests first",
    ]
    assert users[1].by == Author.Extension("hooks")
    assert len(h.provider.requests) == 2
    last = json.loads((world.home / "last-stop.json").read_text())
    assert last["hook_event_name"] == "Stop"
    assert last["stop_hook_active"] is True, "the second stop is one a hook already held open"
    assert last["last_assistant_message"] == "really done"
    await h.close()


@scenario
async def test_a_stop_hook_cannot_keep_a_turn_going_forever(tmp_path: Path, world: World) -> None:
    always = script(world, "always.sh", "echo again >&2; exit 2")
    hooks_file(world, Stop=[group(always)])
    h, _ = await session(tmp_path, world, [say(f"r{i}") for i in range(20)])
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 9, "the person's turn, and 8 held open"
    warnings = [n for n in h.notice_events() if n.level == "warning"]
    assert "8 times in a row" in warnings[-1].text
    await h.close()


@scenario
async def test_a_stop_hook_does_not_run_at_a_sub_agents_end_or_a_tool_batch(
    tmp_path: Path, world: World
) -> None:
    marker = world.home / "stopped"
    touch = script(world, "touch.sh", f"echo x >> {shlex.quote(str(marker))}")
    hooks_file(world, Stop=[group(touch)])
    h, _ = await session(tmp_path, world, [call("a", "bash", command="ls"), say("done")])
    await h.send("go")
    await h.settle()
    assert marker.read_text() == "x\n", "once: at the reply that asked for no tool"
    await h.close()


@scenario
async def test_a_stop_hook_that_fails_is_reported_and_the_turn_ends(
    tmp_path: Path, world: World
) -> None:
    hooks_file(world, Stop=[group(script(world, "broken.sh", "exit 7"))])
    h, _ = await session(tmp_path, world, [say("done"), say("not asked")])
    await h.send("go")
    await h.settle()
    assert len(h.provider.requests) == 1
    assert "broken.sh" in errors(h)[0]
    await h.close()


# -- SessionStart and SessionEnd


@scenario
async def test_session_start_and_end_run_their_commands(tmp_path: Path, world: World) -> None:
    log = world.home / "session.log"
    start = script(
        world, "start.sh", f"cat >> {shlex.quote(str(log))}; echo >> {shlex.quote(str(log))}"
    )
    hooks_file(world, SessionStart=[group(start)], SessionEnd=[group(start)])
    h, _ = await session(tmp_path, world, [])
    started = [json.loads(line) for line in log.read_text().splitlines() if line]
    assert [e["hook_event_name"] for e in started] == ["SessionStart"]
    assert started[0]["source"] == "startup"
    await h.driver.stop()
    ended = [json.loads(line) for line in log.read_text().splitlines() if line]
    assert [e["hook_event_name"] for e in ended] == ["SessionStart", "SessionEnd"]


@scenario
async def test_a_session_event_that_fails_or_writes_text_is_told_on_standard_error(
    tmp_path: Path, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    hooks_file(
        world,
        SessionStart=[
            group(script(world, "ctx.sh", "echo welcome")),
            group(script(world, "broken.sh", "exit 3")),
        ],
    )
    h, _ = await session(tmp_path, world, [])
    err = capsys.readouterr().err
    assert "ctx.sh" in err
    assert "dropped" in err
    assert "broken.sh" in err
    assert "exited 3" in err
    await h.close()


# -- the listing


@scenario
async def test_the_hooks_command_lists_what_is_configured(tmp_path: Path, world: World) -> None:
    hooks_file(
        world,
        PreToolUse=[group("./guard.sh", "bash|write", timeout=5)],
        Stop=[group("./done.sh")],
    )
    h, _ = await session(tmp_path, world, [])
    listing = await h.run("hooks")
    text = str(listing)
    assert "PreToolUse bash|write: ./guard.sh  (5 s, on failure block;" in text
    assert "Stop: ./done.sh  (60 s" in text
    await h.close()
