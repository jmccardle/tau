"""Unit S — ``--session-dir`` (pi ``args.ts:112``) and the ``<tmp>/.tau`` guard.

Three layers, in the order a run goes through them:

1. ``cli.py`` — the flag parses, threads into ``CLIArgs``/the TUI run config,
   and refuses the one combination where it would be silently ignored.
2. ``store_factory`` — the flag becomes the file store's ``base_dir`` (seam 1),
   and refuses the store that has no directory to put it in.
3. ``session_store`` — RPC mode's default base, and the ownership/type guard
   that makes ``<tmp>/.tau`` safe to create on a shared box.

The end-to-end proof that an RPC child stops polluting the user's session list
is a subprocess test, and lives in ``test_rpc_session_dir_isolation.py``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from tau_coding_agent import cli
from tau_coding_agent.cli import CLIArgs, parse_cli_args
from tau_coding_agent.session_store import (
    Session,
    UnsafeSessionDirError,
    _ensure_private_dir,
    list_sessions,
    rpc_default_session_base,
    rpc_tmp_dirname,
)
from tau_coding_agent.store_factory import (
    StoreError,
    build_session_catalog,
    resolve_session_dir,
)

# ── 1. cli.py ────────────────────────────────────────────────────────────────


def test_session_dir_parses_into_cliargs():
    assert parse_cli_args(["--session-dir", "/somewhere/else"]).session_dir == "/somewhere/else"


def test_session_dir_defaults_to_none_meaning_each_modes_own_default():
    assert parse_cli_args([]).session_dir is None


def test_session_dir_reaches_the_tui_run_config(monkeypatch, tmp_path):
    """``_launch_tui`` hands it to ``Parley``, which resolves the catalog with
    it — how a human reviews the sessions an RPC host wrote elsewhere."""
    captured: dict = {}

    class _FakeParley:
        def __init__(self, *, cli_overrides=None, cli_run_config=None, fun=False, resume=False):
            captured["run_config"] = cli_run_config

        def run(self):
            pass

    import tau_coding_agent.app as app_module

    monkeypatch.setattr(app_module, "Parley", _FakeParley)
    assert cli._launch_tui(CLIArgs(session_dir=str(tmp_path)), {}) == 0
    assert captured["run_config"]["session_dir"] == str(tmp_path)


def test_import_session_plus_session_dir_is_refused_not_ignored(capsys):
    """The other place the flag would land nowhere: ``--import-session``/
    ``--export-session`` talk to JMFTS directly and never read a file-store
    directory (they ignore ``--store`` for the same reason)."""
    assert cli.main(["--import-session", "a.jsonl", "--session-dir", "/tmp/x"]) == 2
    assert "one-shot JMFTS copy" in capsys.readouterr().err


def test_no_session_plus_session_dir_is_refused_not_ignored(capsys):
    """Fail-Early: ``--no-session`` persists nothing, so a directory to persist
    into is an incoherent pair, not a flag to drop on the floor."""
    assert cli.main(["-p", "--no-session", "--session-dir", "/tmp/x", "hi"]) == 2
    assert "--no-session" in capsys.readouterr().err


# ── 2. store_factory ─────────────────────────────────────────────────────────


def test_file_catalog_is_built_on_the_given_dir(tmp_path):
    catalog = build_session_catalog({}, "file", tmp_path)
    catalog.create(str(tmp_path / "cwd"), "m", "openai")
    assert list_sessions(base_dir=tmp_path), "nothing was written under --session-dir"


def test_file_catalog_without_the_flag_keeps_the_default(monkeypatch, tmp_path):
    """``None`` means ``~/.tau/sessions`` — the TUI's and ``--print``'s default
    is NOT moved by this unit; only ``--mode rpc``'s is."""
    import tau_coding_agent.session_store as store

    monkeypatch.setattr(store, "TAU_DIR", tmp_path / ".tau")
    catalog = build_session_catalog({}, "file", None)
    catalog.create(str(tmp_path / "cwd"), "m", "openai")
    assert list((tmp_path / ".tau" / "sessions").rglob("*.jsonl"))


