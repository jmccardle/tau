"""Finding the project and trusting it (D13).

A project is the nearest directory upward holding a `.tau/` that is a real
directory owned by the user. Its config and `.env` are read only while it is
trusted. Trust is pinned to the bytes of `.tau/config.json` and of `.env`:
`trusted.json` holds their sha256, which decides, and a redacted copy of the
config for showing diffs. Content decides: there is no owner check on either file.

The files are read once per `ensure_trusted` call. The report, the question,
and the approval all work from those bytes; approval re-reads and refuses if
they changed. Everything inside a project is read through one helper that
refuses symlinks and anything not a regular file.
"""

import contextlib
import difflib
import errno
import hashlib
import json
import os
import shlex
import stat
import unicodedata
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TextIO, cast

from tau_next._config import ConfigError
from tau_next._config_layers import LayerText, ModelCatalog, check_config, read_models
from tau_next._dotenv import DotenvError, parse_dotenv
from tau_next.providers import builtin_providers

TAU_DIR = ".tau"
_ENV = ".env"
_GITIGNORE = "*\n"
_PRIVATE = 0o700
_PRIVATE_FILE = 0o600
_PUBLIC_FILE = 0o644
_WRITABLE_BY_OTHERS = 0o022
_MAX_BYTES = 1 << 20
_LATIN1_MAX = 0xFF
_BMP_MAX = 0xFFFF
_SECRET = "(secret)"


_SURROGATE_BYTES = range(0xDC80, 0xDD00)  # a byte that was not UTF-8, as surrogateescape holds it
_UNSAFE = ("Cc", "Cf", "Cs", "Zl", "Zp")


def safe_text(text: str) -> str:
    """`text` with every control, format (such as right-to-left override), line and paragraph
    separator, and surrogate character written as an escape (`\\x1b`, `\\u202e`), so nothing a
    project file or a directory name says can move the cursor, repaint or reorder a line, or
    fail to encode. A byte that was not UTF-8 in a name shows as `\\xNN`. Newlines are escaped
    too: use `safe_block` for text of several lines."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if unicodedata.category(ch) not in _UNSAFE:
            out.append(ch)
        elif code in _SURROGATE_BYTES:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif code <= _LATIN1_MAX:
            out.append(f"\\x{code:02x}")
        elif code <= _BMP_MAX:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


def safe_block(text: str) -> str:
    """`text` split on `"\\n"` only, each line through `safe_text`, joined again: the lines a
    caller wrote stay lines, and nothing in the data can start another."""
    return "\n".join(safe_text(line) for line in text.split("\n"))


def _p(path: Path) -> str:
    return safe_text(str(path))


class TrustError(PermissionError):
    """A project directory tau cannot or will not trust, with the reason. `where` is the
    path the refusal is about (`None`: the project as a whole)."""

    def __init__(self, message: str, where: Path | None = None) -> None:
        super().__init__(message)
        self.where = where


def _refusal(path: Path, rest: str) -> TrustError:
    return TrustError(f"{_p(path)} {rest}", path)


@contextlib.contextmanager
def _unless_io_fails(path: Path, doing: str) -> Generator[None]:
    """An `OSError` inside is a `TrustError` naming `path` and what we were doing to it."""
    try:
        yield
    except TrustError:
        raise
    except OSError as e:
        raise _refusal(path, f"cannot be {doing}: {e.strerror or e}") from e


class UntrustedProject(TrustError):
    """The project is not trusted (or its config or `.env` changed). The message says what it
    sets and what is wrong with `.tau/`, and the command that trusts it unless it has errors
    to fix first; `layer` is the config it holds (`None`: no file), for showing its problems."""

    def __init__(self, root: Path, message: str, layer: LayerText | None) -> None:
        super().__init__(message, root)
        self.root = root
        self.layer = layer


@dataclass(frozen=True)
class Trusted:
    """A trusted project: its `root`, and the exact `.tau/config.json` and `.env` text that
    was approved (`None`: no such file). Use `layer()`, `read_models()` and
    `project_environment` rather than re-reading the files."""

    root: Path
    config_text: str | None
    env_text: str | None

    def layer(self) -> LayerText | None:
        """The project config layer for `read_models`, or `None` if there is no file."""
        if self.config_text is None:
            return None
        return LayerText(str(project_config_path(self.root)), self.config_text)

    def read_models(
        self, user: Path | LayerText | None = None, cli: Mapping[str, object] | None = None
    ) -> ModelCatalog:
        """`read_models(user, <the approved project config>, cli)`: the project layer is
        the text that was approved, not whatever the file says now."""
        return read_models(user, self.layer(), cli)


@dataclass(frozen=True)
class _Approval:
    sha256: str
    env_sha256: str
    redacted: str


@dataclass(frozen=True)
class _Seen:
    """What one read of the project showed: the text and digest of `.tau/config.json` and of
    `.env` (`None`: absent), and `.tau/`'s mode."""

    root: Path
    config: str | None
    config_sha: str
    env: str | None
    env_sha: str
    tau_mode: int


