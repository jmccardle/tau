"""Project context-file discovery (docs/PLAN-0.9.3.md §1).

A port of pi's ``loadProjectContextFiles`` (``resource-loader.ts:119`` at pi
``5cd93f688``), so these tests are written against pi's rules: agent dir first,
then every ancestor of cwd root-most first, at most one file per directory,
deduplicated by resolved path, with a nested worktree's context file shadowing
the main repository's.

**Every test pins both ``cwd`` and ``agent_dir``.** Discovery walks to the
filesystem root by design, so a test that let it default would read the
developer's real ``~/AGENTS.md`` and ``~/.tau`` — and pass or fail depending on
whose machine it ran on. For the same reason the assertions filter the result to
paths *inside* ``tmp_path``: a stray ``/AGENTS.md`` on the host is legitimately
discovered and must not break the suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tau_agent_core.sdk import (
    BASE_SYSTEM_PROMPT,
    ContextFileError,
    _build_system_prompt,
    load_project_context_files,
)


#: The base prompt's first paragraph, which carries no ``{{field}}`` slots.
#: ``BASE_SYSTEM_PROMPT`` itself is a TEMPLATE — its ``{{cwd}}``/``{{model}}``
#: slots are filled at build time — so the raw constant never appears in a
#: finished prompt, and asserting against it would only test that templating
#: happened. Derived from the constant rather than pasted, so it follows any
#: rewording of τ's voice.
_BASE_OPENING = BASE_SYSTEM_PROMPT.split("\n\n", 1)[0]


def _discovered_under(cwd: Path, agent_dir: Path, root: Path):
    """Discover from ``cwd``, keeping only files inside ``root`` (module docstring)."""
    resolved_root = root.resolve()
    return [
        cf
        for cf in load_project_context_files(cwd, agent_dir=agent_dir)
        if cf.path.is_relative_to(resolved_root)
    ]


def _local(cwd: Path, agent_dir: Path, root: Path) -> list[Path]:
    return [cf.path for cf in _discovered_under(cwd, agent_dir, root)]


@pytest.fixture
def empty_agent_dir(tmp_path: Path) -> Path:
    """An agent dir with no context file, so only the cwd walk contributes."""
    agent_dir = tmp_path / "agent-home" / ".tau"
    agent_dir.mkdir(parents=True)
    return agent_dir


# ─── The ancestor walk ────────────────────────────────────────────────


def test_ancestor_agents_md_is_found_from_a_subdirectory(tmp_path, empty_agent_dir):
    """The headline gap: τ read cwd ONLY, so a repo's AGENTS.md vanished the
    moment the agent was started from ``src/`` instead of the repo root."""
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("repo rules", encoding="utf-8")

    found = _local(repo / "src" / "pkg", empty_agent_dir, tmp_path)

    assert found == [repo / "AGENTS.md"]


def test_nearest_context_file_is_read_last(tmp_path, empty_agent_dir):
    """Root-most first, nearest last — the nearest file gets the last word."""
    outer = tmp_path / "mono"
    inner = outer / "packages" / "web"
    inner.mkdir(parents=True)
    (outer / "AGENTS.md").write_text("monorepo", encoding="utf-8")
    (inner / "AGENTS.md").write_text("package", encoding="utf-8")

    found = _local(inner, empty_agent_dir, tmp_path)

    assert found == [outer / "AGENTS.md", inner / "AGENTS.md"]


def test_claude_md_is_discovered(tmp_path, empty_agent_dir):
    """CLAUDE.md was not a name τ knew at all before this."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("claude rules", encoding="utf-8")

    found = _local(repo, empty_agent_dir, tmp_path)

    assert found == [repo / "CLAUDE.md"]


def test_a_directory_contributes_at_most_one_file(tmp_path, empty_agent_dir):
    """First match in the candidate order wins; the rest are never read."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("agents", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("claude", encoding="utf-8")

    found = _local(repo, empty_agent_dir, tmp_path)

    assert found == [repo / "AGENTS.md"]


def test_override_file_wins_over_the_checked_in_one(tmp_path, empty_agent_dir):
    """AGENTS.override.md is first in the candidate order for exactly this."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.override.md").write_text("mine", encoding="utf-8")
    (repo / "AGENTS.md").write_text("theirs", encoding="utf-8")

    files = _discovered_under(repo, empty_agent_dir, tmp_path)

    assert [f.path for f in files] == [repo / "AGENTS.override.md"]
    assert files[-1].content == "mine"


