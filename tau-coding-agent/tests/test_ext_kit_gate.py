"""Tests for ``examples/ext_kit/gate.py`` — the S55 *gate* primitive.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §4 S55.

Three layers:

* **run_gate** — real (trivial) shell/argv commands: exit-code verdict, regex
  verdict + match extraction, predicate verdict, the honest ``timeout`` verdict,
  and the Fail-Early ``TypeError`` on a bad ``parse``.
* **verdict_node** — pure: the ``{customType, content, display, details}`` payload
  shape, plus a RELOAD-INVARIANCE proof that the payload, once handed to a real
  ``api.send_message``, persists as a ``customMessage`` node and survives a reload.
* **revert_and_recheck** — against a real temporary git repo: the anti-cheat cycle
  (stash → recheck → restore), the ``cheated`` tell, the no-changes path, the
  working-tree-restore invariant, and the not-a-git-repo Fail-Early raise.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.conversation_tree import ConversationTree
from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.types import Model

# ── import the kit as a top-level package (examples/ on the path) ────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = str(_REPO_ROOT / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

from ext_kit import gate  # noqa: E402  (path insertion must precede the import)


# ── run_gate: exit-code verdict ──────────────────────────────────────────────


async def test_run_gate_exit_zero_passes():
    result = await gate.run_gate("exit 0")
    assert result.passed is True
    assert result.verdict == gate.VERDICT_PASS
    assert result.exit_code == 0
    assert result.timed_out is False


async def test_run_gate_nonzero_exit_fails():
    result = await gate.run_gate("exit 3")
    assert result.passed is False
    assert result.verdict == gate.VERDICT_FAIL
    assert result.exit_code == 3


async def test_run_gate_argv_sequence_no_shell():
    # A sequence runs directly (no shell); the display cmd is the joined argv.
    result = await gate.run_gate(["printf", "hello"])
    assert result.passed is True
    assert result.stdout == "hello"
    assert result.cmd == "printf hello"


async def test_run_gate_captures_stdout_and_stderr():
    result = await gate.run_gate("echo out; echo err 1>&2")
    assert "out" in result.stdout
    assert "err" in result.stderr
    assert "out" in result.output()
    assert "err" in result.output()


# ── run_gate: regex verdict ──────────────────────────────────────────────────


async def test_run_gate_regex_match_passes_and_extracts():
    # Exit code is 0 but the verdict is decided by the regex; a group is captured.
    result = await gate.run_gate("echo 'ran 42 tests, all passed'", parse=r"all (\w+)")
    assert result.passed is True
    assert result.matched == "passed"


async def test_run_gate_regex_no_match_fails_even_on_exit_zero():
    result = await gate.run_gate("echo 'nothing to see'", parse=r"FAILED")
    assert result.passed is False
    assert result.matched is None
    # The command itself exited 0, but the regex verdict is what counts.
    assert result.exit_code == 0


async def test_run_gate_regex_whole_match_when_no_group():
    result = await gate.run_gate("echo 'BUILD OK'", parse=r"OK")
    assert result.passed is True
    assert result.matched == "OK"


# ── run_gate: predicate verdict ──────────────────────────────────────────────


async def test_run_gate_predicate_verdict():
    result = await gate.run_gate(
        "echo 'coverage: 91%'", parse=lambda out: "coverage" in out and "91" in out
    )
    assert result.passed is True
    assert result.matched is None

    result2 = await gate.run_gate("echo 'coverage: 40%'", parse=lambda out: "91" in out)
    assert result2.passed is False


# ── run_gate: timeout + Fail-Early ───────────────────────────────────────────


async def test_run_gate_timeout_is_an_honest_fail():
    result = await gate.run_gate("sleep 5", timeout=0.1)
    assert result.timed_out is True
    assert result.passed is False
    assert result.verdict == gate.VERDICT_TIMEOUT
    assert result.exit_code is None


async def test_run_gate_bad_parse_type_raises():
    # Fail-Early: an unusable parse type is not coerced into a fabricated verdict.
    with pytest.raises(TypeError, match="parse"):
        await gate.run_gate("exit 0", parse=123)  # type: ignore[arg-type]


# ── verdict_node: pure payload shape ─────────────────────────────────────────


def _pass_result() -> gate.GateResult:
    return gate.GateResult(
        cmd="pytest -q",
        passed=True,
        verdict=gate.VERDICT_PASS,
        exit_code=0,
        stdout="ok",
        stderr="",
    )


def _fail_result() -> gate.GateResult:
    return gate.GateResult(
        cmd="pytest -q",
        passed=False,
        verdict=gate.VERDICT_FAIL,
        exit_code=1,
        stdout="",
        stderr="1 failed",
    )


def test_verdict_node_shape_for_send_message():
    node = gate.verdict_node(_pass_result(), label="tests")
    assert node["customType"] == gate.DEFAULT_VERDICT_TYPE
    assert node["display"] is True
    assert "tests" in node["content"]
    assert "PASS" in node["content"]
    # The structured verdict rides in details.
    assert node["details"]["passed"] is True
    assert node["details"]["exit_code"] == 0


def test_verdict_node_fail_reports_verdict_and_exit():
    node = gate.verdict_node(_fail_result())
    assert "FAIL" in node["content"]
    assert "exit=1" in node["content"]
    assert node["details"]["passed"] is False


def test_verdict_node_custom_type_override():
    node = gate.verdict_node(_pass_result(), custom_type="ci_gate")
    assert node["customType"] == "ci_gate"


# ── verdict_node → send_message: RELOAD-INVARIANCE ───────────────────────────


def _make_session() -> AgentSession:
    model = Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )
    return AgentSession(session_log=InMemorySessionLog(), model=model, extensions=[])


def _text_blob(messages: list) -> str:
    out: list[str] = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, list):
            for block in content:
                out.append(str(block.get("text", "")) if isinstance(block, dict) else "")
        elif isinstance(content, str):
            out.append(content)
    return "\n".join(out)


def test_verdict_node_persists_and_survives_reload():
    """A verdict_node handed to api.send_message lands durably and reloads."""
    session = _make_session()
    api = ExtensionAPI(session=session)

    result = gate.GateResult(
        cmd="ruff check .",
        passed=False,
        verdict=gate.VERDICT_FAIL,
        exit_code=1,
        stdout="",
        stderr="E501 line too long",
    )
    api.send_message(gate.verdict_node(result, label="lint"))

    # Persisted as a customMessage node on the active path.
    entries = session._session_log.entries()
    custom = [e for e in entries if e.get("type") == "customMessage"]
    assert len(custom) == 1
    assert custom[0]["customType"] == gate.DEFAULT_VERDICT_TYPE

    # Reload-invariance: a fresh fold over the persisted entries still carries it.
    reloaded = ConversationTree(entries, session._session_log.cursor)
    text = _text_blob(reloaded.context_for())
    assert "lint" in text
    assert "FAIL" in text


# ── revert_and_recheck: real git repo ────────────────────────────────────────


def _git(args: list[str], *, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
            "PATH": __import__("os").environ.get("PATH", ""),
        },
    )


@pytest.fixture
def git_repo(tmp_path):
    """A temp git repo with one committed file whose content == 'HEAD'."""
    (tmp_path / "target.txt").write_text("HEAD\n")
    _git(["init", "-q"], cwd=tmp_path)
    _git(["add", "target.txt"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


async def test_revert_and_recheck_reverts_then_restores(git_repo):
    # Agent edits the committed file so it contains the token WORKING.
    target = git_repo / "target.txt"
    target.write_text("WORKING token\n")

    async def probe() -> gate.GateResult:
        # Passes iff the file (as it is on disk *right now*) contains WORKING.
        return await gate.run_gate(["grep", "-q", "WORKING", "target.txt"], cwd=str(git_repo))

    # Baseline (with the edit) passes; the recheck (edit reverted to HEAD) fails.
    baseline = await probe()
    assert baseline.passed is True

    recheck = await gate.revert_and_recheck(["target.txt"], probe, cwd=str(git_repo))
    assert recheck.reverted_paths == ["target.txt"]
    assert recheck.result.passed is False
    # The load-bearing-edit tell fires.
    assert recheck.cheated(baseline) is True

    # Working tree is restored byte-for-byte (the git analog of reload-invariance).
    assert target.read_text() == "WORKING token\n"


async def test_revert_and_recheck_no_changes_runs_against_current_tree(git_repo):
    # No edits to target.txt → nothing to revert; recheck runs against HEAD as-is.
    async def probe() -> gate.GateResult:
        return await gate.run_gate(["grep", "-q", "HEAD", "target.txt"], cwd=str(git_repo))

    baseline = await probe()
    recheck = await gate.revert_and_recheck(["target.txt"], probe, cwd=str(git_repo))
    assert recheck.reverted_paths == []
    assert recheck.result.passed is True
    # With nothing reverted there is no load-bearing edit → never a cheat.
    assert recheck.cheated(baseline) is False


async def test_revert_and_recheck_restores_even_if_gate_raises(git_repo):
    target = git_repo / "target.txt"
    target.write_text("WORKING\n")

    async def boom() -> gate.GateResult:
        raise RuntimeError("gate blew up")

    with pytest.raises(RuntimeError, match="gate blew up"):
        await gate.revert_and_recheck(["target.txt"], boom, cwd=str(git_repo))

    # The stash is restored despite the gate raising.
    assert target.read_text() == "WORKING\n"


async def test_revert_and_recheck_outside_git_repo_raises(tmp_path):
    async def probe() -> gate.GateResult:
        return await gate.run_gate("exit 0", cwd=str(tmp_path))

    with pytest.raises(RuntimeError, match="not inside a git work tree"):
        await gate.revert_and_recheck(["x.txt"], probe, cwd=str(tmp_path))


async def test_revert_and_recheck_empty_paths_raises(git_repo):
    async def probe() -> gate.GateResult:
        return await gate.run_gate("exit 0", cwd=str(git_repo))

    with pytest.raises(ValueError, match="empty"):
        await gate.revert_and_recheck([], probe, cwd=str(git_repo))
