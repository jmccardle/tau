"""The per-session write lock's registry, sidecar and filesystem rules (docs/STEP-DEBUGGER.md §3.7.3)."""

from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tau_agent_core import session_lock
from tau_agent_core.session_lock import (
    SessionLockError,
    SessionReadOnlyError,
    SessionWriteLock,
    held_descriptor_count,
)

HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from tau_agent_core.session_lock import SessionWriteLock, set_head_label
    set_head_label(sys.argv[2])
    lock = SessionWriteLock(Path(sys.argv[1]))
    lock.retain()
    assert not lock.access().read_only, lock.access()
    print("held", flush=True)
    sys.stdin.read()
    """
)


class Holder:
    """Another process holding ``sidecar``'s lock through τ, until :meth:`stop`."""

    def __init__(self, sidecar: Path, head: str = "tui") -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(sidecar), head],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        assert self.proc.stdout.readline().strip() == "held"

    def stop(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=30)


def can_lock(path: Path) -> bool:
    """Whether a fresh open file description can take ``path``'s lock now."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


@pytest.fixture(autouse=True)
def trusted_fs(monkeypatch):
    """Every test's tmp_path counts as a filesystem whose flock is trusted, unless it says otherwise."""
    monkeypatch.setattr(session_lock, "lock_blind_filesystem", lambda _d: None)


def test_two_locks_on_one_sidecar_share_one_descriptor(tmp_path):
    sidecar = tmp_path / "s.jsonl.lock"
    before = held_descriptor_count()
    first, second = SessionWriteLock(sidecar), SessionWriteLock(tmp_path / "." / "s.jsonl.lock")
    first.retain()
    second.retain()
    assert held_descriptor_count() == before + 1
    assert not first.access().read_only and not second.access().read_only
    first.release()
    assert not can_lock(sidecar), "the second object still holds it"
    second.release()
    assert held_descriptor_count() == before
    assert can_lock(sidecar)


def test_a_retain_count_releases_only_at_zero(tmp_path):
    lock = SessionWriteLock(tmp_path / "s.lock")
    lock.retain()
    lock.retain()
    lock.release()
    assert not can_lock(lock.sidecar)
    lock.release()
    assert can_lock(lock.sidecar)
    with pytest.raises(RuntimeError, match="without a retain"):
        lock.release()


def test_another_process_holding_it_makes_this_one_read_only_and_named(tmp_path):
    sidecar = tmp_path / "s.lock"
    holder = Holder(sidecar, head="tui")
    try:
        lock = SessionWriteLock(sidecar)
        lock.retain()
        access = lock.access()
        assert access.read_only
        assert access.holder is not None
        assert access.holder.pid == holder.proc.pid and access.holder.head == "tui"
        assert str(holder.proc.pid) in (access.reason or "") and "(tui)" in (access.reason or "")
        with pytest.raises(SessionReadOnlyError, match=str(holder.proc.pid)):
            lock.guard()
        lock.release()
        lock.guard()
    finally:
        holder.stop()


def test_the_descriptor_is_close_on_exec_so_a_subprocess_never_inherits_it(tmp_path):
    lock = SessionWriteLock(tmp_path / "s.lock")
    lock.retain()
    try:
        fd = next(e.fd for e in session_lock._REGISTRY.values() if e.path == lock.sidecar)
        assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        child = subprocess.Popen(
            [sys.executable, "-c", f"import os, sys\ntry:\n os.fstat({fd})\nexcept OSError:\n sys.exit(7)"],
            close_fds=False,
        )
        assert child.wait(timeout=30) == 7, "the child saw the lock descriptor"
    finally:
        lock.release()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_bare_fork_without_exec_keeps_sharing_the_lock_until_the_child_exits(tmp_path):
    lock = SessionWriteLock(tmp_path / "s.lock")
    lock.retain()
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_end)
        os.read(read_end, 1)
        os._exit(0)
    os.close(read_end)
    lock.release()
    try:
        assert not can_lock(lock.sidecar), "the forked child still holds a share of the flock"
    finally:
        os.close(write_end)
        os.waitpid(pid, 0)
    assert can_lock(lock.sidecar)


def test_an_unlinked_sidecar_is_retaken_at_the_check(tmp_path):
    lock = SessionWriteLock(tmp_path / "s.lock")
    lock.retain()
    try:
        lock.sidecar.unlink()
        assert not lock.check().read_only
        assert lock.sidecar.exists()
        assert not can_lock(lock.sidecar), "the new sidecar is the one held"
    finally:
        lock.release()
    assert can_lock(lock.sidecar)


