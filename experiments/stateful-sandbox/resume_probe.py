"""Restore a sandbox in a fresh process from a session file (the resume test's child).

Usage: ``python resume_probe.py SESSION_FILE LEAF_OR_DEFAULT NAME [NAME ...]``.
Prints ``{name: value-or-"<missing>"}`` as JSON, plus the state entry id used.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pydantic_monty as monty  # noqa: E402
from sandbox_state import BlobStore, MontySandbox  # noqa: E402
from tau_agent_core.agent_session import AgentSession  # noqa: E402
from tau_coding_agent.session_store import Session  # noqa: E402
from tau_llm.types import Model  # noqa: E402


async def main(path: Path, leaf_arg: str, names: list[str]) -> None:
    log = Session.load(path)
    model = Model(
        id="m", provider="openai", api="openai-completions",
        base_url="http://127.0.0.1:1/v1", name="m", context_window=8192, max_tokens=256,
    )
    session = AgentSession(session_log=log, model=model, tools=[])
    leaf = session.cursor.leaf if leaf_arg == "default" else leaf_arg
    cursor = await session.open_cursor(leaf, label="resume")
    async with monty.AsyncMonty(min_processes=1) as pool:
        box = MontySandbox(pool, blobs=BlobStore.beside(path))
        live, state_id = await box.restore(cursor.entries(), cursor.leaf)
        out: dict[str, object] = {}
        for name in names:
            try:
                out[name] = await live.feed_run(name)
            except monty.MontyRuntimeError:
                out[name] = "<missing>"
        await live.__aexit__(None, None, None)
    print(json.dumps({"state_id": state_id, "leaf": leaf, "values": out}))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], sys.argv[3:]))
