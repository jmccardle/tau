"""E7 §3 (S49) — the headless JSON record sink for extension activity (anchor G10).

Extension activity was invisible in ``--mode json`` because ``api.ui.notify`` only
ever hit stderr. S49 adds a parallel record family — ``{"type": "extension", …}`` —
that the ``--mode json`` frontend writes alongside the closed ``AgentEvent`` set (the
same pattern as the session header line). With a sink installed,
``api.ui.notify(...)`` emits a record through it instead of the bare stderr line, so
a parent reading a child ``tau -p --mode json`` stream can see the child's extension
activity (the isolated-agent atom stays orchestratable).

These claims are proven against real ``AgentSession`` / ``ExtensionUI`` machinery —
no frontend, no provider.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §3 S49 (anchor G10).
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extensions.runner import ExtensionError
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.types import Model


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session(*extensions: Any) -> tuple[AgentSession, list[Any]]:
    apis: list[Any] = []

    def _capture(api: Any) -> None:
        apis.append(api)

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[_capture, *extensions],
    )
    return session, apis


def test_sink_captures_notify_as_record_instead_of_stderr(capsys) -> None:
    """With a sink installed, ``api.ui.notify`` emits a record — not a stderr line."""
    session, apis = _session()
    records: list[dict[str, Any]] = []
    session.set_extension_record_sink(records.append)

    apis[0].ui.notify("budget exceeded", "warning")

    assert records == [
        {
            "type": "extension",
            "kind": "notify",
            "extension": None,  # shared UI → no per-call attribution (honest null)
            "level": "warning",
            "message": "budget exceeded",
        }
    ]
    # It went to the sink, NOT stderr.
    assert "budget exceeded" not in capsys.readouterr().err


def test_every_bound_extension_shares_the_one_sink() -> None:
    """One ``set_extension_record_sink`` call routes ALL extensions' notify (shared UI)."""
    apis: list[Any] = []

    def ext_a(api: Any) -> None:
        apis.append(api)

    def ext_b(api: Any) -> None:
        apis.append(api)

    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=[ext_a, ext_b],
    )
    records: list[dict[str, Any]] = []
    session.set_extension_record_sink(records.append)

    # Both bound apis expose the ONE shared ExtensionUI (a test-enforced invariant),
    # so a single sink covers every extension.
    assert apis[0].ui is apis[1].ui
    apis[0].ui.notify("from a")
    apis[1].ui.notify("from b", "error")

    assert [(r["message"], r["level"]) for r in records] == [
        ("from a", "info"),
        ("from b", "error"),
    ]


def test_clearing_the_sink_restores_stderr(capsys) -> None:
    """Passing ``None`` clears the sink → notify falls back to the stderr line."""
    session, apis = _session()
    records: list[dict[str, Any]] = []
    session.set_extension_record_sink(records.append)
    session.set_extension_record_sink(None)

    apis[0].ui.notify("back to stderr", "info")

    assert records == []
    assert "back to stderr" in capsys.readouterr().err


def test_tui_delegate_wins_over_the_record_sink() -> None:
    """A live TUI delegate takes precedence over a record sink (delegate is the human sink)."""

    class _Delegate:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def notify(self, message: str, level: str = "info") -> None:
            self.calls.append((message, level))

    session, apis = _session()
    records: list[dict[str, Any]] = []
    session.set_extension_record_sink(records.append)
    delegate = _Delegate()
    session.set_ui_delegate(delegate)

    apis[0].ui.notify("painted", "warning")

    assert delegate.calls == [("painted", "warning")]
    assert records == []


def test_error_surface_attributes_the_extension_on_the_record(capsys) -> None:
    """The S44 error surface DOES know the failing extension, so its record names it.

    Unlike a plain ``api.ui.notify`` (shared UI, no attribution → ``extension: null``),
    ``_surface_extension_error`` passes the failing extension's path as ``source``, so
    an orchestrator reading the JSON stream can attribute the error.
    """
    session, _apis = _session()
    records: list[dict[str, Any]] = []
    session.set_extension_record_sink(records.append)

    session._surface_extension_error(
        ExtensionError(extension_path="/x/24_budget.py", event="turn_end", error="boom")
    )

    assert len(records) == 1
    record = records[0]
    assert record["type"] == "extension"
    assert record["extension"] == "/x/24_budget.py"
    assert record["level"] == "warning"
    assert "boom" in record["message"]
    # Surfaced through the sink, not stderr.
    assert "boom" not in capsys.readouterr().err
