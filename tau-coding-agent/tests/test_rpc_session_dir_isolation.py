"""Unit S — ``--mode rpc`` stores its sessions somewhere else, and says where.

The regression this file pins, reproduced by the Tier B reviewer in an
isolated ``$HOME``: D-6 moved RPC mode's startup session from
``create_ephemeral`` to ``catalog.create`` so ``set_model``/``set_session_name``
cursors would name real, durable entries. ``create`` writes the session header
IMMEDIATELY and unconditionally, so every ``tau --mode rpc`` spawn then left a
durable, listable, 0-message session in ``~/.tau/sessions/<dashed-cwd>/`` —
*including a child that asks ``get_capabilities`` and exits*::

    real session: 4ff2fa12…  (2 messages, name='my real work')
       … then ONE --mode rpc child that sends get_capabilities and exits …
    most_recent now -> 2026-08-06T02-32-52-003Z_798fc4a7….jsonl
    IS IT THE REAL ONE? False

``headless._select_session`` makes ``--continue`` exactly
``catalog.most_recent(os.getcwd())``, so ``tau -p -c "and then?"`` in a
directory where any RPC host ran more recently resumed an EMPTY conversation
instead of the human's work, and the TUI picker filled with nameless 0-message
rows at one per spawn.

Everything here drives a real ``python -m tau_coding_agent.cli --mode rpc``
child over real pipes, with its own ``$HOME`` **and its own ``$TMPDIR``** — the
child resolves both fresh, so redirecting them is real isolation rather than a
monkeypatch that only fools this process (the MEMORY.md trap
``test_rpc_conformance.py``'s own module note spells out). ``$TMPDIR`` matters
twice over: it is where the fix sends the child's sessions
(``session_store.rpc_default_session_base``), and pointing it at a ``tmp_path``
is what keeps this suite from writing into the developer's real
``/tmp/.tau-<uid>``.

No HTTP server and no LLM: no test here sends a turn. The child needs a
resolvable model config to start, not a reachable one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tau_coding_agent.session_store import (
    Session,
    most_recent,
    rpc_tmp_dirname,
    session_dir_for_cwd,
)

# Port 1 is always connection-refused (the trick test_store_factory.py already
# uses): a model entry that RESOLVES without anything ever answering it.
UNREACHABLE_BASE_URL = "http://127.0.0.1:1/v1"

_CAPABILITIES_REQUEST = {"jsonrpc": "2.0", "id": 1, "method": "get_capabilities"}

#: `dialect.SESSION_NOT_PERSISTED`. Imported as a literal rather than from
#: `tau_agent_core.rpc.dialect`, on purpose: these tests speak to the child as
#: a foreign host would, over a pipe, and a host reading the published error
#: table has the NUMBER, not the module. If the constant is renumbered without
#: the protocol version moving, this file is one of the places that should go
#: red rather than follow along silently.
SESSION_NOT_PERSISTED = -32004


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    """An isolated ``$HOME`` + ``$TMPDIR`` + working directory.

    ``resolve()`` on the workdir because the child reports its own
    ``os.getcwd()``, and ``session_dir_for_cwd`` keys on the absolute path: a
    symlinked temp root would otherwise put the parent's expectation and the
    child's write in two different dashed-cwd directories.
    """
    home = tmp_path / "home"
    tau_dir = home / ".tau"
    tau_dir.mkdir(parents=True)
    (tau_dir / "config.json").write_text(
        json.dumps(
            {
                "models": {
                    "fake": {
                        "backend": "openai",
                        "model": "fake-model",
                        "base_url": UNREACHABLE_BASE_URL,
                        "api_key": "x",
                        "tools": [],
                    }
                },
                "default_model": "fake",
            }
        )
    )
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()
    return {"home": home, "tmpdir": tmpdir, "workdir": workdir.resolve()}


def _user_sessions(env: dict[str, Path]) -> Path:
    return env["home"] / ".tau" / "sessions"


def _rpc_sessions(env: dict[str, Path]) -> Path:
    return env["tmpdir"] / rpc_tmp_dirname() / "sessions"


def _files_under(base: Path) -> set[Path]:
    return set(base.rglob("*.jsonl")) if base.exists() else set()


def _plant_real_session(env: dict[str, Path]) -> Session:
    """The human's own work, in the user's real session list, in ``workdir``."""
    session = Session.create(
        str(env["workdir"]),
        "fake",
        "openai",
        base_dir=_user_sessions(env),
    )
    session.append_session_info("my real work")
    session.append_message({"role": "user", "content": "the actual question"})
    session.append_message({"role": "assistant", "content": [{"type": "text", "text": "answer"}]})
    return session


def _run_rpc_child(
    env: dict[str, Path],
    *,
    extra_args: list[str] | None = None,
    request: dict | list[dict] | None = None,
) -> subprocess.CompletedProcess[str]:
    """One no-op ``--mode rpc`` child: send the request(s), close stdin, exit.

    ``get_capabilities`` by default — the cheapest possible interaction, and the
    exact one from the reproduction: a host that asks what the process can do
    and goes away. stdin EOF is the clean shutdown trigger (T1/P4), so
    ``communicate`` both drives and ends the child.

    ``request`` takes a LIST when a test needs more than one exchange (the
    ``--no-session`` tests below ask ``get_state`` and then try an appending
    verb). The reader is strictly serial, so the replies come back in the order
    the requests went out and a test may index them.
    """
    requests = request if isinstance(request, list) else [request or _CAPABILITIES_REQUEST]
    line = "\n".join(json.dumps(r) for r in requests)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tau_coding_agent.cli",
            "--mode",
            "rpc",
            *(extra_args or []),
        ],
        input=line + "\n",
        cwd=str(env["workdir"]),
        env={**os.environ, "HOME": str(env["home"]), "TMPDIR": str(env["tmpdir"])},
        capture_output=True,
        text=True,
        timeout=120,
    )


# ── the leak, dead ───────────────────────────────────────────────────────────


def test_an_rpc_child_leaves_the_users_session_list_untouched(env):
    """The reproduction verbatim, inverted into a regression test.

    A real session exists in ``~/.tau/sessions`` for this cwd; one ``--mode
    rpc`` child then runs and exits. Afterwards ``most_recent`` — the exact
    call ``--continue`` makes — must still name the human's session, and the
    user's session directory must contain the same files it did before.

    The second half is what stops this passing for the wrong reason: the child
    must still have created a session of its own, just somewhere else. A child
    that crashed at startup would also leave the user's list untouched.

    MUTATION TARGET: make ``rpc_mode._resolve_rpc_session_dir`` return ``None``
    for the no-flag file-store case (i.e. restore the pre-fix behaviour) → red
    at "the RPC child's session landed in the user's session list".
    """
    real = _plant_real_session(env)
    before = _files_under(_user_sessions(env))
    assert before, "fixture failure: the human's session was never written"

    result = _run_rpc_child(env)
    assert result.returncode == 0, f"child failed: {result.stderr}"
    # It really did reach the RPC loop and answer — not a startup crash.
    reply = json.loads(result.stdout.splitlines()[0])
    assert reply["id"] == 1 and "result" in reply, result.stdout

    after = _files_under(_user_sessions(env))
    assert after == before, (
        "the RPC child's session landed in the user's session list: "
        f"{sorted(p.name for p in after - before)}"
    )
    assert most_recent(str(env["workdir"]), base_dir=_user_sessions(env)) == Path(str(real.path)), (
        "`--continue` (catalog.most_recent) no longer resolves to the human's "
        "session after an RPC child ran in the same directory"
    )

    # …and the child's own session is durable, just elsewhere: <tmp>/.tau/sessions.
    rpc_files = _files_under(_rpc_sessions(env))
    assert len(rpc_files) == 1, (
        "the RPC child should still create exactly one real session, under its "
        f"own base dir; found {sorted(p.name for p in rpc_files)}"
    )
    assert rpc_files == _files_under(session_dir_for_cwd(str(env["workdir"]), _rpc_sessions(env)))


def test_explicit_session_dir_puts_an_rpc_session_in_the_users_list(env):
    """The deliberate case: ``--session-dir ~/.tau/sessions`` on an RPC child
    means "yes, I really do want these in the user's list", and gets it.

    This is why ``--session-dir`` is NOT in ``cli.py``'s ``--mode rpc``
    rejection set alongside ``--name``/``--store``.

    MUTATION TARGET: make ``_resolve_rpc_session_dir`` ignore its flag (return
    ``rpc_default_session_base()`` unconditionally) → red at "the explicit
    --session-dir was ignored".
    """
    before = _files_under(_user_sessions(env))
    result = _run_rpc_child(env, extra_args=["--session-dir", str(_user_sessions(env))])
    assert result.returncode == 0, f"child failed: {result.stderr}"

    added = _files_under(_user_sessions(env)) - before
    assert len(added) == 1, (
        "the explicit --session-dir was ignored: expected exactly one new "
        f"session under the user's dir, found {sorted(p.name for p in added)}"
    )
    assert not _files_under(_rpc_sessions(env)), (
        "--session-dir was honored AND the default was used as well; the flag must win outright"
    )


def test_session_dir_is_accepted_at_startup_unlike_name_and_store(env):
    """``--session-dir`` reaches the run rather than being refused by
    ``cli.py``'s ``--mode rpc`` validation — and its neighbours still are.

    MUTATION TARGET: add ``args.session_dir is not None`` to that rejection
    condition in ``cli.py`` → red on the first assertion (exit 2).
    """
    accepted = _run_rpc_child(env, extra_args=["--session-dir", str(_user_sessions(env))])
    assert accepted.returncode == 0, accepted.stderr

    rejected = _run_rpc_child(env, extra_args=["--store", "file"])
    assert rejected.returncode == 2
    assert "--name/--store" in rejected.stderr


# ── hazard 2: <tmp>/.tau on a shared box ─────────────────────────────────────


def test_rpc_refuses_a_tmp_tau_that_is_a_symlink(env):
    """A hostile user pre-creates ``<tmp>/.tau`` as a symlink into someone's
    home. τ must refuse loudly, write nothing through it, and NOT quietly pick
    another path.

    MUTATION TARGET: use ``path.stat()`` instead of ``path.lstat()`` in
    ``session_store._ensure_private_dir`` → the symlink resolves to the
    directory it points at, the guard passes, and this test goes red at "wrote
    through the symlink" (the target collects the session).
    """
    victim = env["home"] / "private-notes"
    victim.mkdir()
    (env["tmpdir"] / rpc_tmp_dirname()).symlink_to(victim, target_is_directory=True)

    result = _run_rpc_child(env)
    assert result.returncode == 2, result.stdout
    assert "refusing to use" in result.stderr and "symlink" in result.stderr
    assert list(victim.iterdir()) == [], (
        f"wrote through the symlink: {[p.name for p in victim.iterdir()]}"
    )


def test_rpc_refuses_a_tmp_tau_that_is_a_regular_file(env):
    """The same refusal for the blunter squat: ``<tmp>/.tau`` already exists and
    is not a directory at all.

    MUTATION TARGET: drop the ``S_ISDIR`` check in ``_ensure_private_dir`` →
    the run dies with a raw ``NotADirectoryError`` traceback and a non-2 exit
    code instead of τ's own refusal.
    """
    (env["tmpdir"] / rpc_tmp_dirname()).write_text("squatted")

    result = _run_rpc_child(env)
    assert result.returncode == 2, result.stdout
    assert "refusing to use" in result.stderr
    assert (env["tmpdir"] / rpc_tmp_dirname()).read_text() == "squatted"


# ── --no-session: the flag that was parsed, accepted, and never read ─────────
#
# `cli.py` has always parsed `--no-session`, and `--mode rpc` has never
# rejected it — but `rpc_mode.run_rpc` read five `args` fields and `no_session`
# was not among them, so `tau --mode rpc --no-session` created a PERSISTED
# session. `run_print` honored the same flag on the same catalog seam, which is
# what made this an inconsistency between modes rather than a missing feature:
# every layer below already supported it (`SessionCatalog.create_ephemeral` is
# on the ABC, both shipped stores implement it honestly, and
# `SessionCatalogContractTests` pins that an ephemeral session never becomes
# listable). Only the wiring was absent.
#
# These tests drive a real child, because the defect was precisely that a
# process-level flag did not reach process-level behaviour — a unit test on a
# patched `run_rpc` could have passed against the broken version.


def test_no_session_writes_nothing_to_either_session_directory(env):
    """The flag's whole content: a ``--no-session`` RPC child persists nothing.

    Both directories are asserted, not just the RPC one. ``<tmp>`` is where a
    persisted RPC session goes today, and ``~/.tau/sessions`` is where it went
    before unit S — a regression in either direction is a session file the host
    asked not to have.

    MUTATION TARGET: restore ``session_catalog.create`` unconditionally in
    ``rpc_mode.run_rpc`` → red at "persisted a session under --no-session".
    """
    result = _run_rpc_child(env, extra_args=["--no-session"])
    assert result.returncode == 0, f"child failed: {result.stderr}"
    reply = json.loads(result.stdout.splitlines()[0])
    assert reply["id"] == 1 and "result" in reply, result.stdout

    leaked = _files_under(_rpc_sessions(env)) | _files_under(_user_sessions(env))
    assert leaked == set(), (
        "persisted a session under --no-session: "
        f"{sorted(str(p.relative_to(env['tmpdir'].parent)) for p in leaked)}"
    )


def test_without_the_flag_the_same_child_does_persist(env):
    """The contrast that stops the test above passing for the wrong reason.

    A child that crashed at startup, or one whose session base moved somewhere
    neither assertion looks, would also leave both directories empty. The only
    difference between this run and that one is the flag.
    """
    result = _run_rpc_child(env)
    assert result.returncode == 0, f"child failed: {result.stderr}"

    assert len(_files_under(_rpc_sessions(env))) == 1, (
        "the default is still a persisted startup session (Blocker 2 of the "
        "Tier B review); only --no-session opts out"
    )


def test_no_session_reports_addressable_false_on_get_state(env):
    """A host learns the session is unpersisted by ASKING, not by tripping a
    refusal on the first appending verb.

    Before ``--mode rpc`` honored ``--no-session`` the startup session was
    always persisted, so nothing needed to report this: ``new_session``/
    ``fork``/``switch_session`` publish ``addressable`` on the sessions THEY
    produce, and a host that called none of the three had no verb to ask.

    MUTATION TARGET: drop ``addressable`` from ``_handle_get_state`` → red at
    the KeyError, and the schema's ``required`` list no longer matches what the
    verb returns.
    """
    result = _run_rpc_child(
        env,
        extra_args=["--no-session"],
        request=[{"jsonrpc": "2.0", "id": 1, "method": "get_state"}],
    )
    assert result.returncode == 0, f"child failed: {result.stderr}"

    state = json.loads(result.stdout.splitlines()[0])["result"]
    assert state["addressable"] is False


def test_the_default_startup_session_reports_addressable_true(env):
    """The same read on a persisted run. Paired with the test above so
    ``addressable`` is pinned as a FUNCTION of the flag rather than as a
    constant that happens to read false — a hardcoded ``False`` would pass the
    previous test alone.
    """
    result = _run_rpc_child(env, request=[{"jsonrpc": "2.0", "id": 1, "method": "get_state"}])
    assert result.returncode == 0, f"child failed: {result.stderr}"

    state = json.loads(result.stdout.splitlines()[0])["result"]
    assert state["addressable"] is True


def test_no_session_makes_the_appending_verbs_refuse_with_their_own_code(env):
    """D-7 on the STARTUP session, which is new: a host can now meet
    ``-32004 SESSION_NOT_PERSISTED`` on its very first request, where before it
    could only reach that state via ``new_session {"persist": false}``.

    ``set_model`` is asked for a model that really is configured, so a refusal
    here cannot be an unknown-name ``-32602`` wearing the wrong number.

    MUTATION TARGET: drop the ``require_durable_session`` call from
    ``set_model`` → the verb returns a cursor for an entry that dies with the
    process, and this goes red at "expected a refusal".
    """
    result = _run_rpc_child(
        env,
        extra_args=["--no-session"],
        request=[{"jsonrpc": "2.0", "id": 1, "method": "set_model", "params": {"name": "fake"}}],
    )
    assert result.returncode == 0, f"child failed: {result.stderr}"

    reply = json.loads(result.stdout.splitlines()[0])
    assert "result" not in reply, f"expected a refusal, got {reply}"
    assert reply["error"]["code"] == SESSION_NOT_PERSISTED, reply["error"]


def test_get_states_addressable_predicts_the_appending_verbs_refusal(env):
    """The two halves agree, measured in ONE child rather than compared across
    two runs.

    This is the property, not the instance: ``addressable`` is documented as
    the same question D-7 asks, so a host is entitled to use the read as a
    precondition for the write. A future change that moves one and not the
    other — a new durable-location attribute recognized by
    ``session_log_is_addressable`` but not by ``require_durable_session``, say
    — makes this red while both individual tests above still pass.
    """
    for extra_args in ([], ["--no-session"]):
        result = _run_rpc_child(
            env,
            extra_args=extra_args,
            request=[
                {"jsonrpc": "2.0", "id": 1, "method": "get_state"},
                {"jsonrpc": "2.0", "id": 2, "method": "set_model", "params": {"name": "fake"}},
            ],
        )
        assert result.returncode == 0, f"child failed ({extra_args}): {result.stderr}"

        state, set_model = (json.loads(line) for line in result.stdout.splitlines()[:2])
        addressable = state["result"]["addressable"]
        refused = "error" in set_model and set_model["error"]["code"] == SESSION_NOT_PERSISTED
        assert addressable is not refused, (
            f"get_state said addressable={addressable} but set_model "
            f"{'refused' if refused else 'succeeded'} ({extra_args}): {set_model}"
        )


def test_no_session_still_reaches_durability_over_the_wire(env):
    """The escape hatch the module docstring promises: ``--no-session`` chooses
    the STARTUP session's persistence, not the process's capability.

    The catalog is built either way — which is also why
    ``_resolve_rpc_session_dir`` still resolves (and hazard-checks) the
    ``<tmp>`` base under the flag. Deferring that would move the hostile-squat
    refusal from startup into whichever wire call first needed a directory.

    MUTATION TARGET: skip building the SessionCatalog under ``--no-session`` →
    ``new_session`` can no longer produce a persisted session and this goes red
    at "addressable".
    """
    result = _run_rpc_child(
        env,
        extra_args=["--no-session"],
        request=[
            {"jsonrpc": "2.0", "id": 1, "method": "get_state"},
            {"jsonrpc": "2.0", "id": 2, "method": "new_session", "params": {"persist": True}},
            {"jsonrpc": "2.0", "id": 3, "method": "get_state"},
        ],
    )
    assert result.returncode == 0, f"child failed: {result.stderr}"

    before, new_session, after = (json.loads(line) for line in result.stdout.splitlines()[:3])
    assert before["result"]["addressable"] is False
    assert new_session["result"]["session"]["addressable"] is True, new_session
    assert after["result"]["addressable"] is True, (
        "the connection did not move onto the persisted session new_session made"
    )

    # …and only THEN does anything land on disk, in the RPC base rather than
    # the user's list.
    assert len(_files_under(_rpc_sessions(env))) == 1
    assert _files_under(_user_sessions(env)) == set()
