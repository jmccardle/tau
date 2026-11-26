"""Finding the project and trusting it (D13).

A project is the nearest directory upward holding a `.tau/` that is a real
directory owned by the user. Its config and `.env` are read only while it is
trusted. Trust is pinned to the bytes of `.tau/config.json`, of `.env`, of every file under
`.tau/extensions/`, of `.tau/SYSTEM.md` (D19 section 4), of `.tau/prompts/*.md` and of
`.tau/skills/*/SKILL.md` (D24 section 6): `trusted.json` holds their sha256,
which decides, and a redacted copy of the config for showing diffs. Content decides: there is
no owner check on any file. What is used afterwards is the bytes that were approved.

The files are read once per `ensure_trusted` call. The report, the question,
and the approval all work from those bytes; approval re-reads and refuses if
they changed. Everything inside a project is read through one helper that
refuses symlinks and anything not a regular file; the one exception is the prompt and skill
files, which may be (or be in) symlinks, read through to the regular file they lead to, with the
resolved path in the report: what is pinned is those bytes.
"""

import contextlib
import difflib
import errno
import os
import shlex
import stat
import sys
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TextIO

from tau._config import ConfigError
from tau._config_layers import LayerText, ModelCatalog, check_config, read_models
from tau._dotenv import DotenvError, parse_dotenv
from tau._mcp_config import PROJECT_FILES as MCP_FILES
from tau._mcp_config import describe as describe_mcp
from tau._project_read import (
    FOREIGN_BASES,
    TrustError,
    prompt_links,
    read_prompt_files,
    read_regular,
    refusal,
    safe_block,
    safe_text,
    shown,
    unless_io_fails,
)
from tau._prompt_files import problems_in, prompt_report
from tau._providers import builtin_providers
from tau._trust_record import Approval as _Approval
from tau._trust_record import approved as _approved
from tau._trust_record import digest as _digest
from tau._trust_record import file_hashes as _file_hashes
from tau._trust_record import files_digest as _extensions_digest
from tau._trust_record import record, redacted
from tau._trust_report import hooks_listing, hooks_problems

TAU_DIR = ".tau"
_ENV = ".env"
_GITIGNORE = "*\n"
_PRIVATE = 0o700
_PUBLIC_FILE = 0o644
EXTENSIONS_DIR = "extensions"
HOOKS_FILE = "hooks.json"
SYSTEM_MD = "SYSTEM.md"


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
    was approved (`None`: no such file). `extension_files` is every file under
    `.tau/extensions/` as approved (path relative to it, with `/`, to bytes; `None`: there is no
    such directory), which the extension loader imports from and never re-reads; `system_md` the
    bytes of `.tau/SYSTEM.md` as approved (`None`: no such file), which the prompt's assembly
    reads. `prompt_files` are the approved `prompts/NAME.md` and `skills/NAME/SKILL.md` (path
    relative to `.tau/`, with `/`, to bytes) and the project's `.agents/skills/NAME/SKILL.md` and
    `.claude/skills/NAME/SKILL.md` (path relative to the project), which the skills extension
    reads. `mcp_files` are the approved `.mcp.json` and `.tau/mcp.json` (path relative to the
    project, to bytes; only those that exist), which the mcp extension reads.
    `hooks_file` is the bytes of `.tau/hooks.json` as approved (`None`: no such file), which the
    `hooks` extension runs. Use `layer()`,
    `read_models()` and `project_environment` rather than re-reading the files."""

    root: Path
    config_text: str | None
    env_text: str | None
    extension_files: Mapping[str, bytes] | None
    system_md: bytes | None
    prompt_files: Mapping[str, bytes]
    mcp_files: Mapping[str, bytes]
    hooks_file: bytes | None = None

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
class _Seen:
    """What one read of the project showed: the text and digest of `.tau/config.json` and of
    `.env` (`None`: absent), the files of `.tau/extensions/` and the bytes of `.tau/SYSTEM.md`
    with their digests, and `.tau/`'s mode."""

    root: Path
    config: str | None
    config_sha: str
    env: str | None
    env_sha: str
    extensions: dict[str, bytes] | None
    extensions_sha: str
    system: bytes | None
    system_sha: str
    prompts: dict[str, bytes]
    prompts_sha: str
    mcp: dict[str, bytes]
    mcp_sha: str
    hooks: bytes | None
    hooks_sha: str
    tau_mode: int
    prompt_targets: Mapping[str, Path]  # prompt key -> where its symlink leads (not pinned)


