"""Live: Haiku backgrounds a slow bash call on a NON-head cursor; the result comes back to that cursor.

Reads the haiku-4.5 entry of ~/.tau/config.json; the key is never printed.
"""
import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from tau_agent_core.extensions_builtin import background as bg
from tau_agent_core.sdk import create_agent_session
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission

entry = json.loads(Path("~/.tau/config.json").expanduser().read_text())["models"]["haiku-4.5"]
holder = {}


def factory(api):
    holder["registry"] = bg.register_backgrounding(api, names=["bash"])
    holder["notes"] = []
    api.ui.notify = lambda m, level="info", **_: holder["notes"].append((level, m))


def path_lines(cursor):
    out = []
    for m in cursor.context():
        role = m.get("role") if isinstance(m, dict) else m.role
        content = m.get("content") if isinstance(m, dict) else m.content
        if isinstance(content, list):
            parts = []
            for b in content:
                t = b.get("type") if isinstance(b, dict) else getattr(b, "type", "")
                if t == "text":
                    parts.append(b.get("text") if isinstance(b, dict) else b.text)
                elif t in ("toolCall", "tool_call", "tool_use"):
                    name = b.get("name") if isinstance(b, dict) else b.name
                    args = b.get("arguments") if isinstance(b, dict) else b.arguments
                    parts.append(f"<call {name} {json.dumps(args)}>")
            content = " ".join(p for p in parts if p)
        out.append(f"{role}: {str(content)[:220]!r}")
    return out


async def main():
    cwd = tempfile.mkdtemp(prefix="bgc-live-")
    from tau_llm.types import Model

    model = Model(
        id=entry["model"],
        name=entry["model"],
        api="anthropic-messages",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        context_window=200000,
        max_tokens=1024,
    )
    session = create_agent_session(
        model=model,
        api_key=entry["api_key"],
        tools=["bash"],
        session_log=InMemorySessionLog(),
        extensions=[factory],
        system_prompt="You are a terse assistant in a test harness.",
        no_context_files=True,
        cwd=cwd,
    )
    head = session.cursor
    await head.append_message({"role": "user", "content": "(shared prefix)"})
    other = await session.open_cursor(head.leaf, label="client-b")
    prompt = (
        "Run the bash command `sleep 5; echo done-marker-42` with background set to true. "
        "Do not wait for it and do not call any other tool. In the same reply, after starting it, "
        "write one short sentence about patience. When the background result arrives later, "
        "reply with exactly the marker you saw."
    )
    t0 = time.monotonic()
    first = await session.submit(
        Submission(
            text=prompt,
            source="interactive",
            submitter="human",
            submission_id=uuid.uuid4().hex,
            multitask_strategy="enqueue",
        ),
        cursor=other,
    )
    t1 = time.monotonic() - t0
    reg = holder["registry"]
    print(f"first turn on cursor {other.id} returned after {t1:.1f}s accepted={first.accepted}")
    print("jobs after first turn:", [(c.handle, c.state, c.origin_cursor_id) for c in reg.jobs()])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if reg.jobs() and all(c.state not in (bg.RUNNING, bg.QUEUED) for c in reg.jobs()):
            if not other.busy:
                break
        await asyncio.sleep(0.2)
    print(f"settled after {time.monotonic() - t0:.1f}s")
    for c in reg.jobs():
        print(f"job {c.handle}: state={c.state} origin={c.origin_cursor_id} delivered_to={c.delivered_to} note={c.note!r}")
    print(f"--- cursor {other.id} (client-b, issuer) path ---")
    for line in path_lines(other):
        print("  ", line)
    print(f"--- cursor {head.id} (head) path ---")
    for line in path_lines(head):
        print("  ", line)
    print("notes:", holder["notes"])
    usage = session.side_usage
    print("session.side_usage:", usage)
    os.environ.pop("ANTHROPIC_API_KEY", None)


asyncio.run(main())