def test_a_directory_named_like_a_context_file_is_skipped(tmp_path, empty_agent_dir):
    """``CLAUDE.md/`` is not a file; the search continues rather than aborting."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.override.md").mkdir()
    (repo / "AGENTS.md").write_text("real", encoding="utf-8")

    found = _local(repo, empty_agent_dir, tmp_path)

    assert found == [repo / "AGENTS.md"]


# ─── The agent dir and deduplication ──────────────────────────────────


def test_agent_dir_context_file_comes_first(tmp_path):
    """~/.tau's file is the global one, so it is the weakest and reads first."""
    agent_dir = tmp_path / "agent-home" / ".tau"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENTS.md").write_text("global", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("project", encoding="utf-8")

    found = _local(repo, agent_dir, tmp_path)

    assert found == [agent_dir / "AGENTS.md", repo / "AGENTS.md"]


def test_dedup_does_not_double_load_the_agent_dir_file(tmp_path):
    """Running τ from inside ~/.tau itself must not apply the same file twice."""
    agent_dir = tmp_path / "agent-home" / ".tau"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENTS.md").write_text("global", encoding="utf-8")

    found = _local(agent_dir, agent_dir, tmp_path)

    assert found == [agent_dir / "AGENTS.md"]


def test_dedup_survives_a_symlinked_cwd(tmp_path, empty_agent_dir):
    """Dedup is by RESOLVED path, so a symlinked route to a directory already
    walked does not read its context file a second time."""
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("repo", encoding="utf-8")
    link = repo / "sub" / "self"
    link.symlink_to(repo)

    found = _local(link / "sub", empty_agent_dir, tmp_path)

    assert found == [repo / "AGENTS.md"]


# ─── τ's own .tau/SYSTEM.md slot ──────────────────────────────────────


def test_tau_system_md_is_still_read_and_reads_last(tmp_path, empty_agent_dir):
    """τ-only addition, kept out of the per-directory candidate list so it never
    COMPETES with AGENTS.md — a project with both keeps both."""
    repo = tmp_path / "repo"
    (repo / ".tau").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("agents", encoding="utf-8")
    (repo / ".tau" / "SYSTEM.md").write_text("tau system", encoding="utf-8")

    found = _local(repo, empty_agent_dir, tmp_path)

    assert found == [repo / "AGENTS.md", repo / ".tau" / "SYSTEM.md"]


def test_tau_system_md_is_read_from_cwd_only(tmp_path, empty_agent_dir):
    """Unlike the AGENTS.md walk, the τ slot is not inherited from ancestors."""
    repo = tmp_path / "repo"
    (repo / ".tau").mkdir(parents=True)
    (repo / ".tau" / "SYSTEM.md").write_text("tau system", encoding="utf-8")
    sub = repo / "src"
    sub.mkdir()

    assert _local(sub, empty_agent_dir, tmp_path) == []
    assert _local(repo, empty_agent_dir, tmp_path) == [repo / ".tau" / "SYSTEM.md"]


# ─── Worktree shadowing (pi findShadowedContextFile) ──────────────────


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "T", cwd=path)
    (path / "README").write_text("x", encoding="utf-8")
    _git("add", "README", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    return path


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@requires_git
def test_nested_worktree_context_file_shadows_the_main_repos(tmp_path, empty_agent_dir):
    """This repo's own layout: worktrees live in ``.claude/worktrees/`` INSIDE
    the repo, so without this rule the ancestor walk applies the same
    repository's instructions twice."""
    repo = _make_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("main copy", encoding="utf-8")
    worktree = repo / ".claude" / "worktrees" / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(worktree), cwd=repo)
    (worktree / "CLAUDE.md").write_text("worktree copy", encoding="utf-8")

    files = _discovered_under(worktree, empty_agent_dir, tmp_path)

    assert [f.path for f in files] == [worktree / "CLAUDE.md"]
    assert files[0].content == "worktree copy"


@requires_git
def test_shadowing_only_suppresses_the_same_name(tmp_path, empty_agent_dir):
    """The rule replaces one file with its own copy; it does not silence the
    main repo's other context file under a different name."""
    repo = _make_repo(tmp_path / "repo")
    (repo / "AGENTS.md").write_text("main agents", encoding="utf-8")
    worktree = repo / ".claude" / "worktrees" / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(worktree), cwd=repo)
    (worktree / "CLAUDE.md").write_text("worktree claude", encoding="utf-8")

    found = _local(worktree, empty_agent_dir, tmp_path)

    assert found == [repo / "AGENTS.md", worktree / "CLAUDE.md"]


@requires_git
def test_a_worktree_with_no_context_file_shadows_nothing(tmp_path, empty_agent_dir):
    repo = _make_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("main copy", encoding="utf-8")
    worktree = repo / ".claude" / "worktrees" / "wt"
    _git("worktree", "add", "-q", "-b", "feat", str(worktree), cwd=repo)

    found = _local(worktree, empty_agent_dir, tmp_path)

    assert found == [repo / "CLAUDE.md"]


