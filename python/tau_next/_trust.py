"""Finding the project and trusting it (D13).

A project is the nearest directory upward holding a `.tau/` that is a real
directory owned by the user. Its config and `.env` are read only while it is
trusted. Trust is pinned to the bytes of `.tau/config.json`: `trusted.json`
holds their sha256, which decides, and a redacted copy for showing diffs.

The config is read once per `ensure_trusted` call. The report, the question,
and the approval all work from those bytes; approval re-reads and refuses if
they changed. Everything inside a project is read through one helper that
refuses symlinks.
"""

import contextlib
import difflib
import errno
import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, cast

from tau_next._config import ConfigError, ModelConfig
from tau_next._config_layers import LayerText, ModelCatalog, check_config, read_models
from tau_next._dotenv import DotenvError, parse_dotenv
from tau_next.providers import builtin_providers

TAU_DIR = ".tau"
_GITIGNORE = "*\n"
_PRIVATE = 0o700
_PRIVATE_FILE = 0o600
_PUBLIC_FILE = 0o644
_WRITABLE_BY_OTHERS = 0o022
_MAX_BYTES = 1 << 20
_LATIN1_MAX = 0xFF
_BMP_MAX = 0xFFFF
_FIELDS = ("model", "base_url", "provider", "api_key_env", "api_key")
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
    """A project directory tau cannot or will not trust, with the reason."""


class UntrustedProject(TrustError):
    """The project is not trusted (or its config changed). `report` says what it sets and
    what is wrong with `.tau/`; the message adds the command that trusts it."""

    def __init__(self, root: Path, report: str) -> None:
        super().__init__(f"{report}\nTrust it with: tau trust {_p(root)}")
        self.root = root
        self.report = report


@dataclass(frozen=True)
class Trusted:
    """A trusted project: its `root` and the exact `.tau/config.json` text that was approved
    (`None`: it has no config file). Use `layer()` or `read_models()` rather than
    re-reading the file."""

    root: Path
    config_text: str | None

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
    redacted: str


@dataclass(frozen=True)
class _Seen:
    """What one read of the project showed: config bytes (`None`: absent) and `.tau/`'s stat."""

    root: Path
    raw: bytes | None
    tau: os.stat_result


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


def _is_project_dir(tau: Path, config_dir: Path) -> bool:
    try:
        info = tau.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and tau.resolve() != config_dir
    )


def skipped_project(start: Path, environ: Mapping[str, str]) -> str | None:
    """Why the nearest `.tau` from `start` upward that `find_project` skipped is not a
    project, or `None` if it skipped none. For saying so when no project is found."""
    home = _home_dir(environ)
    here = start.resolve()
    for directory in (here, *here.parents):
        if directory == home:
            return None
        tau = directory / TAU_DIR
        try:
            info = tau.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            return f"{_p(tau)} is a symlink; tau refuses symlinks inside a project"
        if stat.S_ISDIR(info.st_mode) and info.st_uid != os.geteuid():
            return (
                f"{_p(tau)} is not owned by you (uid {info.st_uid}); "
                "take it over (chown) before trusting it"
            )
    return None


def find_project(start: Path, environ: Mapping[str, str]) -> Path | None:
    """The nearest directory from `start` upward whose `.tau/` is a real directory owned
    by you, or `None`. Skipped, and the search goes on: a symlinked or foreign `.tau`.

    The search stops at `$HOME` (excluded: its `.tau` is the user config) and
    never returns the directory whose `.tau` is `TAU_HOME`. Outside `$HOME` it
    runs to `/`.
    """
    home = _home_dir(environ)
    config_dir = tau_home(environ).resolve()
    here = start.resolve()
    for directory in (here, *here.parents):
        if directory == home:
            return None
        if _is_project_dir(directory / TAU_DIR, config_dir):
            return directory
    return None


# -- reading inside a project, refusing symlinks


