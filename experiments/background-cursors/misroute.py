"""Where does a job issued on a non-head cursor land? Run from the repo root.

usage: misroute.py {new|old} [OLD_PATH]; OLD_PATH as in stress.py.
"""
import asyncio
import importlib.util
import sys
from unittest.mock import patch

sys.path.insert(0, "tau-agent-core/tests")
import test_background_cursors as T  # noqa: E402

if sys.argv[1] == "old":
    spec = importlib.util.spec_from_file_location("background_old", sys.argv[2])
    old = importlib.util.module_from_spec(spec)
    sys.modules["background_old"] = old
    spec.loader.exec_module(old)
    T.bg = old


async def main():
    rig = T._Rig()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
        head = rig.session.cursor
        await head.append_message({"role": "user", "content": "shared"})
        other = await rig.session.open_cursor(head.leaf, label="client-b")
        await rig.submit("go:a", cursor=other)
        rig.gate("a").set()
        for _ in range(500):
            await asyncio.sleep(0.001)
    print(
        f"{sys.argv[1]}: issued on cursor {other.id} (client-b); "
        f"on client-b's path: {T._count(other, 'bg-slow-1')}, on head's path: {T._count(head, 'bg-slow-1')}"
    )


asyncio.run(main())
