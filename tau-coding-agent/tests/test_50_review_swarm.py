"""Tests for ``examples/50_review_swarm.py`` — the fan-out review swarm (E11, S71).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §7 S71. No pi original (τ-native — the
static E11 baseline the other advanced demos build on).

Proves:

* the pure pieces — ``parse_findings`` (fenced / bare / absent JSON), ``dedupe_findings``
  (a defect raised by two lenses collapses to one record, merging lenses), the
  ``_survives`` adversarial predicate, and ``_parse_keep_spec`` triage-arg parsing;
* ``recheck_finding`` composes the GATE atom over a spawned skeptic — it builds a
  read-only ``tau`` child argv and runs it through ``ext_kit.gate.run_gate`` with the
  ``_survives`` predicate;
* the full ``/review`` flow (spawn + gate monkeypatched, no real subprocess): the
  swarm dedupes across lenses, the adversarial re-check drops the refuted finding,
  and the survivors are presented in an S68 panel;
* the S68 panel is emitted as a ``{"type":"extension","kind":"panel",…}`` JSON
  record on the headless stream (the §6.3 CLI parity rule);
* an empty diff reports "nothing to review" and clears the panel (Fail-Early — no
  fabricated findings);
* ``/review_keep`` writes the selected survivors as a durable ``customEntry`` node
  (``TreeStore``, S56) and ``/findings`` lists them; ``/review_discard`` drops them;
* ``compute_diff`` reads a real ``git diff`` and RAISES outside a repo;
* RELOAD-INVARIANCE: a fresh AgentSession/extension over the reloaded on-disk
  Session reports the exact same kept findings.
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
_PATH = _REPO_ROOT / "examples" / "50_review_swarm.py"
_spec = importlib.util.spec_from_file_location("review_swarm_50_example", _PATH)
assert _spec is not None and _spec.loader is not None
review_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = review_mod
_spec.loader.exec_module(review_mod)

spawn = review_mod.spawn
gate = review_mod.gate


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
    review_mod.review_swarm_extension(agent._bind_extension_api("examples/50_review_swarm.py"))
    # A brand-new session already carries bookkeeping entries; add a message so the
    # tree has a real active path for the customEntry nodes to hang off.
    live.append_message(_msg("user", "review my changes"))
    return agent, live


# ── canned lens output + fakes (no real subprocess) ──────────────────────────

_SEC = json.dumps(
    [{"file": "auth.py", "line": 42, "severity": "high", "summary": "SQL built by string concat"}]
)
_PERF = json.dumps(
    [{"file": "loader.py", "line": 88, "severity": "medium", "summary": "unbounded retry loop"}]
)
# correctness re-flags the same loader.py:88 (dedupe target) + a unique finding.
_CORR = json.dumps(
    [
        {"file": "loader.py", "line": 88, "severity": "low", "summary": "unbounded retry loop"},
        {"file": "parser.py", "line": 5, "severity": "low", "summary": "off-by-one in range"},
    ]
)


def _lens_of(prompt: str) -> str:
    for lens in ("security", "performance", "correctness"):
        if f"ONLY on {lens} issues" in prompt:
            return lens
    raise AssertionError(f"unexpected lens prompt: {prompt[:80]!r}")


async def _fake_spawn_tau(prompt: str, **kwargs: Any) -> Any:
    lens = _lens_of(prompt)
    text = {"security": _SEC, "performance": _PERF, "correctness": _CORR}[lens]
    return spawn.ChildResult(prompt=prompt, final_output=text, stop_reason="stop")


# The adversary REFUTES the off-by-one finding; the other two survive.
_REFUTED_SUMMARIES = {"off-by-one in range"}


async def _fake_recheck(finding: dict[str, Any], diff: str, **kwargs: Any) -> Any:
    survives = finding["summary"] not in _REFUTED_SUMMARIES
    return gate.GateResult(
        cmd="adversary",
        passed=survives,
        verdict="pass" if survives else "fail",
        exit_code=0,
        stdout="",
        stderr="",
    )


async def _run_review(agent: AgentSession, monkeypatch, diff: str = "diff --git a b\n+x") -> Any:
    async def _diff(ref: str, cwd: str) -> str:
        return diff

    monkeypatch.setattr(review_mod, "compute_diff", _diff)
    monkeypatch.setattr(review_mod.spawn, "spawn_tau", _fake_spawn_tau)
    monkeypatch.setattr(review_mod, "recheck_finding", _fake_recheck)
    return await agent.run_extension_command("review", "")


# ── pure pieces ──────────────────────────────────────────────────────────────


def test_parse_findings_bare_array() -> None:
    out = review_mod.parse_findings(_SEC, "security")
    assert out == [
        {
            "lens": "security",
            "lenses": ["security"],
            "file": "auth.py",
            "line": 42,
            "severity": "high",
            "summary": "SQL built by string concat",
            "detail": "",
        }
    ]


def test_parse_findings_fenced_block_and_prose() -> None:
    text = "Here is what I found:\n```json\n" + _PERF + "\n```\nThat's all."
    out = review_mod.parse_findings(text, "performance")
    assert len(out) == 1
    assert out[0]["file"] == "loader.py"
    assert out[0]["lens"] == "performance"


def test_parse_findings_no_array_is_empty_not_fabricated() -> None:
    assert review_mod.parse_findings("No issues found in the diff.", "security") == []


def test_parse_findings_skips_malformed_items() -> None:
    text = json.dumps([{"file": "", "summary": "x"}, {"file": "a.py", "summary": ""}, 7])
    assert review_mod.parse_findings(text, "correctness") == []


def test_dedupe_merges_lenses_for_the_same_defect() -> None:
    raw = review_mod.parse_findings(_PERF, "performance") + review_mod.parse_findings(
        _CORR, "correctness"
    )
    deduped = review_mod.dedupe_findings(raw)
    by_file = {f["file"]: f for f in deduped}
    assert set(by_file) == {"loader.py", "parser.py"}
    assert by_file["loader.py"]["lenses"] == ["performance", "correctness"]
    assert by_file["parser.py"]["lenses"] == ["correctness"]


def test_survives_predicate() -> None:
    assert review_mod._survives("analysis...\nVERDICT: SURVIVES") is True
    assert review_mod._survives("analysis...\nVERDICT: REFUTED") is False
    # both present → conservative refute; neither present → inconclusive → refute.
    assert review_mod._survives("VERDICT: SURVIVES\nVERDICT: REFUTED") is False
    assert review_mod._survives("the model went off script") is False


def test_parse_keep_spec() -> None:
    assert review_mod._parse_keep_spec("all", 3) == [0, 1, 2]
    assert review_mod._parse_keep_spec("", 3) == [0, 1, 2]
    assert review_mod._parse_keep_spec("1 3", 3) == [0, 2]
    assert review_mod._parse_keep_spec("2, 2, 1", 3) == [1, 0]
    assert "out of range" in review_mod._parse_keep_spec("5", 3)
    assert "Not a finding number" in review_mod._parse_keep_spec("x", 3)


# ── the gate composition (recheck_finding over a real run_gate call) ─────────


async def test_recheck_finding_runs_a_readonly_tau_child_through_the_gate(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_run_gate(cmd, *, parse=None, cwd=None, timeout=None, env=None):
        captured["cmd"] = cmd
        captured["parse"] = parse
        return gate.GateResult(
            cmd="x", passed=True, verdict="pass", exit_code=0, stdout="", stderr=""
        )

    monkeypatch.setattr(review_mod.gate, "run_gate", _fake_run_gate)
    finding = {
        "file": "auth.py",
        "line": 42,
        "lenses": ["security"],
        "summary": "SQL by concat",
        "detail": "d",
    }
    result = await review_mod.recheck_finding(
        finding, "the diff", model=None, cwd=".", timeout=30.0
    )
    assert result.passed is True
    # It is a tau child, read-only, extension-free (the isolated-adversary shape).
    argv = captured["cmd"]
    assert "tau_coding_agent.cli" in argv
    assert "--no-extensions" in argv
    tools_idx = argv.index("--tools")
    assert argv[tools_idx + 1] == "read,ls,grep,find"
    # The gate keeps a finding iff the adversary could not refute it.
    assert captured["parse"]("VERDICT: SURVIVES") is True
    assert captured["parse"]("VERDICT: REFUTED") is False


# ── registration ─────────────────────────────────────────────────────────────


def test_registers_all_commands(tmp_path) -> None:
    agent, _live = _session(tmp_path)
    for name in ("review", "review_keep", "review_discard", "findings"):
        assert agent._registry.get_command(name) is not None


# ── the full flow ────────────────────────────────────────────────────────────


async def test_review_dedupes_rechecks_and_presents_survivors(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    result = await _run_review(agent, monkeypatch)
    assert result.handled is True
    text = result.output
    # 3 lenses, 3 deduped findings, 2 survive (the off-by-one is refuted).
    assert "3 lens(es), 3 raw finding(s) -> 2 survived" in text
    assert "auth.py:42" in text
    assert "loader.py:88" in text
    assert "parser.py:5" not in text  # dropped on adversarial re-check
    # the merged-lens survivor renders both lenses
    assert "performance+correctness" in text


async def test_review_emits_panel_record_on_headless_stream(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    records: list[dict[str, Any]] = []
    agent.set_extension_record_sink(records.append)

    await _run_review(agent, monkeypatch)

    panels = [r for r in records if r.get("kind") == "panel"]
    assert len(panels) == 1
    panel = panels[0]
    assert panel["key"] == review_mod.PANEL_KEY
    spec = panel["spec"]
    assert "2 finding(s) to triage" in spec["title"]
    assert spec["body"]["kind"] == "table"
    assert spec["body"]["columns"] == ["#", "lens", "sev", "file:line", "summary"]
    assert len(spec["body"]["rows"]) == 2
    # the panel actions dispatch back into this extension as command calls
    commands = {a["command"] for a in spec["actions"]}
    assert commands == {"review_keep", "review_discard"}


async def test_empty_diff_reports_nothing_and_clears_panel(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    records: list[dict[str, Any]] = []
    agent.set_extension_record_sink(records.append)
    result = await _run_review(agent, monkeypatch, diff="   \n")
    assert result.output == "Nothing to review — no diff against HEAD."
    # cleared panel → a panel record with spec None
    assert any(r.get("kind") == "panel" and r.get("spec") is None for r in records)


async def test_review_keep_writes_durable_findings_and_findings_lists_them(
    tmp_path, monkeypatch
) -> None:
    agent, _live = _session(tmp_path)
    await _run_review(agent, monkeypatch)

    kept = await agent.run_extension_command("review_keep", "1")
    assert kept.output == "Kept 1 finding(s) to the session findings ledger."

    listed = await agent.run_extension_command("findings", "")
    assert listed.output == "[security] high  auth.py:42  SQL built by string concat"


async def test_review_keep_all_writes_every_survivor(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    await _run_review(agent, monkeypatch)
    kept = await agent.run_extension_command("review_keep", "all")
    assert kept.output == "Kept 2 finding(s) to the session findings ledger."
    listed = await agent.run_extension_command("findings", "")
    assert "auth.py:42" in listed.output
    assert "loader.py:88" in listed.output


async def test_review_keep_without_pending_reports(tmp_path) -> None:
    agent, _live = _session(tmp_path)
    result = await agent.run_extension_command("review_keep", "all")
    assert result.output == "No pending review to keep from. Run /review first."


async def test_review_discard_drops_pending(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    await _run_review(agent, monkeypatch)
    discarded = await agent.run_extension_command("review_discard", "")
    assert discarded.output == "Discarded 2 pending finding(s)."
    # after a discard there is nothing kept
    listed = await agent.run_extension_command("findings", "")
    assert listed.output == "No findings kept yet. Run /review, then /review_keep."


async def test_findings_empty_report(tmp_path) -> None:
    agent, _live = _session(tmp_path)
    result = await agent.run_extension_command("findings", "")
    assert result.output == "No findings kept yet. Run /review, then /review_keep."


# ── real git diff (Fail-Early outside a repo; real diff inside one) ──────────


async def test_compute_diff_raises_outside_a_git_repo(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="git diff"):
        await review_mod.compute_diff("HEAD", str(tmp_path))


async def test_compute_diff_reads_a_real_working_tree_diff(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    _git("init")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (repo / "f.py").write_text("a = 1\n")
    _git("add", "f.py")
    _git("commit", "-m", "init")
    (repo / "f.py").write_text("a = 2\n")

    diff = await review_mod.compute_diff("HEAD", str(repo))
    assert "-a = 1" in diff
    assert "+a = 2" in diff


# ── reload-invariance ────────────────────────────────────────────────────────


async def test_kept_findings_survive_reload(tmp_path, monkeypatch) -> None:
    agent, live = _session(tmp_path)
    await _run_review(agent, monkeypatch)
    await agent.run_extension_command("review_keep", "all")
    session_path = live.path
    assert session_path is not None

    reloaded_log = Session.load(session_path)
    reloaded_agent = AgentSession(session_log=reloaded_log, model=_model(), extensions=[])
    review_mod.review_swarm_extension(
        reloaded_agent._bind_extension_api("examples/50_review_swarm.py")
    )

    listed = await reloaded_agent.run_extension_command("findings", "")
    assert "auth.py:42" in listed.output
    assert "loader.py:88" in listed.output
