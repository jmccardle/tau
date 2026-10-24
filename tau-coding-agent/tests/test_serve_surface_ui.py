"""Live panels and statuses reach every serve client, including one that attaches late (#90)."""

from __future__ import annotations

from test_serve import _until, extensions_off, provider, served  # noqa: F401

from tau_coding_agent.serve import protocol as p

PANEL = {"title": "Stepping", "lines": ["armed"]}


async def test_a_late_attach_receives_the_live_panels_and_statuses(served, tmp_path):
    early = await served.client("early")
    session_id = (await early.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    replica = await early.attach(session_id)
    assert replica.surface["panels"] == {}
    assert replica.surface["statuses"] == {}
    ui = served.daemon.hosts[session_id].ui

    ui.set_status("debugger", "stepping armed")
    ui.set_status("other", "x")
    ui.panel("steps", PANEL)
    await _until(lambda: early.replicas[session_id].surface["statuses"].get("other") == "x")
    await _until(lambda: early.replicas[session_id].surface["panels"] == {"steps": PANEL})

    late = await served.client("late")
    surface = (await late.attach(session_id)).surface
    assert surface["statuses"] == {"debugger": "stepping armed", "other": "x"}
    assert surface["panels"] == {"steps": PANEL}
    await early.close()
    await late.close()


async def test_clearing_a_key_removes_it_from_the_snapshot_and_from_replicas(served, tmp_path):
    client = await served.client()
    session_id = (await client.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    await client.attach(session_id)
    ui = served.daemon.hosts[session_id].ui
    ui.set_status("debugger", "stepping armed")
    ui.panel("steps", PANEL)
    await _until(lambda: client.replicas[session_id].surface["statuses"] != {})

    ui.set_status("debugger", None)
    ui.panel("steps", None)
    ui.set_status("never-set", None)

    await _until(lambda: client.replicas[session_id].surface["panels"] == {})
    assert client.replicas[session_id].surface["statuses"] == {}
    surface = await client.request(p.Describe(session_id=session_id))
    assert surface["panels"] == {}
    assert surface["statuses"] == {}
    await client.close()


async def test_a_replacing_panel_keeps_one_spec_per_key(served, tmp_path):
    client = await served.client()
    session_id = (await client.request(p.NewSession(cwd=str(tmp_path))))["session"]["session_id"]
    await client.attach(session_id)
    ui = served.daemon.hosts[session_id].ui
    newer = {"title": "Stepping", "lines": ["paused"]}

    ui.panel("steps", PANEL)
    ui.panel("steps", newer)

    surface = await client.request(p.Describe(session_id=session_id))
    assert surface["panels"] == {"steps": newer}
    await client.close()


def test_the_serve_schema_requires_panels_and_statuses_on_the_surface():
    """A client must never have to invent an empty map (tau-code's generated types)."""
    surface = p.json_schema()["$defs"]["Surface"]
    assert {"panels", "statuses"} <= set(surface["required"])
    assert surface["properties"]["statuses"]["additionalProperties"] == {"type": "string"}
    assert surface["properties"]["panels"]["additionalProperties"] == {"$ref": "#/$defs/PanelSpec"}