def _refuse(path: Path, info: os.stat_result, what: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise TrustError(f"{_p(path)} is a symlink; tau refuses symlinks inside a project")
    expected = stat.S_ISDIR if what == "a directory" else stat.S_ISREG
    if not expected(info.st_mode):
        raise TrustError(f"{_p(path)} is not {what}")


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _symlink_error(path: Path, e: OSError) -> TrustError | OSError:
    if e.errno == errno.ELOOP:
        return TrustError(f"{_p(path)} is a symlink; tau refuses symlinks inside a project")
    return e


def _read_regular(path: Path, *, owned: bool = False, limit: int = _MAX_BYTES) -> bytes | None:
    """The bytes of a regular file, or `None` if absent. A symlink or other kind of file is a
    `TrustError` naming the path; with `owned`, so is a file that is not yours, and so is
    one over `limit` bytes. Opened non-blocking, so a FIFO swapped in cannot hang us."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    _refuse(path, before, "a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise _symlink_error(path, e) from e
    with os.fdopen(fd, "rb") as f:
        after = os.fstat(f.fileno())
        if not stat.S_ISREG(after.st_mode) or not _same(before, after):
            raise TrustError(f"{_p(path)} changed while it was being read; run again")
        if owned and after.st_uid != os.geteuid():
            raise TrustError(f"{_p(path)} is not owned by you")
        if after.st_size > limit:
            raise TrustError(f"{_p(path)} is {after.st_size} bytes; the limit is {limit}")
        raw = f.read(limit + 1)
        if len(raw) > limit:
            raise TrustError(f"{_p(path)} grew past {limit} bytes while it was being read")
        return raw


@contextlib.contextmanager
def _tau_dir(root: Path) -> Generator[tuple[int, os.stat_result]]:
    """`<root>/.tau` opened as a directory descriptor, after refusing a symlink."""
    tau = root / TAU_DIR
    before = tau.lstat()
    _refuse(tau, before, "a directory")
    try:
        fd = os.open(tau, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise _symlink_error(tau, e) from e
    try:
        after = os.fstat(fd)
        if not _same(before, after):
            raise TrustError(f"{_p(tau)} changed while it was being read; run again")
        yield fd, after
    finally:
        os.close(fd)


def _see(root: Path) -> _Seen:
    with _tau_dir(root) as (_, info):
        return _Seen(root, _read_regular(project_config_path(root)), info)


def _decode(raw: bytes | None, path: Path) -> str | None:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TrustError(f"{_p(path)} is not valid UTF-8") from e


# -- trusted.json


def _require_private(path: Path, fix: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & _WRITABLE_BY_OTHERS:
            raise TrustError(
                f"{_p(path)} is writable by group or others (mode {mode:o}); run: {fix} {_p(path)}"
            )


def _trusted_path(home: Path) -> Path:
    return home / "trusted.json"


def _approved(home: Path) -> dict[str, _Approval]:
    _require_private(home, "chmod 700")
    path = _trusted_path(home)
    _require_private(path, "chmod 600")
    if not path.exists():
        return {}
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
        projects = cast("dict[str, dict[str, dict[str, object]]]", data)["projects"]
        if not all(
            isinstance(r["sha256"], str) and isinstance(r["config"], str) for r in projects.values()
        ):
            raise TypeError("a project's record is not text")
        found = {
            root: _Approval(str(r["sha256"]), str(r["config"])) for root, r in projects.items()
        }
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ConfigError(f"{path} is not tau's trusted.json ({e!r}); fix or delete it") from e
    return found


def _digest(raw: bytes | None) -> str:
    """sha256 of the bytes behind a marker byte, so a missing config differs from an empty one."""
    marker = b"\x00" if raw is None else b"\x01" + raw
    return hashlib.sha256(marker).hexdigest()


def _blank(value: object) -> object:
    if isinstance(value, dict):
        d = cast("dict[str, object]", value)
        return {k: _SECRET if k == "api_key" else _blank(v) for k, v in d.items()}
    if isinstance(value, list):
        return [_blank(v) for v in cast("list[object]", value)]
    return value


def _redacted(raw: bytes | None) -> str:
    """The config as canonical JSON (sorted keys, 2-space indent) with every `api_key` value
    replaced; `""` for no config. The bytes must be valid JSON (approval ensures it)."""
    if raw is None:
        return ""
    return json.dumps(_blank(json.loads(raw)), sort_keys=True, indent=2)


def _record(home: Path, root: Path, raw: bytes | None) -> None:
    projects = _approved(home)
    projects[str(root.resolve())] = _Approval(_digest(raw), _redacted(raw))
    home.mkdir(mode=_PRIVATE, parents=True, exist_ok=True)
    body = json.dumps(
        {"projects": {r: {"sha256": a.sha256, "config": a.redacted} for r, a in projects.items()}},
        indent=2,
    )
    final = _trusted_path(home)
    scratch = final.with_name(f"trusted.json.{os.getpid()}.tmp")
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


def _pinned(home: Path, seen: _Seen) -> bool:
    approval = _approved(home).get(str(seen.root.resolve()))
    return approval is not None and approval.sha256 == _digest(seen.raw)


def read_project_config(root: Path) -> LayerText | None:
    """The project's `.tau/config.json` read once through the safe reader (no symlinks, size
    limited) as a layer, or `None` if there is no file. For showing what an untrusted
    project says; nothing read this way is used. `TrustError` for a file that is refused."""
    path = project_config_path(root)
    text = _decode(_see(root).raw, path)
    return None if text is None else LayerText(str(path), text)


def is_trusted(root: Path, home: Path) -> bool:
    """Whether `root` was approved with exactly its current `.tau/config.json` bytes."""
    return _pinned(home, _see(root))


# -- the report


def _shown(config: ModelConfig, key: str) -> str:
    if key == "api_key":
        return "(set)" if config.literal_api_key is not None else "(unset)"
    value = cast("str | None", getattr(config, key))
    return "(unset)" if value is None else safe_text(value)


def _override_lines(before: ModelCatalog, after: ModelCatalog) -> list[str]:
    lines: list[str] = []
    for name, found in after.models.items():
        old = before.models.get(name)
        if old is None:
            lines.append(f"  new model {safe_text(name)}")
            lines += [f"    {k}: {_shown(found.config, k)}" for k in _FIELDS]
            continue
        changes = [
            f"    {k}: {_shown(old.config, k)} -> {_shown(found.config, k)}"
            for k in _FIELDS
            if _shown(old.config, k) != _shown(found.config, k)
        ]
        if changes:
            lines += [f"  {safe_text(name)}", *changes]
    lines += [f"  removed model {safe_text(n)}" for n in before.models if n not in after.models]
    if before.default_model != after.default_model:
        was, now = (safe_text(str(m)) for m in (before.default_model, after.default_model))
        lines.append(f"  default_model: {was} -> {now}")
    return lines


def user_config_path(home: Path) -> Path | None:
    """`<home>/config.json` if it exists, else `None` (no user config is fine)."""
    user = home / "config.json"
    return user if user.exists() else None


def _config_errors(seen: _Seen, home: Path) -> list[str]:
    path = project_config_path(seen.root)
    try:
        text = _decode(seen.raw, path)
    except TrustError as e:
        return [str(e)]
    if text is None:
        return []
    layer = LayerText(str(path), text)
    # Trust asks only whether the file is sound; the CLI's providers are the built-in ones.
    problems = check_config(user_config_path(home), layer, providers=builtin_providers({}))
    return [safe_text(str(p)) for p in problems if p.severity == "error"]


def _overrides(seen: _Seen, home: Path) -> list[str]:
    path = project_config_path(seen.root)
    text = _decode(seen.raw, path)
    if text is None:
        return ["  (no .tau/config.json)"]
    user = user_config_path(home)
    lines = _override_lines(read_models(user), read_models(user, LayerText(str(path), text)))
    return lines or ["  (nothing: it sets no model that differs from your config)"]


def _env_names(root: Path) -> tuple[list[str], str | None]:
    """Names set by the project's `.env` (never values), and a parse error if it has one."""
    path = root / ".env"
    raw = _read_regular(path, owned=True)
    if raw is None:
        return [], None
    try:
        return [safe_text(k) for k in parse_dotenv(raw.decode("utf-8-sig"), _p(path))], None
    except UnicodeDecodeError:
        return [], f"{_p(path)} is not valid UTF-8"
    except DotenvError as e:
        return [], safe_text(str(e))


