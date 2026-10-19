"""#44: a subscription to an ``ext:`` topic no loaded extension emits is visible.

Reference: docs/EXTENSION-PACKAGES.md §10. The rename that motivated it: a
subscriber hard-coding ``ext:planner:need_router`` after the emitter became
``tau_router.planner`` receives nothing, and nothing said so.
"""

from __future__ import annotations

import logging

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.sdk import orphan_subscription_notices
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model

_EMITTER = "def register(api):\n    pass\n"

_SUBSCRIBER = """
def register(api):
    api.on("ext:planner:need_router", lambda payload: None)
    api.on("ext:router:ready", lambda payload: None)
    stop = api.on("ext:gone:topic", lambda payload: None)
    stop()
"""


def _session() -> AgentSession:
    model = Model(
        id="m",
        name="m",
        api="openai-completions",
        provider="openai",
        base_url="http://127.0.0.1:1/v1",
        context_window=8192,
        max_tokens=256,
    )
    return AgentSession(session_log=InMemorySessionLog(), model=model)


async def test_a_topic_whose_emitter_is_not_loaded_is_listed_and_logged(tmp_path, caplog):
    (tmp_path / "router.py").write_text(_EMITTER)
    (tmp_path / "sub.py").write_text(_SUBSCRIBER)
    session = _session()
    with caplog.at_level(logging.DEBUG, logger="tau.extensions"):
        await session.load_extensions(
            [str(tmp_path / "router.py"), str(tmp_path / "sub.py")], discover=False
        )

    rows = {r.identifier: r.orphan_topics for r in session.extension_records()}
    assert rows == {"router": (), "sub": ("ext:planner:need_router",)}
    assert "sub subscribes to ext:planner:need_router" in caplog.text
    assert orphan_subscription_notices(session.extension_records()) == [
        "sub subscribes to ext:planner:need_router, but no loaded extension emits under 'planner'"
    ]


async def test_a_dotted_emitter_is_matched_whole(tmp_path):
    pkg = tmp_path / "tau_router.planner.py"
    pkg.write_text(_EMITTER)
    sub = tmp_path / "sub.py"
    sub.write_text('def register(api):\n    api.on("ext:tau_router.planner:need_router", print)\n')
    session = _session()
    await session.load_extensions([str(pkg), str(sub)], discover=False)
    assert session.orphan_subscriptions() == {}
