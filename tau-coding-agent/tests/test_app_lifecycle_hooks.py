"""E6 §2 (S41) — the TUI fires ``session_start`` / ``session_shutdown``.

Driven through the real app via ``App.run_test()`` / Pilot with a REAL
``TauBackend`` (no network in ``__init__``). A file extension records each
lifecycle hook to a temp file so the test can observe them across the app's own
lifecycle:
  * ``session_start`` fires after ``_load_backend_extensions`` (new-chat) — while
    the app is still running;
  * ``session_shutdown`` fires from ``on_unmount`` when the app tears down (the
    Pilot context exits), covering an explicit quit and Ctrl-C alike.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §2 S41 (anchor G1).
"""

from __future__ import annotations

import pytest

from tau_coding_agent.app import Parley
from tau_coding_agent.backends import create_backend


def _lifecycle_ext(out_path: str) -> str:
    # Each hook appends "<tag>:<reason>" to ``out_path`` so the test can read the
    # firing order back after the app has torn down.
    return (
        "def register(api):\n"
        f"    out = {out_path!r}\n"
        "    def rec(tag):\n"
        "        def handler(event, ctx):\n"
        "            with open(out, 'a') as f:\n"
        "                f.write(tag + ':' + str(event.get('reason', '')) + '\\n')\n"
        "        return handler\n"
        "    api.on('session_start', rec('start'))\n"
        "    api.on('session_shutdown', rec('shutdown'))\n"
    )


@pytest.fixture
def app(monkeypatch, tmp_path):
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path)
    monkeypatch.setattr("tau_coding_agent.app.create_backend", create_backend)
    # Isolate the module-global session-event listener list (see test_app_extension_loading).
    monkeypatch.setattr(store, "_session_listeners", [])

    a = Parley()
    a.config = {
        "models": {"m": {"backend": "openai", "model": "m", "api_key": "not-needed"}},
        "default_model": "m",
        "system_prompt": "sys",
    }
    return a


def _lines(path) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text().splitlines() if ln]


async def test_start_on_load_and_shutdown_on_unmount(app, tmp_path):
    out = tmp_path / "lifecycle.log"
    ext = tmp_path / "lifecycle_ext.py"
    ext.write_text(_lifecycle_ext(str(out)))
    app._extension_paths = [str(ext)]
    app._discover_extensions = False

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_chat()
        await pilot.pause()
        # session_start has fired now that the extension is loaded — before quit.
        assert _lines(out) == ["start:startup"]

    # The Pilot context exit unmounts the app → on_unmount fires session_shutdown.
    assert _lines(out) == ["start:startup", "shutdown:quit"]