@requires_git
def test_a_sibling_worktree_shadows_nothing(tmp_path, empty_agent_dir):
    """``git worktree add ../feat``: the main repo is not an ancestor, so
    ordinary inheritance is left alone (pi's explicit non-case)."""
    repo = _make_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("main copy", encoding="utf-8")
    worktree = tmp_path / "feat"
    _git("worktree", "add", "-q", "-b", "feat", str(worktree), cwd=repo)
    (worktree / "CLAUDE.md").write_text("worktree copy", encoding="utf-8")

    found = _local(worktree, empty_agent_dir, tmp_path)

    assert found == [worktree / "CLAUDE.md"]


@requires_git
def test_an_ordinary_repo_shadows_nothing(tmp_path, empty_agent_dir):
    """Worktree root and main repo root are the same dir; there is no second copy."""
    repo = _make_repo(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("only copy", encoding="utf-8")

    found = _local(repo, empty_agent_dir, tmp_path)

    assert found == [repo / "CLAUDE.md"]


# ─── Reading: Fail-Early rather than a warning ────────────────────────


def test_a_found_but_unreadable_context_file_raises(tmp_path, empty_agent_dir):
    """pi warns to stderr and continues. τ raises: a prompt silently missing its
    project instructions looks exactly like a model that ignored them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    agents = repo / "AGENTS.md"
    agents.write_bytes(b"\xff\xfe not utf-8 \xff")

    with pytest.raises(ContextFileError) as excinfo:
        load_project_context_files(repo, agent_dir=empty_agent_dir)

    assert str(agents) in str(excinfo.value)
    assert "--no-context-files" in str(excinfo.value)


def test_a_byte_order_mark_is_stripped(tmp_path, empty_agent_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_bytes(b"\xef\xbb\xbfhello")

    files = _discovered_under(repo, empty_agent_dir, tmp_path)

    assert files[0].content == "hello"


# ─── Composition with the system prompt ───────────────────────────────


def test_context_files_compose_with_the_base_prompt(tmp_path, empty_agent_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("PROJECT MARKER", encoding="utf-8")

    prompt = _build_system_prompt(str(repo), agent_dir=empty_agent_dir)

    assert prompt.startswith(_BASE_OPENING)
    assert "PROJECT MARKER" in prompt


def test_context_files_compose_with_an_explicit_system_prompt(tmp_path, empty_agent_dir):
    """The §1 defect: setting a system prompt used to silently turn project
    context OFF, and nothing said so."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("PROJECT MARKER", encoding="utf-8")

    prompt = _build_system_prompt(
        str(repo), custom_prompt="CUSTOM VOICE", agent_dir=empty_agent_dir
    )

    assert prompt.startswith("CUSTOM VOICE")
    assert _BASE_OPENING not in prompt
    assert "PROJECT MARKER" in prompt


def test_every_context_block_names_the_file_it_came_from(tmp_path, empty_agent_dir):
    """What makes a walk to ``/`` honest: the prompt names every file it carries."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("PROJECT MARKER", encoding="utf-8")

    prompt = _build_system_prompt(str(repo), agent_dir=empty_agent_dir)

    assert f'<project_instructions path="{repo / "AGENTS.md"}">' in prompt
    assert "</project_context>" in prompt


def test_no_context_files_suppresses_all_of_it(tmp_path):
    """``--no-context-files``/``-nc``: agent dir, ancestors and .tau/SYSTEM.md."""
    agent_dir = tmp_path / "agent-home" / ".tau"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENTS.md").write_text("GLOBAL MARKER", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".tau").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("PROJECT MARKER", encoding="utf-8")
    (repo / ".tau" / "SYSTEM.md").write_text("TAU MARKER", encoding="utf-8")

    prompt = _build_system_prompt(str(repo), no_context_files=True, agent_dir=agent_dir)

    # The rendered base and nothing else. Not compared to the raw constant: that
    # is a template, and its ``{{project_context}}`` slot is dropped rather than
    # left as a hole when discovery is off — which is the behaviour under test.
    assert prompt.startswith(_BASE_OPENING)
    assert "<project_context>" not in prompt
    assert "{{" not in prompt
    for marker in ("GLOBAL MARKER", "PROJECT MARKER", "TAU MARKER", "project_context"):
        assert marker not in prompt


def test_no_context_files_still_keeps_the_tools_section(tmp_path, empty_agent_dir):
    """-nc withholds files, not tools — it is a discovery switch, not a gag."""
    from tau_agent_core.sdk import _resolve_tools

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("PROJECT MARKER", encoding="utf-8")

    prompt = _build_system_prompt(
        str(repo),
        tools=_resolve_tools(["read"]),
        no_context_files=True,
        agent_dir=empty_agent_dir,
    )

    assert "PROJECT MARKER" not in prompt
    assert "Available tools:" in prompt
