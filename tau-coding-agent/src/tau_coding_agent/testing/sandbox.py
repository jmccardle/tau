"""Build a ``TauApp`` whose every ``~/.tau`` read and write lands in a temp dir.

``tests/conftest.py`` worked this out first. The moves:

1. ``$TAU_HOME`` names *root*. Every home read resolves at use, so config,
   sessions, settings, extensions and themes all land there.
2. The session-event listeners are emptied for the duration.
3. An **injected** ``session_catalog`` beats resolving one, so the config-driven
   ``build_session_catalog`` branch — and its live network health check — never
   runs.

This module exists because the test suite is no longer the only caller: the
``devshot`` screenshot tool needs the same isolation, and a second hand-rolled
copy of it is how the two would drift apart.

Reference: docs/textual-headless-testing.md
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = ["DEFAULT_CONFIG", "SANDBOX_CWD", "build_tau_app", "sandbox_tau_home"]

DEFAULT_CONFIG: dict[str, Any] = {
    "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
    "default_model": "m",
    "system_prompt": "sys",
}


@contextmanager
def sandbox_tau_home(root: Path) -> Iterator[Path]:
    """Make *root* τ's home for the duration, by setting ``$TAU_HOME``.

    Restores the previous value on exit, so this is safe to nest inside a pytest
    fixture or to call repeatedly from a long-lived process. Child processes
    started inside inherit the sandbox.

    Args:
        root: An existing directory.
    """
    import tau_coding_agent.session_store as store
    from tau_agent_core.tau_home import HOME_ENV

    saved_home = os.environ.get(HOME_ENV)
    saved_listeners = store._session_listeners

    os.environ[HOME_ENV] = str(root)
    store._session_listeners = []
    try:
        yield root
    finally:
        if saved_home is None:
            os.environ.pop(HOME_ENV, None)
        else:
            os.environ[HOME_ENV] = saved_home
        store._session_listeners = saved_listeners


SANDBOX_CWD = Path("/home/dev/project")


def build_tau_app(
    tau_home: Path,
    *,
    config: dict[str, Any] | None = None,
    discover_extensions: bool = False,
    extension_paths: Iterable[str] = (),
    cwd: Path = SANDBOX_CWD,
    **kwargs: Any,
) -> Any:
    """Construct a hermetic ``TauApp`` rooted at *tau_home*.

    Call inside :func:`sandbox_tau_home`. Extension discovery defaults **off** so
    a run never picks up whatever happens to live in the developer's extensions
    dir, and ``cwd`` is a fixed fiction for the same reason. ``kwargs`` pass
    through to ``TauApp.__init__`` (``cli_overrides``, ``cli_run_config``, …).

    Note that ``cwd`` changes what the app *displays*, not where it runs — the
    process's real working directory is untouched.
    """
    from tau_coding_agent.app import TauApp
    from tau_coding_agent.session_store import FileSessionCatalog

    app = TauApp(session_catalog=FileSessionCatalog(tau_home / "sessions"), **kwargs)
    app.config = {**DEFAULT_CONFIG, **(config or {})}
    app._cwd = cwd
    app._extension_paths = list(extension_paths)
    app._discover_extensions = discover_extensions
    return app
