"""S30 — the ``context`` mutating hook is ELIMINATED (E5 §3.2 / §1.2).

Proving test for step S30. The ``context`` hook fired before every LLM call and
replaced the whole message list on a deep copy — an out-of-band, per-send
transform that could silently diverge "what the model saw" from "the path shown in
the interface". Under the durable-hook invariant (§1) the model's input for a call
is exactly the system prompt + the linear active path, so that transform is a
hidden fork and is removed outright (its cases fold into durable ``tool_result``
edits + ``before_agent_start``, S29/S31/S32).

This asserts the removal is complete and Fail-Early, not merely disabled:

- ``context`` is gone from :data:`ExtensionRunner.HOOK_EVENTS` and there is no
  ``emit_context`` dispatch method;
- **no context dispatch remains in the three src trees** (the grep the Verify
  clause asks for — the loop no longer references ``emit_context`` / a
  ``has_handlers("context")`` gate);
- ``api.on("context", …)`` RAISES an unknown/retired-hook error (both on a bound
  and an unbound api) instead of silently binding to the notify ``EventBus`` — and
  the retired name never leaks onto that bus;
- ``examples/23_context_surgeon.py`` — which uses ``ctx.*`` session-control ops,
  NOT the ``context`` hook — still loads and registers its tools against a real
  bound api (S30 must not break it).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from tau_ai.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.extension_types import _RETIRED_HOOKS
from tau_agent_core.extensions.runner import ExtensionRunner
from tau_agent_core.session_log import InMemorySessionLog

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_TREES = (
    _REPO_ROOT / "tau-ai" / "src",
    _REPO_ROOT / "tau-agent-core" / "src",
    _REPO_ROOT / "tau-coding-agent" / "src",
)


def _make_session() -> AgentSession:
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    return AgentSession(session_log=InMemorySessionLog(), model=model, system_prompt="")


# ── the mechanism is gone ────────────────────────────────────────────────────


def test_context_is_not_a_hook_event() -> None:
    assert "context" not in ExtensionRunner.HOOK_EVENTS
    # The exact live membership (``input`` was added in S42, ``turn_end`` in S43,
    # roadmap §2) — the point of this pin is that the retired ``context`` name never
    # reappears here.
    assert ExtensionRunner.HOOK_EVENTS == (
        "tool_call",
        "tool_result",
        "before_agent_start",
        "input",
        "turn_end",
    )


def test_runner_has_no_emit_context_method() -> None:
    assert not hasattr(ExtensionRunner, "emit_context")


def test_no_context_dispatch_remains_in_src() -> None:
    """The grep the Verify clause names: no ``emit_context`` call-site or method,
    and no ``has_handlers("context")`` gate, anywhere in the three src trees."""
    offenders: list[str] = []
    # Match real code (a call/def always has the paren), not prose backticks.
    forbidden = ("emit_context(", 'has_handlers("context")', 'has_hook_handlers("context")')
    for tree in _SRC_TREES:
        for path in tree.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    offenders.append(f"{path}: {token}")
    assert offenders == [], f"context dispatch still present: {offenders}"


# ── registering it is Fail-Early (raise), never a silent bind ────────────────


def test_context_is_a_retired_hook_name() -> None:
    assert "context" in _RETIRED_HOOKS


def test_api_on_context_raises_on_a_bound_api() -> None:
    session = _make_session()
    api = session._bind_extension_api("mem:retired")
    with pytest.raises(RuntimeError, match="removed in E5"):
        api.on("context", lambda event, ctx: None)
    # …and it did not leak onto the notify EventBus (a dead channel) either.
    assert session._extension_runner.has_handlers("context") is False


def test_api_on_context_raises_on_an_unbound_api() -> None:
    from tau_agent_core.extension_types import ExtensionAPI

    api = ExtensionAPI()  # bare — no runner bucket
    # The retired-hook guard fires BEFORE the unbound-bucket guard.
    with pytest.raises(RuntimeError, match="removed in E5"):
        api.on("context", lambda event, ctx: None)


# ── the audited demo (ctx.* ops, not the context hook) still works ───────────


def _load_surgeon() -> Any:
    path = _REPO_ROOT / "examples" / "23_context_surgeon.py"
    spec = importlib.util.spec_from_file_location("context_surgeon_s30_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_context_surgeon_still_registers_without_the_context_hook() -> None:
    """23_context_surgeon registers only ``ctx.*``-backed tools — no ``context``
    hook — so eliminating the hook cannot break it. Binding it against a real api
    must not raise, and it must not touch the retired hook."""
    surgeon = _load_surgeon()
    session = _make_session()
    api = session._bind_extension_api("examples/23_context_surgeon.py")

    surgeon.context_surgeon_extension(api)  # must not raise

    # It registered its session-control tools …
    tool_names = {t.name for t in session._registry.get_all_tools()}
    assert {"compact_now", "summarize_history", "fork_session"} <= tool_names
    # … and never registered a context hook (which would have raised anyway).
    assert session._extension_runner.has_handlers("context") is False
