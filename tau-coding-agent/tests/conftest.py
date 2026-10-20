"""Shared, hermetic fixtures for the ``tau-coding-agent`` TUI suite.

Seventeen test modules used to hand-roll the same ``TauApp`` fixture, and they
did not agree on how to sandbox it; several read the developer's real
``~/.tau/config.json``, and on a machine whose config selects the ``jmfts``
session store every session a test created was written into the running JMFTS
server.

The moves that isolate a ``TauApp`` live in
:mod:`tau_coding_agent.testing.sandbox`: ``$TAU_HOME`` names the temp dir (every
home read resolves at use), and an **injected** ``session_catalog`` keeps the
config-driven ``build_session_catalog`` branch, with its network health check,
from running at all.

``make_app`` is a factory rather than a plain fixture because the call sites do
vary in two real ways (whether they stub ``create_backend``, and what config the
app should hold); everything else they varied in was accident, not intent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import pytest
from textual.app import App
from textual.worker import WorkerCancelled

from tau_agent_core.tau_home import HOME_ENV
from tau_coding_agent.app import TauApp
from tau_coding_agent.testing.sandbox import DEFAULT_CONFIG, build_tau_app, sandbox_tau_home

__all__ = ["DEFAULT_CONFIG"]


@pytest.fixture
def tau_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Make ``tmp_path`` τ's home for the test.

    ``TAU_HOME`` goes through ``monkeypatch`` as well, so a test that also sets it
    there unwinds in order instead of restoring the sandbox's value after it.

    Returns the sandboxed home so a test can assert against what landed on disk.
    """
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    with sandbox_tau_home(tmp_path) as home:
        yield home


@pytest.fixture
def make_app(monkeypatch: pytest.MonkeyPatch, tau_home: Path) -> Callable[..., TauApp]:
    """Build a hermetic ``TauApp``.

    ``create_backend`` stubs ``app.create_backend`` (most callers pass something
    that never touches the network). ``config`` is merged over
    :data:`DEFAULT_CONFIG`. Extension discovery defaults **off** so a test does
    not pick up whatever happens to live in the developer's extensions dir.
    """

    def _make(
        *,
        create_backend: Callable[[dict], Any] | None = None,
        config: dict[str, Any] | None = None,
        discover_extensions: bool = False,
        extension_paths: Iterable[str] = (),
        **kwargs: Any,
    ) -> TauApp:
        if create_backend is not None:
            monkeypatch.setattr("tau_coding_agent.app.create_backend", create_backend)
        app: TauApp = build_tau_app(
            tau_home,
            config=config,
            discover_extensions=discover_extensions,
            extension_paths=extension_paths,
            **kwargs,
        )
        return app

    return _make


@pytest.fixture
def settle_transcript() -> Callable[..., Any]:
    """Wait until a reloaded transcript has stopped moving under its own power.

    ``reload_messages`` and ``move_window`` return when the build is SCHEDULED.
    Two more things happen afterwards, each on a later refresh:
    ``_finish_build`` clears ``ChatDisplay.is_building``, then calls
    ``scroll_to_tail``, whose ``scroll_end`` is itself deferred. A refresh is
    driven by Textual's own screen timer, so how many of those land inside one
    ``pilot.pause()`` is a question about the wall clock — which is why a test
    that pauses a fixed number of times passes on an idle machine and fails
    under a full suite's load.

    Two failures have been paid for here. A click resolved to a widget eighteen
    rows above the screen and raised ``OutOfBounds`` from inside Textual
    (docs/RELEASING.md, 0.10.2). And the 0.11.0 matrix failed
    ``test_scrolling_against_the_top_slides_the_window`` on 3.13 alone: a
    deferred ``scroll_end`` landed after the test's ``scroll_home``, so
    ``_slide_at_edge`` saw ``scroll_offset.y > 0``, read the gesture as an
    ordinary scroll, and the window never moved.

    So the condition is both: the build is done AND the position has stopped
    changing. Raises rather than returning early, because a settle that gives up
    quietly would put the race back.
    """

    async def _settle(pilot: Any, display: Any, *, tries: int = 50) -> None:
        stable = 0
        last = None
        for _ in range(tries):
            await pilot.pause()
            now = (display.is_building, display.scroll_y, display.virtual_size)
            stable = stable + 1 if now == last and not display.is_building else 0
            if stable >= 2:
                return
            last = now
        raise AssertionError(f"the transcript never settled in {tries} pauses: {last}")

    return _settle


@pytest.fixture
def wait_for_workers_settled() -> Callable[[App], Any]:
    """Wait for every worker to finish, the way ``app.workers.wait_for_complete()``
    almost does — except that bare call is unsafe to use on this app.

    ``ChatSidebar.refresh_chats()`` starts an ``exclusive=True`` thread worker
    in the ``"sidebar-refresh"`` group (mount, then again at the end of every
    turn / new-chat / clear-chat). ``exclusive`` cancels the still-running
    previous worker in that group — by design, so a slow superseded catalog
    fetch can never land after a faster one and overwrite the sidebar with
    stale data. But ``Worker.wait()`` raises ``WorkerCancelled`` for a
    cancelled worker, and ``WorkerManager.wait_for_complete()`` only swallows
    ``asyncio.CancelledError`` — a different exception — so it re-raises that
    ``WorkerCancelled`` into whatever test called it, intermittently, whenever
    the mount-time refresh happened to still be running when a turn ended.

    A superseded sidebar-refresh is the expected, benign outcome of that
    design, not a test failure, so it is the one thing this helper forgives.
    Anything else — a real ``WorkerFailed``, or a ``WorkerCancelled`` from any
    other group — still propagates; swallowing those would hide an actual bug.

    A fixture rather than a plain function: every package under ``testpaths``
    ships its own ``tests/conftest.py``, and with no ``__init__.py`` marking
    these directories as packages, ``from conftest import ...`` resolves by
    bare module name — whichever package's ``conftest.py`` pytest happens to
    import first across the whole suite wins that name, and the others
    silently get the wrong one. Fixtures don't have that problem: pytest
    resolves them through its own per-directory discovery, not Python's
    import system.
    """

    async def _wait(app: App) -> None:
        workers = list(app.workers)
        results = await asyncio.gather(*(w.wait() for w in workers), return_exceptions=True)
        for worker, result in zip(workers, results):
            if isinstance(result, WorkerCancelled) and worker.group == "sidebar-refresh":
                continue
            if isinstance(result, BaseException):
                raise result

    return _wait


_REAL_HOME = __import__("pathlib").Path.home()


@pytest.fixture(autouse=True)
def _hermetic_extensions_dir(tmp_path_factory, monkeypatch):
    """Keep drop-in discovery, and the authorization store beside it, out of the real home.

    Discovery read the developer's real ``~/.tau/extensions``; since 0.14.0 a first
    discovery also writes the authorization store (docs/EXTENSION-PACKAGES.md §5). A
    test that points ``HOME`` somewhere itself keeps that home.
    """
    from pathlib import Path

    from tau_agent_core import extension_sources
    from tau_agent_core.tau_home import resolve_tau_home

    spare = tmp_path_factory.mktemp("tau-home")

    def _directory() -> Path:
        if Path.home() != _REAL_HOME:
            return resolve_tau_home() / "extensions"
        return spare / "extensions"

    monkeypatch.setattr(extension_sources, "default_extensions_dir", _directory)