# -- paths


def _home_dir(environ: Mapping[str, str]) -> Path:
    home = environ.get("HOME")
    if not home:
        raise ConfigError("HOME is not set")
    return Path(home).resolve()


def tau_home(environ: Mapping[str, str]) -> Path:
    """`$TAU_HOME`, or `~/.tau`; a leading `~` means `$HOME` from `environ`."""
    given = environ.get("TAU_HOME")
    if not given:
        return _home_dir(environ) / TAU_DIR
    if given == "~" or given.startswith("~/"):
        return _home_dir(environ) / given[2:]
    path = Path(given)
    if not path.is_absolute():
        raise ConfigError(f"TAU_HOME must be an absolute path, not {given!r}")
    return path


def project_config_path(root: Path) -> Path:
    """`<root>/.tau/config.json`, for display. To read the project config use
    `Trusted.read_models`; never pass this path to `read_models`."""
    return root / TAU_DIR / "config.json"


class ProjectSearch(NamedTuple):
    """What `find_project` found: the project `root` (`None`: there is none) and, when there
    is none, why the nearest `.tau` it passed over is not a project (`None`: it passed none)."""

    root: Path | None
    skipped: str | None


def _not_a_project(tau: Path, info: os.stat_result) -> str | None:
    if stat.S_ISLNK(info.st_mode):
        return f"{_p(tau)} is a symlink; tau refuses symlinks inside a project"
    if stat.S_ISDIR(info.st_mode):
        return (
            f"{_p(tau)} is not owned by you (uid {info.st_uid}); "
            "take it over (chown) before trusting it"
        )
    return None


def _holds_tau_home(directory: Path, config_dir: Path) -> bool:
    """Whether `directory/.tau` is `TAU_HOME`: that directory is never a project."""
    return config_dir.name == TAU_DIR and config_dir.parent == directory


def find_project(start: Path, environ: Mapping[str, str]) -> ProjectSearch:
    """The nearest directory from `start` upward whose `.tau/` is a real directory owned
    by you. A symlinked or foreign `.tau` is passed over and the search goes on; if it
    ends without a project, the first one passed over is the `skipped` reason.

    The search stops at `$HOME` (excluded: its `.tau` is the user config) and
    never returns the directory whose `.tau` is `TAU_HOME`. Outside `$HOME` it
    runs to `/`.
    """
    home = _home_dir(environ)
    config_dir = tau_home(environ).resolve()
    here = start.resolve()
    skipped: str | None = None
    for directory in (here, *here.parents):
        if directory == home:
            break
        tau = directory / TAU_DIR
        try:
            info = tau.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid():
            if not _holds_tau_home(directory, config_dir):
                return ProjectSearch(directory, None)
        elif skipped is None:
            skipped = _not_a_project(tau, info)
    return ProjectSearch(None, skipped)


def refuse_special(path: Path, environ: Mapping[str, str]) -> None:
    """`TrustError` if `path` is `$HOME`, `TAU_HOME` or anything inside it, or the directory
    whose `.tau/` is `TAU_HOME` (the parent of a `TAU_HOME` named `.tau`): none is ever a
    project."""
    config = tau_home(environ).resolve()
    special = {_home_dir(environ): "your home directory"}
    special.setdefault(config, "TAU_HOME")
    what = special.get(path) or ("inside TAU_HOME" if config in path.parents else None)
    if what is None and _holds_tau_home(path, config):
        what = "the parent of TAU_HOME"
    if what is not None:
        raise TrustError(f"refusing to trust {_p(path)}: it is {what}")


