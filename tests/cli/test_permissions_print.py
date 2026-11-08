"""Permissions under `tau -p` (D24 section 7, D22 section 5): a model that calls `bash` once, a
rule that asks, and `--answer` or a decline. The model is an OpenAI-compatible endpoint."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
from world import Result, World, tau

COMMAND = "touch ran"

ASK = {"extensions": {"permissions": {"ask": ["bash", "read"]}}}


class Model(BaseHTTPRequestHandler):
    """Calls `bash` once, then says what the tool said. `told` collects the tool messages."""

    told: ClassVar[list[str]] = []
    run: ClassVar[str] = COMMAND
    tool: ClassVar[str] = "bash"
    args: ClassVar[dict[str, str] | None] = None

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        last = body["messages"][-1]
        if last["role"] == "tool":
            Model.told.append(last["content"])
            delta = {"role": "assistant", "content": "done"}
            finish = "stop"
        else:
            call = {
                "index": 0,
                "id": "c0",
                "type": "function",
                "function": {
                    "name": Model.tool,
                    "arguments": json.dumps(Model.args or {"command": Model.run}),
                },
            }
            delta, finish = {"tool_calls": [call]}, "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {
            "id": "x",
            "object": "chat.completion.chunk",
            "model": "f",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def model(world: World) -> Iterator[World]:
    Model.told, Model.run, Model.tool, Model.args = [], COMMAND, "bash", None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    config = {"default_model": "f", "models": {"f": {"model": "f", "base_url": url}}}
    world.user_config({**config, **ASK})
    try:
        yield world
    finally:
        server.shutdown()
        server.server_close()


def p(world: World, *argv: str) -> Result:
    return tau(world, "-p", "--no-context-files", *argv, "go")


def ran(world: World) -> bool:
    return (world.home / "ran").exists()


def test_no_rules_asks_nothing_and_the_call_runs(model: World) -> None:
    model.user_config(json.loads((model.tau / "config.json").read_text()) | {"extensions": {}})
    result = p(model)
    assert result.code == 0, result.err
    assert ran(model)
    assert "question" not in result.err


def test_without_an_answer_rule_the_question_is_declined_and_the_call_refused(model: World) -> None:
    result = p(model)
    assert result.code == 0, result.err
    assert not ran(model)
    assert 'tau: declined question 0 from permissions on cursor 0: "bash: touch ran' in result.err
    assert Model.told == ["Error: the user refused this call"], "the model read the refusal"
    assert result.out == "done\n"


def test_an_answer_rule_says_yes(model: World) -> None:
    result = p(model, "--answer", "permissions=yes")
    assert result.code == 0, result.err
    assert ran(model)
    assert "answered question 0 from permissions with yes" in result.err


def test_an_answer_rule_can_match_the_command(model: World) -> None:
    refuse = ["--answer", "permissions:bash: touch*=no", "--answer", "permissions=yes"]
    result = p(model, *refuse)
    assert result.code == 0, result.err
    assert not ran(model)
    assert Model.told == ["Error: the user refused this call"]


def test_always_saves_the_rule_to_the_users_file(model: World) -> None:
    result = p(model, "--answer", "permissions=always")
    assert result.code == 0, result.err
    assert ran(model)
    saved = json.loads((model.tau / "config.json").read_text(encoding="utf-8"))
    assert saved["extensions"]["permissions"]["allow"] == ["bash:touch ran"]
    (model.home / "ran").unlink()
    again = p(model)  # no --answer: nothing is asked, the saved rule allows it
    assert again.code == 0, again.err
    assert ran(model)
    assert "question" not in again.err


def test_an_answer_the_question_does_not_offer_is_exit_1(model: World) -> None:
    result = p(model, "--answer", "permissions=maybe")
    assert result.code == 1
    assert "--answer permissions=maybe" in result.err
    assert not ran(model)


def test_a_rule_naming_a_tool_nothing_provides_is_a_warning_at_the_start_and_the_check(
    model: World,
) -> None:
    config = json.loads((model.tau / "config.json").read_text())
    config["extensions"]["permissions"]["deny"] = ["Bash", "wirte:/etc/*", "write:/etc/*"]
    model.user_config(config)
    result = p(model)
    assert result.code == 0, result.err
    assert (
        "warning: extensions.permissions: deny rule 'Bash' names the tool 'Bash', which no "
        "loaded extension provides" in result.err
    )
    assert "deny rule 'wirte:/etc/*' names the tool 'wirte:/etc/*'" in result.err
    checked = tau(model, "config", "check")
    assert checked.code == 0
    assert "names the tool 'Bash'" in checked.both


def test_an_empty_pattern_stops_the_start(model: World) -> None:
    config = json.loads((model.tau / "config.json").read_text())
    config["extensions"]["permissions"]["ask"] = ["bash:"]
    model.user_config(config)
    result = p(model)
    assert result.code == 1
    assert "empty pattern" in result.err
    assert not ran(model)


def test_an_answer_text_never_matches_a_command_that_chains_unless_it_says_so(
    model: World,
) -> None:
    Model.run = "touch ran; touch other"
    plain = p(model, "--answer", "permissions:bash: touch*=yes")
    assert plain.code == 0, plain.err
    assert not ran(model), "the glob did not vouch for the chain: declined"
    assert "declined question 0" in plain.err
    chained = p(model, "--answer", "permissions:bash: touch ran; touch other=yes")
    assert chained.code == 0, chained.err
    assert ran(model)


def test_a_blanket_answer_still_answers_every_question(model: World) -> None:
    Model.run = "touch ran; touch other"
    result = p(model, "--answer", "permissions=yes")
    assert result.code == 0, result.err
    assert ran(model)


def test_an_answer_text_is_matched_against_the_call_not_the_rule_line(model: World) -> None:
    result = p(model, "--answer", "permissions:bash: touch ran=yes")
    assert result.code == 0, result.err
    assert ran(model), "no trailing * needed"


def test_an_answer_text_is_matched_against_the_resolved_path(model: World) -> None:
    """`/proj/*` spells the model's path, but the call lands on `/etc/shadow`."""
    Model.tool, Model.args = "read", {"path": "/proj/../etc/shadow"}
    result = p(model, "--answer", "permissions:read: /proj/*=yes")
    assert result.code == 0, result.err
    assert "declined question 0" in result.err
    assert Model.told == ["Error: the user refused this call"]
    allowed = p(model, "--answer", "permissions:read: /etc/*=yes")
    assert "answered question 0" in allowed.err


def test_an_answer_text_may_be_written_in_rule_syntax_without_the_space(model: World) -> None:
    result = p(model, "--answer", "permissions:bash:touch*=yes")
    assert result.code == 0, result.err
    assert ran(model)
    assert "answered question 0" in result.err


def test_an_answer_for_a_tool_that_is_no_command_ignores_the_shell_operators(
    model: World,
) -> None:
    """`agent`'s arguments are JSON: a `;` in its task is no chain."""
    config = json.loads((model.tau / "config.json").read_text())
    config["load"] = {"agents": True}
    config["extensions"] = {"permissions": {"ask": ["agent"]}}
    model.user_config(config)
    Model.tool, Model.args = "agent", {"task": "list files; then summarize"}
    result = p(model, "--answer", "permissions:agent:*=yes")
    assert "answered question 0 from permissions with yes" in result.err, result.err
