"""Tests for ``examples/54_consequence_engine.py`` — the consequence engine (E11, S75).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §7 S75. No pi original (τ-native — the last
E11 dial: composition generated per task, a bounded composer-lite).

Proves:

* the pure pieces — ``parse_hypotheses`` (fenced / bare / absent / malformed JSON),
  ``_slug`` worktree naming, ``_counts`` break/hold/unscored tally, and
  ``consequence_report`` / ``_record_line`` rendering (naming what breaks);
* ``compose_hypotheses`` composes over a spawned read-only child and BOUNDS the result
  to ``max_n`` (the bound is enforced by the engine, not trusted to the child); a
  failed composer yields no hypotheses;
* ``score_hypothesis`` carries the change in a worktree and scores the consequence:
  a failed carrier / a timed-out check is UNSCORED (never fabricated), a failing check
  BREAKS, a passing check HOLDS — and the worktree is ALWAYS removed (even on error);
* ``add_worktree`` / ``remove_worktree`` create and tear down a REAL detached git
  worktree, and ``add_worktree`` RAISES outside a git work tree (Fail-Early);
* the full ``/what-if`` flow (compose + carry + gate monkeypatched, no real subprocess
  or worktree): the report names what breaks and one durable ``customEntry`` records
  the run; an empty ``<change>`` returns usage;
* ``/consequences`` lists every recorded run;
* RELOAD-INVARIANCE: a fresh AgentSession/extension over the reloaded on-disk Session
  lists the exact same what-if runs.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_ai.types import Model

from tau_coding_agent.session_store import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "54_consequence_engine.py"
_spec = importlib.util.spec_from_file_location("consequence_engine_54_example", _PATH)
assert _spec is not None and _spec.loader is not None
ce_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ce_mod
_spec.loader.exec_module(ce_mod)

spawn = ce_mod.spawn
gate = ce_mod.gate


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


def _session(tmp_path: Path) -> tuple[AgentSession, Session]:
    live = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    agent = AgentSession(session_log=live, model=_model(), extensions=[])
    ce_mod.consequence_engine_extension(
        agent._bind_extension_api("examples/54_consequence_engine.py")
    )
    # A message so the tree has a real active path for the customEntry nodes to hang off.
    live.append_message(_msg("user", "what if I drop the retry limit?"))
    return agent, live


# ── canned composer output ───────────────────────────────────────────────────

# Two consequences: one whose check BREAKS, one whose check HOLDS.
_HYPS = json.dumps(
    [
        {"consequence": "callers relying on at-least-one retry", "check": "run-check breaks"},
        {"consequence": "the config schema still validates", "check": "run-check holds"},
    ]
)

_CHANGE = "drop the retry limit to zero"


async def _fake_spawn_tau(prompt: str, **kwargs: Any) -> Any:
    """One fake for both roles: the composer emits the hypotheses, the carrier carries."""
    if "change-impact analyst" in prompt:
        return spawn.ChildResult(prompt=prompt, final_output=_HYPS, stop_reason="stop")
    # carrier child — it "carried" the change successfully.
    assert "throwaway, isolated checkout" in prompt
    return spawn.ChildResult(prompt=prompt, final_output="done", stop_reason="stop")


async def _fake_run_gate(cmd: Any, **kwargs: Any) -> Any:
    """Score a check: 'holds' commands pass, everything else fails (breaks)."""
    passed = "holds" in cmd
    return gate.GateResult(
        cmd=str(cmd),
        passed=passed,
        verdict="pass" if passed else "fail",
        exit_code=0 if passed else 1,
        stdout="",
        stderr="",
    )


def _patch_engine(monkeypatch, removed: list[str] | None = None) -> None:
    """Monkeypatch spawn/gate/worktree so /what-if runs with no real subprocess."""
    added: list[str] = []

    async def _add(repo_cwd: str, slug: str) -> str:
        path = f"/tmp/fake-wt/{slug}"
        added.append(path)
        return path

    async def _remove(repo_cwd: str, path: str) -> None:
        if removed is not None:
            removed.append(path)

    monkeypatch.setattr(ce_mod.spawn, "spawn_tau", _fake_spawn_tau)
    monkeypatch.setattr(ce_mod.gate, "run_gate", _fake_run_gate)
    monkeypatch.setattr(ce_mod, "add_worktree", _add)
    monkeypatch.setattr(ce_mod, "remove_worktree", _remove)


# ── pure pieces ──────────────────────────────────────────────────────────────


def test_parse_hypotheses_bare_array() -> None:
    out = ce_mod.parse_hypotheses(_HYPS)
    assert out == [
        ce_mod.Hypothesis("callers relying on at-least-one retry", "run-check breaks"),
        ce_mod.Hypothesis("the config schema still validates", "run-check holds"),
    ]


def test_parse_hypotheses_fenced_block_and_prose() -> None:
    text = "Here is my analysis:\n```json\n" + _HYPS + "\n```\nThat's all."
    out = ce_mod.parse_hypotheses(text)
    assert len(out) == 2
    assert out[0].consequence == "callers relying on at-least-one retry"


def test_parse_hypotheses_no_array_is_empty_not_fabricated() -> None:
    assert ce_mod.parse_hypotheses("This change has no testable consequences.") == []


def test_parse_hypotheses_skips_items_missing_consequence_or_check() -> None:
    text = json.dumps(
        [
            {"consequence": "", "check": "x"},
            {"consequence": "y", "check": ""},
            {"consequence": "z"},
            7,
            {"consequence": "kept", "check": "run"},
        ]
    )
    out = ce_mod.parse_hypotheses(text)
    assert out == [ce_mod.Hypothesis("kept", "run")]


def test_slug_is_filesystem_safe() -> None:
    assert ce_mod._slug(0, "callers relying on retry!") == "wt-0-callers-relying-on-retry"
    assert ce_mod._slug(2, "!!!") == "wt-2"


def test_counts_tally_breaks_holds_unscored() -> None:
    results = [
        {"broke": True},
        {"broke": False},
        {"broke": None},
        {"broke": True},
    ]
    assert ce_mod._counts(results) == (2, 1, 1)


def test_consequence_report_names_what_breaks() -> None:
    outcome = {
        "change": _CHANGE,
        "results": [
            {
                "consequence": "callers rely on retry",
                "check": "pytest a",
                "broke": True,
                "verdict": "breaks",
            },
            {
                "consequence": "config validates",
                "check": "check b",
                "broke": False,
                "verdict": "holds",
            },
            {
                "consequence": "metrics query",
                "check": "check c",
                "broke": None,
                "verdict": "carrier max_turns",
            },
        ],
    }
    report = ce_mod.consequence_report(outcome)
    assert f'What-if: "{_CHANGE}"' in report
    assert "BREAKS  callers rely on retry" in report
    assert "holds   config validates" in report
    assert "?       metrics query" in report
    assert "carrier max_turns — unscored" in report
    assert "Summary: 1 consequence(s) break." in report


def test_consequence_report_empty_is_honest() -> None:
    report = ce_mod.consequence_report({"change": _CHANGE, "results": []})
    assert "Composed 0 consequence(s)" in report


def test_record_line() -> None:
    record = {
        "change": _CHANGE,
        "results": [{"broke": True}, {"broke": False}, {"broke": None}],
    }
    assert ce_mod._record_line(record) == f'"{_CHANGE}" — 1 break(s), 1 hold(s), 1 unscored'


# ── the composer-lite (bounded composition per task) ─────────────────────────


async def test_compose_hypotheses_bounds_to_max_n(monkeypatch) -> None:
    monkeypatch.setattr(ce_mod.spawn, "spawn_tau", _fake_spawn_tau)
    out = await ce_mod.compose_hypotheses(
        _CHANGE, model=None, cwd=".", timeout=30.0, signal=None, max_n=1
    )
    # The composer proposed 2; the engine enforces the bound and returns exactly 1.
    assert len(out) == 1
    assert out[0].consequence == "callers relying on at-least-one retry"


async def test_compose_hypotheses_failed_composer_yields_nothing(monkeypatch) -> None:
    async def _failed(prompt: str, **kwargs: Any) -> Any:
        return spawn.ChildResult(prompt=prompt, final_output=_HYPS, stop_reason="timeout")

    monkeypatch.setattr(ce_mod.spawn, "spawn_tau", _failed)
    out = await ce_mod.compose_hypotheses(
        _CHANGE, model=None, cwd=".", timeout=30.0, signal=None, max_n=4
    )
    assert out == []


# ── carry + score one hypothesis (worktree always removed) ───────────────────


async def test_score_hypothesis_breaks_holds_and_always_removes_worktree(monkeypatch) -> None:
    removed: list[str] = []
    _patch_engine(monkeypatch, removed=removed)

    broke = await ce_mod.score_hypothesis(
        _CHANGE,
        ce_mod.Hypothesis("callers rely on retry", "run-check breaks"),
        0,
        model=None,
        repo_cwd=".",
        timeout=30.0,
        signal=None,
    )
    assert broke.carried is True
    assert broke.broke is True
    assert broke.verdict == "breaks"

    holds = await ce_mod.score_hypothesis(
        _CHANGE,
        ce_mod.Hypothesis("config validates", "run-check holds"),
        1,
        model=None,
        repo_cwd=".",
        timeout=30.0,
        signal=None,
    )
    assert holds.broke is False
    assert holds.verdict == "holds"
    # both worktrees were torn down
    assert removed == [
        "/tmp/fake-wt/wt-0-callers-rely-on-retry",
        "/tmp/fake-wt/wt-1-config-validates",
    ]


async def test_score_hypothesis_failed_carrier_is_unscored(monkeypatch) -> None:
    removed: list[str] = []

    async def _add(repo_cwd: str, slug: str) -> str:
        return f"/tmp/fake-wt/{slug}"

    async def _remove(repo_cwd: str, path: str) -> None:
        removed.append(path)

    async def _failed_carrier(prompt: str, **kwargs: Any) -> Any:
        return spawn.ChildResult(prompt=prompt, final_output="", stop_reason="max_turns")

    monkeypatch.setattr(ce_mod, "add_worktree", _add)
    monkeypatch.setattr(ce_mod, "remove_worktree", _remove)
    monkeypatch.setattr(ce_mod.spawn, "spawn_tau", _failed_carrier)

    result = await ce_mod.score_hypothesis(
        _CHANGE,
        ce_mod.Hypothesis("x", "run"),
        0,
        model=None,
        repo_cwd=".",
        timeout=30.0,
        signal=None,
    )
    assert result.carried is False
    assert result.broke is None
    assert "max_turns" in result.verdict
    assert removed  # worktree still removed even though the carrier failed


async def test_score_hypothesis_check_timeout_is_unscored(monkeypatch) -> None:
    _patch_engine(monkeypatch)

    async def _timeout_gate(cmd: Any, **kwargs: Any) -> Any:
        return gate.GateResult(
            cmd=str(cmd),
            passed=False,
            verdict="timeout",
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=True,
        )

    monkeypatch.setattr(ce_mod.gate, "run_gate", _timeout_gate)

    result = await ce_mod.score_hypothesis(
        _CHANGE,
        ce_mod.Hypothesis("x", "run"),
        0,
        model=None,
        repo_cwd=".",
        timeout=30.0,
        signal=None,
    )
    assert result.carried is True
    assert result.broke is None  # a timed-out check did not score (never fabricated)
    assert result.verdict == "check timeout"


# ── real git worktree isolation (Fail-Early outside a repo) ──────────────────


def _init_repo(repo: Path) -> None:
    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    _git("init")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (repo / "f.py").write_text("a = 1\n")
    _git("add", "f.py")
    _git("commit", "-m", "init")


async def test_add_and_remove_a_real_worktree(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    path = await ce_mod.add_worktree(str(repo), "wt-0-probe")
    try:
        assert Path(path).is_dir()
        # a real checkout of HEAD — the committed file is present in the worktree
        assert (Path(path) / "f.py").read_text() == "a = 1\n"
        listed = subprocess.run(
            ["git", "worktree", "list"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout
        assert path in listed
    finally:
        await ce_mod.remove_worktree(str(repo), path)

    assert not Path(path).exists()
    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert path not in listed


async def test_add_worktree_raises_outside_a_git_repo(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="worktree add"):
        await ce_mod.add_worktree(str(tmp_path), "wt-0")


# ── registration ─────────────────────────────────────────────────────────────


def test_registers_both_commands(tmp_path) -> None:
    agent, _live = _session(tmp_path)
    for name in ("what-if", "consequences"):
        assert agent._registry.get_command(name) is not None


# ── the full flow ────────────────────────────────────────────────────────────


async def test_what_if_names_what_breaks_and_records_it(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    _patch_engine(monkeypatch)

    result = await agent.run_extension_command("what-if", _CHANGE)
    assert result.handled is True
    text = result.output
    assert f'What-if: "{_CHANGE}"' in text
    assert "Composed 2 consequence(s); 2 worktree(s) scored." in text
    assert "BREAKS  callers relying on at-least-one retry" in text
    assert "holds   the config schema still validates" in text
    assert "Summary: 1 consequence(s) break." in text


async def test_what_if_empty_change_returns_usage(tmp_path) -> None:
    agent, _live = _session(tmp_path)
    result = await agent.run_extension_command("what-if", "   ")
    assert result.output.startswith("usage: /what-if <change>")


async def test_consequences_lists_recorded_runs(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    _patch_engine(monkeypatch)

    empty = await agent.run_extension_command("consequences", "")
    assert empty.output == "No what-ifs run yet. Ask one with /what-if <change>."

    await agent.run_extension_command("what-if", _CHANGE)
    listed = await agent.run_extension_command("consequences", "")
    assert listed.output == f'"{_CHANGE}" — 1 break(s), 1 hold(s), 0 unscored'


# ── reload-invariance ────────────────────────────────────────────────────────


async def test_recorded_runs_survive_reload(tmp_path, monkeypatch) -> None:
    agent, live = _session(tmp_path)
    _patch_engine(monkeypatch)
    await agent.run_extension_command("what-if", _CHANGE)
    session_path = live.path
    assert session_path is not None

    reloaded_log = Session.load(session_path)
    reloaded_agent = AgentSession(session_log=reloaded_log, model=_model(), extensions=[])
    ce_mod.consequence_engine_extension(
        reloaded_agent._bind_extension_api("examples/54_consequence_engine.py")
    )

    listed = await reloaded_agent.run_extension_command("consequences", "")
    assert listed.output == f'"{_CHANGE}" — 1 break(s), 1 hold(s), 0 unscored'
