"""`tau serve`'s HTTP edge (D29) as a person or a program uses it: both transports by default and
the flags that turn one off, the port rule (strict `--port N`, else 3393 then any), the serve
record's HTTP fields and its file, the banner and its paste URL, and `tau -p` attaching over HTTP.
The servers are real processes with a mock model, started in a throwaway TAU_HOME under a short
directory and killed when the test is over. On Windows the edge is the only transport: what needs
the socket or a pseudo-terminal is skipped there, and a server is stopped cleanly with Ctrl+Break
(its own process group) where Unix sends SIGTERM."""

import asyncio
import contextlib
import dataclasses
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from proctable import first_line, interpreter, own_console, signal_to
from world import Result, World, tau

from tau import RemoteClient

WINDOWS = sys.platform == "win32"
posix_only = pytest.mark.skipif(WINDOWS, reason="Windows: no Unix socket (D29: the edge alone)")
needs_pty = pytest.mark.skipif(WINDOWS, reason="Windows: no pseudo-terminals")
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
    world: World,
    *argv: str,
    stdout: int | None = subprocess.PIPE,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    program, environ = interpreter({**world.environ, "PYTHONUNBUFFERED": "1", **(env or {})})
    return subprocess.Popen(
        [program, "-m", "tau", "serve", "--session", OLD, *argv],
        cwd=work(world),
        env=environ,
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
        **own_console(),
    )


CLEAN_STOPS = (
    [signal.CTRL_BREAK_EVENT] if sys.platform == "win32" else [signal.SIGTERM, signal.SIGINT]
)
"""What stops a foreground server cleanly: SIGTERM or SIGINT; on Windows, Ctrl+Break sent to its
process group (Ctrl+C is not delivered to a group of its own)."""


def end(server: subprocess.Popen[str]) -> None:
    """Stop a `foreground` server as a person or a service manager does: cleanly."""
    signal_to(server, CLEAN_STOPS[0])


@contextlib.contextmanager
def holding(port: int) -> Iterator[bool]:
    """A listener on 127.0.0.1:`port` for the duration (`False`: something else holds it). Bound
    as asyncio binds a server (`SO_REUSEADDR` off Windows), so a closed connection's TIME_WAIT on
    the port does not count as a holder: it does not stop the server from binding."""
    listener = socket.socket()
    if sys.platform != "win32":  # on Windows the option lets a second listener share the port
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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


@posix_only
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


@posix_only
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
        (["--no-http", "--web-root", "."], "--web-root configure HTTP"),
        (["--web-root", "."], "holds no index.html"),
    ],
)
def test_flags_that_leave_nowhere_to_listen_or_contradict_fail_before_starting(
    home: World, argv: list[str], says: str
) -> None:
    if WINDOWS and "--no-http" in argv:
        says = "--no-http is not available on Windows"
    result = serve(home, "--session", OLD, *argv)
    assert result.code == 1
    assert says in result.err
    assert not record_file(home).exists()


def test_no_http_is_refused_on_windows_where_the_edge_is_all_there_is(home: World) -> None:
    if not WINDOWS:
        pytest.skip("the socket serves alone off Windows (test_no_http_serves_on_the_socket_alone)")
    result = serve(home, "--session", OLD, "--no-http")
    assert result.code == 1
    assert "--no-http is not available on Windows" in result.err
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
    if not WINDOWS:  # Windows: the serve directory's access list (test_serve_command)
        assert path.stat().st_mode & 0o777 == 0o600
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {k: v for k, v in record.items() if k != "started"}
    assert set(on_disk) >= {"session", "pid", "url", "host", "port", "token", "cwd", "version"}
    assert set(on_disk) >= {"protocol", "since", "log"}
    assert ("socket" in on_disk) is not WINDOWS
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
            # Windows: a file being renamed over cannot be opened without sharing delete
            with contextlib.suppress(FileNotFoundError, PermissionError):
                text = record_file(home).read_text(encoding="utf-8")
                json.loads(text)  # a part would not parse
                reads += 1
                if reads > 20:
                    break
        assert reads > 20
    finally:
        end(server)
        server.communicate(timeout=10)
    assert not record_file(home).exists(), "removed on a clean exit (SIGTERM)"