# -- paths


def _home_dir(environ: Mapping[str, str]) -> Path:
    """`$HOME`; on Windows, where a shell need not set it, `%USERPROFILE%` when it is unset."""
    home = environ.get("HOME")
    if not home and sys.platform == "win32":
        home = environ.get("USERPROFILE")
        if not home:
            raise ConfigError("neither HOME nor USERPROFILE is set")
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
        return f"{shown(tau)} is a symlink; tau refuses symlinks inside a project"
    if stat.S_ISDIR(info.st_mode):
        return (
            f"{shown(tau)} is not owned by you (uid {info.st_uid}); "
            "take it over (chown) before trusting it"
        )
    return None


def _owned_by_you(tau: Path, info: os.stat_result) -> bool:
    """Whether the directory `tau` (its `lstat` is `info`) is the effective user's. On Windows a
    file has no owner uid, and checking its owner and who may write it (D13) is not ported:
    finding a project there is a `TrustError` that says so."""
    refuse_on_windows(tau)
    return info.st_uid == os.geteuid()


def refuse_on_windows(tau: Path) -> None:
    """`TrustError` on Windows, naming the `.tau` directory `tau` that a project would need."""
    if sys.platform == "win32":
        raise TrustError(
            f"{shown(tau)}: projects are not available on Windows yet: tau cannot check there "
            f"who owns {TAU_DIR}/ and who may change it",
            tau,
        )


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
        if stat.S_ISDIR(info.st_mode) and _holds_tau_home(directory, config_dir):
            continue  # TAU_HOME itself: never a project, and its owner is not asked about
        if stat.S_ISDIR(info.st_mode) and _owned_by_you(tau, info):
            return ProjectSearch(directory, None)
        if skipped is None:
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
        raise TrustError(f"refusing to trust {shown(path)}: it is {what}")


def create_project(root: Path) -> None:
    """Make `<root>/.tau/` (mode 700) for `tau trust` to approve. `TrustError` if it cannot."""
    tau = root / TAU_DIR
    refuse_on_windows(tau)
    try:
        tau.mkdir(mode=_PRIVATE)
    except OSError as e:
        raise TrustError(f"cannot create {shown(tau)}: {e.strerror}", tau) from e


# -- reading inside a project, refusing symlinks


