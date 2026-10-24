"""The per-session write lock: one writing process per stored session (docs/STEP-DEBUGGER.md §3.7.3).

A process that hosts a session for writing holds an exclusive, non-blocking
``fcntl.flock`` on a sidecar file beside it, and keeps it until the last cursor
on that session closes. Another process opening the same session gets a
read-only replica: every write through it raises :class:`SessionReadOnlyError`,
naming the holder recorded in the sidecar.

``flock`` locks belong to the open file description, so a second ``open`` and
``flock`` in the same process would refuse exactly as another process does.
The process therefore keeps one registry of descriptors keyed by the sidecar's
``(st_dev, st_ino)``, counted across every :class:`SessionWriteLock` (one per
session object) on that file. The descriptor closes, and the lock is released,
when that count reaches zero; it is never dropped while a cursor still writes.

Descriptors are opened ``O_CLOEXEC``, so a child that ``exec``\\ s never
inherits the lock. **A bare ``os.fork()`` without ``exec`` does**: the child
shares the open file description, and the lock stays held until both processes
have closed it. τ's own subprocesses all ``exec``.

``flock`` is advisory, and on some filesystems it succeeds while excluding
nothing. :func:`lock_blind_filesystem` reads the mount from
``/proc/self/mountinfo`` (the only place NFS's ``nolock``/``local_lock`` options
are visible); a lock-blind mount still opens for writing, with a warning that
``sessions.allow_unlocked_fs`` silences. A ``flock`` that raises anything but
"held" refuses writing with that reason.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import socket
import stat
import sys
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tau_llm.docs import agent_facing

LOCK_SUFFIX = ".lock"
"""The sidecar's suffix, appended to the session file's full name."""

READ_ONLY_WARNING_KEY = "session-lock:read-only"
"""The ``tau`` warning a session carries while its tree is open read-only."""

UNLOCKED_FS_WARNING_KEY = "session-lock:unlocked-fs"
"""The ``tau`` warning a session carries while its lock sits on a lock-blind filesystem."""

_TMP_ROOT = Path("/tmp")

_head_label = "sdk"
_head_detail: str | None = None


def set_head_label(label: str, detail: str | None = None) -> None:
    """Name this process's head in every sidecar it writes from now on.

    Args:
        label: ``"tui"``, ``"repl"``, ``"-p"``, ``"rpc"`` or ``"serve"``; an
            embedder keeps the default ``"sdk"``.
        detail: Where to reach it instead, such as serve's socket address.
    """
    global _head_label, _head_detail
    _head_label = label
    _head_detail = detail


def head_label() -> str:
    """The head this process names in its sidecars (:func:`set_head_label`)."""
    return _head_label


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class LockHolder:
    """The process a sidecar says holds a session's write lock.

    Read from the sidecar, which the holder writes after taking the lock, so a
    field is ``None`` when the holder has not written it yet or wrote garbage.

    Attributes:
        pid: The holder's process id.
        host: The holder's hostname.
        head: Which head it runs: ``tui``, ``repl``, ``-p``, ``rpc``, ``serve`` or ``sdk``.
        started: When it took the lock, ISO-8601 UTC.
        detail: Where to reach the holder instead (serve's address), or ``None``.
    """

    pid: int | None
    host: str | None
    head: str | None
    started: str | None
    detail: str | None = None

    def describe(self) -> str:
        """Name the holder for a refusal.

        Returns:
            ``pid 4242 (tui) on box since 2026-10-24T09:00:00Z``, unknown parts left out.
        """
        if self.pid is None and self.head is None:
            return "another process (its sidecar names no holder yet)"
        parts = [f"pid {self.pid}" if self.pid is not None else "an unknown pid"]
        if self.head:
            parts.append(f"({self.head} at {self.detail})" if self.detail else f"({self.head})")
        if self.host:
            parts.append(f"on {self.host}")
        if self.started:
            parts.append(f"since {self.started}")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """The wire form.

        Returns:
            ``{pid, host, head, started, detail}``.
        """
        return {
            "pid": self.pid,
            "host": self.host,
            "head": self.head,
            "started": self.started,
            "detail": self.detail,
        }


@agent_facing(topic="sessions")
@dataclass(frozen=True)
class WriteAccess:
    """Whether this process may write a session, and if not, why.

    Attributes:
        read_only: Writes are refused.
        holder: The process that holds the lock, when that is the reason.
        reason: The refusal every write and submit carries; ``None`` when writable.
        warning: Set when the lock is held but cannot exclude anyone (a
            lock-blind filesystem); ``None`` otherwise.
    """

    read_only: bool
    holder: LockHolder | None
    reason: str | None
    warning: str | None = None


WRITABLE = WriteAccess(read_only=False, holder=None, reason=None)
"""The access of a session nothing locks: an ephemeral one, or an SDK log."""


@agent_facing(topic="sessions")
class SessionReadOnlyError(RuntimeError):
    """A write to a session this process opened read-only.

    Args:
        reason: The refusal, naming the holder when there is one.
        holder: As the attribute.

    Attributes:
        holder: The process holding the session's lock, or ``None`` when the
            lock could not be taken at all.
    """

    def __init__(self, reason: str, holder: LockHolder | None) -> None:
        """Carry the refusal and the holder it names."""
        super().__init__(reason)
        self.holder = holder


class SessionLockError(OSError):
    """A lock directory or sidecar τ will not use, named in the message."""


class _Entry:
    """One held sidecar descriptor in :data:`_REGISTRY`."""

    def __init__(self, path: Path, fd: int, key: tuple[int, int]) -> None:
        """Register one held descriptor with a count of one."""
        self.path = path
        self.fd = fd
        self.key = key
        self.count = 1
        self.lost: LockHolder | None = None
        self.merged_into: _Entry | None = None


def _live(entry: _Entry) -> _Entry:
    """The entry ``entry`` was folded into, following every fold; ``entry`` itself if none."""
    while entry.merged_into is not None:
        entry = entry.merged_into
    return entry


_REGISTRY: dict[tuple[int, int], _Entry] = {}
_GUARD = threading.RLock()
_PENDING: deque[SessionWriteLock] = deque()
_draining = False


def release_later(locks: Iterable[SessionWriteLock]) -> None:
    """Queue one release of each lock, run at the start of the next acquire or release.

    For a garbage-collector finalizer: it may fire inside :func:`_acquire`
    under the re-entrant guard, where releasing at once could close the
    descriptor that call just looked up.

    Args:
        locks: Each is released once, later.
    """
    _PENDING.extend(locks)


def _drain_pending() -> None:
    """Run queued releases; the caller holds ``_GUARD`` and has not touched an entry yet."""
    global _draining
    if _draining:
        return
    _draining = True
    try:
        while _PENDING:
            lock = _PENDING.popleft()
            try:
                lock.release()
            except Exception:
                logging.getLogger(__name__).exception("queued release of %s failed", lock.sidecar)
    finally:
        _draining = False


def held_descriptor_count() -> int:
    """How many sidecar descriptors this process holds open, for tests and diagnostics."""
    with _GUARD:
        return len(_REGISTRY)


def _now_iso() -> str:
    """Now, UTC, to the second, ``Z``-suffixed."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_holder(fd: int) -> None:
    """Replace the sidecar's contents with this process's holder record."""
    record = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "head": _head_label,
        "detail": _head_detail,
    }
    data = json.dumps({**record, "started": _now_iso()}).encode()
    os.ftruncate(fd, 0)
    os.pwrite(fd, data, 0)


