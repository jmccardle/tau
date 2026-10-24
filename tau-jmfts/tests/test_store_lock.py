"""A JMFTS-backed session takes a host-local write lock in the runtime directory (docs/STEP-DEBUGGER.md §3.7.3)."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import textwrap

import pytest
from test_store_deferred import _Client

from tau_agent_core import session_lock
from tau_agent_core.cursor import Cursor
from tau_agent_core.session_lock import SessionReadOnlyError
from tau_jmfts.catalog import JmftsSessionCatalog

HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from tau_agent_core.session_lock import SessionWriteLock, set_head_label
    set_head_label("serve")
    lock = SessionWriteLock(Path(sys.argv[1]))
    lock.retain()
    print("held", flush=True)
    sys.stdin.read()
    """
)


@pytest.fixture
def runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(session_lock, "lock_blind_filesystem", lambda _d: None)
    return tmp_path


def test_the_sidecar_is_host_local_under_the_runtime_dir(runtime_dir):
    log = JmftsSessionCatalog(_Client()).create(".", "m", "b")  # type: ignore[arg-type]
    log.write_lock.retain()
    try:
        sidecar = runtime_dir / "tau" / "locks" / f"jmfts-{log.id}.lock"
        assert log.write_lock.sidecar == sidecar and sidecar.exists()
        fd = os.open(sidecar, os.O_RDWR)
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)
    finally:
        log.write_lock.release()


async def test_another_process_on_this_host_makes_it_read_only(runtime_dir):
    log = JmftsSessionCatalog(_Client()).create(".", "m", "b")  # type: ignore[arg-type]
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(log.write_lock.sidecar)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        log.write_lock.retain()
        assert log.write_lock.access().read_only
        with pytest.raises(SessionReadOnlyError, match=f"pid {proc.pid} \\(serve\\)"):
            await Cursor.newest(log).append_message({"role": "user", "content": "x"})
        log.write_lock.release()
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)


def test_an_unsafe_fallback_dir_refuses_writing_naming_it(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(session_lock, "_TMP_ROOT", tmp_path)
    squat = tmp_path / f".tau-{os.getuid()}"
    squat.mkdir(mode=0o700)
    squat.chmod(0o777)
    log = JmftsSessionCatalog(_Client()).create(".", "m", "b")  # type: ignore[arg-type]
    log.write_lock.retain()
    access = log.write_lock.access()
    assert access.read_only and str(squat) in (access.reason or "")
    log.write_lock.release()