def _gitignore_check(tau: Path) -> str:
    raw = _read_regular(tau / ".gitignore")
    if raw is None:
        return "  [fix] .tau/.gitignore is missing, approval writes it"
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if "*" in lines and not any(line.startswith("!") for line in lines):
        return "  [ok] .tau/.gitignore exists"
    return "  [warn] .tau/.gitignore does not ignore everything"


def _checks(seen: _Seen) -> list[str]:
    owned = seen.tau.st_uid == os.geteuid()
    mode = stat.S_IMODE(seen.tau.st_mode)
    return [
        f"  [{'ok' if owned else 'FAIL'}] {TAU_DIR}/ is owned by you"
        + ("" if owned else f" (uid {seen.tau.st_uid}; approval cannot fix this)"),
        f"  [{'ok' if mode == _PRIVATE else 'fix'}] {TAU_DIR}/ mode is {mode:o}"
        + ("" if mode == _PRIVATE else ", approval sets 700"),
        _gitignore_check(seen.root / TAU_DIR),
    ]


def _diff(approval: _Approval, seen: _Seen) -> list[str]:
    shown = _redacted(seen.raw)
    diff = list(
        difflib.unified_diff(
            approval.redacted.splitlines(),
            shown.splitlines(),
            "approved .tau/config.json",
            "current .tau/config.json",
            lineterm="",
        )
    )
    diff = [safe_text(line) for line in diff]
    return diff or ["  (no visible difference: a secret value changed, or only the formatting)"]