def _read_holder(fd: int) -> LockHolder:
    """The holder record in a sidecar; unknown fields ``None`` when it is empty or garbage."""
    try:
        raw = json.loads(os.pread(fd, 4096, 0) or b"{}")
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    pid = raw.get("pid")
    return LockHolder(
        pid=pid if isinstance(pid, int) else None,
        host=str(raw["host"]) if raw.get("host") else None,
        head=str(raw["head"]) if raw.get("head") else None,
        started=str(raw["started"]) if raw.get("started") else None,
        detail=str(raw["detail"]) if raw.get("detail") else None,
    )


def _open_sidecar(path: Path) -> int:
    """Open or create ``path`` read-write, ``O_CLOEXEC``, ``0600``."""
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)


def _flock(fd: int, path: Path) -> bool:
    """Try the lock; ``False`` when another description holds it.

    Raises:
        SessionLockError: ``flock`` failed for another reason (unsupported here).
    """
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        raise SessionLockError(
            exc.errno, f"flock is unavailable on {path}: {exc.strerror}"
        ) from exc


def _acquire(path: Path) -> _Entry | LockHolder:
    """A counted reference to ``path``'s lock, or the holder when another process has it."""
    with _GUARD:
        _drain_pending()
        fd = _open_sidecar(path)
        try:
            st = os.fstat(fd)
            key = (st.st_dev, st.st_ino)
            entry = _REGISTRY.get(key)
            if entry is not None:
                os.close(fd)
                entry.count += 1
                return entry
            if not _flock(fd, path):
                holder = _read_holder(fd)
                os.close(fd)
                return holder
            _write_holder(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        entry = _Entry(path, fd, key)
        _REGISTRY[key] = entry
        return entry


def _release(entry: _Entry) -> None:
    """Drop one reference; at zero close the descriptor, which releases the lock."""
    with _GUARD:
        _drain_pending()
        entry = _live(entry)
        entry.count -= 1
        if entry.count > 0:
            return
        if _REGISTRY.get(entry.key) is entry:
            del _REGISTRY[entry.key]
        os.close(entry.fd)


def _verify(entry: _Entry) -> LockHolder | None:
    """Re-check that ``entry`` still locks the file at its path; re-take it if replaced.

    A replaced file this process already holds through another entry (a
    second session object retained after the delete) gets this entry's count
    folded in, and this entry's descriptor on the unlinked inode is closed.

    Returns:
        ``None`` when this process holds the lock on the file now at the path;
        the other holder when the sidecar was replaced and another process took
        the new one.
    """
    with _GUARD:
        entry = _live(entry)
        if entry.lost is not None:
            return entry.lost
        try:
            st = os.stat(entry.path)
            if (st.st_dev, st.st_ino) == entry.key:
                return None
        except FileNotFoundError:
            pass
        fd = _open_sidecar(entry.path)
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino)
        other = _REGISTRY.get(key)
        if other is not None:
            os.close(fd)
            other.count += entry.count
            if _REGISTRY.get(entry.key) is entry:
                del _REGISTRY[entry.key]
            os.close(entry.fd)
            entry.count = 0
            entry.merged_into = other
            return None
        if _flock(fd, entry.path):
            _write_holder(fd)
            os.close(entry.fd)
            entry.fd = fd
            _REGISTRY[key] = entry
        else:
            entry.lost = _read_holder(fd)
            os.close(fd)
            return entry.lost
        if _REGISTRY.get(entry.key) is entry:
            del _REGISTRY[entry.key]
        entry.key = key
        return None