@contextlib.contextmanager
def _tau_dir(root: Path) -> Generator[tuple[int, os.stat_result]]:
    """`<root>/.tau` opened as a directory descriptor; a symlink or a non-directory is refused."""
    tau = root / TAU_DIR
    try:
        fd = os.open(tau, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            if tau.is_symlink():
                raise refusal(tau, "is a symlink; tau refuses symlinks inside a project") from e
            raise refusal(tau, "is not a directory") from e
        raise refusal(tau, f"cannot be opened: {e.strerror}") from e
    try:
        yield fd, os.fstat(fd)
    finally:
        os.close(fd)


def _decode(raw: bytes | None, path: Path) -> str | None:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise refusal(path, "is not valid UTF-8") from e


def _read_extensions(root: Path) -> dict[str, bytes] | None:
    """Every file under `.tau/extensions/` but `__pycache__/` and names starting with `.` (an
    editor's swap file; the importer serves only what is listed here, so such a file can never
    be imported). Path relative to it, with `/`, to bytes; `None` if there is no such directory.
    A symlink, a FIFO or any file the reader refuses is a `TrustError`."""
    base = root / TAU_DIR / EXTENSIONS_DIR
    try:
        info = base.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise refusal(base, "is a symlink or not a directory; tau refuses those inside a project")
    files: dict[str, bytes] = {}

    def walk(directory: Path, prefix: str) -> None:
        with unless_io_fails(directory, "listed"), os.scandir(directory) as listing:
            found = sorted(listing, key=lambda e: e.name)
        for item in found:
            path = directory / item.name
            if item.name == "__pycache__" or item.name.startswith("."):
                continue
            if item.is_dir(follow_symlinks=False):
                walk(path, f"{prefix}{item.name}/")
                continue
            raw = read_regular(path)  # refuses a symlink, a FIFO, an oversize file
            if raw is None:
                raise refusal(path, "vanished while the project was being read")
            files[f"{prefix}{item.name}"] = raw

    walk(base, "")
    return files


class _Read(NamedTuple):
    """The project's files as bytes, in one pass: config, `.env`, extension files, SYSTEM.md, and
    the prompt and skill files."""

    config: bytes | None
    env: bytes | None
    extensions: dict[str, bytes] | None
    system: bytes | None
    prompts: dict[str, bytes]
    mcp: dict[str, bytes]
    hooks: bytes | None
    targets: Mapping[str, Path]

    def digests(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            _digest(self.config),
            _digest(self.env),
            _extensions_digest(self.extensions),
            _digest(self.system),
            _extensions_digest(self.prompts),
            _extensions_digest(self.mcp),
            _digest(self.hooks),
        )


def _read_all(root: Path) -> _Read:
    prompts = read_prompt_files(root / TAU_DIR)
    return _Read(
        read_regular(project_config_path(root)),
        read_regular(root / _ENV),
        _read_extensions(root),
        read_regular(root / TAU_DIR / SYSTEM_MD),
        prompts,
        {p: raw for p in MCP_FILES if (raw := read_regular(root / p)) is not None},
        read_regular(root / TAU_DIR / HOOKS_FILE),
        prompt_links(root / TAU_DIR, prompts),
    )


def _see(root: Path) -> _Seen:
    with _tau_dir(root) as (_, info):
        read = _read_all(root)
    sha, env_sha, extensions_sha, system_sha, prompts_sha, mcp_sha, hooks_sha = read.digests()
    return _Seen(
        root,
        _decode(read.config, project_config_path(root)),
        sha,
        _decode(read.env, root / _ENV),
        env_sha,
        read.extensions,
        extensions_sha,
        read.system,
        system_sha,
        read.prompts,
        prompts_sha,
        read.mcp,
        mcp_sha,
        read.hooks,
        hooks_sha,
        stat.S_IMODE(info.st_mode),
        read.targets,
    )


# -- trusted.json


def _record(home: Path, seen: _Seen) -> None:
    approval = _Approval(
        seen.config_sha,
        seen.env_sha,
        seen.extensions_sha,
        seen.system_sha,
        seen.prompts_sha,
        seen.mcp_sha,
        redacted(seen.config),
        _file_hashes(seen.extensions or {}),
        _file_hashes(seen.prompts),
        _file_hashes(seen.mcp),
        seen.hooks_sha,
    )
    record(home, seen.root, approval)


def _pinned(approval: _Approval | None, seen: _Seen) -> bool:
    return (
        approval is not None
        and approval.sha256 == seen.config_sha
        and approval.env_sha256 == seen.env_sha
        and approval.extensions_sha256 == seen.extensions_sha
        and approval.system_sha256 == seen.system_sha
        and approval.prompts_sha256 == seen.prompts_sha
        and approval.mcp_sha256 == seen.mcp_sha
        and approval.hooks_sha256 == seen.hooks_sha
    )


# -- the report


def user_config_path(home: Path) -> Path | None:
    """`<home>/config.json` if it exists or is a symlink (a dangling one is a file that cannot
    be read, not no file: its reader fails naming it), else `None` (no user config is fine).
    `TrustError` if `<home>` cannot be searched."""
    user = home / "config.json"
    with unless_io_fails(user, "inspected"):
        return user if user.exists() or user.is_symlink() else None


def _layer(seen: _Seen) -> LayerText | None:
    if seen.config is None:
        return None
    return LayerText(str(project_config_path(seen.root)), seen.config)


def _errors(seen: _Seen, home: Path) -> list[str]:
    """What keeps the project from being approved: config problems, a malformed `.env`, every
    problem with its prompt and skill files."""
    layer = _layer(seen)
    errors: list[str] = []
    if layer is not None:
        # Trust asks only whether the file is sound; the CLI's providers are the built-in ones.
        problems = check_config(user_config_path(home), layer, providers=builtin_providers({}))
        errors = [safe_text(str(p)) for p in problems if p.severity == "error"]
    if seen.env is not None:
        try:
            parse_dotenv(seen.env, shown(seen.root / _ENV))
        except DotenvError as e:
            errors.append(safe_text(str(e)))
    errors += hooks_problems(seen.hooks, seen.root / TAU_DIR / HOOKS_FILE)
    return [*errors, *problems_in(seen.prompts, seen.root / TAU_DIR, fatal=True)]


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
    return [f"  {line}" for line in safe_block(redacted(seen.config)).split("\n")]


def _gitignore_check(tau: Path) -> str:
    raw = read_regular(tau / ".gitignore")
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
            redacted(seen.config).splitlines(),
            "approved .tau/config.json",
            "current .tau/config.json",
            lineterm="",
        )
        lines += [safe_text(line) for line in diff] or [
            "  (no visible difference: a secret value changed, or only the formatting)"
        ]
    if approval.env_sha256 != seen.env_sha:
        lines.append("  .env changed since you approved it")
    lines += _code_changes(approval, seen)
    return lines


