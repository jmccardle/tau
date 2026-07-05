"""Tests for ``examples/51_delegate_fleet.py`` — the supervised delegate fleet (E11, S72).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §7 S72. No pi original (τ-native — pi's
``subagent`` has no live status surface and no mid-run steering).

Proves:

* the pure pieces — ``_parse_tasks``, ``_resolve_budget`` (USD mode / token mode and
  the two Fail-Early pairings), ``_config_nonneg_int``, the ``_ChildRow`` cells, the
  ``_fleet_panel_spec`` S68 shape, and ``_fleet_report`` / ``_ledger_report``;
* ``_run_child_streamed`` drives a child's live event stream through the composed
  supervisors (no real subprocess — ``spawn.stream_tau`` is faked): a clean stream is
  ``done`` with turns/tokens/last-tool folded; ``stuck_limit`` identical consecutive
  tool calls trip ``stuck`` (``ext_kit.stream.StuckDetector``); an over-budget child
  trips ``over_budget`` (``ext_kit.ledger`` meter+ceiling); a tripped abort signal
  yields ``aborted``;
* ``_run_task`` RE-ROUTES a stuck child to a fresh child once (the steering dial) and
  stops after ``max_reroutes``;
* the full ``/fleet`` command: launches a bounded pool, appends one ``CostLedger``
  record per child, returns the outcome report, and emits the live dashboard as
  ``{"type":"extension","kind":"panel",…}`` records (the §6.3 CLI parity rule);
* ``/fleet_abort <id>`` / ``all`` trips the child's abort signal (the panel→extension
  steering seam) and ``/fleet_ledger`` rolls the cross-session ledger up;
* an empty ``/fleet`` reports usage (Fail-Early — no empty fleet launched);
* ``ledger_dir`` config routes the cross-session ledger to a chosen root;
* RELOAD-INVARIANCE: a fresh ``CostLedger`` over the same on-disk file reports the
  exact same fleet records (the S57 cross-session guarantee).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_ai import AbortSignal
from tau_ai.types import Model

from tau_coding_agent.session_store import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "51_delegate_fleet.py"
_spec = importlib.util.spec_from_file_location("delegate_fleet_51_example", _PATH)
assert _spec is not None and _spec.loader is not None
fleet_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fleet_mod
_spec.loader.exec_module(fleet_mod)

ledger = fleet_mod.ledger
spawn = fleet_mod.spawn


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


# ── canned child events (the shape spawn.stream_tau yields) ──────────────────


def _turn() -> dict:
    return {"type": "turn_start"}


def _tool(name: str = "read", args: dict | None = None) -> dict:
    return {"type": "tool_execution_start", "tool_name": name, "args": args or {"path": "x"}}


def _end(*, input_: int = 10, output: int = 5, total: int = 15, text: str = "ok") -> dict:
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "model": "gpt-4o",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": input_,
                "output_tokens": output,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": total,
            },
        },
    }


def _scripted_stream(scripts: list[list[dict]]) -> Any:
    """A fake ``spawn.stream_tau`` that replays one canned event list per call.

    Each ``/fleet`` child = one call → the next script. The returned async generator
    polls ``signal`` before each event (like the real ``stream_tau``) so a tripped
    abort stops it mid-stream, and is a real async generator so ``monitor_stream``'s
    ``aclose()`` (the kill) works.
    """
    calls = {"n": 0}

    async def _fake(prompt: str, *, signal: Any = None, **kwargs: Any) -> Any:
        index = calls["n"]
        calls["n"] += 1
        events = scripts[index] if index < len(scripts) else []
        for event in events:
            if signal is not None and signal.is_aborted():
                return
            yield event

    _fake.calls = calls  # type: ignore[attr-defined]
    return _fake


# ── pure pieces ──────────────────────────────────────────────────────────────


def test_parse_tasks_one_per_line() -> None:
    assert fleet_mod._parse_tasks("audit auth\n\n  find slow test  \ncheck links") == [
        "audit auth",
        "find slow test",
        "check links",
    ]
    assert fleet_mod._parse_tasks("   \n  ") == []


def test_resolve_budget_token_mode_default() -> None:
    budget = fleet_mod._resolve_budget({})
    assert budget.unit == "tokens"
    assert budget.pricing is None
    assert budget.limit == float(fleet_mod.DEFAULT_MAX_TOKENS)


def test_resolve_budget_usd_mode() -> None:
    cost = {"input": 1.0, "output": 2.0, "cache_read": 0.0}
    budget = fleet_mod._resolve_budget({"cost": cost, "max_usd": 0.5})
    assert budget.unit == "usd"
    assert budget.pricing is not None and budget.pricing.priced
    assert budget.limit == 0.5


def test_resolve_budget_cost_requires_max_usd() -> None:
    with pytest.raises(ValueError, match="no 'max_usd'"):
        fleet_mod._resolve_budget({"cost": {"input": 1.0}})


def test_resolve_budget_max_usd_requires_cost() -> None:
    with pytest.raises(ValueError, match="needs a 'cost' block"):
        fleet_mod._resolve_budget({"max_usd": 1.0})


def test_config_nonneg_int_allows_zero_rejects_negative() -> None:
    assert fleet_mod._config_nonneg_int({"max_reroutes": 0}, "max_reroutes", 1) == 0
    with pytest.raises(ValueError, match="non-negative"):
        fleet_mod._config_nonneg_int({"max_reroutes": -1}, "max_reroutes", 1)


def test_child_row_cost_cell_and_row() -> None:
    priced = fleet_mod._ChildRow(id="c-1", task="t", model="gpt-4o", cost=0.1234, turns=2)
    assert priced.cost_cell() == "$0.1234"
    unpriced = fleet_mod._ChildRow(id="c-2", task="t", model="m", tokens=1500)
    assert unpriced.cost_cell() == "1500 tok"
    assert fleet_mod._ChildRow(id="c-3", task="t", model="m").cost_cell() == "—"
    row = priced.as_row()
    assert row[0] == "c-1" and row[3] == "2" and len(row) == len(fleet_mod._PANEL_COLUMNS)


def test_child_row_truncates_long_task() -> None:
    long_task = "x" * 100
    row = fleet_mod._ChildRow(id="c-1", task=long_task, model="m").as_row()
    assert row[1].endswith("…")
    assert len(row[1]) == fleet_mod._TASK_DISPLAY_WIDTH


def test_fleet_panel_spec_is_a_valid_s68_spec_with_per_child_abort() -> None:
    from tau_agent_core.extension_types import validate_panel_spec

    state = fleet_mod._FleetState()
    state.rows["c-1"] = fleet_mod._ChildRow(id="c-1", task="a", model="m", status="running")
    state.rows["c-2"] = fleet_mod._ChildRow(id="c-2", task="b", model="m", status="done")
    state.active = True
    spec = fleet_mod._fleet_panel_spec(state)
    normalized = validate_panel_spec(spec)  # raises if malformed
    assert normalized["body"]["columns"] == list(fleet_mod._PANEL_COLUMNS)
    assert len(normalized["body"]["rows"]) == 2
    # one Abort per RUNNING child + an Abort all (the done child has no abort button).
    labels = {a["label"] for a in normalized["actions"]}
    assert labels == {"Abort c-1", "Abort all"}
    assert all(a["command"] == "fleet_abort" for a in normalized["actions"])


def test_fleet_report_counts_and_reroutes() -> None:
    state = fleet_mod._FleetState()
    state.rows["c-1"] = fleet_mod._ChildRow(id="c-1", task="a", model="m", attempt=2)
    state.rows["c-2"] = fleet_mod._ChildRow(id="c-2", task="b", model="m")
    report = fleet_mod._fleet_report(["a", "b"], ["done", "done"], state)
    assert "2 child(ren)" in report
    assert "2 done" in report
    assert "re-routed 1x" in report


# ── the streamed child driver (spawn.stream_tau faked) ───────────────────────


def _cfg(**overrides: Any) -> Any:
    return fleet_mod._resolve_config(overrides)


async def _run_child(script: list[dict], *, cfg: Any, signal: AbortSignal | None = None) -> Any:
    row = fleet_mod._ChildRow(id="c-1", task="t", model="default")
    outcome = await fleet_mod._run_child_streamed(
        row,
        signal=signal or AbortSignal(),
        cfg=cfg,
        cwd=".",
        render=lambda: None,
    )
    return outcome, row


async def test_run_child_clean_stream_is_done(monkeypatch) -> None:
    monkeypatch.setattr(
        fleet_mod.spawn, "stream_tau", _scripted_stream([[_turn(), _tool("grep"), _end()]])
    )
    outcome, row = await _run_child([], cfg=_cfg())
    assert outcome == "done"
    assert row.turns == 1
    assert row.last_tool == "grep"
    assert row.tokens == 15  # 10 input + 5 output
    assert row.model == "gpt-4o"
    assert row.final_output == "ok"


async def test_run_child_detects_stuck_loop(monkeypatch) -> None:
    # three identical consecutive tool calls → StuckDetector (default limit 3) flags.
    same = _tool("read", {"path": "same"})
    monkeypatch.setattr(
        fleet_mod.spawn, "stream_tau", _scripted_stream([[same, same, same, _end()]])
    )
    outcome, row = await _run_child([], cfg=_cfg())
    assert outcome == "stuck"


async def test_run_child_trips_over_budget(monkeypatch) -> None:
    # token-mode ceiling of 20; a completion metering 25 tokens trips it.
    monkeypatch.setattr(
        fleet_mod.spawn, "stream_tau", _scripted_stream([[_end(input_=20, output=5, total=25)]])
    )
    outcome, row = await _run_child([], cfg=_cfg(max_tokens=20))
    assert outcome == "over_budget"
    assert row.tokens == 25


async def test_run_child_honors_a_tripped_abort_signal(monkeypatch) -> None:
    monkeypatch.setattr(
        fleet_mod.spawn, "stream_tau", _scripted_stream([[_turn(), _tool(), _end()]])
    )
    signal = AbortSignal()
    signal.abort()
    outcome, _row = await _run_child([], cfg=_cfg(), signal=signal)
    assert outcome == "aborted"


async def test_run_task_reroutes_a_stuck_child_once(monkeypatch) -> None:
    same = _tool("read", {"path": "loop"})
    fake = _scripted_stream(
        [[same, same, same], [_turn(), _end()]]
    )  # attempt 1 stuck, attempt 2 done
    monkeypatch.setattr(fleet_mod.spawn, "stream_tau", fake)
    state = fleet_mod._FleetState()
    row = fleet_mod._ChildRow(id="c-1", task="t", model="default")
    state.rows["c-1"] = row
    state.signals["c-1"] = AbortSignal()
    outcome = await fleet_mod._run_task(row, state=state, cfg=_cfg(), cwd=".", render=lambda: None)
    assert outcome == "done"
    assert row.attempt == 2  # it was re-routed once
    assert fake.calls["n"] == 2  # two children spawned for the one task


async def test_run_task_stops_rerouting_after_max_reroutes(monkeypatch) -> None:
    same = _tool("read", {"path": "loop"})
    fake = _scripted_stream([[same, same, same], [same, same, same], [same, same, same]])
    monkeypatch.setattr(fleet_mod.spawn, "stream_tau", fake)
    state = fleet_mod._FleetState()
    row = fleet_mod._ChildRow(id="c-1", task="t", model="default")
    state.rows["c-1"] = row
    state.signals["c-1"] = AbortSignal()
    outcome = await fleet_mod._run_task(
        row, state=state, cfg=_cfg(max_reroutes=1), cwd=".", render=lambda: None
    )
    assert outcome == "stuck"
    assert fake.calls["n"] == 2  # original + 1 re-route, then gives up


# ── the full command flow (real AgentSession, faked subprocess) ──────────────


def _session(tmp_path: Path, monkeypatch) -> tuple[AgentSession, Session]:
    # Route the cross-session CostLedger under tmp_path via $HOME (CostLedger default root).
    monkeypatch.setenv("HOME", str(tmp_path))
    live = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    agent = AgentSession(session_log=live, model=_model(), extensions=[])
    fleet_mod.delegate_fleet_extension(agent._bind_extension_api("examples/51_delegate_fleet.py"))
    live.append_message(_msg("user", "run a fleet"))
    return agent, live


async def test_registers_all_commands(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path, monkeypatch)
    for name in ("fleet", "fleet_abort", "fleet_ledger"):
        assert agent._registry.get_command(name) is not None


async def test_fleet_launches_children_ledgers_them_and_reports(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fleet_mod.spawn,
        "stream_tau",
        _scripted_stream([[_turn(), _tool("grep"), _end()], [_turn(), _end()]]),
    )
    result = await agent.run_extension_command("fleet", "audit auth\nfind slow test")
    assert result.handled is True
    assert "2 child(ren)" in result.output
    assert "2 done" in result.output
    # the cross-session ledger recorded one line per child.
    listed = await agent.run_extension_command("fleet_ledger", "")
    assert "2 event(s)" in listed.output
    assert "done: 2 event(s)" in listed.output


async def test_fleet_emits_live_dashboard_panel_records(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path, monkeypatch)
    monkeypatch.setattr(fleet_mod.spawn, "stream_tau", _scripted_stream([[_turn(), _end()]]))
    records: list[dict[str, Any]] = []
    agent.set_extension_record_sink(records.append)

    await agent.run_extension_command("fleet", "one task")

    panels = [r for r in records if r.get("kind") == "panel"]
    assert panels, "the fleet must emit its dashboard as panel records"
    assert all(p["key"] == fleet_mod.PANEL_KEY for p in panels)
    # the final panel shows the fleet done (no more running children / abort actions).
    final = panels[-1]["spec"]
    assert "(done)" in final["title"]
    assert final["actions"] == []
    assert final["body"]["columns"] == list(fleet_mod._PANEL_COLUMNS)


async def test_empty_fleet_reports_usage(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path, monkeypatch)
    result = await agent.run_extension_command("fleet", "   \n  ")
    assert result.output == "No tasks — usage: /fleet <one task per line>."


async def test_fleet_ledger_empty_report(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path, monkeypatch)
    result = await agent.run_extension_command("fleet_ledger", "")
    assert result.output == "Fleet ledger: no children recorded yet. Run /fleet first."


# ── steering: /fleet_abort trips the child's signal ──────────────────────────


def test_fleet_abort_trips_a_running_childs_signal() -> None:
    state = fleet_mod._FleetState()
    state.rows["c-1"] = fleet_mod._ChildRow(id="c-1", task="a", model="m", status="running")
    state.rows["c-2"] = fleet_mod._ChildRow(id="c-2", task="b", model="m", status="done")
    state.signals["c-1"] = AbortSignal()
    state.signals["c-2"] = AbortSignal()
    state.active = True

    out = fleet_mod._fleet_abort_command("c-1", ctx=None, state=state)
    assert "Aborting c-1" in out
    assert state.signals["c-1"].is_aborted() is True


def test_fleet_abort_all_trips_every_running_child() -> None:
    state = fleet_mod._FleetState()
    for cid, status in (("c-1", "running"), ("c-2", "queued"), ("c-3", "done")):
        state.rows[cid] = fleet_mod._ChildRow(id=cid, task="t", model="m", status=status)
        state.signals[cid] = AbortSignal()
    state.active = True
    out = fleet_mod._fleet_abort_command("all", ctx=None, state=state)
    assert "2 running child(ren)" in out
    assert state.signals["c-1"].is_aborted() and state.signals["c-2"].is_aborted()
    assert state.signals["c-3"].is_aborted() is False  # the done child is left alone


def test_fleet_abort_without_a_running_fleet() -> None:
    state = fleet_mod._FleetState()
    assert fleet_mod._fleet_abort_command("c-1", ctx=None, state=state) == "No fleet is running."


def test_fleet_abort_unknown_child_is_reported() -> None:
    state = fleet_mod._FleetState()
    state.active = True
    state.signals["c-1"] = AbortSignal()
    out = fleet_mod._fleet_abort_command("c-9", ctx=None, state=state)
    assert "Unknown child" in out


# ── config: ledger_dir routes the cross-session ledger ───────────────────────


async def test_ledger_dir_config_routes_the_ledger(tmp_path, monkeypatch) -> None:
    ledger_root = tmp_path / "custom-state"
    live = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    agent = AgentSession(session_log=live, model=_model(), extensions=[])
    agent._extensions_config = {
        "51_delegate_fleet": {"ledger_dir": str(ledger_root), "ledger_name": "fleet-run"}
    }
    fleet_mod.delegate_fleet_extension(agent._bind_extension_api("examples/51_delegate_fleet.py"))
    live.append_message(_msg("user", "go"))
    monkeypatch.setattr(fleet_mod.spawn, "stream_tau", _scripted_stream([[_turn(), _end()]]))

    await agent.run_extension_command("fleet", "one task")

    assert (ledger_root / "fleet-run.jsonl").exists()


# ── reload-invariance (cross-session CostLedger over the same file) ──────────


async def test_fleet_ledger_survives_reload(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fleet_mod.spawn,
        "stream_tau",
        _scripted_stream([[_turn(), _end()], [_turn(), _end()]]),
    )
    await agent.run_extension_command("fleet", "task a\ntask b")

    # A brand-new CostLedger bound to the same on-disk file sees the same records.
    reopened = ledger.CostLedger("51_delegate_fleet", base_dir=tmp_path / ".tau" / "ext-state")
    records = reopened.records()
    assert len(records) == 2
    assert {r["outcome"] for r in records} == {"done"}
    assert all(r["child"] in ("c-1", "c-2") for r in records)
