"""Shared, hermetic fixtures for the ``tau-coding-agent`` TUI suite.

Seventeen test modules used to hand-roll the same ``Parley`` fixture, and they
did not agree on how to sandbox it. Two of the idioms in circulation were
outright no-ops:

* ``monkeypatch.setattr("tau_coding_agent.app.TAU_DIR", tmp_path)`` patches a
  *from-import binding* that nothing reads — ``Parley.load_config`` delegates to
  ``config.bootstrap_config``, which resolves its own module-level name.
* ``monkeypatch.setattr("tau_coding_agent.config.TAU_DIR", tmp_path)`` is no
  better: ``config.CONFIG_PATH`` is computed at import time (``TAU_DIR /
  "config.json"``), so rebinding ``TAU_DIR`` afterwards cannot move it.

The net effect was that several modules read the developer's real
``~/.tau/config.json``. On a machine whose config selects the ``jmfts`` session
store that is not a cosmetic problem: ``Parley.__init__`` resolves a catalog
through ``build_session_catalog``, which performs a live health check and then
writes every session the test creates into the running JMFTS server.

There are exactly three moves that isolate a ``Parley``, and this module is the
one place they are spelled out:

1. ``config.CONFIG_PATH`` — the only name ``bootstrap_config`` actually reads.
2. ``session_store.TAU_DIR`` — where the file store roots its ``sessions/`` dir.
3. An **injected** ``session_catalog``. ``Parley.__init__`` takes one and
   documents that it "always wins over resolving one", so injecting it means the
   config-driven ``build_session_catalog`` branch — and its network health check
   — never runs at all. Sandboxing paths alone never achieved this.

``make_app`` is a factory rather than a plain fixture because the call sites do
vary in two real ways (whether they stub ``create_backend``, and what config the
app should hold); everything else they varied in was accident, not intent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest
from textual.app import App
from textual.worker import WorkerCancelled

from tau_coding_agent.app import Parley
from tau_coding_agent.session_store import FileSessionCatalog

# The config every hand-rolled fixture assigned, modulo an ``api_key`` that some
# included and some did not. It is present here because a model entry without one
# is only safe as long as nothing tries to build a real client from it.
DEFAULT_CONFIG: dict[str, Any] = {
    "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
    "default_model": "m",
    "system_prompt": "sys",
}


@pytest.fixture
def tau_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect every ``~/.tau`` read and write into ``tmp_path``.

    Patches the two names that are actually consulted at call time. Returns the
    sandboxed home so a test can assert against what landed on disk.
    """
    import tau_coding_agent.session_store as store

    monkeypatch.setattr("tau_coding_agent.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    # A real backend gives the app a real AgentSession, so _bind_backend_session
    # registers a MODULE-GLOBAL session-event listener. The app never unsubscribes
    # on shutdown (harmless in a one-shot process, a cross-test leak here), so hand
    # every test a fresh list; monkeypatch restores the original on teardown and
    # discards whatever this test leaked. Three modules each carried their own copy
    # of this reasoning — it is isolation, so it is correct for all of them.
    monkeypatch.setattr(store, "_session_listeners", [])
    return tmp_path


@pytest.fixture
def make_app(monkeypatch: pytest.MonkeyPatch, tau_home: Path) -> Callable[..., Parley]:
    """Build a hermetic ``Parley``.

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
    ) -> Parley:
        if create_backend is not None:
            monkeypatch.setattr("tau_coding_agent.app.create_backend", create_backend)
        app = Parley(session_catalog=FileSessionCatalog(tau_home / "sessions"))
        app.config = {**DEFAULT_CONFIG, **(config or {})}
        app._extension_paths = list(extension_paths)
        app._discover_extensions = discover_extensions
        return app

    return _make


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