@pytest.mark.parametrize("stopped", CLEAN_STOPS)
def test_a_signalled_server_removes_its_record(home: World, stopped: int) -> None:
    server = foreground(home, "--json", "--no-unix")
    try:
        record = json.loads(first_line(server))
        assert record_file(home).exists()
        signal_to(server, stopped)
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
        record = json.loads(first_line(server))
        assert record["pid"] == server.pid
        on_disk = json.loads(record_file(home).read_text(encoding="utf-8"))
        assert on_disk["pid"] == server.pid
        assert on_disk["token"] != "stale"
    finally:
        end(server)
        server.communicate(timeout=10)


# The banner


def test_the_banner_names_every_endpoint_on_one_line_and_no_token_off_a_terminal(
    home: World,
) -> None:
    server = foreground(home)
    try:
        line = first_line(server).rstrip("\n")
        record = json.loads(record_file(home).read_text(encoding="utf-8"))
        socket_file = home.tau / "serve" / f"{OLD}.sock"
        where = record["url"] if WINDOWS else f"{socket_file} and {record['url']}"
        assert line == f"serving {OLD} on {where} (pid {server.pid})"
        end(server)
        rest, _ = server.communicate(timeout=10)
    finally:
        server.kill()
    assert record["token"] not in rest


def on_a_terminal(
    world: World, *argv: str, env: dict[str, str] | None = None
) -> tuple[list[str], dict[str, object]]:
    """The first lines `tau serve` writes to a terminal, and its record."""
    if sys.platform == "win32":
        pytest.skip("Windows: no pseudo-terminals")
    import pty  # noqa: PLC0415 - Unix only

    leader, follower = pty.openpty()
    server = foreground(world, *argv, stdout=follower, env=env)
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


@needs_pty
def test_on_a_terminal_a_second_line_opens_the_installed_client_with_the_token(home: World) -> None:
    lines, record = on_a_terminal(home, "--no-unix")
    assert lines[0] == f"serving {OLD} on {record['url']} (pid {record['pid']})"
    assert lines[1] == f"open: {record['url']}#token={record['token']}"


@needs_pty
def test_with_no_web_the_paste_url_is_for_a_web_client_served_elsewhere(home: World) -> None:
    lines, record = on_a_terminal(home, "--no-unix", "--no-web")
    assert lines[1] == f"for a web client: {record['url']}#token={record['token']}"


@needs_pty
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


# The web client


def built_client(world: World) -> Path:
    root = world.home / "client"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>mine</title>", encoding="utf-8")
    return root


@needs_pty
def test_with_a_web_root_the_banner_says_open(home: World) -> None:
    built_client(home)
    lines, record = on_a_terminal(home, "--no-unix", "--web-root", "../client")
    assert lines[1] == f"open: {record['url']}#token={record['token']}"


@needs_pty
def test_an_installed_tau_web_is_served(home: World) -> None:
    packages = home.home / "packages"
    dist = packages / "tau_web" / "dist"
    dist.mkdir(parents=True)
    (packages / "tau_web" / "__init__.py").write_text("", encoding="utf-8")
    (dist / "index.html").write_text("<!doctype html><title>installed</title>", encoding="utf-8")
    lines, _ = on_a_terminal(home, "--no-unix", env={"PYTHONPATH": str(packages)})
    assert lines[1].startswith("open: "), "the server found the client"
    with_package = dataclasses.replace(home, environ={**home.environ, "PYTHONPATH": str(packages)})
    record = detached(with_package, "--no-unix")
    page = httpx.get(str(record["url"]), trust_env=False, timeout=5)
    assert page.text.endswith("<title>installed</title>")
    stop(home)


def without_tau_web(world: World) -> World:
    """`world` where `tau_web` is a plain module, not the package: as good as not installed, to a
    process that reads `PYTHONPATH`."""
    shadow = world.home / "shadow"
    shadow.mkdir()
    (shadow / "tau_web.py").write_text("", encoding="utf-8")
    return dataclasses.replace(world, environ={**world.environ, "PYTHONPATH": str(shadow)})


