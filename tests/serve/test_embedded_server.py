"""A program that hosts a `Driver` with `Started.serve` is attachable as `tau serve` is (D21
section 9): the serve record names it while it runs, `tau -c` in its directory attaches and talks
to its live session, and when it closes the record is gone and `tau -c` opens the session itself.
The host is a real process (a downstream package's), started in a throwaway TAU_HOME."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Windows: tau serve does not run there (a Unix domain socket, D21; not ported)",
        allow_module_level=True,
    )

import argparse
import asyncio
import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass

from world import World, tau

from tau import PROTOCOL, ConfigError, RemoteClient, open_client, start_session
from tau.cli import extension_flags, session_flags

MOCK = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}
HOST = """
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from tau import start_session
from tau.cli import extension_flags, session_flags


async def main() -> None:
    home, work = Path(sys.argv[1]), Path(sys.argv[2])
    args = argparse.ArgumentParser(parents=[extension_flags(), session_flags()]).parse_args([])
    os.chdir(work)
    started = await start_session(args, environ={"HOME": str(home)}, cwd=work)
    slow = os.environ.get("SLOW_RELEASE")
    if slow:  # a session that takes its time to let go, to hold the window open
        stop = started.stop

        async def slowly() -> None:
            await asyncio.sleep(float(slow))
            await stop()

        started.stop = slowly
    server = await started.serve()
    print(started.directory.name, flush=True)
    closing = None
    while command := await asyncio.to_thread(sys.stdin.readline):
        if command.strip() == "history":
            at = started.session.cursor(0).at
            print(json.dumps([repr(n.body) for n in started.session.tree.walk(at)]), flush=True)
        elif command.strip() == "close":
            closing = asyncio.ensure_future(server.close())
            print("closing", flush=True)
            break
    await (closing or server.close())