def test_a_replaced_sidecar_another_process_took_turns_this_one_read_only(tmp_path):
    lock = SessionWriteLock(tmp_path / "s.lock")
    lock.retain()
    lock.sidecar.unlink()
    holder = Holder(lock.sidecar, head="serve")
    try:
        access = lock.check()
        assert access.read_only and access.holder is not None
        assert access.holder.pid == holder.proc.pid
        with pytest.raises(SessionReadOnlyError, match="serve"):
            lock.guard()
    finally:
        lock.release()
        holder.stop()


def test_a_lock_blind_filesystem_warns_and_the_opt_in_silences_it(tmp_path, monkeypatch):
    monkeypatch.setattr(session_lock, "lock_blind_filesystem", lambda _d: "fuse.sshfs")
    warned = SessionWriteLock(tmp_path / "a.lock")
    warned.retain()
    assert warned.access().warning == (
        "cross-process exclusion unavailable on fuse.sshfs; a second writer is not detected"
    )
    assert not warned.access().read_only, "opening for writing still works"
    trusted = SessionWriteLock(tmp_path / "b.lock", allow_unlocked_fs=True)
    trusted.retain()
    assert trusted.access().warning is None
    warned.release()
    trusted.release()


@pytest.mark.parametrize(
    ("mount", "blind"),
    [
        (("ext4", {"rw", "relatime"}), None),
        (("nfs4", {"rw", "vers=4.2"}), None),
        (("nfs4", {"rw", "local_lock=flock"}), "nfs4 (local_lock=flock)"),
        (("nfs", {"nolock"}), "nfs (nolock)"),
        (("cifs", {"rw"}), "cifs"),
        (("fuse.sshfs", {"rw"}), "fuse.sshfs"),
    ],
)
def test_lock_blind_filesystems_are_named(monkeypatch, tmp_path, mount, blind):
    monkeypatch.undo()
    monkeypatch.setattr(session_lock, "mount_of", lambda _d: mount)
    assert session_lock.lock_blind_filesystem(tmp_path) == blind


def test_the_mount_of_a_real_directory_is_read_from_mountinfo(tmp_path):
    if not Path("/proc/self/mountinfo").exists():
        pytest.skip("no mountinfo on this platform")
    mount = session_lock.mount_of(tmp_path)
    assert mount is not None and mount[0]


def test_a_flock_that_fails_for_another_reason_refuses_writing(tmp_path, monkeypatch):
    def unsupported(_fd: int, _op: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(session_lock.fcntl, "flock", unsupported)
    lock = SessionWriteLock(tmp_path / "s.lock")
    lock.retain()
    access = lock.access()
    assert access.read_only and access.holder is None
    assert "flock is unavailable" in (access.reason or "")
    with pytest.raises(SessionReadOnlyError, match="No locks available"):
        lock.guard()
    lock.release()


def test_the_private_lock_dir_is_under_the_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    locks = session_lock.private_lock_dir()
    assert locks == tmp_path / "tau" / "locks"
    assert (locks.stat().st_mode & 0o777) == 0o700


def test_the_private_lock_dir_falls_back_to_a_per_user_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(session_lock, "_TMP_ROOT", tmp_path)
    locks = session_lock.private_lock_dir()
    assert locks == tmp_path / f".tau-{os.getuid()}" / "locks"
    assert (locks.parent.stat().st_mode & 0o777) == 0o700


def test_the_private_lock_dir_refuses_a_symlink_or_open_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(session_lock, "_TMP_ROOT", tmp_path)
    squat = tmp_path / f".tau-{os.getuid()}"
    (tmp_path / "elsewhere").mkdir()
    squat.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(SessionLockError, match="symlink") as refused:
        session_lock.private_lock_dir()
    assert str(squat) in str(refused.value)
    squat.unlink()
    squat.mkdir(mode=0o755)
    squat.chmod(0o755)
    with pytest.raises(SessionLockError, match="0700"):
        session_lock.private_lock_dir()


def test_the_private_lock_dir_refuses_another_users_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(session_lock, "_TMP_ROOT", tmp_path)
    (tmp_path / f".tau-{os.getuid() + 1}").mkdir(mode=0o700)
    monkeypatch.setattr(session_lock.os, "getuid", lambda: os.stat(tmp_path).st_uid + 1)
    with pytest.raises(SessionLockError, match="owned by uid"):
        session_lock.private_lock_dir()


def test_an_unusable_lock_dir_refuses_writing_naming_it(tmp_path, monkeypatch):
    def refused() -> Path:
        raise SessionLockError(errno.ELOOP, f"refusing lock directory {tmp_path}: it is a symlink")

    lock = SessionWriteLock(lambda: refused() / "x.lock")
    lock.retain()
    assert lock.access().read_only and str(tmp_path) in (lock.access().reason or "")
    lock.release()