def _reread_holder(path: Path, known: LockHolder) -> LockHolder:
    """The holder the sidecar names now, or ``known`` when it names none (mid-write, or gone)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return known
    try:
        fresh = _read_holder(fd)
    finally:
        os.close(fd)
    return fresh if fresh.pid is not None else known


def _unescape_mount(field: str) -> str:
    """Undo mountinfo's octal escapes (``\\040`` is a space)."""
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and field[i + 1 : i + 4].isdigit() and i + 4 <= len(field):
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def mount_of(directory: Path) -> tuple[str, set[str]] | None:
    """The filesystem type and mount options of the mount holding ``directory``.

    Reads ``/proc/self/mountinfo`` and takes the longest mount point containing
    the resolved path; the options are the per-mount and superblock sets merged.

    Returns:
        ``(fstype, options)``, or ``None`` where there is no mountinfo (not Linux).
    """
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return None
    target = os.path.realpath(directory)
    best: tuple[int, str, set[str]] | None = None
    for line in lines:
        head, sep, tail = line.partition(" - ")
        fields, rest = head.split(), tail.split()
        if not sep or len(fields) < 6 or len(rest) < 3:
            continue
        point = _unescape_mount(fields[4])
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and (best is None or len(point) >= best[0]):
            options = set(fields[5].split(",")) | set(rest[2].split(","))
            best = (len(point), rest[0], options)
    return None if best is None else (best[1], best[2])