def create_project(root: Path) -> None:
    """Make `<root>/.tau/` (mode 700) for `tau trust` to approve. `TrustError` if it cannot."""
    tau = root / TAU_DIR
    try:
        tau.mkdir(mode=_PRIVATE)
    except OSError as e:
        raise TrustError(f"cannot create {_p(tau)}: {e.strerror}", tau) from e


# -- reading inside a project, refusing symlinks


def _read_regular(path: Path) -> bytes | None:
    """The bytes of a regular file, or `None` if absent. A symlink or other kind of file is a
    `TrustError` naming the path, and so is one over the size limit or one that cannot be
    read. Opened non-blocking, so a FIFO swapped in cannot hang us."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise _refusal(path, "is a symlink; tau refuses symlinks inside a project") from e
        raise _refusal(path, f"cannot be read: {e.strerror}") from e
    with _unless_io_fails(path, "read"), os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise _refusal(path, "is not a regular file")
        if info.st_size > _MAX_BYTES:
            raise _refusal(path, f"is {info.st_size} bytes; the limit is {_MAX_BYTES}")
        raw = f.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise _refusal(path, f"grew past {_MAX_BYTES} bytes while it was being read")
        return raw


@contextlib.contextmanager
def _tau_dir(root: Path) -> Generator[tuple[int, os.stat_result]]:
    """`<root>/.tau` opened as a directory descriptor; a symlink or a non-directory is refused."""
    tau = root / TAU_DIR
    try:
        fd = os.open(tau, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            if tau.is_symlink():
                raise _refusal(tau, "is a symlink; tau refuses symlinks inside a project") from e
            raise _refusal(tau, "is not a directory") from e
        raise _refusal(tau, f"cannot be opened: {e.strerror}") from e
    try:
        yield fd, os.fstat(fd)
    finally:
        os.close(fd)


def _digest(raw: bytes | None) -> str:
    """sha256 of the bytes behind a marker byte, so a missing file differs from an empty one."""
    marker = b"\x00" if raw is None else b"\x01" + raw
    return hashlib.sha256(marker).hexdigest()


def _decode(raw: bytes | None, path: Path) -> str | None:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _refusal(path, "is not valid UTF-8") from e


def _read_pair(root: Path) -> tuple[bytes | None, bytes | None]:
    return _read_regular(project_config_path(root)), _read_regular(root / _ENV)


def _see(root: Path) -> _Seen:
    with _tau_dir(root) as (_, info):
        config, env = _read_pair(root)
    return _Seen(
        root,
        _decode(config, project_config_path(root)),
        _digest(config),
        _decode(env, root / _ENV),
        _digest(env),
        stat.S_IMODE(info.st_mode),
    )


# -- trusted.json


def _require_private(path: Path, fix: str) -> None:
    with _unless_io_fails(path, "inspected"), contextlib.suppress(FileNotFoundError):
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & _WRITABLE_BY_OTHERS:
            raise _refusal(
                path, f"is writable by group or others (mode {mode:o}); run: {fix} {_p(path)}"
            )


def _trusted_path(home: Path) -> Path:
    return home / "trusted.json"


def _approved(home: Path) -> dict[str, _Approval]:
    _require_private(home, "chmod 700")
    path = _trusted_path(home)
    _require_private(path, "chmod 600")
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise _refusal(path, f"cannot be read ({e.strerror}); fix or delete it") from e
    except ValueError as e:
        raise ConfigError(f"{path} is not tau's trusted.json ({e!r}); fix or delete it") from e
    try:
        projects = cast("dict[str, dict[str, object]]", cast("dict[str, object]", data)["projects"])
        # A record from before `.env` was pinned has no `env_sha256`: not approved.
        current = {root: r for root, r in projects.items() if "env_sha256" in r}
        keys = ("sha256", "env_sha256", "config")
        if not all(isinstance(r[k], str) for r in current.values() for k in keys):
            raise TypeError("a project's record is not text")
        return {
            root: _Approval(str(r["sha256"]), str(r["env_sha256"]), str(r["config"]))
            for root, r in current.items()
        }
    except (KeyError, TypeError, AttributeError) as e:
        raise ConfigError(f"{path} is not tau's trusted.json ({e!r}); fix or delete it") from e


def _blank(value: object) -> object:
    if isinstance(value, dict):
        d = cast("dict[str, object]", value)
        return {k: _SECRET if k == "api_key" else _blank(v) for k, v in d.items()}
    if isinstance(value, list):
        return [_blank(v) for v in cast("list[object]", value)]
    return value


def _redacted(text: str | None) -> str:
    """The config as canonical JSON (sorted keys, 2-space indent) with every `api_key` value
    replaced; `""` for no config. The text must be valid JSON (approval ensures it)."""
    if text is None:
        return ""
    return json.dumps(_blank(json.loads(text)), sort_keys=True, indent=2)


def _record(home: Path, seen: _Seen) -> None:
    projects = _approved(home)
    projects[str(seen.root.resolve())] = _Approval(
        seen.config_sha, seen.env_sha, _redacted(seen.config)
    )
    body = json.dumps(
        {
            "projects": {
                r: {"sha256": a.sha256, "env_sha256": a.env_sha256, "config": a.redacted}
                for r, a in projects.items()
            }
        },
        indent=2,
    )
    final = _trusted_path(home)
    scratch = final.with_name(f"trusted.json.{os.getpid()}.tmp")
    with _unless_io_fails(final, "written"):
        home.mkdir(mode=_PRIVATE, parents=True, exist_ok=True)
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_FILE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(body)
                out.flush()
                os.fsync(out.fileno())
            scratch.replace(final)
        except BaseException:
            scratch.unlink(missing_ok=True)
            raise


def _pinned(approval: _Approval | None, seen: _Seen) -> bool:
    return (
        approval is not None
        and approval.sha256 == seen.config_sha
        and approval.env_sha256 == seen.env_sha
    )


# -- the report


def user_config_path(home: Path) -> Path | None:
    """`<home>/config.json` if it exists, else `None` (no user config is fine). `TrustError`
    if `<home>` cannot be searched."""
    user = home / "config.json"
    with _unless_io_fails(user, "inspected"):
        return user if user.exists() else None


def _layer(seen: _Seen) -> LayerText | None:
    if seen.config is None:
        return None
    return LayerText(str(project_config_path(seen.root)), seen.config)


def _errors(seen: _Seen, home: Path) -> list[str]:
    """What keeps the project from being approved: config problems, a malformed `.env`."""
    layer = _layer(seen)
    errors: list[str] = []
    if layer is not None:
        # Trust asks only whether the file is sound; the CLI's providers are the built-in ones.
        problems = check_config(user_config_path(home), layer, providers=builtin_providers({}))
        errors = [safe_text(str(p)) for p in problems if p.severity == "error"]
    if seen.env is not None:
        try:
            parse_dotenv(seen.env, _p(seen.root / _ENV))
        except DotenvError as e:
            errors.append(safe_text(str(e)))
    return errors


def _env_names(seen: _Seen) -> list[str]:
    """Names set by the project's `.env` (never values); it parsed, or `_errors` said so."""
    if seen.env is None:
        return []
    try:
        return [safe_text(k) for k in parse_dotenv(seen.env, _ENV)]
    except DotenvError:
        return []


