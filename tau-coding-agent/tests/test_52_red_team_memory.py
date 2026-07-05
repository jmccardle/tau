"""Tests for ``examples/52_red_team_memory.py`` — the red-team-memory swarm (E11, S73).

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §7 S73. No pi original (τ-native — the S71
``50_review_swarm`` baseline plus one dial: the backplane accretes across sessions).

Proves:

* the pure corpus algebra — ``accrete_corpus`` (dedupe by S71 signature, a re-confirmed
  finding bumps ``keeps`` instead of duplicating), ``corpus_seed_lines`` (ranked by
  ``keeps`` + capped), and ``load_corpus`` (Fail-Early on a corrupt / non-list blob);
* ``_adversary_prompt`` seeds the skeptic with the corpus when it is non-empty and is
  the baseline brief when it is empty;
* ``recheck_finding`` composes the GATE atom over a spawned, read-only ``tau`` child and
  carries the RED-TEAM MEMORY seed into the child's prompt;
* the full ``/review`` flow (spawn + re-check monkeypatched, no real subprocess): the
  swarm dedupes, drops the refuted finding, and presents survivors in an S68 panel;
* ``/review_keep`` writes the durable conversation ledger (S71) AND promotes the kept
  findings into the cross-session corpus; ``/red_team`` lists that corpus;
* a re-keep of the same finding bumps its ``keeps`` counter, not the corpus length;
* CROSS-SESSION accrual: a fresh AgentSession/extension over the SAME corpus dir sees
  the finding a prior session confirmed, and a new ``/review`` seeds its adversaries
  from it;
* RELOAD-INVARIANCE (S71 conversation ledger): a fresh session over the reloaded
  on-disk Session reports the same kept findings.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from tau_agent_core.agent_session import AgentSession
from tau_ai.types import Model

from tau_coding_agent.session_store import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH = _REPO_ROOT / "examples" / "52_red_team_memory.py"
_spec = importlib.util.spec_from_file_location("red_team_memory_52_example", _PATH)
assert _spec is not None and _spec.loader is not None
rt_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rt_mod
_spec.loader.exec_module(rt_mod)

spawn = rt_mod.spawn
gate = rt_mod.gate
review = rt_mod.review

_STEM = "52_red_team_memory"


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


def _session(tmp_path: Path, corpus_dir: Path | None = None) -> tuple[AgentSession, Session]:
    corpus_dir = corpus_dir if corpus_dir is not None else tmp_path / "corpus"
    live = Session.create("/tmp", "gpt-4o", "openai", base_dir=tmp_path)
    agent = AgentSession(session_log=live, model=_model(), extensions=[])
    agent._extensions_config = {_STEM: {"corpus_dir": str(corpus_dir)}}
    rt_mod.red_team_memory_extension(agent._bind_extension_api("examples/52_red_team_memory.py"))
    live.append_message(_msg("user", "review my changes"))
    return agent, live


# ── canned lens output + fakes (no real subprocess) ──────────────────────────

_SEC = json.dumps(
    [{"file": "auth.py", "line": 42, "severity": "high", "summary": "SQL built by string concat"}]
)
_PERF = json.dumps(
    [{"file": "loader.py", "line": 88, "severity": "medium", "summary": "unbounded retry loop"}]
)
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

    monkeypatch.setattr(rt_mod.review, "compute_diff", _diff)
    monkeypatch.setattr(rt_mod.spawn, "spawn_tau", _fake_spawn_tau)
    monkeypatch.setattr(rt_mod, "recheck_finding", _fake_recheck)
    return await agent.run_extension_command("review", "")


def _finding(summary: str = "SQL built by string concat", **over: Any) -> dict[str, Any]:
    base = {
        "file": "auth.py",
        "line": 42,
        "severity": "high",
        "lenses": ["security"],
        "summary": summary,
        "detail": "concat",
    }
    base.update(over)
    return base


# ── pure corpus algebra ──────────────────────────────────────────────────────


def test_accrete_corpus_adds_new_and_bumps_keeps_on_reconfirm() -> None:
    corpus, added = rt_mod.accrete_corpus([], [_finding()])
    assert added == 1
    assert len(corpus) == 1
    assert corpus[0]["keeps"] == 1

    # re-confirm the SAME finding (same signature) → no new record, keeps bumped
    corpus2, added2 = rt_mod.accrete_corpus(corpus, [_finding()])
    assert added2 == 0
    assert len(corpus2) == 1
    assert corpus2[0]["keeps"] == 2


def test_accrete_corpus_does_not_mutate_input() -> None:
    corpus = [rt_mod.corpus_record(_finding())]
    before = json.dumps(corpus, sort_keys=True)
    rt_mod.accrete_corpus(corpus, [_finding()])
    assert json.dumps(corpus, sort_keys=True) == before


def test_corpus_seed_lines_ranks_by_keeps_and_caps() -> None:
    corpus = [
        rt_mod.corpus_record(_finding("a", file="a.py", line=1)),
        rt_mod.corpus_record(_finding("b", file="b.py", line=2)),
    ]
    corpus[1]["keeps"] = 5  # b is more re-confirmed → ranked first
    lines = rt_mod.corpus_seed_lines(corpus, limit=1)
    assert len(lines) == 1
    assert "b.py:2" in lines[0]


def test_load_corpus_first_run_is_empty() -> None:
    store = rt_mod.FileStore("nope", base_dir=str(Path("/tmp/does-not-exist-red-team")))
    assert rt_mod.load_corpus(store) == []


def test_load_corpus_raises_on_corrupt_blob(tmp_path) -> None:
    store = rt_mod.FileStore("corpus", base_dir=str(tmp_path))
    store.save({"not": "a list"})
    try:
        rt_mod.load_corpus(store)
    except ValueError as exc:
        assert "not a list" in str(exc)
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("load_corpus must raise on a non-list blob")


# ── the corpus-seeded adversary ──────────────────────────────────────────────


def test_adversary_prompt_seeds_memory_when_corpus_present() -> None:
    corpus = [rt_mod.corpus_record(_finding("prior confirmed defect", file="old.py", line=9))]
    prompt = rt_mod._adversary_prompt(_finding(), "the diff", corpus)
    assert "RED-TEAM MEMORY" in prompt
    assert "old.py:9: prior confirmed defect" in prompt
    # empty corpus → the baseline brief, no memory block
    plain = rt_mod._adversary_prompt(_finding(), "the diff", [])
    assert "RED-TEAM MEMORY" not in plain
    assert rt_mod._VERDICT_SURVIVES in plain


async def test_recheck_finding_runs_a_readonly_child_and_carries_the_seed(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_run_gate(cmd, *, parse=None, cwd=None, timeout=None, env=None):
        captured["cmd"] = cmd
        captured["parse"] = parse
        return gate.GateResult(
            cmd="x", passed=True, verdict="pass", exit_code=0, stdout="", stderr=""
        )

    monkeypatch.setattr(rt_mod.gate, "run_gate", _fake_run_gate)
    corpus = [rt_mod.corpus_record(_finding("prior confirmed defect", file="old.py", line=9))]
    result = await rt_mod.recheck_finding(
        _finding(), "the diff", model=None, cwd=".", timeout=30.0, corpus=corpus
    )
    assert result.passed is True
    argv = captured["cmd"]
    assert "tau_coding_agent.cli" in argv
    assert "--no-extensions" in argv
    tools_idx = argv.index("--tools")
    assert argv[tools_idx + 1] == "read,ls,grep,find"
    # the seed reached the child's prompt (the last positional arg)
    assert "RED-TEAM MEMORY" in argv[-1]
    # the gate speaks the shared S71 verdict protocol
    assert captured["parse"]("VERDICT: SURVIVES") is True
    assert captured["parse"]("VERDICT: REFUTED") is False


# ── registration ─────────────────────────────────────────────────────────────


def test_registers_all_commands(tmp_path) -> None:
    agent, _live = _session(tmp_path)
    for name in ("review", "review_keep", "review_discard", "findings", "red_team"):
        assert agent._registry.get_command(name) is not None


# ── the full flow ────────────────────────────────────────────────────────────


async def test_review_dedupes_rechecks_and_presents_survivors(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    result = await _run_review(agent, monkeypatch)
    assert result.handled is True
    text = result.output
    assert "3 lens(es), 3 raw finding(s) -> 2 survived" in text
    assert "auth.py:42" in text
    assert "loader.py:88" in text
    assert "parser.py:5" not in text  # dropped on adversarial re-check
    assert "performance+correctness" in text
    # first run: empty corpus → no "seeded from" note
    assert "seeded from" not in text


async def test_review_emits_panel_record_on_headless_stream(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    records: list[dict[str, Any]] = []
    agent.set_extension_record_sink(records.append)

    await _run_review(agent, monkeypatch)

    panels = [r for r in records if r.get("kind") == "panel"]
    assert len(panels) == 1
    assert panels[0]["key"] == rt_mod.PANEL_KEY
    spec = panels[0]["spec"]
    assert "2 finding(s) to triage" in spec["title"]
    assert len(spec["body"]["rows"]) == 2
    commands = {a["command"] for a in spec["actions"]}
    assert commands == {"review_keep", "review_discard"}


async def test_empty_diff_reports_nothing_and_clears_panel(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    records: list[dict[str, Any]] = []
    agent.set_extension_record_sink(records.append)
    result = await _run_review(agent, monkeypatch, diff="   \n")
    assert result.output == "Nothing to review — no diff against HEAD."
    assert any(r.get("kind") == "panel" and r.get("spec") is None for r in records)


# ── the S73 dial: keep promotes to the cross-session corpus ──────────────────


async def test_review_keep_promotes_to_corpus_and_red_team_lists_it(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    await _run_review(agent, monkeypatch)

    kept = await agent.run_extension_command("review_keep", "all")
    assert kept.output == (
        "Kept 2 finding(s) to the session findings ledger. (2 new to red-team memory)"
    )

    listed = await agent.run_extension_command("red_team", "")
    assert "2 confirmed finding(s) across sessions" in listed.output
    assert "auth.py:42" in listed.output
    assert "loader.py:88" in listed.output
    assert "(x1)" in listed.output

    # the conversation ledger (S71) still lists them too
    findings = await agent.run_extension_command("findings", "")
    assert "auth.py:42" in findings.output


async def test_discard_does_not_promote(tmp_path, monkeypatch) -> None:
    agent, _live = _session(tmp_path)
    await _run_review(agent, monkeypatch)
    await agent.run_extension_command("review_discard", "")
    listed = await agent.run_extension_command("red_team", "")
    assert listed.output.startswith("Red-team memory is empty")


async def test_red_team_empty_report(tmp_path) -> None:
    agent, _live = _session(tmp_path)
    result = await agent.run_extension_command("red_team", "")
    assert result.output.startswith("Red-team memory is empty")


async def test_review_keep_without_pending_reports(tmp_path) -> None:
    agent, _live = _session(tmp_path)
    result = await agent.run_extension_command("review_keep", "all")
    assert result.output == "No pending review to keep from. Run /review first."


# ── cross-session accrual (the FileStore backplane outlives one session) ─────


async def test_corpus_accretes_across_sessions_and_seeds_the_adversaries(
    tmp_path, monkeypatch
) -> None:
    corpus_dir = tmp_path / "shared-corpus"

    # Session A: confirm two findings → they land in the shared corpus.
    agent_a, _a = _session(tmp_path / "a", corpus_dir=corpus_dir)
    await _run_review(agent_a, monkeypatch)
    await agent_a.run_extension_command("review_keep", "all")

    # Session B: a brand-new session over the SAME corpus dir already remembers them.
    agent_b, _b = _session(tmp_path / "b", corpus_dir=corpus_dir)
    listed = await agent_b.run_extension_command("red_team", "")
    assert "2 confirmed finding(s) across sessions" in listed.output

    # ...and B's next /review seeds its adversaries from that memory.
    result = await _run_review(agent_b, monkeypatch)
    assert "seeded from 2 remembered finding(s)" in result.output


async def test_reconfirm_across_sessions_bumps_keeps_not_length(tmp_path, monkeypatch) -> None:
    corpus_dir = tmp_path / "shared-corpus"

    agent_a, _a = _session(tmp_path / "a", corpus_dir=corpus_dir)
    await _run_review(agent_a, monkeypatch)
    await agent_a.run_extension_command("review_keep", "all")

    # Session B re-confirms the same findings → keeps bump, corpus length unchanged.
    agent_b, _b = _session(tmp_path / "b", corpus_dir=corpus_dir)
    await _run_review(agent_b, monkeypatch)
    kept = await agent_b.run_extension_command("review_keep", "all")
    assert kept.output == (
        "Kept 2 finding(s) to the session findings ledger. (all already in red-team memory)"
    )
    listed = await agent_b.run_extension_command("red_team", "")
    assert "2 confirmed finding(s) across sessions" in listed.output
    assert "(x2)" in listed.output


# ── reload-invariance (S71 conversation ledger survives a reload) ────────────


async def test_kept_findings_survive_reload(tmp_path, monkeypatch) -> None:
    agent, live = _session(tmp_path)
    await _run_review(agent, monkeypatch)
    await agent.run_extension_command("review_keep", "all")
    session_path = live.path
    assert session_path is not None

    reloaded_log = Session.load(session_path)
    reloaded_agent = AgentSession(session_log=reloaded_log, model=_model(), extensions=[])
    reloaded_agent._extensions_config = {_STEM: {"corpus_dir": str(tmp_path / "corpus")}}
    rt_mod.red_team_memory_extension(
        reloaded_agent._bind_extension_api("examples/52_red_team_memory.py")
    )

    listed = await reloaded_agent.run_extension_command("findings", "")
    assert "auth.py:42" in listed.output
    assert "loader.py:88" in listed.output