def test_session_dir_expands_a_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_session_dir("~/sessions") == tmp_path / "sessions"


def test_empty_session_dir_raises_rather_than_meaning_the_default():
    with pytest.raises(StoreError, match="empty"):
        resolve_session_dir("")


def test_session_dir_with_the_jmfts_store_refuses_before_any_network_call():
    """Fail-Early over "accept and ignore": the JMFTS store is a document
    server with no directory. The refusal must precede ``build_jmfts_client``,
    so an unreachable/absent server is not what this reports."""
    config = {"session_store": {"backend": "jmfts", "url": "http://127.0.0.1:1"}}
    with pytest.raises(StoreError, match="--session-dir"):
        build_session_catalog(config, None, "/somewhere")


# ── 3. session_store: RPC's default base, and the <tmp>/.tau guard ───────────


@pytest.fixture
def temp_root(monkeypatch, tmp_path: Path) -> Path:
    """Point ``tempfile.gettempdir()`` at ``tmp_path`` for this test.

    ``$TMPDIR`` alone is not enough IN-PROCESS: ``gettempdir()`` memoizes its
    answer in ``tempfile.tempdir`` on first use, and something has always used
    it by the time a test runs. Clearing that cache is what makes the env var
    the thing actually being read — the subprocess tests in
    ``test_rpc_session_dir_isolation.py`` set only ``$TMPDIR``, on a fresh
    interpreter where no cache exists, and that is the real proof it is
    honored.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    return tmp_path


def test_rpc_default_base_is_under_the_temp_dir_not_the_home_dir(temp_root, tmp_path):
    assert rpc_default_session_base() == tmp_path / rpc_tmp_dirname() / "sessions"
    assert (tmp_path / rpc_tmp_dirname() / "sessions").is_dir()


def test_rpc_default_base_creates_both_levels_private(temp_root, tmp_path):
    """Hazard 2: created ``0o700``, so another user cannot plant a file — let
    alone a symlink — inside the directory τ is about to write transcripts to."""
    base = rpc_default_session_base()
    assert (tmp_path / rpc_tmp_dirname()).stat().st_mode & 0o777 == 0o700
    assert base.stat().st_mode & 0o777 == 0o700


def test_rpc_default_base_is_idempotent(temp_root):
    """A second run reuses the directory the first created rather than
    refusing it — the guard rejects foreign dirs, not our own."""
    assert rpc_default_session_base() == rpc_default_session_base()


def test_an_over_permissive_dir_we_own_is_tightened(tmp_path):
    """We own it, so narrowing it is ours to do: a 0o777 session base is one
    another user can plant symlinks in, which is the hazard itself."""
    target = tmp_path / ".tau"
    target.mkdir(mode=0o777)
    _ensure_private_dir(target)
    assert target.stat().st_mode & 0o777 == 0o700


def test_a_symlink_is_refused_and_never_followed(tmp_path):
    """The named attack: pre-create ``<tmp>/.tau`` as a symlink into a home."""
    victim = tmp_path / "victim"
    victim.mkdir()
    link = tmp_path / ".tau"
    link.symlink_to(victim, target_is_directory=True)
    with pytest.raises(UnsafeSessionDirError, match="symlink"):
        _ensure_private_dir(link)
    assert list(victim.iterdir()) == []


def test_a_regular_file_is_refused(tmp_path):
    squatted = tmp_path / ".tau"
    squatted.write_text("mine now")
    with pytest.raises(UnsafeSessionDirError, match="non-directory file"):
        _ensure_private_dir(squatted)
    assert squatted.read_text() == "mine now"


def test_a_directory_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    """The other pre-creation attack: a real directory, someone else's.

    ``chown`` needs root, so the *identity* is what gets moved instead of the
    file: ``os.getuid`` reports a uid this directory is not owned by, which is
    indistinguishable to the guard from the reverse.
    """
    foreign = tmp_path / ".tau"
    foreign.mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(foreign).st_uid + 1)
    with pytest.raises(UnsafeSessionDirError, match="owned by uid"):
        _ensure_private_dir(foreign)


def test_two_users_sharing_one_temp_dir_do_not_contend_for_one_entry(
    temp_root, tmp_path, monkeypatch
):
    """Round-3 finding 1, as the property rather than the instance.

    The first shape of this default was a flat ``<tmp>/.tau``. On a default
    distro that is ``/tmp/.tau`` and ``/tmp`` is ``drwxrwxrwt``, so user A's
    first ``--mode rpc`` run created it ``0700`` and every other user on the
    box was then refused by :func:`_ensure_private_dir`'s (correct) ownership
    check — ``--mode rpc`` unavailable to everyone but the first uid, until
    reboot, with an error telling them to remove a sticky-bit entry they do
    not own.

    Two uids, one temp dir, in sequence — exactly the shared-``/tmp`` shape.
    Both must get a base, and the two bases must be DIFFERENT paths. This is
    the assertion a re-flattened name cannot pass, whatever the name becomes:
    it never says ``.tau-`` anywhere.
    """
    real_uid = os.getuid()

    monkeypatch.setattr(os, "getuid", lambda: real_uid)
    first = rpc_default_session_base()
    (first / "a-real-session.jsonl").write_text("{}\n")

    # A different uid on the same box. Only the IDENTITY moves (chown needs
    # root); to the code under test this is indistinguishable from being the
    # second real user to arrive.
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    second = rpc_default_session_base()

    assert second != first, (
        "both uids resolved to the same temp entry — the second user is about "
        "to be refused by the ownership guard, or to share the first's transcripts"
    )
    assert second.is_dir()
    assert list(second.iterdir()) == []
    # …and the first user's transcripts are untouched and still theirs.
    assert (first / "a-real-session.jsonl").read_text() == "{}\n"


def test_the_hostile_squat_on_our_own_name_is_still_refused(temp_root, tmp_path, monkeypatch):
    """The uid in the name retires the COLLISION, not the ATTACK.

    An attacker who knows the target's uid can still pre-create the entry that
    target will use — that is the hazard ``_ensure_private_dir`` exists for,
    and it must survive the fix for finding 1 rather than be traded away by it.

    We play the victim, uid ``real+1``; the entry ``real+1`` resolves to has
    already been planted by ``real`` (``chown`` needs root, so the identity is
    what moves — the guard sees the same mismatch either way).
    """
    victim_uid = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: victim_uid)
    squatted = tmp_path / rpc_tmp_dirname()
    assert squatted.name.endswith(str(victim_uid))
    squatted.mkdir(mode=0o700)  # created BY us, so owned by the real uid

    with pytest.raises(UnsafeSessionDirError, match="owned by uid"):
        rpc_default_session_base()
    assert list(squatted.iterdir()) == []


def test_a_refused_base_writes_nothing_and_offers_no_alternative(temp_root, tmp_path):
    """Fail-Early, stated as behaviour: on refusal τ neither creates the path
    nor silently falls back to ``~/.tau/sessions``."""
    (tmp_path / rpc_tmp_dirname()).write_text("squatted")
    with pytest.raises(UnsafeSessionDirError):
        rpc_default_session_base()
    assert not (tmp_path / "sessions").exists()


# ── the seam still works end to end for a real session ───────────────────────


def test_a_session_created_under_a_custom_dir_is_listed_only_there(tmp_path):
    """The partition is total: what ``--session-dir`` writes is invisible to a
    listing of the default base, which is the whole mechanism behind "RPC mode
    stops polluting the user's session list"."""
    other = tmp_path / "elsewhere"
    cwd = str(tmp_path / "cwd")
    Session.create(cwd, "m", "openai", base_dir=other)
    assert len(list_sessions(cwd, base_dir=other)) == 1
    assert list_sessions(cwd, base_dir=tmp_path / "default") == []
