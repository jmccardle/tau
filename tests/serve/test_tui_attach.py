"""The terminal UI on a session a `tau serve` holds (D23 section 1, D22 section 8): it attaches
through `open_client` and the app runs over the `RemoteClient`; what a running server would ignore
is refused before the screen. The server is a real process with a mock model, started in a
throwaway TAU_HOME and killed when the test is over."""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Windows: tau serve does not run there (a Unix domain socket, D21; not ported)",
        allow_module_level=True,
    )

import argparse
import asyncio
import io

import pytest
from tuikit import SIZE, shows, type_line
from world import World, tau

from tau import RemoteClient, open_client
from tau.cli import extension_flags, main, session_flags
from tau.tui import TauApp

MOCK = {
    "default_model": "m",
    "models": {"m": {"model": "m", "provider": "mock", "options": {"delay_ms": 0}}},
}
OLD = "20261030-091502-1c2d3e"


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def home(served: World) -> World:
    served.user_config(MOCK)
    (served.home / "work").mkdir(exist_ok=True)
    served.session(OLD, cwd=served.home / "work")
    result = tau(served, "serve", "-d", "--json", "--session", OLD, cwd=served.home / "work")
    assert result.code == 0, result.err
    return served


def flags(*argv: str) -> argparse.Namespace:
    return argparse.ArgumentParser(parents=[extension_flags(), session_flags()]).parse_args(
        list(argv)
    )


def test_the_app_runs_over_the_attached_client_and_the_server_keeps_the_messages(
    home: World,
) -> None:
    async def run() -> None:
        opened = await open_client(
            flags("--session", OLD), client="test:tui", environ=home.environ, cwd=home.home / "work"
        )
        assert isinstance(opened.client, RemoteClient)
        app = TauApp(
            opened.client,
            opened.cursor,
            welcome=opened.welcome,
            home=home.tau,
            cwd=home.home / "work",
            snapshot=opened.snapshot,
        )
        async with app.run_test(size=SIZE) as pilot:
            await shows(pilot, app, f"{OLD}")
            await type_line(pilot, "hello")
            await pilot.press("enter")
            await shows(pilot, app, "mock: hello")
            assert f"{OLD}" in app.screen_text().splitlines()[0], "the header names the session"
        await opened.close()

    asyncio.run(run())
    shown = tau(home, "export", OLD, cwd=home.home / "work")
    assert "hello" in shown.out
    assert "mock: hello" in shown.out


def test_flags_the_running_server_would_ignore_are_refused_before_the_screen(home: World) -> None:
    out, err = Tty(), io.StringIO()
    code = main(
        ["--session", OLD, "-n", "a name"],
        stdin=Tty(),
        stdout=out,
        stderr=err,
        environ=home.environ,
        cwd=home.home / "work",
    )
    assert code == 1
    assert "-n" in err.getvalue()


def test_a_reconnect_dials_the_address_in_the_servers_record_and_close_closes_the_new_client(
    served: World,
) -> None:
    served.user_config(MOCK)
    work = served.home / "work"
    work.mkdir(exist_ok=True)
    served.session(OLD, cwd=work)
    custom = served.home / "mine.sock"
    result = tau(
        served, "serve", "-d", "--json", "--session", OLD, "--socket", str(custom), cwd=work
    )
    assert result.code == 0, result.err

    async def run() -> None:
        opened = await open_client(
            flags("--session", OLD), client="test:tui", environ=served.environ, cwd=work
        )
        first = opened.client
        again = await opened.reconnect()
        assert again is opened.client, "the opened session now holds the new client"
        assert again is not first
        assert await again.node(0) is not None, "the new client talks to the server"
        await opened.close()
        with pytest.raises(ConnectionError):
            await again.node(0)

    asyncio.run(run())