def _code_changes(approval: _Approval, seen: _Seen) -> list[str]:
    """Which files under `.tau/extensions/` were added, removed or changed since approval, and
    whether `.tau/SYSTEM.md` changed. Names only: no code is printed."""
    lines: list[str] = []
    now = _file_hashes(seen.extensions or {})
    before = approval.extensions
    for label, paths in (
        ("added", [p for p in now if p not in before]),
        ("removed", [p for p in before if p not in now]),
        ("changed", [p for p in now if p in before and now[p] != before[p]]),
    ):
        lines += [safe_text(f"  .tau/extensions/{p} {label}") for p in paths]
    if approval.extensions_sha256 != seen.extensions_sha and not lines:
        lines.append("  .tau/extensions/ appeared or went away")
    if approval.system_sha256 != seen.system_sha:
        lines.append("  .tau/SYSTEM.md changed since you approved it")
    if approval.hooks_sha256 != seen.hooks_sha:
        lines.append("  .tau/hooks.json changed since you approved it")
    return lines + _mcp_changes(approval, seen) + _prompt_changes(approval, seen)


def _mcp_changes(approval: _Approval, seen: _Seen) -> list[str]:
    """Which of `.mcp.json` and `.tau/mcp.json` were added, removed or changed since approval."""
    now = _file_hashes(seen.mcp)
    lines = [
        safe_text(f"  {p} {'changed' if p in approval.mcp else 'added'}")
        for p in now
        if approval.mcp.get(p) != now[p]
    ]
    return lines + [safe_text(f"  {p} removed") for p in approval.mcp if p not in now]


def _shown(path: str) -> str:
    """A key of `prompts` as the report names it: under `.tau/`, or `.agents/`, `.claude/`."""
    return path if path.split("/", maxsplit=1)[0] in FOREIGN_BASES else f".tau/{path}"