asyncio.run(main())
"""


@dataclass
class Host:
    process: subprocess.Popen[str]
    session: str

    def say(self, command: str) -> str:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        return self.process.stdout.readline().strip()

    def history(self) -> list[str]:
        return json.loads(self.say("history"))

    def finish(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        assert self.process.wait(timeout=15) == 0


@pytest.fixture
def home(served: World) -> World:
    served.user_config(MOCK)
    (served.home / "work").mkdir(exist_ok=True)
    return served


@pytest.fixture
def host(home: World, request: pytest.FixtureRequest) -> Iterator[Host]:
    script = home.home / "host.py"
    script.write_text(HOST, encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script), str(home.home), str(home.home / "work")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**home.environ, **getattr(request, "param", {})},
    )
    try:
        assert process.stdout is not None
        session = process.stdout.readline().strip()
        if not session:  # it failed to start: say why
            process.kill()
            pytest.fail(f"the host printed no session id; stderr:\n{process.stderr.read()}")  # type: ignore[union-attr]
        yield Host(process, session)
    finally:
        process.kill()
        process.wait()


def record_file(home: World, host: Host):
    return home.tau / "serve" / f"{host.session}.json"


def test_continue_attaches_and_the_turn_lands_in_the_hosts_session(home: World, host: Host) -> None:
    work = home.home / "work"
    record = json.loads(record_file(home, host).read_text(encoding="utf-8"))
    assert (record["pid"], record["session"], record["protocol"]) == (
        host.process.pid,
        host.session,
        PROTOCOL,
    )
    assert (record["cwd"], record["log"]) == (str(work), None)
    assert record["socket"] == str(home.tau / "serve" / f"{host.session}.sock")
    result = tau(home, "-p", "-c", "hello", cwd=work)
    assert (result.code, result.out, result.err) == (0, "mock: hello\n", "")
    assert any("mock: hello" in body for body in host.history()), "the host's own session has it"

    async def opened() -> None:
        args = argparse.ArgumentParser(parents=[extension_flags(), session_flags()]).parse_args(
            ["-c"]
        )
        found = await open_client(args, client="test:open", environ=home.environ, cwd=work)
        try:
            assert isinstance(found.client, RemoteClient)
            assert found.welcome is not None
            assert (found.welcome.pid, found.welcome.session) == (host.process.pid, host.session)
        finally:
            await found.close()

    asyncio.run(opened())


def test_closing_the_host_removes_the_record_and_frees_the_session(home: World, host: Host) -> None:
    work = home.home / "work"
    assert tau(home, "-p", "-c", "one", cwd=work).out == "mock: one\n"
    host.finish()
    assert not record_file(home, host).exists()
    assert not (home.tau / "serve" / f"{host.session}.sock").exists()
    after = tau(home, "-p", "-c", "two", cwd=work)
    assert (after.code, after.out, after.err) == (0, "mock: two\n", "")


@pytest.mark.parametrize("host", [{"SLOW_RELEASE": "2"}], indirect=True)
def test_a_client_while_the_host_closes_is_told_it_is_stopping(home: World, host: Host) -> None:
    work = home.home / "work"
    assert tau(home, "-p", "-c", "kept", cwd=work).code == 0  # a session in use is kept
    assert host.say("close") == "closing"
    deadline = time.monotonic() + 15
    seen = []
    while host.process.poll() is None and time.monotonic() < deadline:
        seen.append(tau(home, "-p", "-c", "hi", cwd=work))
    host.finish()
    assert seen, "the host outlived the close, which holds the session for two seconds"
    assert any("is stopping; try again" in r.err for r in seen), [r.err for r in seen]
    for result in seen:
        assert "stream ended" not in result.err, result.err
        assert result.code == 0 or "stopping" in result.err, result.err
    assert record_file(home, host).exists() is False


def test_stop_reaches_an_embedder(home: World, host: Host) -> None:
    work = home.home / "work"
    result = tau(home, "serve", "--stop", "-c", "--json", cwd=work)
    assert result.code == 0, result.err
    assert json.loads(result.out) == {
        "session": host.session,
        "pid": host.process.pid,
        "stopped": True,
    }
    assert not record_file(home, host).exists()
    assert tau(home, "-p", "-c", "after", cwd=work).out == "mock: after\n"


def test_a_session_in_memory_cannot_be_served(home: World) -> None:
    args = argparse.ArgumentParser(parents=[extension_flags(), session_flags()]).parse_args(
        ["--no-session"]
    )

    async def serve() -> None:
        started = await start_session(args, environ=home.environ, cwd=home.home / "work")
        try:
            with pytest.raises(ConfigError, match="in memory"):
                await started.serve()
        finally:
            await started.stop()

    asyncio.run(serve())


# -- in this process: what Started.serve does with its arguments and its files


def started_in(home: World, name: str):
    work = home.home / name
    work.mkdir(exist_ok=True)
    args = argparse.ArgumentParser(parents=[extension_flags(), session_flags()]).parse_args([])
    return start_session(args, environ=home.environ, cwd=work)


def test_a_socket_of_its_own_and_a_second_close(home: World) -> None:
    async def scenario() -> None:
        started = await started_in(home, "work")
        mine = home.home / "mine.sock"
        server = await started.serve(mine)
        assert started.directory is not None
        record = json.loads(
            (home.tau / "serve" / f"{started.directory.name}.json").read_text(encoding="utf-8")
        )
        assert record["socket"] == str(mine)
        client = await RemoteClient.connect(mine, "test")
        assert client.welcome.session == started.directory.name
        await client.close()
        await server.close()
        await server.close()  # harmless: the session is released once
        assert not mine.exists()
        assert not (home.tau / "serve" / f"{started.directory.name}.json").exists()

    asyncio.run(scenario())


def test_a_socket_a_server_answers_at_is_refused(home: World, host: Host) -> None:
    taken = home.tau / "serve" / f"{host.session}.sock"

    async def scenario() -> None:
        started = await started_in(home, "other")
        try:
            with pytest.raises(ConfigError, match="another server already answers"):
                await started.serve(taken)
            assert taken.exists(), "the first server is untouched"
        finally:
            await started.stop()

    asyncio.run(scenario())
    assert tau(home, "-p", "-c", "still", cwd=home.home / "work").out == "mock: still\n"


def test_a_record_that_is_not_ours_is_kept(home: World) -> None:
    async def scenario() -> None:
        started = await started_in(home, "work")
        server = await started.serve()
        assert started.directory is not None
        path = home.tau / "serve" / f"{started.directory.name}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**record, "pid": record["pid"] + 1}), encoding="utf-8")
        await server.close()
        assert path.exists(), "another server's record is not ours to remove"

    asyncio.run(scenario())


def test_a_failed_record_write_unbinds(home: World) -> None:
    async def scenario() -> None:
        started = await started_in(home, "work")
        assert started.directory is not None
        (home.tau / "serve").mkdir(mode=0o700, exist_ok=True)
        (home.tau / "serve" / f"{started.directory.name}.json").mkdir()  # cannot be written
        try:
            with pytest.raises(OSError, match="Is a directory"):
                await started.serve()
            assert not (home.tau / "serve" / f"{started.directory.name}.sock").exists()
        finally:
            await started.stop()

    asyncio.run(scenario())


FAILING = """
class Fatal(BaseException):
    pass


class Boom:
    name = "boom"

    async def pre_sample(self, cursor, subject):
        # A hook's Exception fails its step; a BaseException (as a core panic is) is what the
        # driver fails of: the one thing `tau serve` must then report.
        raise Fatal("boom")


extensions = [Boom()]
"""


def test_tau_serve_reports_the_drivers_failure_and_nothing_else(home: World) -> None:
    ext = home.home / "boom.py"
    ext.write_text(FAILING, encoding="utf-8")
    work = home.home / "work"
    server = subprocess.Popen(
        [sys.executable, "-m", "tau", "serve", "--json", "-e", str(ext)],
        cwd=work,
        env=home.environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert server.stdout is not None
        assert server.stdout.readline()  # the record: it is serving
        tau(home, "-p", "-c", "hi", cwd=work)
        out, err = server.communicate(timeout=15)
    finally:
        server.kill()
    assert server.returncode == 1
    assert "the driver failed: RuntimeError: Fatal: boom" in out + err
    assert "Traceback" not in err
