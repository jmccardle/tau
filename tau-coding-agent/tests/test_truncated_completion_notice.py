"""Saying out loud that a completion stopped at the output cap.

Reference: docs/TRUNCATED-TOOL-CALLS.md §3.

``stop_reason="length"`` means the server stopped because generation reached the
cap τ sends as ``max_tokens`` — the answer is a prefix, not an answer. Every other
surface already carried it (``--mode json`` emits it on ``message_end``); the TUI
did not, so a turn the server cut off mid-sentence looked exactly like a turn that
finished.

The case that made this matter is a cut that lands inside a tool call's
``arguments``. The provider drops that call rather than execute a truncated one,
so the turn ends having run nothing — which on screen reads as the model choosing
to stop.
"""

from __future__ import annotations

from typing import Any

from tau_coding_agent.app import ChatDisplay, MessageBox, Parley


class _Backend:
    """A backend with no way to run a turn. Nothing here needs one — the render
    events are fed to ``_on_render_event`` directly, which is the same entry point
    the live bus subscription uses."""

    async def submit_turn(self, submission: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("no test in this module runs a turn")


def _app(make_app, config: dict[str, Any] | None = None) -> Parley:
    return make_app(create_backend=lambda cfg: _Backend(), config=config)


def _system_text(app: Parley) -> str:
    boxes = app.query_one(ChatDisplay).query(MessageBox)
    return "\n".join(b._content for b in boxes if b.role == "system")


async def test_a_length_stop_says_so(make_app):
    app = _app(make_app)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_render_event(
            {"kind": "completion_end", "output": 4096, "context": 900, "stop_reason": "length"}
        )
        await pilot.pause()
        text = _system_text(app)
        assert "output cap" in text
        assert "stop_reason: length" in text


async def test_it_quotes_the_cap_actually_in_force(make_app):
    """The number on screen must be the number on the wire. A config that states
    no ``max_tokens`` resolves to ``DEFAULT_MAX_TOKENS``, which is what the local
    server receives as ``n_predict`` — so that is the figure to report."""
    app = _app(
        make_app,
        {"models": {"m": {"backend": "openai", "model": "m", "api_key": "x", "max_tokens": 900}}},
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_render_event(
            {"kind": "completion_end", "output": 900, "context": 10, "stop_reason": "length"}
        )
        await pilot.pause()
        assert "max_tokens = 900" in _system_text(app)


async def test_an_unstated_cap_reports_the_default(make_app):
    app = _app(make_app)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_render_event(
            {"kind": "completion_end", "output": 1, "context": 1, "stop_reason": "length"}
        )
        await pilot.pause()
        assert "max_tokens = 4096" in _system_text(app)


async def test_an_ordinary_completion_says_nothing(make_app):
    """The notice costs a box, so it must fire only on the stop reason that means
    something was lost. Every turn ends with a completion_end."""
    app = _app(make_app)
    async with app.run_test() as pilot:
        await pilot.pause()
        for reason in ("stop", "toolUse", None):
            await app._on_render_event(
                {"kind": "completion_end", "output": 30, "context": 10, "stop_reason": reason}
            )
        await pilot.pause()
        assert _system_text(app) == ""


async def test_an_unresolvable_model_entry_reports_the_cap_as_unknown(make_app):
    """Fail-Early: a cap that cannot be read is reported as not known, never as a
    stand-in figure. Quoting 4096 at a user whose real cap is something else sends
    them to change a number that is already right."""
    app = _app(make_app, {"default_model": "absent-from-config"})
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_render_event(
            {"kind": "completion_end", "output": 1, "context": 1, "stop_reason": "length"}
        )
        await pilot.pause()
        assert "max_tokens = unknown" in _system_text(app)