_LOCK_BLIND_TYPES = frozenset(
    {"cifs", "smb3", "smbfs", "sshfs", "fuse", "9p", "virtiofs", "vboxsf"}
)


def lock_blind_filesystem(directory: Path) -> str | None:
    """Name the filesystem under ``directory`` if ``flock`` there excludes nothing.

    Any FUSE filesystem (``fuse``, ``fuse.*``), 9p, virtiofs and vboxsf count
    as blind: each hands ``flock`` to a daemon or host that need not honour it.
    Sync folders (Dropbox, Syncthing) sit on a local filesystem and cannot be
    told apart here; the record's platform matrix says so.

    Returns:
        ``"cifs"``, ``"fuse.sshfs"``, ``"nfs4 (local_lock=flock)"`` and the like;
        ``"an unprobed filesystem (<platform>)"`` where the mount cannot be read;
        ``None`` for a filesystem whose ``flock`` is trusted.
    """
    mount = mount_of(directory)
    if mount is None:
        return f"an unprobed filesystem ({sys.platform})"
    fstype, options = mount
    if fstype in _LOCK_BLIND_TYPES or fstype.startswith("fuse."):
        return fstype
    if fstype in ("nfs", "nfs4"):
        for option in ("nolock", "local_lock=flock", "local_lock=all"):
            if option in options:
                return f"{fstype} ({option})"
    return None


_NETWORK_TYPES = ("nfs", "cifs", "smb", "9p", "sshfs", "fuse.sshfs", "fuse.rclone")


def blind_filesystem_warning(blind: str) -> str:
    """The session warning for a filesystem :func:`lock_blind_filesystem` named.

    A network filesystem gets the strong wording. A local FUSE or host-shared
    one (encfs, mergerfs, virtiofs) locks on this host, so the wording says
    only that other hosts or mounts may go undetected.

    Args:
        blind: What :func:`lock_blind_filesystem` returned.

    Returns:
        One line for the warnings store.
    """
    if blind.split(" ")[0].startswith(_NETWORK_TYPES):
        return f"cross-process exclusion unavailable on {blind}; a second writer is not detected"
    return (
        f"cross-process locking on {blind} may not reach other hosts or mounts; "
        "a writer elsewhere may not be detected"
    )