def _shown_config(seen: _Seen) -> list[str]:
    if seen.config is None:
        return ["  (no .tau/config.json)"]
    return [f"  {line}" for line in safe_block(_redacted(seen.config)).split("\n")]


def _gitignore_check(tau: Path) -> str:
    raw = _read_regular(tau / ".gitignore")
    if raw is None:
        return "  [fix] .tau/.gitignore is missing, approval writes it"
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if "*" in lines and not any(line.startswith("!") for line in lines):
        return "  [ok] .tau/.gitignore exists"
    return "  [warn] .tau/.gitignore does not ignore everything"


def _checks(seen: _Seen) -> list[str]:
    private = seen.tau_mode == _PRIVATE
    return [
        f"  [{'ok' if private else 'fix'}] {TAU_DIR}/ mode is {seen.tau_mode:o}"
        + ("" if private else ", approval sets 700"),
        _gitignore_check(seen.root / TAU_DIR),
    ]


def _changes(approval: _Approval, seen: _Seen) -> list[str]:
    lines: list[str] = []
    if approval.sha256 != seen.config_sha:
        diff = difflib.unified_diff(
            approval.redacted.splitlines(),
            _redacted(seen.config).splitlines(),
            "approved .tau/config.json",
            "current .tau/config.json",
            lineterm="",
        )
        lines += [safe_text(line) for line in diff] or [
            "  (no visible difference: a secret value changed, or only the formatting)"
        ]
    if approval.env_sha256 != seen.env_sha:
        lines.append("  .env changed since you approved it")
    return lines


