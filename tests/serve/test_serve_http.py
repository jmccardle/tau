"""`tau serve`'s HTTP edge (D29) as a person or a program uses it: both transports by default and
the flags that turn one off, the port rule (strict `--port N`, else 3393 then any), the serve
record's HTTP fields and its file, the banner and its paste URL, and `tau -p` attaching over HTTP.
The servers are real processes with a mock model, started in a throwaway TAU_HOME under a short
directory and killed when the test is over."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Windows: tau serve's HTTP-only default there is T2, not tested yet (D29)",
        allow_module_level=True,
    )

import asyncio
import contextlib
import json
import os
import pty
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

from world import Result, World, tau

from tau import RemoteClient

MOCK = {"default_model": "m", "models": {"m": {"model": "m", "provider": "mock"}}}
OLD = "20261030-091502-1c2d3e"
SOFT = 3393


@pytest.fixture
def home(served: World) -> World:
    served.user_config(MOCK)
    served.session(OLD, cwd=served.home / "work")
    return served


def work(world: World) -> Path:
    return world.home / "work"


def serve(world: World, *argv: str) -> Result:
    return tau(world, "serve", *argv, cwd=work(world))


def detached(world: World, *argv: str) -> dict[str, object]:
    """`tau serve -d --json` for OLD; its record."""
    result = serve(world, "-d", "--json", "--session", OLD, *argv)
    assert result.code == 0, result.err
    return json.loads(result.out)


def record_file(world: World) -> Path:
    return world.tau / "serve" / f"{OLD}.json"


def stop(world: World) -> None:
    assert serve(world, "--stop", "--session", OLD).code == 0
    assert not record_file(world).exists()


async def welcome_over_http(record: dict[str, object]) -> object:
    url, token = str(record["url"]), str(record["token"])
    async with await RemoteClient.connect_http(url, token, "test:probe") as client:
        return client.welcome


def foreground(
    world: World, *argv: str, stdout: int | None = subprocess.PIPE
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "tau", "serve", "--session", OLD, *argv],
        cwd=work(world),
        env={**world.environ, "PYTHONUNBUFFERED": "1"},
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
    )


@contextlib.contextmanager
def holding(port: int) -> Iterator[bool]:
    """A listener on 127.0.0.1:`port` for the duration (`False`: something else holds it)."""
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen()
    except OSError:
        listener.close()
        yield False
        return
    try:
        yield True
    finally:
        listener.close()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# Transports


def test_by_default_both_transports_are_on_and_the_record_names_both(home: World) -> None:
    record = detached(home)
    assert record["socket"] == str(home.tau / "serve" / f"{OLD}.sock")
    assert record["host"] == "127.0.0.1"
    assert isinstance(record["port"], int)
    assert record["url"] == f"http://127.0.0.1:{record['port']}/"
    assert isinstance(record["token"], str)
    assert len(str(record["token"])) >= 32
    welcome = asyncio.run(welcome_over_http(record))
    assert welcome.pid == record["pid"]  # type: ignore[attr-defined]
    stop(home)


def test_no_unix_serves_over_http_alone_and_stop_finds_it_there(home: World) -> None:
    record = detached(home, "--no-unix")
    assert "socket" not in record
    assert not (home.tau / "serve" / f"{OLD}.sock").exists()
    assert asyncio.run(welcome_over_http(record)).session == OLD  # type: ignore[attr-defined]
    again = serve(home, "-d", "--json", "--session", OLD)
    assert json.loads(again.out)["started"] is False, "-d finds the live HTTP server"
    stop(home)


def test_no_http_serves_on_the_socket_alone(home: World) -> None:
    record = detached(home, "--no-http")
    assert record["socket"]
    for field in ("url", "host", "port", "token"):
        assert field not in record
    stop(home)


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["--no-unix", "--no-http"], "nowhere to listen"),
        (["--no-http", "--port", "4000"], "--port configure HTTP"),
        (["--no-unix", "--socket", "s"], "--socket names the Unix socket"),
        (["--host", "0.0.0.0"], "--allow-network"),
        (["--port", "70000"], "not a TCP port"),
        (["--port", "-1"], "not a TCP port"),
    ],
)
def test_flags_that_leave_nowhere_to_listen_or_contradict_fail_before_starting(
    home: World, argv: list[str], says: str
) -> None:
    result = serve(home, "--session", OLD, *argv)
    assert result.code == 1
    assert says in result.err
    assert not record_file(home).exists()


def test_unix_and_no_unix_together_are_a_usage_error(home: World) -> None:
    with pytest.raises(SystemExit) as usage:
        serve(home, "--unix", "--no-unix")
    assert usage.value.code == 2


# The port


def test_a_port_asked_for_that_is_taken_fails_and_names_it(home: World) -> None:
    port = free_port()
    with holding(port) as held:
        assert held
        result = serve(home, "--session", OLD, "--port", str(port))
    assert result.code == 1
    assert f"cannot listen on 127.0.0.1:{port}" in result.err
    assert not record_file(home).exists()
    assert not (home.tau / "serve" / f"{OLD}.sock").exists(), "the socket was unbound"


def test_a_port_asked_for_that_is_free_is_the_one_bound(home: World) -> None:
    port = free_port()
    assert detached(home, "--port", str(port))["port"] == port
    stop(home)


@pytest.mark.parametrize("argv", [[], ["--port", "0"]])
def test_without_a_port_3393_is_tried_and_a_held_one_gives_way_to_any(
    home: World, argv: list[str]
) -> None:
    with holding(SOFT):  # held by this test, or by something else already
        record = detached(home, *argv)
    assert record["port"] != SOFT
    assert record["url"] == f"http://127.0.0.1:{record['port']}/"
    stop(home)


def test_without_a_port_a_free_3393_is_taken(home: World) -> None:
    with holding(SOFT) as free:
        pass
    if not free:
        pytest.skip("something on this machine holds 3393")
    assert detached(home)["port"] == SOFT
    stop(home)


# The record


def test_the_record_is_private_and_holds_the_edge(home: World) -> None:
    record = detached(home)
    path = record_file(home)
    assert path.stat().st_mode & 0o777 == 0o600
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {k: v for k, v in record.items() if k != "started"}
    assert set(on_disk) >= {"session", "pid", "url", "host", "port", "token", "cwd", "version"}
    assert set(on_disk) >= {"protocol", "since", "log", "socket"}
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []
    stop(home)


def test_the_record_names_the_session_name(home: World) -> None:
    assert tau(home, "sessions", "name", OLD, "the work", cwd=work(home)).code == 0
    assert detached(home)["name"] == "the work"
    stop(home)


def test_the_record_is_only_ever_read_whole(home: World) -> None:
    """Written to a scratch file and renamed: a reader polling from the start never sees a part
    of it."""
    server = foreground(home, "--no-unix")
    reads = 0
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with contextlib.suppress(FileNotFoundError):
                text = record_file(home).read_text(encoding="utf-8")
                json.loads(text)  # a part would not parse
                reads += 1
                if reads > 20:
                    break
        assert reads > 20
    finally:
        server.terminate()
        server.communicate(timeout=10)
    assert not record_file(home).exists(), "removed on a clean exit (SIGTERM)"


@pytest.mark.parametrize("stopped", [signal.SIGTERM, signal.SIGINT])
def test_a_signalled_server_removes_its_record(home: World, stopped: signal.Signals) -> None:
    server = foreground(home, "--json", "--no-unix")
    try:
        assert server.stdout is not None
        record = json.loads(server.stdout.readline())
        assert record_file(home).exists()
        server.send_signal(stopped)
        assert server.wait(timeout=10) == 0
    finally:
        server.kill()
        server.communicate()
    assert not record_file(home).exists()
    assert record["url"]


def test_a_stale_record_of_the_session_is_replaced_at_startup(home: World) -> None:
    serve_dir = home.tau / "serve"
    serve_dir.mkdir(mode=0o700, exist_ok=True)
    port = free_port()  # nothing listens there
    stale = {
        "pid": 999_999,
        "session": OLD,
        "cwd": str(work(home)),
        "protocol": 1,
        "version": "0",
        "since": 0,
        "log": None,
        "url": f"http://127.0.0.1:{port}/",
        "host": "127.0.0.1",
        "port": port,
        "token": "stale",
    }
    record_file(home).write_text(json.dumps(stale), encoding="utf-8")
    server = foreground(home, "--json", "--no-unix")
    try:
        assert server.stdout is not None
        record = json.loads(server.stdout.readline())
        assert record["pid"] == server.pid
        on_disk = json.loads(record_file(home).read_text(encoding="utf-8"))
        assert on_disk["pid"] == server.pid
        assert on_disk["token"] != "stale"
    finally:
        server.terminate()
        server.communicate(timeout=10)


# The banner


def test_the_banner_names_every_endpoint_on_one_line_and_no_token_off_a_terminal(
    home: World,
) -> None:
    server = foreground(home)
    try:
        assert server.stdout is not None
        line = server.stdout.readline().rstrip("\n")
        record = json.loads(record_file(home).read_text(encoding="utf-8"))
        socket_file = home.tau / "serve" / f"{OLD}.sock"
        assert line == f"serving {OLD} on {socket_file} and {record['url']} (pid {server.pid})"
        server.terminate()
        rest, _ = server.communicate(timeout=10)
    finally:
        server.kill()
    assert record["token"] not in rest


def on_a_terminal(world: World, *argv: str) -> tuple[list[str], dict[str, object]]:
    """The first lines `tau serve` writes to a terminal, and its record."""
    leader, follower = pty.openpty()
    server = foreground(world, *argv, stdout=follower)
    os.close(follower)
    try:
        text = b""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not record_file(world).exists():
            time.sleep(0.02)
        time.sleep(0.2)  # the banner is written after the record
        while True:
            import select  # noqa: PLC0415 - only this helper waits on a descriptor

            ready, _, _ = select.select([leader], [], [], 0.5)
            if not ready:
                break
            text += os.read(leader, 4096)
        record = json.loads(record_file(world).read_text(encoding="utf-8"))
    finally:
        server.terminate()
        server.communicate(timeout=10)
        os.close(leader)
    return text.decode().replace("\r\n", "\n").splitlines(), record


def test_on_a_terminal_a_second_line_is_the_paste_url_with_the_token(home: World) -> None:
    lines, record = on_a_terminal(home, "--no-unix")
    assert lines[0] == f"serving {OLD} on {record['url']} (pid {record['pid']})"
    assert lines[1] == f"paste URL: {record['url']}#token={record['token']}"


def test_quiet_keeps_the_token_off_the_terminal(home: World) -> None:
    lines, record = on_a_terminal(home, "--quiet")
    assert len(lines) == 1
    assert str(record["token"]) not in "\n".join(lines)


def test_d_names_the_endpoints_it_started(home: World) -> None:
    result = serve(home, "-d", "--session", OLD, "--no-unix")
    assert result.code == 0, result.err
    record = json.loads(record_file(home).read_text(encoding="utf-8"))
    assert result.out == f"started {OLD} on {record['url']} (pid {record['pid']})\n"
    stop(home)


# Attaching over HTTP


def test_p_attaches_over_http_to_a_server_without_a_socket(home: World) -> None:
    detached(home, "--no-unix")
    result = tau(home, "-p", "--session", OLD, "hello", cwd=work(home))
    assert result == Result(0, "mock: hello\n", "")
    stop(home)


def test_a_crashed_servers_record_whose_port_another_server_took_is_stale(home: World) -> None:
    """3393 is every server's first choice: the record of one that died names a port another
    session may hold now. Its pid is gone, so the record is stale, not a server that refuses."""
    other = "20261030-091503-4f5e6d"
    home.session(other, cwd=work(home))
    result = serve(home, "-d", "--json", "--session", other, "--no-unix")
    assert result.code == 0, result.err
    live = json.loads(result.out)
    serve_dir = home.tau / "serve"
    dead = {**live, "pid": 999_999, "session": OLD, "token": "not-the-other-one"}
    del dead["started"]
    record_file(home).write_text(json.dumps(dead), encoding="utf-8")
    attached = tau(home, "-p", "--session", OLD, "hello", cwd=work(home))
    assert attached == Result(0, "mock: hello\n", ""), "OLD ran here: its record was stale"
    assert serve(home, "--stop", "--session", other).code == 0
    assert not (serve_dir / f"{other}.json").exists()