@agent_facing(topic="sessions")
class SessionWriteLock:
    """One session object's share of its sidecar's lock, counted by its hosts.

    A store gives each session object one of these (``write_lock``). Every host
    (an ``AgentSession`` with a live cursor on the session) calls
    :meth:`retain` once and :meth:`release` once; the object takes one registry
    reference on the first retain and drops it on the last release. A store
    calls :meth:`guard` before each write.

    While nothing retains it, a write is not guarded: a library caller writing a
    session it never hosted gets the pre-lock behaviour.
    """

    def __init__(
        self,
        sidecar: Path | Callable[[], Path],
        *,
        ready: Callable[[], bool] = lambda: True,
        allow_unlocked_fs: bool = False,
        probe_directory: Path | None = None,
        on_acquire: Callable[[], bool] | None = None,
    ) -> None:
        """Bind to a sidecar path.

        Args:
            sidecar: The lock file, or a callable resolving it when the lock is
                first taken; an ``OSError`` it raises refuses writing, naming why.
            ready: Whether the sidecar may be created now; a deferred session
                answers ``False`` until its first write and then calls :meth:`attach`.
            allow_unlocked_fs: ``sessions.allow_unlocked_fs``: trust ``flock``
                on a lock-blind filesystem, without the warning.
            probe_directory: Where to read the filesystem type; the sidecar's
                directory by default.
            on_acquire: Called once the lock is held; the store re-reads what
                another writer appended since it loaded, returning whether it did.
        """
        self._sidecar = sidecar
        self._ready = ready
        self._allow_unlocked_fs = allow_unlocked_fs
        self._probe_directory = probe_directory
        self._on_acquire = on_acquire
        self._refreshed = False
        self._seen_key: tuple[int, int] | None = None
        self._retains = 0
        self._entry: _Entry | None = None
        self._holder: LockHolder | None = None
        self._refusal: str | None = None
        self._warning: str | None = None

    @property
    def sidecar(self) -> Path:
        """The lock file's path, resolved now if it was given as a callable."""
        return self._sidecar if isinstance(self._sidecar, Path) else self._sidecar()

    @property
    def retained(self) -> bool:
        """Whether any host holds this session open for writing."""
        return self._retains > 0

    def retain(self) -> None:
        """Open for writing once more; the first call takes the lock or records the holder."""
        self._retains += 1
        if self._retains == 1:
            try:
                self._take()
            except BaseException:
                self._retains -= 1
                if self._entry is not None:
                    _release(self._entry)
                self._entry = None
                raise

    def release(self) -> None:
        """Drop one retain; the last drops this object's registry reference.

        Raises:
            RuntimeError: nothing retains it.
        """
        if self._retains == 0:
            raise RuntimeError(f"release of {self.sidecar} without a retain")
        self._retains -= 1
        if self._retains:
            return
        if self._entry is not None:
            _release(self._entry)
        self._entry = None
        self._holder = None
        self._refusal = None
        self._warning = None

    def attach(self) -> None:
        """Take the lock now that the sidecar may exist (a deferred session's first write)."""
        if self._retains and self._entry is None and self._holder is None and not self._refusal:
            self._take()

    def _probe(self, sidecar: Path) -> str | None:
        """The lock-blind filesystem warning for ``sidecar``'s directory, or ``None``."""
        if self._allow_unlocked_fs:
            return None
        blind = lock_blind_filesystem(self._probe_directory or sidecar.parent)
        return None if blind is None else blind_filesystem_warning(blind)

    def _take(self) -> None:
        """Take a registry reference, or record the holder or the refusal."""
        try:
            sidecar = self.sidecar
            self._warning = self._probe(sidecar)
            if not self._ready():
                return
            got = _acquire(sidecar)
        except OSError as exc:
            self._refusal = f"session opened read-only: {exc.strerror or exc}"
            return
        if isinstance(got, LockHolder):
            self._holder = got
            return
        self._entry = got
        self._seen_key = got.key
        self._refresh()

    def _refresh(self) -> None:
        """Have the store re-read what another writer appended; read-only, released, if it cannot."""
        if self._on_acquire is None:
            return
        try:
            if self._on_acquire():
                self._refreshed = True
        except Exception as exc:
            self._refusal = (
                f"session opened read-only: it could not be re-read after taking its lock: {exc}"
            )
            if self._entry is not None:
                _release(self._entry)
            self._entry = None

    def take_refreshed(self) -> bool:
        """Whether the store reloaded entries when the lock was taken; resets the flag.

        Returns:
            True once after a reload, so a host can move cursors off the stale leaf.
        """
        refreshed, self._refreshed = self._refreshed, False
        return refreshed

    def access(self) -> WriteAccess:
        """This process's write access to the session now, without re-checking the sidecar.

        Runs releases a collected host queued (:func:`release_later`) first,
        so a long-lived process frees them at its next banner or admission.

        Returns:
            Writable, or read-only with the reason and holder.
        """
        with _GUARD:
            _drain_pending()
        if self._holder is not None:
            self._holder = _reread_holder(self.sidecar, self._holder)
        entry = _live(self._entry) if self._entry is not None else None
        if entry is not None and entry.lost is not None:
            entry.lost = _reread_holder(entry.path, entry.lost)
        holder = self._holder or (entry.lost if entry is not None else None)
        if holder is not None:
            reason = (
                f"session is open read-only: {holder.describe()} holds it for writing "
                f"({self.sidecar})"
            )
            return WriteAccess(read_only=True, holder=holder, reason=reason, warning=self._warning)
        if self._refusal is not None:
            return WriteAccess(
                read_only=True, holder=None, reason=self._refusal, warning=self._warning
            )
        return WriteAccess(read_only=False, holder=None, reason=None, warning=self._warning)

    def check(self) -> WriteAccess:
        """Re-check the held lock against the sidecar on disk (turn admission).

        A sidecar unlinked or replaced since it was locked is re-taken on the
        path, and the store re-reads what another writer may have appended in
        between; if another process got the new file first, this object turns
        read-only, naming it.

        Returns:
            The access after the re-check.
        """
        if self._entry is not None and self._retains:
            try:
                _verify(self._entry)
            except OSError as exc:
                self._refusal = f"session lock lost: {exc.strerror or exc}"
                return self.access()
            live = _live(self._entry)
            if live.lost is None and live.key != self._seen_key:
                self._seen_key = live.key
                self._refresh()
        return self.access()

    def guard(self) -> None:
        """Refuse a write while this process holds the session read-only.

        Raises:
            SessionReadOnlyError: another process holds the lock, or it could
                not be taken.
        """
        if not self._retains:
            return
        access = self.access()
        if access.read_only:
            raise SessionReadOnlyError(access.reason or "session is open read-only", access.holder)