def detached_process(world: World, *argv: str, isolated: bool = False) -> Result:
    """`tau serve -d --json` for OLD as its own process (`python -I` if `isolated`), so that the
    parent, too, reads `world`'s environment."""
    command = [sys.executable, *(["-I"] if isolated else []), "-m", "tau", "serve", "-d", "--json"]
    done = subprocess.run(
        [*command, "--session", OLD, "--no-unix", *argv],
        cwd=work(world),
        env=world.environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return Result(done.returncode, done.stdout, done.stderr)


def test_without_tau_web_the_server_says_the_install_is_broken_and_serves_the_wire(
    home: World,
) -> None:
    result = detached_process(without_tau_web(home))
    assert result.code == 0, result.err
    assert "web client is not installed" in result.err
    assert "this install is broken" in result.err
    record = json.loads(result.out)
    page = httpx.get(str(record["url"]), trust_env=False, timeout=5)
    assert page.status_code == 200
    assert "This install of τ is broken" in page.text
    assert "ffwf-tau-web" in page.text
    assert str(record["token"]) not in page.text
    assert asyncio.run(welcome_over_http(record)) is not None, "the wire is served all the same"
    stop(home)


def test_d_started_isolated_starts_its_server_isolated(home: World) -> None:
    """`python -I -m tau serve -d` (a bundled runtime) re-launches the server with `-I` too (τ
    #79): the child ignores `PYTHONPATH` and the user's site-packages as its parent did. Here
    `PYTHONPATH` would shadow `tau_web`, which the test above shows a child without `-I` takes."""
    result = detached_process(without_tau_web(home), isolated=True)
    assert result.code == 0, result.err
    assert "not installed" not in result.err
    record = json.loads(result.out)
    page = httpx.get(str(record["url"]), trust_env=False, timeout=5)
    assert "is broken" not in page.text, "the child took PYTHONPATH: it lost -I"
    stop(home)


def test_no_web_says_nothing_of_a_missing_client(home: World) -> None:
    result = detached_process(without_tau_web(home), "--no-web")
    assert result.code == 0, result.err
    assert "not installed" not in result.err
    stop(home)


def test_no_web_with_a_web_root_is_refused(home: World) -> None:
    result = serve(home, "--no-web", "--web-root", ".", "--session", OLD)
    assert result.code != 0
    assert "one or the other" in result.err


def test_no_web_without_http_is_refused(home: World) -> None:
    result = serve(home, "--no-http", "--no-web", "--session", OLD)
    assert result.code != 0
    if WINDOWS:
        assert "--no-http is not available on Windows" in result.err
    else:
        assert "--no-web configure HTTP, which --no-http turns off" in result.err


# Stopping over HTTP (D29 section 10)


def shutdown(record: dict[str, object], token: str | None) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    url = str(record["url"]) + "shutdown"
    return httpx.post(url, headers=headers, trust_env=False, timeout=5)


def test_shutdown_needs_the_token_and_post(home: World) -> None:
    record = detached(home, "--no-unix")
    assert shutdown(record, None).status_code == 401
    assert shutdown(record, "not-the-token").status_code == 401
    url = str(record["url"]) + "shutdown"
    bearer = {"Authorization": f"Bearer {record['token']}"}
    assert httpx.get(url, headers=bearer, trust_env=False, timeout=5).status_code == 405
    assert record_file(home).exists(), "a refused stop stops nothing"
    stop(home)


def test_shutdown_with_the_token_stops_the_server_as_a_signal_does(home: World) -> None:
    server = foreground(home, "--json", "--no-unix")
    try:
        record = json.loads(first_line(server))
        assert shutdown(record, str(record["token"])).status_code == 202
        assert server.wait(timeout=15) == 0
    finally:
        server.kill()
        server.communicate()
    assert not record_file(home).exists()


def test_stop_goes_over_http_when_the_server_has_an_edge(home: World) -> None:
    """`--stop` asks the edge, so it reaches a server that no signal can (Windows) or that this
    user may not signal; the record goes once the server has wound down."""
    record = detached(home, "--no-unix")
    result = serve(home, "--stop", "--json", "--session", OLD)
    assert result.code == 0, result.err
    assert json.loads(result.out) == {"session": OLD, "pid": record["pid"], "stopped": True}
    assert not record_file(home).exists()
