"""Extension warnings reach every serve client, including one that attaches late (#60)."""

from __future__ import annotations

import asyncio
import threading

from test_serve import _until, extensions_off, provider, served  # noqa: F401

from tau_coding_agent.serve import protocol as p


async def test_a_late_attach_receives_the_warnings_and_an_attached_client_sees_each_change(
    served, tmp_path
):
    early = await served.client("early")
    session_id = (await early.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    replica = await early.attach(session_id)
    assert replica.surface["warnings"] == []
    session = served.daemon.hosts[session_id].agent_session

    session.set_extension_warning("probe", "excluded", "bash was left out", "error")
    expected = {
        "source": "probe",
        "key": "excluded",
        "message": "bash was left out",
        "severity": "error",
    }
    await _until(lambda: early.replicas[session_id].surface["warnings"] == [expected])

    late = await served.client("late")
    assert (await late.attach(session_id)).surface["warnings"] == [expected]

    session.clear_extension_warning("probe", "excluded")
    await _until(lambda: early.replicas[session_id].surface["warnings"] == [])
    await _until(lambda: late.replicas[session_id].surface["warnings"] == [])
    await early.close()
    await late.close()


async def test_describe_reports_the_current_warnings(served, tmp_path):
    client = await served.client()
    session_id = (await client.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    await client.attach(session_id)
    served.daemon.hosts[session_id].agent_session.set_extension_warning("tau", "k", "m")

    surface = await client.request(p.Describe(session_id=session_id))

    assert surface["warnings"] == [
        {"source": "tau", "key": "k", "message": "m", "severity": "warning"}
    ]
    await client.close()


def test_every_warning_field_is_required_in_the_serve_schema():
    """A client must never have to invent a severity (tau-code's generated types)."""
    schema = p.json_schema()
    record = schema["$defs"]["ExtensionWarning"]
    assert sorted(record["required"]) == ["key", "message", "severity", "source"]
    assert schema["$defs"]["Surface"]["properties"]["warnings"]["items"] == {
        "$ref": "#/$defs/ExtensionWarning"
    }
    assert schema["$defs"]["UiWarnings"]["properties"]["warnings"]["items"] == {
        "$ref": "#/$defs/ExtensionWarning"
    }


async def test_a_warning_set_from_a_worker_thread_is_published_on_the_loop(served, tmp_path):
    """#60 review: serve's publish is loop-only (seq, history, queue.put_nowait)."""
    client = await served.client()
    session_id = (await client.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    await client.attach(session_id)
    host = served.daemon.hosts[session_id]
    published: list[tuple[str, int]] = []
    publish = host.publish

    def recording(kind, data):
        published.append((kind, threading.get_ident()))
        publish(kind, data)

    host.publish = recording  # type: ignore[method-assign]

    await asyncio.to_thread(host.agent_session.set_extension_warning, "probe", "k", "failing")
    await _until(lambda: client.replicas[session_id].surface["warnings"] != [])

    loop_thread = threading.get_ident()
    assert published and all(thread == loop_thread for _, thread in published)
    await client.close()