def write_lock_of(log: object) -> SessionWriteLock | None:
    """The :class:`SessionWriteLock` a stored session carries, or ``None`` for one nothing locks.

    Args:
        log: Any ``SessionLog``; a store opts in with a ``write_lock`` attribute.
    """
    lock = getattr(log, "write_lock", None)
    return lock if isinstance(lock, SessionWriteLock) else None


def sidecar_for(session_file: Path) -> Path:
    """``<session file>.lock``: the sidecar beside a file-store session."""
    return session_file.with_name(session_file.name + LOCK_SUFFIX)


def private_lock_dir() -> Path:
    """The host-local directory for sidecars of sessions with no file of their own (JMFTS).

    ``$XDG_RUNTIME_DIR/tau/locks`` when that is set, else ``/tmp/.tau-<uid>/locks``.
    Each level τ creates is made ``0700``; one that already exists must be a real
    directory (not a symlink), owned by this user, with no group or other access.

    Raises:
        SessionLockError: a level exists as a symlink, a non-directory, another
            user's directory, or with group or other permissions; named.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime)
        levels = [base / "tau", base / "tau" / "locks"]
    else:
        base = _TMP_ROOT / f".tau-{os.getuid()}"
        levels = [base, base / "locks"]
    for level in levels:
        _ensure_private_dir(level)
    return levels[-1]


def _ensure_private_dir(path: Path) -> None:
    """``mkdir(0700)`` ``path``, or verify an existing one is a private directory we own."""
    try:
        os.mkdir(path, 0o700)
        return
    except FileExistsError:
        pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise SessionLockError(errno.ELOOP, f"refusing lock directory {path}: it is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise SessionLockError(errno.ENOTDIR, f"refusing lock directory {path}: not a directory")
    if st.st_uid != os.getuid():
        raise SessionLockError(
            errno.EPERM, f"refusing lock directory {path}: owned by uid {st.st_uid}, not you"
        )
    if st.st_mode & 0o077:
        raise SessionLockError(
            errno.EPERM,
            f"refusing lock directory {path}: mode {stat.S_IMODE(st.st_mode):o} "
            "lets other users in; it must be 0700",
        )


@agent_facing(topic="sessions")
def read_only_banner(warnings: list[Any]) -> str | None:
    """The banner a head shows while its session is a read-only replica.

    Args:
        warnings: The current warnings, as ``AgentSession.extension_warnings`` reads them.

    Returns:
        ``"READ-ONLY — <reason naming the holder>"``, or ``None`` when writable.
    """
    for warning in warnings:
        if getattr(warning, "key", None) == READ_ONLY_WARNING_KEY:
            return f"READ-ONLY — {warning.message}"
    return None
