"""Real processes: a `tau serve` on τ's mock model (or a stub OpenAI endpoint), and `tau-a2a` in
front of it on a free loopback port. Each test gets its own TAU_HOME under /tmp (a socket path
has a length limit, and a home under a project would be read as that project)."""

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

MOCK = {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}
GATE = """
from typing import Literal

class Gate:
    name = "gate"

    async def pre_sample(self, cursor, subject):
        verdict = await cursor.ask("run the thing?", Literal["yes", "no"])
        return None if verdict == "yes" else f"verdict: {verdict}"

extensions = [Gate()]
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Served:
    base: Path
    url: str
    process: subprocess.Popen
    session: str

    def log(self) -> str:
        return (self.base / "a2a.log").read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def serve() -> Iterator[Callable[..., Served]]:
    """`serve(model=MOCK, extensions=(), a2a_args=())`: a session and its A2A server."""
    bases: list[Path] = []
    processes: list[subprocess.Popen] = []

    def start(
        model: dict = MOCK, extensions: tuple[str, ...] = (), a2a_args: tuple[str, ...] = ()
    ) -> Served:
        base = Path(tempfile.mkdtemp(prefix="ta2a", dir="/tmp"))
        bases.append(base)
        home, work = base / "h", base / "h" / "work"
        (home / ".tau").mkdir(parents=True)
        work.mkdir()
        (home / ".tau" / "config.json").write_text(
            json.dumps({"default_model": "m", "models": {"m": model}}), encoding="utf-8"
        )
        env = {**os.environ, "HOME": str(home), "TAU_HOME": str(home / ".tau")}
        flags: list[str] = []
        for i, source in enumerate(extensions):
            path = home / f"ext{i}.py"
            path.write_text(source, encoding="utf-8")
            flags += ["-e", str(path)]
        sock = base / "s.sock"
        started = subprocess.run(  # noqa: PLW1510 - the code is asserted with the output
            [sys.executable, "-m", "tau", "serve", "-d", "--json", "--socket", str(sock), *flags],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert started.returncode == 0, started.stdout + started.stderr
        session = json.loads(started.stdout.strip().splitlines()[-1])["session"]
        port = free_port()
        log = open(base / "a2a.log", "w")  # noqa: SIM115
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tau_a2a",
                "--socket",
                str(sock),
                "--db",
                str(base / "a2a.db"),
                "--port",
                str(port),
                *(a2a_args or ("--auth", "none")),
            ],
            cwd=base,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(p)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(url + "/.well-known/agent-card.json", timeout=1).status_code == 200:
                    return Served(base, url, p, session)
            assert p.poll() is None, (base / "a2a.log").read_text()
            time.sleep(0.1)
        raise AssertionError("tau-a2a did not come up:\n" + (base / "a2a.log").read_text())

    try:
        yield start
    finally:
        for p in processes:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
        for base in bases:
            # the `tau serve -d` of the test has the base in its arguments
            for pid in _pids_naming(str(base)):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            shutil.rmtree(base, ignore_errors=True)


def _pids_naming(text: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():  # rules: allow(entry): a directory listing
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                if text in (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                    errors="replace"
                ):
                    found.append(int(entry.name))
    return found


@pytest.fixture
def http_stub() -> Iterator[Callable[[int, dict], str]]:
    """`stub(status, body)`: an HTTP server answering every request so; its base URL."""
    servers: list[ThreadingHTTPServer] = []

    def start(status: int, body: dict) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length", 0)))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for s in servers:
        s.shutdown()