@dataclass(frozen=True)
class _Assessment:
    report: str
    errors: tuple[str, ...]


def _assess(seen: _Seen, home: Path, approval: _Approval | None) -> _Assessment:
    root = seen.root
    state = "is not trusted" if approval is None else "has changed since you approved it"
    errors = _config_errors(seen, home)
    names, env_error = _env_names(root)
    if env_error is not None:
        errors.append(env_error)
    lines = [f"Project {_p(root)} {state}.", ""]
    if errors:
        lines += ["It cannot be trusted until these are fixed:", *(f"  {e}" for e in errors)]
    else:
        lines += ["Its config overrides yours:", *_overrides(seen, home)]
    if names:
        lines += ["", f"Its .env sets: {', '.join(names)}"]
    lines += ["", "Checks:", *_checks(seen)]
    if approval is not None and not errors:
        lines += ["", "Changes since approval:", *_diff(approval, seen)]
    return _Assessment("\n".join(lines), tuple(errors))


def trust_report(root: Path, home: Path) -> str:
    """What approving `root` consents to, as text a user reads before agreeing.

    Lists what the project config overrides in the user config (old -> new,
    `api_key` shown only as `(set)`), the names (never values) its `.env` sets,
    the `.tau/` checks, and for a project whose config changed since approval,
    a unified diff of the redacted configs. Config errors are listed instead of
    overrides: such a project cannot be approved.
    """
    seen = _see(root)
    return _assess(seen, home, _approved(home).get(str(root.resolve()))).report


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
    with _tau_dir(root) as (fd, info):
        if info.st_uid != os.geteuid():
            raise TrustError(
                f"{_p(root / TAU_DIR)} is not owned by you; take it over (chown) before trusting it"
            )
        if _read_regular(project_config_path(root)) != seen.raw:
            raise TrustError(
                f"{_p(project_config_path(root))} changed while you were reading; run again"
            )
        os.fchmod(fd, _PRIVATE)
        _write_gitignore(fd)
        _read_regular(root / TAU_DIR / ".gitignore")  # refuses a symlink that was already there
    _record(home, root, seen.raw)
    return Trusted(root, _decode(seen.raw, project_config_path(root)))


def _refuse_errors(assessment: _Assessment) -> None:
    if assessment.errors:
        raise TrustError(assessment.report)


def approve_project(root: Path, home: Path) -> Trusted:
    """Trust `root`: make `.tau/` mode 700, write `.tau/.gitignore` (`*`) if missing, and
    record the config. Raises `TrustError` for a config with errors, or a `.tau/` not owned
    by you (ownership is never changed)."""
    seen = _see(root)
    _refuse_errors(_assess(seen, home, _approved(home).get(str(root.resolve()))))
    return _approve(seen, home)


def ensure_trusted(root: Path, home: Path, *, ask: Callable[[str], bool] | None) -> Trusted:
    """Return the `Trusted` project if `root` is trusted. Otherwise, with `ask` (a live
    terminal), show it the report and approve on yes; no `ask`, or a no, raises
    `UntrustedProject`. The config is read once: what is reported, asked about, and
    approved is those bytes, and approval refuses if the file changed meanwhile."""
    seen = _see(root)
    if _pinned(home, seen):
        return Trusted(root, _decode(seen.raw, project_config_path(root)))
    assessment = _assess(seen, home, _approved(home).get(str(root.resolve())))
    _refuse_errors(assessment)
    if ask is None or not ask(assessment.report):
        raise UntrustedProject(root, assessment.report)
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
    """`environ` over the `.env` of a trusted project (`None`: no project, just `environ`).
    A symlinked or foreign `.env` is a `TrustError`; the shell environment wins."""
    found: dict[str, str] = {}
    if project is not None:
        path = project.root / ".env"
        raw = _read_regular(path, owned=True)
        if raw is not None:
            try:
                found = parse_dotenv(raw.decode("utf-8-sig"), _p(path))
            except UnicodeDecodeError as e:
                raise DotenvError(f"{_p(path)} is not valid UTF-8") from e
    return {**found, **environ}