@dataclass(frozen=True)
class _Assessment:
    report: str
    errors: tuple[str, ...]


def _assess(seen: _Seen, home: Path, approval: _Approval | None) -> _Assessment:
    state = "is not trusted" if approval is None else "has changed since you approved it"
    errors = _errors(seen, home)
    lines = [f"Project {_p(seen.root)} {state}.", ""]
    if errors:
        lines += ["It cannot be trusted until these are fixed:", *(f"  {e}" for e in errors)]
    else:
        lines += ["Its config:", *_shown_config(seen)]
    if names := _env_names(seen):
        lines += ["", f"Its .env sets: {', '.join(names)}"]
    lines += ["", "Checks:", *_checks(seen)]
    if approval is not None and not errors:
        lines += ["", "Changes since approval:", *_changes(approval, seen)]
    return _Assessment("\n".join(lines), tuple(errors))


# -- approving


def _write_gitignore(tau_fd: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(".gitignore", flags, _PUBLIC_FILE, dir_fd=tau_fd)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        os.fchmod(out.fileno(), _PUBLIC_FILE)
        out.write(_GITIGNORE)


def _approve(seen: _Seen, home: Path) -> Trusted:
    root = seen.root
    with _tau_dir(root) as (fd, _), _unless_io_fails(root / TAU_DIR, "set up for approval"):
        again = tuple(_digest(raw) for raw in _read_pair(root))
        if again != (seen.config_sha, seen.env_sha):
            raise TrustError(
                f"{_p(root)}: the config or .env changed while you were reading; run again", root
            )
        os.fchmod(fd, _PRIVATE)
        _write_gitignore(fd)
        _read_regular(root / TAU_DIR / ".gitignore")  # refuses a symlink that was already there
    _record(home, seen)
    return Trusted(root, seen.config, seen.env)


def ensure_trusted(root: Path, home: Path, *, ask: Callable[[str], bool] | None) -> Trusted:
    """Return the `Trusted` project if `root` is trusted: approved with exactly its current
    `.tau/config.json` and `.env`. Otherwise, with `ask` (a live terminal), show it the report
    and approve on yes (making `.tau/` mode 700 and writing `.tau/.gitignore` if missing); no
    `ask`, or a no, raises `UntrustedProject`. With an `ask`, a config or `.env` with errors
    cannot be approved: `TrustError` with the report. The files are read once: what is
    reported, asked about, and approved is those bytes, and approval refuses if they changed
    meanwhile."""
    seen = _see(root)
    approval = _approved(home).get(str(root.resolve()))
    if _pinned(approval, seen):
        return Trusted(root, seen.config, seen.env)
    assessment = _assess(seen, home, approval)
    hint = "" if assessment.errors else f"\nTrust it with: tau trust {shlex.quote(_p(root))}"
    refusal = UntrustedProject(root, assessment.report + hint, _layer(seen))
    if ask is None:
        raise refusal
    if assessment.errors:
        raise TrustError(assessment.report, root)
    if not ask(assessment.report):
        raise refusal
    return _approve(seen, home)


def is_interactive(stdin: TextIO, stdout: TextIO) -> bool:
    """Whether both streams are terminals, so a person can be asked."""
    return stdin.isatty() and stdout.isatty()


def terminal_ask(stdin: TextIO, stdout: TextIO) -> Callable[[str], bool]:
    """An `ask` that prints the report to `stdout` and reads `y`/`yes` (else no) from `stdin`."""

    def ask(report: str) -> bool:
        stdout.write(f"{report}\n\nTrust this project? [y/N] ")
        stdout.flush()
        line = stdin.readline()
        if not line:  # end of input: leave the prompt line
            stdout.write("\n")
        return line.strip().lower() in ("y", "yes")

    return ask


def project_environment(project: Trusted | None, environ: Mapping[str, str]) -> dict[str, str]:
    """`environ` over the approved `.env` of a trusted project (`None`: no project, just
    `environ`); the shell environment wins."""
    found: dict[str, str] = {}
    if project is not None and project.env_text is not None:
        found = parse_dotenv(project.env_text, _p(project.root / _ENV))
    return {**found, **environ}
