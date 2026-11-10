"""`tau serve` (D21 section 9) as a person or a program uses it: the foreground server, `-d` and its
record, `--stop`, `--schema`. The servers are real processes with a mock model, started in a
throwaway TAU_HOME under a short directory and killed when the test is over."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Windows: tau serve does not run there (a Unix domain socket, D21; not ported)",
        allow_module_level=True,
    )

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from proctable import alive
from world import Result, World, tau

from tau import (
    PROTOCOL,
    CursorView,
    Event,
    RemoteClient,
    Session,
    list_sessions,
)

MOCK = {"default_model": "m", "models": {"m": {"model": "m", "provider": "mock"}}}
OLD = "20261030-091502-1c2d3e"
REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "docs" / "protocol.schema.json"


@pytest.fixture
def home(served: World) -> World:
    served.user_config(MOCK)
    return served


def work(world: World) -> Path:
    return world.home / "work"


def serve(world: World, *argv: str) -> Result:
    return tau(world, "serve", *argv, cwd=work(world))


def record_of(result: Result) -> dict[str, object]:
    """The one JSON object `--json` printed."""
    (line,) = result.out.splitlines()
    return json.loads(line)


def existing(world: World) -> Path:
    """A session in the project, by hand: it is not deleted when its server stops unused."""
    return world.session(OLD, cwd=work(world))


def wait_gone(pid: int, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while alive(pid):
        assert time.monotonic() < deadline, f"process {pid} is still running"
        time.sleep(0.02)


async def welcome_of(record: dict[str, object]) -> object:
    async with await RemoteClient.connect(str(record["socket"]), "test:probe") as client:
        return client.welcome


# --schema


def test_the_schema_is_what_the_checked_in_file_holds(home: World) -> None:
    result = serve(home, "--schema")
    assert result.code == 0
    assert result.out == SCHEMA.read_text(encoding="utf-8")
    assert json.loads(result.out)["x-protocol"] == PROTOCOL
    with pytest.raises(SystemExit) as usage:
        serve(home, "--schema", "-d")
    assert usage.value.code == 2, "one thing at a time"


# -d


def test_d_starts_a_server_and_prints_its_record(home: World) -> None:
    result = serve(home, "-d", "--json", "--session", existing(home).name)
    assert result.code == 0, result.err
    record = record_of(result)
    assert record["started"] is True
    assert record["session"] == OLD
    assert record["cwd"] == str(work(home).resolve())
    assert record["protocol"] == PROTOCOL
    assert isinstance(record["pid"], int)
    serve_dir = home.tau / "serve"
    assert (serve_dir.stat().st_mode & 0o777) == 0o700
    assert record["socket"] == str(serve_dir / f"{OLD}.sock")
    assert record["log"] == str(serve_dir / f"{OLD}.log")
    assert json.loads((serve_dir / f"{OLD}.json").read_text(encoding="utf-8")) == {
        k: v for k, v in record.items() if k != "started"
    }, "the record file is the same object, without `started`"
    welcome = asyncio.run(welcome_of(record))
    assert (welcome.pid, welcome.session) == (record["pid"], OLD)  # type: ignore[attr-defined]


def test_d_prints_a_human_line_without_json(home: World) -> None:
    result = serve(home, "-d", "--session", existing(home).name)
    assert result.code == 0, result.err
    assert result.out.startswith(f"started {OLD} on ")
    again = serve(home, "-d", "--session", OLD)
    assert again.out.startswith(f"already serving {OLD} on ")


def test_d_again_prints_the_same_record_and_starts_nothing(home: World) -> None:
    first = record_of(serve(home, "-d", "--json", "--session", existing(home).name))
    second = record_of(serve(home, "-d", "--json", "--session", OLD))
    assert second["started"] is False
    assert {k: v for k, v in second.items() if k != "started"} == {
        k: v for k, v in first.items() if k != "started"
    }
    assert [s.id for s in list_sessions(home.tau / "sessions")] == [OLD]


def test_a_new_session_each_time_when_none_is_named(home: World) -> None:
    one = record_of(serve(home, "-d", "--json"))
    two = record_of(serve(home, "-d", "--json"))
    assert (one["started"], two["started"]) == (True, True)
    assert one["session"] != two["session"]
    assert one["pid"] != two["pid"]


def test_two_d_at_once_start_one_server_and_both_print_it(home: World) -> None:
    existing(home)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: serve(home, "-d", "--json", "--session", OLD), range(2)))
    assert [r.code for r in results] == [0, 0], [(r.out, r.err) for r in results]
    records = [record_of(r) for r in results]
    assert records[0]["pid"] == records[1]["pid"], "the loser prints the winner's record"
    assert sorted(bool(r["started"]) for r in records) == [False, True]


def test_a_record_whose_socket_does_not_answer_is_stale_and_replaced(home: World) -> None:
    dead = record_of(serve(home, "-d", "--json", "--session", existing(home).name))
    pid = dead["pid"]
    assert isinstance(pid, int)
    os.kill(pid, signal.SIGKILL)  # a crash: no cleanup, the record stays
    wait_gone(pid)
    assert (home.tau / "serve" / f"{OLD}.json").exists()
    fresh = record_of(serve(home, "-d", "--json", "--session", OLD))
    assert fresh["started"] is True
    assert fresh["pid"] != pid


def test_a_session_held_by_a_process_that_is_no_server_fails_leased_after_the_bound(
    home: World,
) -> None:
    path = existing(home)
    held = Session.load(path)  # a lease, and no socket
    try:
        began = time.monotonic()
        result = serve(home, "-d", "--json", "--session", OLD)
        waited = time.monotonic() - began
    finally:
        held.close()
    assert result.code == 1
    error = record_of(result)["error"]
    assert isinstance(error, dict)
    assert error["type"] == "Leased"
    assert OLD in error["message"]
    assert f"tau --fork {OLD}" in error["message"]
    assert 4.5 < waited < 10, "bounded, not infinite and not instant"
    assert not (home.tau / "serve" / f"{OLD}.json").exists()


def test_a_start_that_fails_prints_its_message_intact_and_exits_1(home: World) -> None:
    (work(home) / "AGENTS.md").write_bytes(b"\xff\xfe not utf-8")
    result = serve(home, "-d", "--json")
    assert result.code == 1
    error = record_of(result)["error"]
    assert isinstance(error, dict)
    assert error["type"] == "ContextFileError"
    assert "--no-context-files" in error["message"]
    plain = serve(home, "-d")
    assert plain.code == 1
    assert plain.out == ""
    assert plain.err.startswith("tau: ")
    assert "--no-context-files" in plain.err
    assert not list((home.tau / "serve").glob(".starting-*")), "the pending log is cleaned"


def test_an_untrusted_project_is_refused_by_the_parent_before_any_child_starts(
    home: World,
) -> None:
    project = work(home)
    (project / ".tau").mkdir()
    (project / ".tau" / "config.json").write_text("{}", encoding="utf-8")
    result = serve(home, "-d", "--json")
    assert result.code == 1
    error = record_of(result)["error"]
    assert isinstance(error, dict)
    assert error["type"] == "UntrustedProject"
    assert not list((home.tau / "serve").glob("*")), "nothing was started"


def test_a_session_that_is_kept_in_memory_has_no_id_to_serve_under(home: World) -> None:
    for argv in (["-d", "--no-session"], ["--no-session"]):
        result = serve(home, *argv)
        assert result.code == 1
        assert "--no-session" in result.err


def test_a_socket_path_that_is_too_long_fails_naming_socket(home: World) -> None:
    long = home.home / ("deep" * 25)
    long.mkdir()
    deep = World(long, long / ".tau", {"HOME": str(long)})
    deep.user_config(MOCK)
    (long / "work").mkdir()
    result = tau(deep, "serve", "-d", cwd=long / "work")
    assert result.code == 1
    assert "--socket" in result.err
    assert not list((deep.tau / "sessions").glob("*")), "no session was left behind"


def test_the_serve_directory_must_be_private(home: World) -> None:
    (home.tau / "serve").mkdir()
    (home.tau / "serve").chmod(0o777)
    result = serve(home, "-d")
    assert result.code == 1
    assert "writable by group or others" in result.err


# --stop


def test_stop_ends_the_server_and_removes_its_files(home: World) -> None:
    record = record_of(serve(home, "-d", "--json", "--session", existing(home).name))
    pid = record["pid"]
    assert isinstance(pid, int)
    result = serve(home, "--stop", "--json", "--session", OLD)
    assert result.code == 0, result.err
    assert record_of(result) == {"session": OLD, "pid": pid, "stopped": True}
    wait_gone(pid)
    assert list((home.tau / "serve").iterdir()) == [], "a clean exit leaves nothing, log included"
    assert [s.id for s in list_sessions(home.tau / "sessions")] == [OLD], "the session is kept"
    again = serve(home, "--stop", "--json", "--session", OLD)
    assert record_of(again) == {"session": OLD, "pid": None, "stopped": False}
    assert serve(home, "--stop", "--session", OLD).out == f"no server is serving {OLD}\n"


class Hanging(BaseHTTPRequestHandler):
    """The parent calls `agent("sub")`; the agent's request never answers until `release` is set."""

    reached = threading.Event()
    release = threading.Event()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["messages"][-1]["content"] == "sub":
            Hanging.reached.set()
            Hanging.release.wait(30)
            return
        call = {
            "index": 0,
            "id": "c0",
            "type": "function",
            "function": {"name": "agent", "arguments": json.dumps({"task": "sub"})},
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {
            "id": "x",
            "object": "chat.completion.chunk",
            "model": "f",
            "choices": [
                {"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}
            ],
        }
        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


def test_stop_aborts_what_is_running_and_closes_the_agents_first(home: World) -> None:
    """The server is stopped while an agent works: the agent's cursor is closed in the session,
    as an interrupted `-p` leaves it, so a later `to="agent:1"` resumes it instead of being told it
    is still working."""
    Hanging.reached.clear()
    Hanging.release.clear()
    model = ThreadingHTTPServer(("127.0.0.1", 0), Hanging)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{model.server_port}/v1"
    config = {"default_model": "m", "models": {"m": {"model": "m", "base_url": url}}}
    home.user_config({**config, "load": {"agents": True}})
    record = record_of(serve(home, "-d", "--json", "--session", existing(home).name))
    pid = record["pid"]
    assert isinstance(pid, int)

    async def start_work() -> None:
        async with await RemoteClient.connect(str(record["socket"]), "test:starter") as client:
            await client.send(0, "go")
            deadline = time.monotonic() + 20
            while not Hanging.reached.is_set():
                assert time.monotonic() < deadline, "the agent never started"
                await asyncio.sleep(0.02)

    try:
        asyncio.run(start_work())
        assert serve(home, "--stop", "--json", "--session", OLD).code == 0
        wait_gone(pid)
    finally:
        Hanging.release.set()
        model.shutdown()
        model.server_close()
    session = Session.load(home.tau / "sessions" / OLD)
    try:
        child = session.label("agent:1")
        assert child not in session.cursors(), "the agent's cursor is closed"
        assert dict(session.closed()).keys() == {child}
    finally:
        session.close()


def test_stop_cleans_up_a_stale_record_and_the_session_serves_again(home: World) -> None:
    record = record_of(serve(home, "-d", "--json", "--session", existing(home).name))
    pid = record["pid"]
    assert isinstance(pid, int)
    os.kill(pid, signal.SIGKILL)
    wait_gone(pid)
    result = serve(home, "--stop", "--json", "--session", OLD)
    assert record_of(result) == {"session": OLD, "pid": None, "stopped": False}
    assert not (home.tau / "serve" / f"{OLD}.json").exists()


def test_stop_names_a_session(home: World) -> None:
    result = serve(home, "--stop")
    assert result.code == 1
    assert "--session" in result.err


def test_stopping_a_server_tells_its_clients_end(home: World) -> None:
    record = record_of(serve(home, "-d", "--json", "--session", existing(home).name))

    async def watch() -> list[Event]:
        async with await RemoteClient.connect(str(record["socket"]), "test:watcher") as client:
            first = await asyncio.wait_for(anext(client), 5)
            assert isinstance(first, CursorView)
            stopper = asyncio.get_running_loop().run_in_executor(
                None, lambda: serve(home, "--stop", "--session", OLD)
            )
            rest = [e async for e in client]  # the stream ends: the driver stopped
            assert (await stopper).code == 0
            with pytest.raises(ConnectionError, match="the driver is stopped"):
                await client.node(0)
            return rest

    asyncio.run(watch())


# The server in the foreground


def test_verbose_reports_the_start_on_stderr_and_set_is_checked(home: World) -> None:
    existing(home)
    bad = serve(home, "--set", "step_limit.limit=0", "--session", OLD)
    assert bad.code == 1
    assert "at least 1" in bad.err
    server = start_foreground(
        home, "--json", "--verbose", "--set", "step_limit.limit=3", "--session", OLD
    )
    try:
        assert server.stdout is not None
        json.loads(server.stdout.readline())
        server.send_signal(signal.SIGTERM)
        assert server.wait(timeout=10) == 0
        assert server.stderr is not None
        report = server.stderr.read()
    finally:
        server.kill()
        server.communicate()
    assert f"tau: session {OLD} (opened)" in report
    assert "tau: extension step_limit" in report


def test_a_server_starts_with_session_settings_and_says_what_the_config_lacks(
    home: World,
) -> None:
    existing(home)
    home.environ["TAU_SETTINGS"] = json.dumps({"load": {"retry": False}})
    config = json.loads((home.tau / "config.json").read_text(encoding="utf-8"))
    home.user_config({**config, "load": {"deleted": True}})
    server = start_foreground(home, "--json", "--verbose", "--session", OLD)
    try:
        assert server.stdout is not None
        json.loads(server.stdout.readline())
        server.send_signal(signal.SIGTERM)
        assert server.wait(timeout=10) == 0
        assert server.stderr is not None
        report = server.stderr.read()
    finally:
        server.kill()
    assert "tau: extension step_limit" in report
    assert "tau: retry (" not in report, "TAU_SETTINGS switched it off for this server"
    assert "warning: load.deleted: no extension source has the key 'deleted'" in report


def start_foreground(world: World, *argv: str) -> subprocess.Popen[str]:
    env = {**world.environ, "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen(
        [sys.executable, "-m", "tau", "serve", *argv],
        cwd=work(world),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGINT])
def test_a_foreground_server_serves_until_signalled_then_cleans_up_and_exits_0(
    home: World, stop: signal.Signals
) -> None:
    existing(home)
    server = start_foreground(home, "--json", "--session", OLD)
    try:
        assert server.stdout is not None
        record = json.loads(server.stdout.readline())
        assert record["pid"] == server.pid
        assert record["log"] is None, "a foreground server has no log"
        assert (home.tau / "serve" / f"{OLD}.json").exists()
        welcome = asyncio.run(welcome_of(record))
        assert welcome.pid == server.pid  # type: ignore[attr-defined]
        server.send_signal(stop)
        assert server.wait(timeout=10) == 0
    finally:
        server.kill()
        server.communicate()
    assert list((home.tau / "serve").iterdir()) == [], "the socket and the record are gone"


def test_a_foreground_server_names_itself_without_json(home: World) -> None:
    existing(home)
    server = start_foreground(home, "--session", OLD)
    try:
        assert server.stdout is not None
        line = server.stdout.readline()
        assert line.startswith(f"serving {OLD} on {home.tau / 'serve' / OLD}")
        server.terminate()
        assert server.wait(timeout=10) == 0
    finally:
        server.kill()
        server.communicate()


def test_a_second_server_for_a_held_session_fails_with_leased(home: World) -> None:
    existing(home)
    first = start_foreground(home, "--json", "--session", OLD)
    try:
        assert first.stdout is not None
        first.stdout.readline()
        second = serve(home, "--session", OLD)
        assert second.code == 1
        assert "in use by another process" in second.err
        assert f"tau --fork {OLD}" in second.err
    finally:
        first.terminate()
        first.communicate(timeout=10)


def test_a_server_that_is_slow_to_answer_is_not_taken_for_dead(home: World) -> None:
    """A live server whose loop is busy has a socket that accepts and does not answer `hello`:
    `--stop` says so and keeps its record."""
    import socket as sockets  # noqa: PLC0415 - only this test makes a socket by hand

    serve_dir = home.tau / "serve"
    serve_dir.mkdir(mode=0o700)
    where = serve_dir / f"{OLD}.sock"
    existing(home)
    listener = sockets.socket(sockets.AF_UNIX)
    listener.bind(str(where))
    listener.listen(8)
    record = {
        "socket": str(where),
        "pid": os.getpid(),
        "session": OLD,
        "cwd": str(work(home)),
        "protocol": PROTOCOL,
        "version": "x",
        "since": 0,
        "log": None,
    }
    path = serve_dir / f"{OLD}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    try:
        result = serve(home, "--stop", "--json", "--session", OLD)
    finally:
        listener.close()
    assert result.code == 1
    error = record_of(result)["error"]
    assert isinstance(error, dict)
    assert str(where) in error["message"]
    assert path.exists(), "a timeout is not proof of death"
