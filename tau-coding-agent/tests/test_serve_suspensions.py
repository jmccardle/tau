"""Suspensions under ``tau serve`` (docs/STEP-DEBUGGER.md §3.4, §3.5, §3.8, M1e).

The daemon runs in this process over a real socket, as in ``test_serve.py``,
with one file extension that suspends at each user entry's ``entry_written``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tau_coding_agent.serve import protocol as p
from tau_coding_agent.serve.daemon import SUSPENSION_WAITING_KEY

from test_serve import _rpc, _serve, _until, extensions_off, provider  # noqa: F401

_STEPPER = """
def register(api):
    async def on_written(event, ctx):
        if event["kind"] == "user":
            await api.suspend({
                "title": "Break",
                "fields": [{"name": "note", "kind": "text"}],
                "actions": [{"label": "step"}, {"label": "abort"}],
            })

    api.on("entry_written", on_written, policy="human")
"""


@pytest.fixture
async def stepping(tmp_path: Path, provider: Any, extensions_off: Any):  # noqa: F811
    extensions = tmp_path / "home" / ".tau" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "stepper.py").write_text(_STEPPER)
    server, served = await _serve(tmp_path)
    yield served
    server.close()
    await server.wait_closed()
    await served.daemon.shutdown()


async def _submit(client: Any, session_id: str, cursor_id: str, text: str) -> None:
    await _rpc(
        client,
        "submit",
        session_id,
        cursor_id,
        text=text,
        source="interactive",
        submitter="test",
        submission_id=f"s-{text}",
        allow_user_input=True,
    )


async def test_a_late_client_sees_the_suspension_in_its_snapshot_and_answers_it(
    stepping: Any, tmp_path: Path
) -> None:
    driver = await stepping.client("driver", suspensions=True)
    session_id = (await driver.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    replica = await driver.attach(session_id)
    await _submit(driver, session_id, replica.head_cursor_id, "hello")
    await _until(lambda: bool(replica.suspensions))
    [suspension] = replica.suspensions.values()
    assert suspension["cursor_id"] == replica.head_cursor_id
    assert suspension["site"] == "entry_written" and suspension["queued"] is False

    other = await stepping.client("other")
    late = await other.attach(session_id)
    assert list(late.suspensions) == [suspension["id"]]
    answered = await _rpc(
        other,
        "answer_suspension",
        session_id,
        late.head_cursor_id,
        suspension_id=suspension["id"],
        action="step",
        values={"note": "seen"},
    )
    assert answered["accepted"] is True
    assert answered["resolution"]["answered_by"] == {
        "client_id": other.hello["client_id"],
        "client": "other",
    }
    again = await _rpc(
        driver,
        "answer_suspension",
        session_id,
        replica.head_cursor_id,
        suspension_id=suspension["id"],
        action="step",
        values={"note": "late"},
    )
    assert (again["accepted"], again["reason"]) == (False, "already resolved")
    await _until(lambda: not replica.suspensions and not late.suspensions)
    host = stepping.daemon.hosts[session_id]
    await _until(lambda: not host.agent_session.cursor.busy)
    [entry] = [e for e in host.log.entries() if e.get("type") == "suspension"]
    assert entry["resolution"]["values"] == {"note": "seen"}
    assert entry["reason"] == "answered"
    await other.close()
    await driver.close()


async def test_a_suspension_waits_and_warns_while_no_capable_client_is_attached(
    stepping: Any, tmp_path: Path
) -> None:
    watcher = await stepping.client("watcher")
    session_id = (await watcher.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    replica = await watcher.attach(session_id)
    await _submit(watcher, session_id, replica.head_cursor_id, "hello")
    await _until(lambda: bool(replica.suspensions))

    def waiting() -> bool:
        return any(w["key"] == SUSPENSION_WAITING_KEY for w in replica.surface["warnings"])

    await _until(waiting)
    capable = await stepping.client("capable", suspensions=True)
    seen = await capable.attach(session_id)
    await _until(lambda: not waiting())
    [suspension_id] = seen.suspensions
    await _rpc(
        capable,
        "answer_suspension",
        session_id,
        seen.head_cursor_id,
        suspension_id=suspension_id,
        action="abort",
        values={"note": ""},
    )
    await _until(lambda: not replica.suspensions)
    host = stepping.daemon.hosts[session_id]
    await _until(lambda: not host.agent_session.cursor.busy)
    assert not waiting()
    await capable.close()
    await watcher.close()


async def test_pending_suspensions_reads_through_the_named_cursor(
    stepping: Any, tmp_path: Path
) -> None:
    client = await stepping.client("c", suspensions=True)
    session_id = (await client.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    replica = await client.attach(session_id)
    head = replica.head_cursor_id
    assert (await _rpc(client, "get_pending_suspensions", session_id, head))["suspensions"] == []
    await _submit(client, session_id, head, "hello")
    await _until(lambda: bool(replica.suspensions))
    listed = await _rpc(client, "get_pending_suspensions", session_id, head, scope="session")
    assert [s["id"] for s in listed["suspensions"]] == list(replica.suspensions)
    await _rpc(
        client,
        "answer_suspension",
        session_id,
        head,
        suspension_id=listed["suspensions"][0]["id"],
        action="step",
        values={"note": ""},
    )
    await _until(lambda: not replica.suspensions)
    await client.close()
