"""An extension's ``ctx.cwd`` is its session's cwd, not the process cwd.

Under ``tau serve`` one process holds sessions in different directories; the
built-in tools resolve paths in the session's, so a ``tool_call`` hook that
checks a path has to resolve it in the same one.
"""

from __future__ import annotations

from tau_llm.types import Model

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog


def _model() -> Model:
    return Model(
        id="m",
        name="m",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


async def test_hook_ctx_cwd_is_the_session_cwd(tmp_path, monkeypatch) -> None:
    elsewhere = tmp_path / "process"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt="",
        extensions=[],
        cwd=str(tmp_path / "session"),
    )
    seen = []
    api = session._bind_extension_api("test")
    api.on("tool_call", lambda event, ctx: seen.append(ctx.cwd))
    await session._extension_runner.emit_tool_call(
        {"type": "tool_call", "tool_name": "read", "input": {}}
    )
    assert seen == [str(tmp_path / "session")]