def _prompt_changes(approval: _Approval, seen: _Seen) -> list[str]:
    """Which prompt templates and skill files were added, removed or changed since approval."""
    now = _file_hashes(seen.prompts)
    before = approval.prompts
    lines: list[str] = []
    for label, paths in (
        ("added", [p for p in now if p not in before]),
        ("removed", [p for p in before if p not in now]),
        ("changed", [p for p in now if p in before and now[p] != before[p]]),
    ):
        lines += [safe_text(f"  {_shown(p)} {label}") for p in paths]
    return lines


def _code_listing(seen: _Seen, approval: _Approval | None) -> list[str]:
    """The files that will run with your authority by name and size, and the prompt and skill
    files, with the text of each that is new or changed since approval."""
    lines: list[str] = []
    if seen.extensions:
        lines += ["", "Its extensions (code that runs with your authority):"]
        lines += [safe_text(f"  {p}  {len(raw)} bytes") for p, raw in seen.extensions.items()]
    if seen.system is not None:
        lines += ["", f"Its SYSTEM.md: {len(seen.system)} bytes"]
    if seen.hooks is not None:
        changed = approval is None or approval.hooks_sha256 != seen.hooks_sha
        lines += hooks_listing(seen.hooks, changed=changed)
    for path, raw in seen.mcp.items():
        lines += ["", f"Its {path}: MCP servers (commands run with your authority if mcp is on):"]
        lines += [f"  {safe_text(line)}" for line in describe_mcp(raw, path)]
    if seen.prompts:
        before: Mapping[str, str] = {} if approval is None else approval.prompts
        lines += prompt_report(
            seen.prompts, seen.prompt_targets, before, _file_hashes(seen.prompts)
        )
    return lines


@dataclass(frozen=True)
class _Assessment:
    report: str
    errors: tuple[str, ...]


def _assess(seen: _Seen, home: Path, approval: _Approval | None) -> _Assessment:
    state = "is not trusted" if approval is None else "has changed since you approved it"
    errors = _errors(seen, home)
    lines = [f"Project {shown(seen.root)} {state}.", ""]
    if errors:
        lines += ["It cannot be trusted until these are fixed:", *(f"  {e}" for e in errors)]
    else:
        lines += ["Its config:", *_shown_config(seen)]
    if names := _env_names(seen):
        lines += ["", f"Its .env sets: {', '.join(names)}"]
    lines += _code_listing(seen, approval)
    if skipped := problems_in(seen.prompts, seen.root / TAU_DIR, fatal=False):
        lines += [
            "",
            "Prompt and skill files tau cannot load (skipped; it starts without them):",
        ]
        lines += [f"  {p}" for p in skipped]
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
    with _tau_dir(root) as (fd, _), unless_io_fails(root / TAU_DIR, "set up for approval"):
        again = _read_all(root).digests()
        if again != (
            seen.config_sha,
            seen.env_sha,
            seen.extensions_sha,
            seen.system_sha,
            seen.prompts_sha,
            seen.mcp_sha,
            seen.hooks_sha,
        ):
            raise TrustError(
                f"{shown(root)}: a file under .tau/ changed while you were reading; run again", root
            )
        os.fchmod(fd, _PRIVATE)
        _write_gitignore(fd)
        read_regular(root / TAU_DIR / ".gitignore")  # refuses a symlink that was already there
    _record(home, seen)
    return _trusted(seen)


def _trusted(seen: _Seen) -> Trusted:
    return Trusted(
        seen.root,
        seen.config,
        seen.env,
        seen.extensions,
        seen.system,
        seen.prompts,
        seen.mcp,
        seen.hooks,
    )


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
        return _trusted(seen)
    assessment = _assess(seen, home, approval)
    hint = "" if assessment.errors else f"\nTrust it with: tau trust {shlex.quote(shown(root))}"
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
        found = parse_dotenv(project.env_text, shown(project.root / _ENV))
    return {**found, **environ}
